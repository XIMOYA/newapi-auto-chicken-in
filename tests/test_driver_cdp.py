"""driver_cdp.py 的行为测试：全程用假 playwright，不启动也不连接真实浏览器。

重点覆盖两件事：
1. 各条失败路径给出的提示必须可操作（怎么开调试端口、为什么等不到 token）
2. 收尾只关自己新开的标签页并断开连接，绝不能把用户正在用的 Chrome 关掉
"""

from __future__ import annotations

import json

import pytest

from newapi_checkin import config as cfgmod
from newapi_checkin.cf import driver_cdp
from newapi_checkin.cf.driver_base import DriverUnavailable

BASE = "https://tabiai.example.com"

SITE_KEY_SCRIPT_MARK = "fetch"
MOUNT_SCRIPT_MARK = "turnstile.render"
READ_SCRIPT_MARK = "__newapi_turnstile_token ||"


class FakePage:
    """按脚本类型回放预置结果；同时记录关键调用，供收尾行为断言。"""

    def __init__(self, status_payload=None, mount=True, tokens=(), status_raw=None,
                 evaluate_error=None):
        self.status_payload = status_payload
        self.status_raw = status_raw
        self.mount = mount
        self.tokens = list(tokens)
        self.evaluate_error = evaluate_error
        self.goto_calls = []
        self.timeouts = []
        self.fetch_urls = []
        self.closed = False

    def set_default_timeout(self, ms):
        self.timeouts.append(ms)

    def goto(self, url, **_kw):
        self.goto_calls.append(url)

    def evaluate(self, script, arg=None):
        if SITE_KEY_SCRIPT_MARK in script and "turnstile" not in script:
            self.fetch_urls.append(arg)
            if self.evaluate_error is not None:
                raise self.evaluate_error
            if self.status_raw is not None:
                return self.status_raw
            return json.dumps(self.status_payload or {})
        if MOUNT_SCRIPT_MARK in script:
            return self.mount
        if READ_SCRIPT_MARK in script:
            return self.tokens.pop(0) if self.tokens else ""
        raise AssertionError(f"未预期的脚本: {script[:60]}")

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        return self._page


class FakeBrowser:
    def __init__(self, page):
        self.contexts = [FakeContext(page)]
        self.closed = False

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser=None, error=None):
        self._browser = browser
        self._error = error
        self.cdp_urls = []

    def connect_over_cdp(self, url):
        self.cdp_urls.append(url)
        if self._error is not None:
            raise self._error
        return self._browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class FakeManager:
    def __init__(self, playwright):
        self._playwright = playwright
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self._playwright

    def __exit__(self, *_exc):
        self.exited = True
        return False


def wire(monkeypatch, page=None, error=None):
    """把假 playwright 注入 driver_cdp，返回 (browser, chromium, manager)。"""
    browser = FakeBrowser(page) if page is not None else None
    chromium = FakeChromium(browser, error)
    manager = FakeManager(FakePlaywright(chromium))
    monkeypatch.setattr(driver_cdp, "_sync_playwright", lambda: (lambda: manager))
    return browser, chromium, manager


def make_account(**kw):
    cfg = cfgmod.build_config({
        "tabiai": {"enabled": True, **kw},
        "accounts": [{
            "name": "TaBiAI",
            "url": BASE,
            "login_method": "tabiai",
            "cookie": "new_api_refresh=sid.gen1",
        }],
    })
    return cfg.tabiai, cfg.accounts[0]


class TestSyncPlaywrightResolution:
    def test_missing_both_libs_is_actionable(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name in ("patchright.sync_api", "playwright.sync_api"):
                raise ImportError(f"no module named {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(DriverUnavailable) as exc:
            driver_cdp._sync_playwright()
        assert "pip install patchright" in str(exc.value)


class TestSiteKey:
    @pytest.mark.parametrize("key_name", [
        "turnstile_site_key", "TurnstileSiteKey", "turnstile_sitekey",
    ])
    def test_reads_known_key_aliases(self, key_name):
        page = FakePage({"data": {key_name: "0x4AAA"}})
        _, account = make_account()
        assert driver_cdp._site_key(page, account) == "0x4AAA"

    def test_status_url_is_site_scoped(self):
        page = FakePage({"data": {"turnstile_site_key": "0x1"}})
        _, account = make_account()
        driver_cdp._site_key(page, account)
        # site key 必须从账号自己的站点读，不能拼到别的域名上
        assert page.fetch_urls == [f"{BASE}/api/status"]

    def test_non_json_returns_empty(self):
        page = FakePage(status_raw="<html>502</html>")
        _, account = make_account()
        assert driver_cdp._site_key(page, account) == ""

    def test_missing_key_returns_empty(self):
        page = FakePage({"data": {"something_else": "x"}})
        _, account = make_account()
        assert driver_cdp._site_key(page, account) == ""

    def test_evaluate_failure_degrades_quietly(self):
        page = FakePage(evaluate_error=RuntimeError("page closed"))
        _, account = make_account()
        assert driver_cdp._site_key(page, account) == ""


class TestFetchToken:
    def test_cdp_connect_failure_tells_how_to_start_chrome(self, monkeypatch):
        wire(monkeypatch, error=OSError("connection refused"))
        tabiai, account = make_account(cdp_url="http://127.0.0.1:9333")
        token, error = driver_cdp.fetch_turnstile_token(tabiai, account)
        assert token == ""
        assert "127.0.0.1:9333" in error
        assert "--remote-debugging-port" in error

    def test_missing_site_key_is_reported(self, monkeypatch):
        page = FakePage({"data": {}})
        wire(monkeypatch, page)
        tabiai, account = make_account()
        token, error = driver_cdp.fetch_turnstile_token(tabiai, account)
        assert token == ""
        assert "turnstile_site_key" in error

    def test_mount_failure_is_reported(self, monkeypatch):
        page = FakePage({"data": {"turnstile_site_key": "0x1"}}, mount=False)
        wire(monkeypatch, page)
        tabiai, account = make_account()
        token, error = driver_cdp.fetch_turnstile_token(tabiai, account)
        assert token == ""
        assert "挂载失败" in error

    def test_token_is_returned_and_page_is_site_origin(self, monkeypatch):
        page = FakePage({"data": {"turnstile_site_key": "0x1"}}, tokens=["", "tok-abc"])
        browser, chromium, manager = wire(monkeypatch, page)
        tabiai, account = make_account()
        token, error = driver_cdp.fetch_turnstile_token(tabiai, account)
        assert (token, error) == ("tok-abc", "")
        # widget 必须挂在站点自己的源上，Turnstile 会校验域名
        assert page.goto_calls == [f"{BASE}/console"]
        assert chromium.cdp_urls == [tabiai.cdp_url]
        assert manager.exited is True

    def test_timeout_mentions_rate_limit(self, monkeypatch):
        page = FakePage({"data": {"turnstile_site_key": "0x1"}}, tokens=[])
        wire(monkeypatch, page)
        tabiai, account = make_account(token_timeout=10)
        monkeypatch.setattr(driver_cdp.time, "sleep", lambda *_a: None)
        ticks = iter([0.0] + [i * 5.0 for i in range(1, 20)])
        monkeypatch.setattr(driver_cdp.time, "time", lambda: next(ticks))
        token, error = driver_cdp.fetch_turnstile_token(tabiai, account)
        assert token == ""
        assert "频率限制" in error

    def test_read_failure_is_reported(self, monkeypatch):
        class Boom(FakePage):
            def evaluate(self, script, arg=None):
                if READ_SCRIPT_MARK in script:
                    raise RuntimeError("target crashed")
                return super().evaluate(script, arg)

        page = Boom({"data": {"turnstile_site_key": "0x1"}})
        wire(monkeypatch, page)
        tabiai, account = make_account()
        token, error = driver_cdp.fetch_turnstile_token(tabiai, account)
        assert token == ""
        assert "target crashed" in error


class TestCleanup:
    def test_own_tab_is_closed_but_user_chrome_survives(self, monkeypatch):
        page = FakePage({"data": {"turnstile_site_key": "0x1"}}, tokens=["tok"])
        browser, _chromium, manager = wire(monkeypatch, page)
        tabiai, account = make_account()
        driver_cdp.fetch_turnstile_token(tabiai, account)
        assert page.closed is True
        # connect_over_cdp 的 close 只断开连接，不会终止用户自己的 Chrome 进程
        assert browser.closed is True
        assert manager.exited is True

    def test_keep_page_leaves_tab_open_for_debugging(self, monkeypatch):
        page = FakePage({"data": {"turnstile_site_key": "0x1"}}, tokens=["tok"])
        wire(monkeypatch, page)
        tabiai, account = make_account(keep_page=True)
        driver_cdp.fetch_turnstile_token(tabiai, account)
        assert page.closed is False

    def test_cleanup_runs_even_on_failure(self, monkeypatch):
        page = FakePage({"data": {}})
        browser, _chromium, manager = wire(monkeypatch, page)
        tabiai, account = make_account()
        driver_cdp.fetch_turnstile_token(tabiai, account)
        assert page.closed is True
        assert browser.closed is True
        assert manager.exited is True

    def test_close_errors_are_swallowed(self, monkeypatch):
        class Stubborn(FakePage):
            def close(self):
                raise RuntimeError("already gone")

        page = Stubborn({"data": {"turnstile_site_key": "0x1"}}, tokens=["tok"])
        wire(monkeypatch, page)
        tabiai, account = make_account()
        token, error = driver_cdp.fetch_turnstile_token(tabiai, account)
        assert (token, error) == ("tok", "")
