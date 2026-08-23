"""端到端验证策略链（不含浏览器）：起一个本地 HTTP 服务模拟 New API。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from newapi_checkin import config as cfgmod
from newapi_checkin import logger as log
from newapi_checkin import runner as runner_mod

STATE = {"mode": "ok", "hits": [], "balance": None, "quota_per_unit": None}

CF_BODY = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<script src='/cdn-cgi/challenge-platform/x'></script></body></html>"
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # 关掉 stderr 噪音
        pass

    def _send(self, status, payload, content_type="application/json", headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _cf_challenge(self):
        self._send(403, CF_BODY.encode(), "text/html",
                   {"server": "cloudflare", "cf-ray": "test-ray", "cf-mitigated": "challenge"})

    def _waf_block(self):
        body = ("<html><head><title>Attention Required! | Cloudflare</title></head>"
                "<body>Sorry, you have been blocked. Error 1020</body></html>")
        self._send(403, body.encode(), "text/html",
                   {"server": "cloudflare", "cf-ray": "test-ray"})

    def do_GET(self):
        STATE["hits"].append(("GET", self.path))
        if STATE["mode"] == "cf":
            self._cf_challenge()
            return
        if STATE["mode"] == "waf":
            self._waf_block()
            return
        if self.path == "/api/user/self":
            if STATE["mode"] == "auth":
                self._send(401, {"success": False, "message": "无权进行此操作"})
                return
            payload = {"id": 42, "username": "kiro"}
            # quota 是账户剩余额度。默认不给，保留老用例「拿不到余额」的场景；
            # 需要验证余额链路的用例把 STATE["balance"] 设上
            if STATE.get("balance") is not None:
                payload["quota"] = STATE["balance"]
            self._send(200, {"success": True, "data": payload})
            return
        if self.path == "/api/status":
            unit = STATE.get("quota_per_unit")
            if unit is None:
                self._send(404, {"success": False, "message": "not found"})
                return
            self._send(200, {"success": True, "data": {"quota_per_unit": unit}})
            return
        self._send(404, {"success": False, "message": "not found"})

    def do_POST(self):
        STATE["hits"].append(("POST", self.path))
        if STATE["mode"] == "cf":
            self._cf_challenge()
            return
        # 第一个候选路径故意不存在，用来验证路径探测
        if self.path == "/api/user/checkin":
            self._send(404, {"success": False, "message": "not found"})
            return
        if self.path == "/api/user/check_in":
            if STATE["mode"] == "already":
                self._send(200, {"success": False, "message": "今日已签到"})
                return
            self._send(200, {"success": True, "message": "签到成功",
                             "data": {"quota_awarded": 1000}})
            return
        self._send(404, {"success": False, "message": "not found"})


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    STATE["mode"] = "ok"
    STATE["hits"] = []
    STATE["balance"] = None
    STATE["quota_per_unit"] = None
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def wire(tmp_path, monkeypatch):
    """把会话缓存指向临时目录，并屏蔽出口 IP 探测（避免真实联网）。"""
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=8: "127.0.0.1")
    return tmp_path


def make_runner(url, **opts):
    cfg = cfgmod.build_config({
        "defaults": {"retry": 1, "interval_seconds": [0, 0]},
        "http": {"timeout": 10},
        "accounts": [{"name": "本地站", "url": url, "cookie": "session=test"}],
    })
    options = runner_mod.RunOptions(use_browser=False, use_ai=False, **opts)
    return runner_mod.Runner(cfg, options)


class TestHappyPath:
    def test_success_with_path_probing(self, server, wire):
        runner = make_runner(server)
        assert runner.run() == 0
        row = runner.summary.rows[0]
        assert row.status == "success"
        assert row.quota == 1000
        assert row.strategy == "S1 指纹直连"
        # 第一个候选 404 后自动试到第二个
        assert ("POST", "/api/user/checkin") in STATE["hits"]
        assert ("POST", "/api/user/check_in") in STATE["hits"]

    def test_second_run_reuses_cached_path_and_user_id(self, server, wire):
        assert make_runner(server).run() == 0
        STATE["hits"] = []
        assert make_runner(server).run() == 0
        # 路径已缓存 -> 不再试那个 404 的候选
        assert ("POST", "/api/user/checkin") not in STATE["hits"]
        assert ("POST", "/api/user/check_in") in STATE["hits"]
        # self 仍会被请求，但目的变了：不是拿 user_id（已缓存），而是签到后查剩余额度。
        # 顺序能证明这一点 —— 它出现在签到之后，而不是之前。
        hits = STATE["hits"]
        assert hits.index(("POST", "/api/user/check_in")) < hits.index(("GET", "/api/user/self"))

    def test_already_checked_in_counts_as_success(self, server, wire):
        STATE["mode"] = "already"
        runner = make_runner(server)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "already_done"


class TestBalance:
    """签到后补查余额：这是「额度列一直空着」的修复点。"""

    def test_balance_attached_after_checkin(self, server, wire):
        STATE["balance"] = 6170000
        runner = make_runner(server)
        assert runner.run() == 0
        row = runner.summary.rows[0]
        assert row.balance == 6170000
        # quota 仍是本次奖励，两个字段各管各的，不许互相覆盖
        assert row.quota == 1000

    def test_already_done_also_gets_balance(self, server, wire):
        """今日已签时签到接口不返回奖励额度，余额是那一列唯一能显示的东西。"""
        STATE["mode"] = "already"
        STATE["balance"] = 250000
        runner = make_runner(server)
        assert runner.run() == 0
        row = runner.summary.rows[0]
        assert row.status == "already_done" and row.balance == 250000

    def test_quota_per_unit_probed_and_cached(self, server, wire):
        STATE["balance"] = 1000000
        STATE["quota_per_unit"] = 250000
        assert make_runner(server).run() == 0
        STATE["hits"] = []
        runner = make_runner(server)
        assert runner.run() == 0
        # 换算率已落 sessions.json，第二轮不该再打 /api/status
        assert ("GET", "/api/status") not in STATE["hits"]
        assert runner.summary.rows[0].quota_per_unit == 250000

    def test_status_404_falls_back_to_default_unit(self, server, wire):
        """站点不给 quota_per_unit 也要能出金额，按默认 500000 算。"""
        STATE["balance"] = 1000000          # /api/status 返回 404
        runner = make_runner(server)
        assert runner.run() == 0
        row = runner.summary.rows[0]
        assert row.quota_per_unit is None
        assert "$2.00" in log.format_balance(row.balance, row.quota_per_unit, None)

    def test_self_failure_does_not_fail_checkin(self, server, wire, monkeypatch):
        """查余额炸了也不能把签成功的账号弄成失败。"""
        import newapi_checkin.client as client_mod

        original = client_mod.ApiClient.fetch_self
        calls = {"n": 0}

        def flaky(self):
            calls["n"] += 1
            if calls["n"] > 1:            # 第一次照常（拿 user_id），补查那次炸
                raise RuntimeError("boom")
            return original(self)

        monkeypatch.setattr(client_mod.ApiClient, "fetch_self", flaky)
        runner = make_runner(server)
        assert runner.run() == 0
        row = runner.summary.rows[0]
        assert row.status == "success" and row.balance is None


class TestFailures:
    def test_auth_failure_does_not_retry(self, server, wire):
        STATE["mode"] = "auth"
        runner = make_runner(server)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "skipped"
        # cookie 过期重试无意义，只能请求一次
        assert STATE["hits"].count(("GET", "/api/user/self")) == 1

    def test_cf_blocked_without_browser(self, server, wire):
        STATE["mode"] = "cf"
        runner = make_runner(server)
        assert runner.run() == 0
        row = runner.summary.rows[0]
        assert row.status == "skipped"
        assert "no-browser" in row.detail or "禁用" in row.detail

    def test_waf_block_is_terminal_and_not_retried(self, server, wire):
        """WAF 硬封禁不是质询，重试只会加重风控，必须一次就停。"""
        STATE["mode"] = "waf"
        runner = make_runner(server)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "skipped"
        assert STATE["hits"].count(("GET", "/api/user/self")) == 1

    def test_missing_cookie_is_skipped(self, server, wire):
        cfg = cfgmod.build_config({
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [{"name": "无 cookie", "url": server}],
        })
        runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_browser=False, use_ai=False))
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "skipped"

    def test_no_enabled_accounts_returns_config_error_code(self, server, wire):
        cfg = cfgmod.build_config({
            "accounts": [{"name": "A", "url": server, "cookie": "c", "enabled": False}],
        })
        runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_browser=False))
        assert runner.run() == 2


class TestDryRun:
    def test_dry_run_never_posts(self, server, wire):
        runner = make_runner(server, dry_run=True)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "success"
        assert not any(method == "POST" for method, _ in STATE["hits"])


class TestTabiAIMode:
    def test_cookie_test_only_refreshes_and_persists_user_id(self, tmp_path, monkeypatch):
        from newapi_checkin import client as api
        from newapi_checkin import tabiai

        calls = []

        class FakeTabiAIClient:
            def __init__(self, account, http, cookie, cf=None, on_rotate=None,
                         on_inflight=None, on_settled=None):
                calls.append(("init", account.login_method, cookie, cf))
                self.impersonate = "chrome"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                pass

            def checkin(self, turnstile_provider=None, dry_run=False):
                calls.append(("checkin", dry_run, turnstile_provider is None))
                return api.ApiResult(api.SUCCESS, message="TaBiAI 凭据有效", user_id=42)

        monkeypatch.setattr(tabiai, "TabiAIClient", FakeTabiAIClient)
        monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=8: "127.0.0.1")
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        cfg = cfgmod.build_config({
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [{
                "name": "TaBiAI",
                "url": "https://tabiai.example.com",
                "login_method": "tabiai",
                "cookie": "new_api_refresh=sid.secret",
            }],
        })
        options = runner_mod.RunOptions(use_browser=False, use_ai=False, cookie_test="tabiai")

        runner = runner_mod.Runner(cfg, options)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == api.SUCCESS
        # 只验证凭据时不应准备 Turnstile provider（省掉浏览器和频率配额）
        assert calls == [
            ("init", "tabiai", "new_api_refresh=sid.secret", None),
            ("checkin", True, True),
        ]
        session_data = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
        assert session_data[cfg.accounts[0].slug]["user_id"] == 42

    def test_legacy_github_method_maps_to_tabiai(self):
        cfg = cfgmod.build_config({
            "accounts": [{
                "name": "旧配置",
                "url": "https://tabiai.example.com",
                "login_method": "github_cookie",
                "cookie": "new_api_refresh=sid.secret",
            }],
        })
        assert cfg.accounts[0].login_method == cfgmod.LOGIN_METHOD_TABIAI

    def test_missing_credential_is_skipped_without_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        cfg = cfgmod.build_config({
            "accounts": [{
                "name": "缺凭据",
                "url": "https://tabiai.example.com",
                "login_method": "tabiai",
            }],
        })
        runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_browser=False, use_ai=False))
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "skipped"
        assert "TaBiAI 凭据" in runner.summary.rows[0].detail

    def test_stored_rotated_cookie_is_used_when_config_empty(self, tmp_path, monkeypatch):
        """凭据轮转后配置里可能还是旧值，本地 store 的新代次必须能兜住。"""
        from newapi_checkin.cf.session_store import SessionStore

        sessions = tmp_path / "sessions.json"
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", sessions)
        cfg = cfgmod.build_config({
            "accounts": [{
                "name": "TaBiAI",
                "url": "https://tabiai.example.com",
                "login_method": "tabiai",
            }],
        })
        store = SessionStore(sessions)
        store.remember_refresh_cookie(cfg.accounts[0].slug, "new_api_refresh=sid.gen2")
        runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_browser=False, use_ai=False))
        assert runner._tabiai_cookie(cfg.accounts[0]) == "new_api_refresh=sid.gen2"


class TestSessionCache:
    def test_cf_cookie_cache_is_used_then_invalidated(self, server, wire, monkeypatch):
        from newapi_checkin.cf.session_store import CFSession, SessionStore
        from newapi_checkin.utils import now

        # 先写一份「有效」缓存，再让服务端返回质询，确认缓存被作废
        store = SessionStore(wire / "sessions.json")
        store.update_cf(
            cfgmod.slugify("本地站"),
            CFSession(cookies={"cf_clearance": "stale"}, user_agent="Mozilla/5.0 Firefox/133.0",
                      exit_ip="127.0.0.1", expires_at=now() + 3600, saved_at=now()),
        )
        store.flush()

        STATE["mode"] = "cf"
        runner = make_runner(server)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "skipped"

        reloaded = SessionStore(wire / "sessions.json")
        assert reloaded.get(cfgmod.slugify("本地站")).cf is None
