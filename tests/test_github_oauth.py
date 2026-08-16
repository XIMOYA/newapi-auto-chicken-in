"""GitHub Cookie OAuth 流程与独立凭据检查测试。"""

from urllib.parse import urlparse

import pytest

from newapi_checkin import github_oauth as oauth
from newapi_checkin import client
from newapi_checkin.config import Account, HttpConfig


class FakeResponse:
    def __init__(self, status=200, payload=None, *, headers=None, text=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = text if text is not None else ("" if payload is None else "{}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.routes(method, url, kwargs)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.routes("GET", url, kwargs)

    def close(self):
        pass


def make_account(**overrides):
    data = {
        "name": "GitHub 账号",
        "url": "https://site.example.com",
        "login_method": "github_cookie",
        "github_user_session": "github-session",
        "github_client_id": "client-id",
    }
    data.update(overrides)
    return Account(**data)


def install_session(monkeypatch, routes):
    holder = {}

    def factory(**_kwargs):
        holder["session"] = FakeSession(routes)
        return holder["session"]

    monkeypatch.setattr(oauth.cffi, "Session", factory)
    return holder


def default_routes(method, url, kwargs):
    path = urlparse(url).path
    if path == "/api/oauth/state":
        return FakeResponse(payload={"success": True, "data": {"flow_token": "state-1"}})
    if path == "/api/status":
        return FakeResponse(payload={"success": True, "data": {"github_client_id": "site-client-id"}})
    if "github.com/login/oauth/authorize" in url:
        return FakeResponse(
            status=302,
            headers={"Location": "https://site.example.com/oauth/callback?code=code-1&state=state-1"},
            text="",
        )
    if path == "/api/oauth/github":
        return FakeResponse(payload={
            "success": True,
            "data": {"checked_in": True, "id": 42, "display_name": "tester"},
        })
    if path == "/api/user/self":
        return FakeResponse(payload={
            "success": True,
            "data": {"id": 42, "username": "tester", "quota": 1234},
        })
    return FakeResponse(status=404, payload={"success": False, "message": "not found"})


def test_cookie_check_gets_code_without_callback(monkeypatch):
    holder = install_session(monkeypatch, default_routes)
    with oauth.GithubOAuthClient(make_account(), HttpConfig()) as client_obj:
        result = client_obj.test_cookie()

    assert result.kind == client.SUCCESS
    assert "未执行签到" in result.message
    state_call = holder["session"].calls[0]
    assert state_call[0] == "POST"
    assert urlparse(state_call[1]).path == "/api/oauth/state"
    assert not any("/api/oauth/github" in call[1] for call in holder["session"].calls)
    github_call = next(call for call in holder["session"].calls if "github.com/login/oauth/authorize" in call[1])
    assert github_call[2]["params"]["client_id"] == "site-client-id"
    assert "user_session=github-session" in github_call[2]["headers"]["Cookie"]
    assert "__Host-user_session_same_site=github-session" in github_call[2]["headers"]["Cookie"]


def test_legacy_oauth_state_fallback(monkeypatch):
    def routes(method, url, kwargs):
        path = urlparse(url).path
        if path == "/api/oauth/state":
            if method == "POST":
                return FakeResponse(status=400, payload={"success": False, "message": "未知的 OAuth 提供商"})
            return FakeResponse(payload={"success": True, "data": "legacy-state"})
        if "github.com/login/oauth/authorize" in url:
            assert kwargs["params"]["state"] == "legacy-state"
            return FakeResponse(
                status=302,
                headers={"Location": "https://site.example.com/oauth/callback?code=legacy-code&state=legacy-state"},
                text="",
            )
        return FakeResponse(status=404, payload={"success": False, "message": "not found"})

    holder = install_session(monkeypatch, routes)
    with oauth.GithubOAuthClient(make_account(), HttpConfig()) as client_obj:
        result = client_obj.test_cookie()

    assert result.kind == client.SUCCESS
    assert holder["session"].calls[0][0] == "POST"
    assert any(call[0] == "GET" and "mode=login" in call[1] for call in holder["session"].calls)


def test_checkin_callback_and_self_are_used(monkeypatch):
    install_session(monkeypatch, default_routes)
    with oauth.GithubOAuthClient(make_account(), HttpConfig()) as client_obj:
        result = client_obj.checkin()

    assert result.kind == client.SUCCESS
    assert result.user_id == 42
    assert result.quota == 1234
    assert "tester" in result.message


def test_success_callback_without_checked_in_is_already_done(monkeypatch):
    def routes(method, url, kwargs):
        response = default_routes(method, url, kwargs)
        if urlparse(url).path == "/api/oauth/github":
            return FakeResponse(payload={
                "success": True,
                "data": {"checked_in": False, "id": 42, "username": "tester"},
            })
        return response

    install_session(monkeypatch, routes)
    with oauth.GithubOAuthClient(make_account(), HttpConfig()) as client_obj:
        result = client_obj.checkin()

    assert result.kind == client.ALREADY_DONE


def test_github_login_redirect_is_auth_failure(monkeypatch):
    def routes(method, url, kwargs):
        if "github.com/login/oauth/authorize" in url:
            return FakeResponse(
                status=302,
                headers={"Location": "https://github.com/login"},
                text="",
            )
        return default_routes(method, url, kwargs)

    install_session(monkeypatch, routes)
    with oauth.GithubOAuthClient(make_account(), HttpConfig()) as client_obj:
        result = client_obj.test_cookie()

    assert result.kind == client.AUTH_FAILED
    assert "user_session" in result.message


def test_site_waf_is_classified_for_runner_retry(monkeypatch):
    def routes(method, url, kwargs):
        if urlparse(url).path == "/api/oauth/state":
            return FakeResponse(
                status=403,
                headers={"Content-Type": "text/html", "server": "cloudflare", "cf-ray": "ray"},
                text="<html>Sorry, you have been blocked. Error 1020</html>",
            )
        return default_routes(method, url, kwargs)

    install_session(monkeypatch, routes)
    with oauth.GithubOAuthClient(make_account(), HttpConfig()) as client_obj:
        result = client_obj.test_cookie()

    assert result.kind == client.WAF_BLOCKED
    assert result.verdict is not None
    assert result.verdict.challenge == "waf_block"
