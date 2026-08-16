"""端到端验证策略链（不含浏览器）：起一个本地 HTTP 服务模拟 New API。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from newapi_checkin import config as cfgmod
from newapi_checkin import runner as runner_mod

STATE = {"mode": "ok", "hits": []}

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
            self._send(200, {"success": True, "data": {"id": 42, "username": "kiro"}})
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
        # user_id 已缓存 -> 不再调 /api/user/self；路径已缓存 -> 不再试 404 的那个
        assert ("GET", "/api/user/self") not in STATE["hits"]
        assert ("POST", "/api/user/checkin") not in STATE["hits"]
        assert ("POST", "/api/user/check_in") in STATE["hits"]

    def test_already_checked_in_counts_as_success(self, server, wire):
        STATE["mode"] = "already"
        runner = make_runner(server)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "already_done"


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


class TestGithubCookieMode:
    def test_github_cookie_uses_github_session_and_dry_run(self, tmp_path, monkeypatch):
        from newapi_checkin import client as api
        from newapi_checkin import github_oauth

        calls = []

        class FakeGithubClient:
            def __init__(self, account, http, cf=None):
                calls.append(("init", account.login_method, account.github_user_session, cf))
                self.impersonate = "chrome"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                pass

            def checkin(self, dry_run=False):
                calls.append(("checkin", dry_run))
                return api.ApiResult(api.SUCCESS, message="GitHub Cookie 可用")

        monkeypatch.setattr(github_oauth, "GithubOAuthClient", FakeGithubClient)
        monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=8: "127.0.0.1")
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        cfg = cfgmod.build_config({
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [{
                "name": "GitHub",
                "url": "https://github.example.com",
                "login_method": "github_cookie",
                "github_user_session": "secret-session",
            }],
        })
        options = runner_mod.RunOptions(
            use_browser=False,
            use_ai=False,
            cookie_test="github_cookie",
        )

        runner = runner_mod.Runner(cfg, options)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == api.SUCCESS
        assert calls == [
            ("init", "github_cookie", "secret-session", None),
            ("checkin", True),
        ]

    def test_missing_github_cookie_is_skipped_without_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        cfg = cfgmod.build_config({
            "accounts": [{
                "name": "缺 GitHub Cookie",
                "url": "https://github.example.com",
                "login_method": "github_cookie",
            }],
        })
        runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_browser=False, use_ai=False))
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "skipped"
        assert "GitHub Cookie" in runner.summary.rows[0].detail


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
