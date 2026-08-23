"""tabiai.py 的协议行为测试：轮转落盘、先查后签、业务失败判定。

这些用例全部用假响应，不发真实网络请求。重点盯住实测踩过的坑：
refresh 会下发新一代凭据，漏存就会在下一轮被判重放并撤销整条会话。
"""

from __future__ import annotations

import json

import pytest

from newapi_checkin import client as api
from newapi_checkin import config as cfgmod
from newapi_checkin import tabiai


class FakeHeaders(dict):
    """curl_cffi 的 headers 既能 items() 又能 get_list()，这里两者都模拟。"""

    def __init__(self, set_cookie=()):
        super().__init__()
        self._set_cookie = list(set_cookie)
        if self._set_cookie:
            self["Set-Cookie"] = self._set_cookie[0]
        self["Content-Type"] = "application/json"

    def get_list(self, name):
        return list(self._set_cookie) if name.lower() == "set-cookie" else []


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", cookies=None, set_cookie=()):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.cookies = cookies or {}
        self.headers = FakeHeaders(set_cookie)
        self.url = "https://tabiai.example.com/api/user/auth/refresh"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按 (method, path前缀) 顺序返回预置响应，并记录实际发出的请求。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}
        self.closed = False

    def request(self, method, url, headers=None, **kwargs):
        # 连 kwargs 一起记：refresh 用的是专用短超时而不是 http.timeout，
        # 不记下来就没法断言那条安全约束
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "kwargs": dict(kwargs)})
        if not self.script:
            raise AssertionError(f"没有为 {method} {url} 预置响应")
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def make_account(cookie="new_api_refresh=sid.gen1"):
    cfg = cfgmod.build_config({
        "accounts": [{
            "name": "TaBiAI",
            "url": "https://tabiai.example.com",
            "login_method": "tabiai",
            "cookie": cookie,
        }],
    })
    return cfg, cfg.accounts[0]


def make_client(script, cookie="new_api_refresh=sid.gen1", on_rotate=None, monkeypatch=None,
                on_inflight=None, on_settled=None):
    cfg, account = make_account(cookie)
    client = tabiai.TabiAIClient.__new__(tabiai.TabiAIClient)
    client.account = account
    client.http = cfg.http
    client.cf = None
    client.on_rotate = on_rotate
    # 代次悬空记账的两端。__new__ 绕过了 __init__，这里必须手动补齐，
    # 漏一个就是 AttributeError 而不是「回调没配」
    client.on_inflight = on_inflight
    client.on_settled = on_settled
    client._cookie = tabiai.normalize_refresh_cookie(cookie)
    client.user_id = account.user_id
    client.impersonate = "chrome"
    client._session = FakeSession(script)
    return client


def refresh_ok(access_token="jwt-token", uid=42, set_cookie=(), cookies=None):
    return FakeResponse(
        200,
        {
            "success": True,
            "data": {
                "access_token": access_token,
                "user": {"id": uid, "username": "kiq"},
            },
        },
        cookies=cookies,
        set_cookie=set_cookie,
    )


def status_resp(checked):
    return FakeResponse(200, {
        "success": True,
        "data": {"stats": {"checked_in_today": checked, "continuous_days": 3}},
    })


def self_resp(quota=6170000, uid=42):
    """GET /api/user/self 的假响应。签到有结论后会被补查一次，用来拿剩余额度。"""
    return FakeResponse(200, {
        "success": True,
        "data": {"id": uid, "username": "kiq", "quota": quota},
    })


class TestNormalize:
    def test_bare_value_gets_cookie_name(self):
        assert tabiai.normalize_refresh_cookie("sid.secret") == "new_api_refresh=sid.secret"

    def test_full_header_is_kept(self):
        raw = "new_api_refresh=sid.secret"
        assert tabiai.normalize_refresh_cookie(raw) == raw

    def test_value_extraction_ignores_other_cookies(self):
        raw = "other=1; new_api_refresh=sid.secret; more=2"
        assert tabiai.refresh_cookie_value(raw) == "sid.secret"

    def test_empty_stays_empty(self):
        assert tabiai.normalize_refresh_cookie("  ") == ""
        assert tabiai.refresh_cookie_value("") == ""


class TestRefresh:
    def test_rotation_is_reported_immediately(self):
        rotated = []
        client = make_client(
            [refresh_ok(set_cookie=["new_api_refresh=sid.gen2; Path=/api/user/auth; HttpOnly"])],
            on_rotate=rotated.append,
        )
        step = client.refresh()
        assert step.result.kind == api.SUCCESS
        assert step.access_token == "jwt-token"
        assert step.user_id == 42
        # 新代次必须当场回调出去，并且客户端自身也切到新值
        assert rotated == ["new_api_refresh=sid.gen2"]
        assert client._cookie == "new_api_refresh=sid.gen2"

    def test_rotation_read_from_cookie_jar(self):
        rotated = []
        client = make_client(
            [refresh_ok(cookies={"new_api_refresh": "sid.gen3"})],
            on_rotate=rotated.append,
        )
        client.refresh()
        assert rotated == ["new_api_refresh=sid.gen3"]

    def test_same_generation_does_not_trigger_writeback(self):
        rotated = []
        client = make_client(
            [refresh_ok(set_cookie=["new_api_refresh=sid.gen1; Path=/api/user/auth"])],
            on_rotate=rotated.append,
        )
        client.refresh()
        assert rotated == []

    def test_session_revoked_is_auth_failed_with_actionable_message(self):
        client = make_client([FakeResponse(401, {
            "success": False, "code": "AUTH_SESSION_REVOKED", "message": "unauthorized",
        })])
        step = client.refresh()
        assert step.result.kind == api.AUTH_FAILED
        assert "撤销" in step.result.message

    def test_unauthorized_hints_generation_replaced(self):
        client = make_client([FakeResponse(401, {
            "success": False, "code": "AUTH_UNAUTHORIZED", "message": "unauthorized",
        })])
        step = client.refresh()
        assert step.result.kind == api.AUTH_FAILED
        assert "代次" in step.result.message

    def test_cookie_goes_to_auth_path_only(self):
        client = make_client([refresh_ok()])
        client.refresh()
        call = client._session.calls[0]
        assert call["url"].endswith("/api/user/auth/refresh")
        assert call["headers"]["Cookie"] == "new_api_refresh=sid.gen1"
        # refresh 阶段还没有 token，不该带 Authorization
        assert "Authorization" not in call["headers"]

    def test_non_json_response_is_unknown(self):
        client = make_client([FakeResponse(502, None, text="<html>bad gateway</html>")])
        step = client.refresh()
        assert step.result.kind == api.UNKNOWN
        assert "非 JSON" in step.result.message



class TestCheckinFlow:
    def test_already_checked_in_skips_turnstile(self):
        """已签到时绝不能去取 Turnstile token —— 那是有频率配额的稀缺资源。"""
        called = []

        def provider():
            called.append(True)
            return "token", ""

        client = make_client([refresh_ok(), status_resp(True), self_resp()])
        result = client.checkin(turnstile_provider=provider)
        assert result.kind == api.ALREADY_DONE
        assert called == []
        # refresh + 查签到状态 + 查余额；关键是没有第四次（取 token / 发签到）
        assert len(client._session.calls) == 3
        assert result.balance == 6170000

    def test_already_checked_in_still_reports_balance(self):
        """今日已签也要带出余额 —— 老版本这里没有奖励额度，额度列就一直空着。"""
        client = make_client([refresh_ok(), status_resp(True), self_resp(quota=1234500)])
        assert client.checkin(turnstile_provider=lambda: ("t", "")).balance == 1234500

    def test_balance_failure_does_not_break_checkin(self):
        """查余额失败只能让余额缺失，不能把已签到的结论改坏。"""
        client = make_client([refresh_ok(), status_resp(True),
                              FakeResponse(500, None, text="boom")])
        result = client.checkin(turnstile_provider=lambda: ("t", ""))
        assert result.kind == api.ALREADY_DONE and result.balance is None

    def test_full_checkin_sends_token_in_query(self):
        client = make_client([
            refresh_ok(),
            status_resp(False),
            FakeResponse(200, {"success": True, "message": "签到成功，获得 1000 额度"}),
            self_resp(),
        ])
        result = client.checkin(turnstile_provider=lambda: ("tok-123", ""))
        assert result.kind == api.SUCCESS
        assert result.user_id == 42
        assert "kiq" in result.message
        # 最后一次调用现在是补查余额的 GET self，签到那次要按方法找
        post = next(c for c in client._session.calls if c["method"] == "POST"
                    and "checkin" in c["url"])
        assert "turnstile=tok-123" in post["url"]
        # 业务接口只认 Bearer，不该再带 refresh cookie
        assert post["headers"]["Authorization"] == "Bearer jwt-token"
        assert result.balance == 6170000

    def test_business_failure_on_http_200_is_not_success(self):
        client = make_client([
            refresh_ok(),
            status_resp(False),
            FakeResponse(200, {"success": False, "message": "turnstile 校验失败"}),
        ])
        result = client.checkin(turnstile_provider=lambda: ("tok", ""))
        assert result.ok is False
        assert result.kind == api.TURNSTILE_REQUIRED

    def test_missing_provider_reports_actionable_reason(self):
        client = make_client([refresh_ok(), status_resp(False)])
        result = client.checkin(turnstile_provider=None)
        assert result.kind == api.TURNSTILE_REQUIRED
        assert "tabiai.enabled" in result.message

    def test_provider_error_is_surfaced(self):
        client = make_client([refresh_ok(), status_resp(False)])
        result = client.checkin(turnstile_provider=lambda: ("", "Chrome 没开调试端口"))
        assert result.kind == api.TURNSTILE_REQUIRED
        assert "Chrome 没开调试端口" in result.message

    def test_refresh_failure_short_circuits(self):
        client = make_client([FakeResponse(401, {
            "success": False, "code": "AUTH_UNAUTHORIZED", "message": "unauthorized",
        })])
        result = client.checkin(turnstile_provider=lambda: ("tok", ""))
        assert result.kind == api.AUTH_FAILED
        assert len(client._session.calls) == 1

    def test_dry_run_only_refreshes(self):
        client = make_client([refresh_ok()])
        result = client.checkin(turnstile_provider=lambda: ("tok", ""), dry_run=True)
        assert result.kind == api.SUCCESS
        assert "未执行签到" in result.message
        assert len(client._session.calls) == 1

    def test_unknown_checked_flag_still_attempts_checkin(self):
        """站点没返回 checked_in_today 时按未签处理，宁可多签一次也不漏签。"""
        client = make_client([
            refresh_ok(),
            FakeResponse(200, {"success": True, "data": {"stats": {}}}),
            FakeResponse(200, {"success": True, "message": "签到成功"}),
        ])
        result = client.checkin(turnstile_provider=lambda: ("tok", ""))
        assert result.kind == api.SUCCESS

    def test_network_error_is_reported(self):
        client = make_client([])

        def boom(*_a, **_k):
            raise OSError("connection reset")

        client._session.request = boom
        result = client.checkin(turnstile_provider=lambda: ("tok", ""))
        assert result.kind == api.NETWORK_ERROR
        assert "connection reset" in result.message


class TestWritebackEndpoint:
    """凭据回写：平台还捏着旧代次的话，网页端检测会直接失败，所以这条链路要可靠。"""

    def make_sync(self, **kw):
        raw = {"enabled": True, "url": "https://panel.example.com/api/config"}
        raw.update(kw)
        return cfgmod.ConfigSyncConfig.from_raw(raw)

    def test_endpoint_derived_from_pull_url(self):
        from newapi_checkin.remote_sync import _writeback_endpoint

        endpoint = _writeback_endpoint(self.make_sync(), "TaBiAI")
        assert endpoint == "https://panel.example.com/api/accounts/TaBiAI/refresh-cookie"

    def test_account_name_is_url_encoded(self):
        from newapi_checkin.remote_sync import _writeback_endpoint

        endpoint = _writeback_endpoint(self.make_sync(), "我的 站点/A")
        assert " " not in endpoint and "/A/" not in endpoint
        assert endpoint.endswith("/refresh-cookie")

    def test_explicit_template_wins(self):
        from newapi_checkin.remote_sync import _writeback_endpoint

        sync = self.make_sync(writeback_url="https://other.example.com/hook/{name}")
        assert _writeback_endpoint(sync, "A") == "https://other.example.com/hook/A"

    def test_disabled_sync_reports_reason_without_request(self, monkeypatch):
        from newapi_checkin import remote_sync

        def boom(*_a, **_k):
            raise AssertionError("未启用同步时不应发请求")

        monkeypatch.setattr(remote_sync.cffi, "request", boom)
        sync = cfgmod.ConfigSyncConfig.from_raw({"enabled": False})
        ok, detail = remote_sync.writeback_refresh_cookie(sync, "A", "new_api_refresh=x")
        assert ok is False and "未启用" in detail

    def test_successful_writeback_posts_cookie_with_token(self, monkeypatch):
        from newapi_checkin import remote_sync

        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update({"method": method, "url": url, **kwargs})
            return FakeResponse(200, {"ok": True})

        monkeypatch.setattr(remote_sync.cffi, "request", fake_request)
        sync = self.make_sync(token="k1")
        ok, detail = remote_sync.writeback_refresh_cookie(sync, "A", "new_api_refresh=sid.gen2")
        assert ok is True
        assert seen["method"] == "POST"
        assert seen["json"] == {"cookie": "new_api_refresh=sid.gen2"}
        assert seen["headers"]["Authorization"] == "Bearer k1"
        assert detail.endswith("/api/accounts/A/refresh-cookie")

    def test_http_error_does_not_raise(self, monkeypatch):
        from newapi_checkin import remote_sync

        monkeypatch.setattr(remote_sync.cffi, "request",
                            lambda *a, **k: FakeResponse(500, None, text="boom"))
        ok, detail = remote_sync.writeback_refresh_cookie(self.make_sync(), "A", "c")
        assert ok is False and "HTTP 500" in detail

    def test_transport_error_does_not_raise(self, monkeypatch):
        from newapi_checkin import remote_sync

        def boom(*_a, **_k):
            raise OSError("dns fail")

        monkeypatch.setattr(remote_sync.cffi, "request", boom)
        ok, detail = remote_sync.writeback_refresh_cookie(self.make_sync(), "A", "c")
        assert ok is False and "dns fail" in detail


class TestSessionPersistence:
    def test_rotated_cookie_is_flushed_immediately(self, tmp_path):
        """轮转值不能走节流：进程被杀就等于丢一代，下轮必被判重放。"""
        from newapi_checkin.cf.session_store import SessionStore

        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.remember_refresh_cookie("acct", "new_api_refresh=sid.gen2")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["acct"]["refresh_cookie"] == "new_api_refresh=sid.gen2"

    def test_reload_keeps_latest_generation(self, tmp_path):
        from newapi_checkin.cf.session_store import SessionStore

        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.remember_refresh_cookie("acct", "new_api_refresh=sid.gen2")
        store.remember_refresh_cookie("acct", "new_api_refresh=sid.gen3")
        assert SessionStore(path).get("acct").refresh_cookie == "new_api_refresh=sid.gen3"

    def test_empty_value_is_ignored(self, tmp_path):
        from newapi_checkin.cf.session_store import SessionStore

        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.remember_refresh_cookie("acct", "new_api_refresh=sid.gen2")
        store.remember_refresh_cookie("acct", "   ")
        assert store.get("acct").refresh_cookie == "new_api_refresh=sid.gen2"

    def test_cf_clear_keeps_refresh_cookie(self, tmp_path):
        """作废 CF 会话是常规操作，不能顺手把签到凭据一起清掉。"""
        from newapi_checkin.cf.session_store import SessionStore

        store = SessionStore(tmp_path / "sessions.json")
        store.remember_refresh_cookie("acct", "new_api_refresh=sid.gen2")
        store.clear_cf("acct")
        assert store.get("acct").refresh_cookie == "new_api_refresh=sid.gen2"


class TestRefreshInflightAccounting:
    """refresh 的代次悬空记账。

    这组断言守的是「超时后还敢不敢拿这一代重试」。实测旧代重放的安全窗口只有 20~45 秒
    （放 20s 仍幂等成功，放 45s 直接 AUTH_SESSION_REVOKED、整条会话报废），而 refresh
    一次超时就吃掉半个窗口。所以「请求发出前就记账、超时后不销账」这个顺序是硬要求：
    记晚了或者超时也销账，上层就看不出这一代已经危险，会接着换 IP 重放。
    """

    def test_refresh_uses_its_own_short_timeout(self):
        """必须用 REFRESH_TIMEOUT_SECONDS，不能沿用 http.timeout（默认 20s）。

        20s 正好是已证实安全窗口的下界 —— 用它做超时等于一次超时就烧光预算。
        """
        client = make_client([refresh_ok()])
        client.refresh()
        sent = client._session.calls[0]
        assert sent["kwargs"]["timeout"] == tabiai.REFRESH_TIMEOUT_SECONDS
        assert tabiai.REFRESH_TIMEOUT_SECONDS < client.http.timeout

    def test_budget_leaves_room_for_one_retry(self):
        """两次 refresh 超时（8+8）要仍落在 15s 预算内，否则重试机会等于零。"""
        assert tabiai.REFRESH_TIMEOUT_SECONDS * 2 <= tabiai.INFLIGHT_BUDGET_SECONDS + 1
        assert tabiai.INFLIGHT_BUDGET_SECONDS < 20      # 必须小于实测窗口下界

    def test_inflight_is_marked_before_the_request_goes_out(self):
        """记账必须早于请求发出 —— 超时的那一次恰恰是最需要记账的。"""
        order = []
        client = make_client([refresh_ok()],
                             on_inflight=lambda cookie: order.append(("mark", cookie)))
        client._session.request = (
            lambda method, url, headers=None, **kw: (
                order.append(("request", method)),
                refresh_ok(),
            )[1]
        )
        client.refresh()
        assert [step for step, _ in order] == ["mark", "request"]
        assert order[0][1] == "new_api_refresh=sid.gen1"      # 带的是当前这一代

    def test_settled_clears_the_mark_once_a_response_arrives(self):
        client = make_client([refresh_ok()])
        settled = []
        client.on_settled = lambda: settled.append(True)
        client.refresh()
        assert settled == [True]

    def test_http_error_still_settles(self):
        """4xx 也是确定答案：站点侧状态已知，悬空态就该结束。"""
        client = make_client([FakeResponse(401, {"code": "AUTH_UNAUTHORIZED"})])
        settled = []
        client.on_settled = lambda: settled.append(True)
        client.refresh()
        assert settled == [True]

    def test_timeout_keeps_the_mark(self):
        """超时**不能**销账：站点可能已经推进代次，这一代必须继续被视为危险。"""
        client = make_client([OSError("timed out")])
        settled = []
        client.on_inflight = lambda cookie: None
        client.on_settled = lambda: settled.append(True)
        result = client.refresh()
        assert result.result.kind == api.NETWORK_ERROR
        assert settled == []                    # 标记留着，挡住预算外的重试

    def test_missing_callbacks_do_not_break_refresh(self):
        """回调是可选的：桌面版/测试里不接线也不能炸。"""
        client = make_client([refresh_ok()])
        assert client.on_inflight is None and client.on_settled is None
        assert client.refresh().result.kind == api.SUCCESS
