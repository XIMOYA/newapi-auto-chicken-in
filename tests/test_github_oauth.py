"""
tests/test_github_oauth.py
测试：github_oauth.issue_refresh_cookie（GitHub OAuth 三步签发 new_api_refresh）
盯住的点（都是 Go 侧 server/tabiai.go 踩过坑后固化下来的约束）：
- 三步必须同一个 session：state 与站点会话绑定，换 session 即失效
- 每一发都必须走 account.proxy，包括打 github.com 那步 —— 签发和签到必须同一出口 IP
- 一律不跟随重定向：code 在第 2 步 302 的 Location 里
- 错误分支的判定顺序与文案照搬 extractGithubAuthorizeCode
- 任何失败都返回 (,"原因") 而不是抛异常，且文案里绝不出现 user_session 明文
全部用假响应，不发真实网络请求。
"""

from __future__ import annotations

import json

import pytest

from newapi_checkin import config as cfgmod
from newapi_checkin import github_oauth as oauth

BASE = "https://tabiai.example.com"
PROXY = "http://proxy.example.com:8080"
USER_SESSION = "gh-user-session-secret-value"
FLOW_TOKEN = "flow-token-1"


class FakeHeaders(dict):
    """curl_cffi 的 headers 既能 get()/items() 又能 get_list()，这里都模拟一份。"""

    def __init__(self, set_cookie=(), location=""):
        super().__init__()
        self._set_cookie = list(set_cookie)
        if self._set_cookie:
            self["Set-Cookie"] = self._set_cookie[0]
        if location:
            self["Location"] = location
        self["Content-Type"] = "application/json"

    def get_list(self, name):
        return list(self._set_cookie) if name.lower() == "set-cookie" else []


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", set_cookie=(), location="",
                 cookies=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.cookies = cookies or {}
        self.headers = FakeHeaders(set_cookie, location)
        self.url = BASE

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _fake_curl(monkeypatch, script):
    """替掉 curl_cffi.Session：记构造参数与每一发的 kwargs，并模拟 cookiejar 的跨步复用。

    jar 快照是「同一 session」这条约束的证据：第 1 步响应里的会话 cookie 被 jar 收下后，
    第 3 步发请求时必须还看得见它。
    """
    pending = list(script)
    made = []

    class FakeCurlSession:
        def __init__(self, **kwargs):
            self.init_kwargs = dict(kwargs)
            self.headers = {}
            self.cookies = {}
            self.calls = []
            self.closed = False
            made.append(self)

        def request(self, method, url, headers=None, **kwargs):
            self.calls.append({
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "kwargs": dict(kwargs),
                "jar": dict(self.cookies),
            })
            if not pending:
                raise AssertionError(f"没有为 {method} {url} 预置响应")
            outcome = pending.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            for raw in outcome.headers.get_list("Set-Cookie"):
                name, _, rest = str(raw).partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0].strip()
            return outcome

        def close(self):
            self.closed = True

    monkeypatch.setattr(oauth.cffi, "Session", FakeCurlSession)
    return made


def make_account(proxy=PROXY, client_id="cid-from-account", user_session=USER_SESSION,
                 url=BASE):
    item = {
        "name": "TaBiAI",
        "url": url,
        "login_method": "tabiai",
        "cookie": "new_api_refresh=sid.old",
        "github_user_session": user_session,
        "github_client_id": client_id,
    }
    if proxy:
        item["proxy"] = proxy
    cfg = cfgmod.build_config({"accounts": [item]})
    return cfg.accounts[0], cfg.http


def state_ok(token=FLOW_TOKEN, session_cookie="new_api_session=site-sess"):
    return FakeResponse(200, {"success": True, "data": {"flow_token": token}},
                        set_cookie=[f"{session_cookie}; Path=/; HttpOnly"])


def status_ok(client_id="cid-from-status"):
    return FakeResponse(200, {"success": True, "data": {"github_client_id": client_id}})


def authorize_ok(code="gh-code-1"):
    return FakeResponse(302, location=f"{BASE}/oauth/github?code={code}&state={FLOW_TOKEN}")


def callback_ok(value="sid.gen1"):
    return FakeResponse(200, {"success": True},
                        set_cookie=[f"new_api_refresh={value}; Path=/api/user/auth; HttpOnly"])


def run(monkeypatch, script, **account_kwargs):
    """跑一次签发，返回 (cookie, error, 被造出来的 session 列表)。"""
    account, http = make_account(**account_kwargs)
    made = _fake_curl(monkeypatch, script)
    cookie, error = oauth.issue_refresh_cookie(account, http)
    return cookie, error, made


class TestHappyPath:
    def test_three_steps_return_the_new_cookie(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()])

        assert error == ""
        assert cookie == "new_api_refresh=sid.gen1"
        calls = made[0].calls
        assert [call["method"] for call in calls] == ["POST", "GET", "GET"]
        assert calls[0]["url"] == f"{BASE}/api/oauth/state"
        assert calls[1]["url"].startswith("https://github.com/login/oauth/authorize?")
        assert calls[2]["url"].startswith(f"{BASE}/api/oauth/github?")
        assert "code=gh-code-1" in calls[2]["url"]
        assert f"state={FLOW_TOKEN}" in calls[2]["url"]
        # 返回值必须已经是 name=value 形态，能直接喂给 normalize_refresh_cookie
        from newapi_checkin import tabiai

        assert tabiai.normalize_refresh_cookie(cookie) == cookie


    def test_authorize_query_carries_client_id_scope_and_state(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()])
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(made[0].calls[1]["url"]).query)
        assert error == "" and cookie
        assert query["client_id"] == ["cid-from-account"]
        assert query["scope"] == ["user:email"]
        assert query["state"] == [FLOW_TOKEN]

    def test_github_step_sends_the_three_cookie_keys(self, monkeypatch):
        _, error, made = run(monkeypatch, [state_ok(), authorize_ok(), callback_ok()])
        header = made[0].calls[1]["headers"]["Cookie"]

        assert error == ""
        assert f"user_session={USER_SESSION}" in header
        assert f"__Host-user_session_same_site={USER_SESSION}" in header
        assert header.endswith("logged_in=yes")
        # github.com 那步不该带站点的 Referer / Origin
        assert "Referer" not in made[0].calls[1]["headers"]
        assert "Origin" not in made[0].calls[1]["headers"]

    def test_site_steps_carry_common_headers(self, monkeypatch):
        _, error, made = run(monkeypatch, [state_ok(), authorize_ok(), callback_ok()])
        state_headers = made[0].calls[0]["headers"]
        callback_headers = made[0].calls[2]["headers"]

        assert error == ""
        assert state_headers["Origin"] == BASE
        assert state_headers["Referer"] == BASE + "/"
        assert state_headers["Content-Type"] == "application/json"
        assert state_headers["Cache-Control"] == "no-store"
        # 回调那步的 Referer 被覆盖成 OAuth 回调页（照 Go）
        assert callback_headers["Referer"] == BASE + "/oauth/github"

    def test_state_body_is_the_github_login_intent(self, monkeypatch):
        _, error, made = run(monkeypatch, [state_ok(), authorize_ok(), callback_ok()])
        payload = json.loads(made[0].calls[0]["kwargs"]["data"].decode("utf-8"))

        assert error == ""
        assert payload == {"provider": "github", "intent": "login"}

    def test_session_config_comes_from_http_config(self, monkeypatch):
        account, http = make_account()
        made = _fake_curl(monkeypatch, [state_ok(), authorize_ok(), callback_ok()])
        oauth.issue_refresh_cookie(account, http)

        assert made[0].init_kwargs["timeout"] == http.timeout
        assert made[0].init_kwargs["verify"] == http.verify
        assert made[0].init_kwargs["impersonate"] == http.impersonate
        assert made[0].closed is True


class TestSameSessionAndProxy:
    def test_all_three_steps_share_one_session(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()])

        assert error == "" and cookie
        # 只造了一个 session，三发全落在它身上
        assert len(made) == 1
        assert len(made[0].calls) == 3

    def test_cookie_jar_is_really_reused_across_steps(self, monkeypatch):
        """第 1 步收下的站点会话 cookie，到第 3 步必须还在 jar 里。

        state 是与站点会话绑定的，jar 一断第 3 步就会被站点判成「state 不属于你」。
        """
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()])
        calls = made[0].calls

        assert error == "" and cookie
        assert calls[0]["jar"] == {}                               # 第 1 步之前 jar 是空的
        assert calls[1]["jar"]["new_api_session"] == "site-sess"    # 第 1 步的 cookie 收下了
        assert calls[2]["jar"]["new_api_session"] == "site-sess"    # 第 3 步还看得见

    def test_every_step_goes_through_the_account_proxy(self, monkeypatch):
        """包括打 github.com 那一步：签发和签到必须同一个出口 IP。"""
        cookie, error, made = run(
            monkeypatch, [state_ok(), status_ok(), authorize_ok(), callback_ok()],
            client_id="")
        expected = {"http": PROXY, "https": PROXY}

        assert error == "" and cookie
        assert made[0].init_kwargs["proxies"] == expected
        assert len(made[0].calls) == 4
        for call in made[0].calls:
            assert call["kwargs"]["proxies"] == expected, f"{call['url']} 没带代理"

    def test_no_step_follows_redirects(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()])

        assert error == "" and cookie
        for call in made[0].calls:
            assert call["kwargs"]["allow_redirects"] is False

    def test_broken_proxy_url_fails_instead_of_going_direct(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()],
            proxy="proxy.example.com:8080")

        assert cookie == ""
        assert error == "代理地址无效"
        assert made == []       # 连 session 都没造，绝不可能悄悄走直连


class TestStateStep:
    def test_success_false_reports_site_message(self, monkeypatch):
        cookie, error, made = run(monkeypatch, [
            FakeResponse(200, {"success": False, "message": "OAuth 未开启"}),
        ])

        assert cookie == ""
        assert error == "取 OAuth state 失败：OAuth 未开启"
        assert len(made[0].calls) == 1       # 失败即收工，不再往下打

    def test_success_false_without_message_falls_back_to_status(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [FakeResponse(400, {"success": False})])

        assert cookie == ""
        assert error == "取 OAuth state 失败：HTTP 400"

    def test_missing_flow_token_is_reported(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            FakeResponse(200, {"success": True, "data": {"other": "x"}}),
        ])

        assert cookie == ""
        assert error == "OAuth state 成功但未返回 flow_token"

    def test_non_json_hints_at_the_interception(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            FakeResponse(403, None, text="<html>Just a moment...</html>"),
        ])

        assert cookie == ""
        assert error == "OAuth state HTTP 403 非 JSON 响应（站点可能拦截了当前出口）"

    def test_legacy_plain_state_string_is_accepted(self, monkeypatch):
        """旧结构把 state 直接放在 data 上给字符串，Go 也兼容这一形态。"""
        cookie, error, made = run(monkeypatch, [
            FakeResponse(200, {"success": True, "data": "legacy-state"}),
            FakeResponse(302, location=f"{BASE}/oauth/github?code=c1&state=legacy-state"),
            callback_ok(),
        ])

        assert error == "" and cookie == "new_api_refresh=sid.gen1"
        assert "state=legacy-state" in made[0].calls[1]["url"]


class TestClientID:
    def test_account_value_wins_and_skips_status(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()])

        assert error == "" and cookie
        # 只有三发：账号已配 client_id，就不该再去问 /api/status
        assert [call["url"] for call in made[0].calls].count(f"{BASE}/api/status") == 0
        assert "client_id=cid-from-account" in made[0].calls[1]["url"]

    def test_falls_back_to_site_status(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), status_ok(), authorize_ok(), callback_ok()],
            client_id="")

        assert error == "" and cookie
        assert made[0].calls[1]["url"] == f"{BASE}/api/status"
        assert "client_id=cid-from-status" in made[0].calls[2]["url"]

    def test_missing_everywhere_asks_the_user_to_fill_it(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), FakeResponse(200, {"success": True, "data": {}})],
            client_id="")

        assert cookie == ""
        assert error == "站点状态未返回 github_client_id，请在账号里手动填写"
        # 绝不能内置默认 client_id 兜底 —— 用错会授权到别人的应用上
        assert len(made[0].calls) == 2

    def test_status_error_status_is_reported(self, monkeypatch):
        cookie, error, _ = run(
            monkeypatch, [state_ok(), FakeResponse(502, {"success": False})],
            client_id="")

        assert cookie == ""
        assert error == "站点状态 HTTP 502 非法响应"

    def test_status_non_json_is_reported(self, monkeypatch):
        cookie, error, _ = run(
            monkeypatch, [state_ok(), FakeResponse(200, None, text="<html>nope</html>")],
            client_id="")

        assert cookie == ""
        assert error == "站点状态 HTTP 200 非法响应"


class TestAuthorizeStep:
    def test_redirect_to_login_means_user_session_expired(self, monkeypatch):
        """最关键的一条：调用方要靠它告诉用户去重新粘贴 user_session。"""
        cookie, error, made = run(monkeypatch, [
            state_ok(),
            FakeResponse(302, location="https://github.com/login?client_id=cid&return_to=%2F"),
        ])

        assert cookie == ""
        assert error == "GitHub 要求重新登录，user_session 已失效"
        assert len(made[0].calls) == 2       # 没拿到 code 就不去打回调

    def test_login_check_wins_even_without_a_redirect_status(self, monkeypatch):
        """判 /login 压在「是否重定向」之前，顺序照 Go，别调换。"""
        cookie, error, _ = run(monkeypatch, [
            state_ok(),
            FakeResponse(200, None, text="<html>login</html>",
                         location="https://github.com/login/oauth/authorize?x=1"),
        ])

        assert cookie == ""
        assert error == "GitHub 要求重新登录，user_session 已失效"

    @pytest.mark.parametrize("status", [403, 429])
    def test_rate_limited_exit_is_called_out(self, monkeypatch, status):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), FakeResponse(status, None, text="rate limited"),
        ])

        assert cookie == ""
        assert error == f"GitHub authorize HTTP {status}，当前出口被 GitHub 限制，稍后再试"

    def test_non_redirect_suggests_authorizing_the_app(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), FakeResponse(200, None, text="<html>authorize</html>"),
        ])

        assert cookie == ""
        assert error == ("GitHub 未返回授权重定向（HTTP 200），"
                        "可能需要先在 GitHub 授权该 OAuth 应用")

    def test_redirect_without_code_carries_the_reason(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(),
            FakeResponse(302, location=f"{BASE}/oauth/github?error=access_denied"
                                       "&error_description=The+user+denied+access"),
        ])

        assert cookie == ""
        assert error == "GitHub 未返回 code：The user denied access"

    def test_redirect_with_only_error_code(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), FakeResponse(302, location=f"{BASE}/oauth/github?error=redirect_uri_mismatch"),
        ])

        assert cookie == ""
        assert error == "GitHub 未返回 code：redirect_uri_mismatch"

    def test_redirect_with_nothing_useful(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), FakeResponse(302, location=f"{BASE}/oauth/github"),
        ])

        assert cookie == ""
        assert error == "GitHub 未返回授权 code（HTTP 302）"


class TestCallbackStep:
    def test_missing_set_cookie_is_reported(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), authorize_ok(), FakeResponse(200, {"success": True}),
        ])

        assert cookie == ""
        assert error == "OAuth 回调成功但站点未下发 new_api_refresh（HTTP 200）"

    def test_business_failure_message_wins_over_the_generic_line(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), authorize_ok(),
            FakeResponse(400, {"success": False, "message": "state 已过期"}),
        ])

        assert cookie == ""
        assert error == "OAuth 回调失败：state 已过期"

    def test_failure_without_message_falls_back_to_the_error_field(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), authorize_ok(),
            FakeResponse(401, {"success": False, "error": "invalid_state"}),
        ])

        assert cookie == ""
        assert error == "OAuth 回调失败：invalid_state"

    def test_refresh_cookie_is_picked_out_of_many_set_cookies(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(), authorize_ok(),
            FakeResponse(200, {"success": True}, set_cookie=[
                "other=1; Path=/",
                "new_api_refresh=sid.gen7; Path=/api/user/auth; HttpOnly; Secure",
                "trailing=2; Path=/",
            ]),
        ])

        assert error == ""
        assert cookie == "new_api_refresh=sid.gen7"

    def test_cookie_jar_is_the_fallback_when_headers_hide_set_cookie(self, monkeypatch):
        """curl_cffi 个别版本不把 Set-Cookie 原样暴露，此时从 jar 里兜底取（同 tabiai）。"""
        cookie, error, _ = run(monkeypatch, [
            state_ok(), authorize_ok(),
            FakeResponse(200, {"success": True}, cookies={"new_api_refresh": "sid.gen8"}),
        ])

        assert error == ""
        assert cookie == "new_api_refresh=sid.gen8"


class TestNetworkErrors:
    """网络异常一律翻成 error 返回：调用方在签到主循环里，抛异常会打断整轮。"""

    def test_state_step_network_error(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [TimeoutError("Connection timed out after 20000 ms")])

        assert cookie == ""
        assert error.startswith("OAuth state 网络错误：")
        assert "TimeoutError" in error

    def test_status_step_network_error(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [state_ok(), OSError("proxy tunnel failed")],
                               client_id="")

        assert cookie == ""
        assert error.startswith("站点状态网络错误：")

    def test_authorize_step_network_error(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [state_ok(), OSError("connection reset by peer")])

        assert cookie == ""
        assert error.startswith("GitHub authorize 网络错误：")

    def test_callback_step_network_error(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [state_ok(), authorize_ok(), OSError("read timeout")])

        assert cookie == ""
        assert error.startswith("OAuth 回调网络错误：")

    def test_session_construction_failure_is_swallowed(self, monkeypatch):
        account, http = make_account()

        def boom(**_kwargs):
            raise RuntimeError("curl init failed")

        monkeypatch.setattr(oauth.cffi, "Session", boom)
        cookie, error = oauth.issue_refresh_cookie(account, http)

        assert cookie == ""
        assert error.startswith("HTTP 客户端配置失败：")

    def test_unexpected_internal_exception_never_escapes(self, monkeypatch):
        account, http = make_account()
        made = _fake_curl(monkeypatch, [state_ok(), authorize_ok(), callback_ok()])

        def explode(*_args, **_kwargs):
            raise KeyError("something nobody predicted")

        monkeypatch.setattr(oauth, "_fetch_flow_token", explode)
        cookie, error = oauth.issue_refresh_cookie(account, http)

        assert cookie == ""
        assert error == "签发流程内部异常：KeyError"
        assert made[0].closed is True     # 兜底路径也要把 session 关掉


class TestNoSecretLeaks:
    SECRET = "SUPER-SECRET-USER-SESSION-abcdef123456"

    @pytest.mark.parametrize("script", [
        [FakeResponse(200, {"success": False, "message": "站点拒绝"})],
        [state_ok(), FakeResponse(302, location="https://github.com/login?x=1")],
        [state_ok(), FakeResponse(403, None, text="blocked")],
        [state_ok(), authorize_ok(), FakeResponse(200, {"success": True})],
        [TimeoutError("timed out")],
    ])
    def test_error_text_never_contains_the_user_session(self, monkeypatch, script):
        cookie, error, _ = run(monkeypatch, script, user_session=self.SECRET)

        assert cookie == ""
        assert error
        assert self.SECRET not in error
        assert "user_session=" not in error      # 只提字段名可以，绝不能带上值

    def test_site_message_is_truncated_before_going_into_the_mail(self, monkeypatch):
        cookie, error, _ = run(monkeypatch, [
            state_ok(),
            FakeResponse(302, location=f"{BASE}/cb?error_description=" + "x" * 400),
        ])

        assert cookie == ""
        assert error.endswith("…")
        assert len(error) < 400

    def test_control_chars_in_user_session_are_stripped_from_headers(self, monkeypatch):
        """带换行的 user_session 不能把 Cookie 头劈成两截（HTTP 头注入）。"""
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()],
            user_session="abc\r\nX-Injected: 1\ndef中文")
        header = made[0].calls[1]["headers"]["Cookie"]

        assert error == "" and cookie
        assert "\r" not in header and "\n" not in header
        assert "中文" not in header               # 非 latin-1 字符会让 curl_cffi 直接抛错


class TestInputGuards:
    def test_missing_user_session_reports_it_up_front(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()], user_session="  ")

        assert cookie == ""
        assert error.startswith("该账号未填写 GitHub user_session")
        assert made == []

    def test_direct_connection_account_sends_no_proxies(self, monkeypatch):
        cookie, error, made = run(
            monkeypatch, [state_ok(), authorize_ok(), callback_ok()], proxy=None)

        assert error == "" and cookie
        assert made[0].init_kwargs["proxies"] is None
        for call in made[0].calls:
            assert call["kwargs"]["proxies"] is None

    def test_invalid_site_url_is_rejected(self, monkeypatch):
        # build_config 会挡住非 http(s) 的 url，这里直接构 Account 来验证兜底那道校验
        account = cfgmod.Account(name="x", url="ftp://nope", login_method="tabiai",
                                 github_user_session=USER_SESSION)
        made = _fake_curl(monkeypatch, [])
        cookie, error = oauth.issue_refresh_cookie(account, cfgmod.HttpConfig())

        assert cookie == ""
        assert error == "站点 URL 无效：必须是有效的 http(s) 地址"
        assert made == []

    def test_cf_session_supplies_one_ua_for_every_step(self, monkeypatch):
        """cf 缓存里的真实 UA 顶上时，站点两步和 github 那步必须还是同一个 UA。"""
        from newapi_checkin.cf.session_store import CFSession

        account, http = make_account()
        made = _fake_curl(monkeypatch, [state_ok(), authorize_ok(), callback_ok()])
        cf = CFSession(user_agent="Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0",
                       accept_language="en-US,en;q=0.9")
        cookie, error = oauth.issue_refresh_cookie(account, http, cf)
        calls = made[0].calls

        assert error == "" and cookie
        assert calls[0]["headers"]["User-Agent"] == cf.user_agent
        assert calls[1]["headers"]["User-Agent"] == cf.user_agent
        assert calls[0]["headers"]["Accept-Language"] == "en-US,en;q=0.9"
        # TLS 指纹跟着 UA 走：Firefox 的 UA 配 Chrome 的 JA3 一眼假
        assert made[0].init_kwargs["impersonate"] == "firefox"
