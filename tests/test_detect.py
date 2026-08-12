"""Cloudflare 拦截识别的多信号判定。"""

from newapi_checkin.cf import detect

CF_HEADERS = {"server": "cloudflare", "cf-ray": "8a1b2c3d4e5f-HKG"}

CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><div id='cf-please-wait'></div>"
    "<script src='/cdn-cgi/challenge-platform/h/b/scripts/jsd/main.js'></script></body></html>"
)
TURNSTILE_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<div class='cf-turnstile'></div>"
    "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"
    "<script src='/cdn-cgi/challenge-platform/x'></script></body></html>"
)
WAF_HTML = (
    "<html><head><title>Attention Required! | Cloudflare</title></head>"
    "<body>Sorry, you have been blocked. Error 1020</body></html>"
)
JS_HTML = "<html><body>Checking your browser before accessing the site. cloudflare</body></html>"


class TestNotBlocked:
    def test_normal_json_through_cloudflare(self):
        verdict = detect.analyze(200, CF_HEADERS, '{"success":true,"data":{"id":3}}')
        assert verdict.blocked is False
        assert verdict.challenge is None

    def test_business_403_is_not_a_challenge(self):
        """业务层的 403（无权限）不能被误判成盾。"""
        verdict = detect.analyze(403, CF_HEADERS, '{"success":false,"message":"无权限"}')
        assert verdict.blocked is False

    def test_no_cloudflare_at_all(self):
        verdict = detect.analyze(500, {"server": "nginx"}, "internal error")
        assert verdict.blocked is False
        assert verdict.signals == []

    def test_turnstile_script_on_normal_login_page(self):
        """登录页引了 Turnstile 脚本但请求本身成功，不算被拦。"""
        html = ("<html><title>登录</title><script "
                "src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script></html>")
        assert detect.analyze(200, CF_HEADERS, html).blocked is False


class TestBlocked:
    def test_managed_challenge(self):
        verdict = detect.analyze(403, {**CF_HEADERS, "cf-mitigated": "challenge"}, CHALLENGE_HTML)
        assert verdict.blocked is True
        assert verdict.challenge == detect.MANAGED_CHALLENGE
        assert verdict.recoverable is True
        assert any("cf-mitigated" in s for s in verdict.signals)

    def test_turnstile_wins_over_managed(self):
        verdict = detect.analyze(403, CF_HEADERS, TURNSTILE_HTML)
        assert verdict.challenge == detect.TURNSTILE
        assert verdict.recoverable is True

    def test_js_challenge(self):
        verdict = detect.analyze(503, CF_HEADERS, JS_HTML)
        assert verdict.blocked is True
        assert verdict.challenge == detect.JS_CHALLENGE

    def test_waf_block_is_not_recoverable(self):
        verdict = detect.analyze(403, CF_HEADERS, WAF_HTML)
        assert verdict.blocked is True
        assert verdict.challenge == detect.WAF_BLOCK
        assert verdict.recoverable is False

    def test_rate_limit_429_without_challenge_is_waf(self):
        verdict = detect.analyze(429, CF_HEADERS, "")
        assert verdict.blocked is True
        assert verdict.challenge == detect.WAF_BLOCK

    def test_challenge_served_with_200(self):
        """CF 有时用 200 直接回质询页。"""
        verdict = detect.analyze(200, CF_HEADERS, CHALLENGE_HTML)
        assert verdict.blocked is True
        assert verdict.challenge == detect.MANAGED_CHALLENGE

    def test_empty_403_from_cloudflare(self):
        verdict = detect.analyze(403, CF_HEADERS, "")
        assert verdict.blocked is True

    def test_mitigated_none_is_not_blocked(self):
        verdict = detect.analyze(200, {**CF_HEADERS, "cf-mitigated": "none"},
                                 '{"success":true}')
        assert verdict.blocked is False


class TestResponseObject:
    class FakeResp:
        def __init__(self, status, headers, text):
            self.status_code = status
            self.headers = headers
            self.text = text

    def test_analyze_response(self):
        resp = self.FakeResp(503, CF_HEADERS, CHALLENGE_HTML)
        assert detect.analyze_response(resp).challenge == detect.MANAGED_CHALLENGE

    def test_analyze_response_with_broken_text(self):
        class Broken:
            status_code = 403
            headers = CF_HEADERS

            @property
            def text(self):
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")

            content = b"just a moment"

        assert detect.analyze_response(Broken()).blocked is True

    def test_bytes_body(self):
        assert detect.analyze(403, CF_HEADERS, CHALLENGE_HTML.encode()).blocked is True


class TestPageChallengeType:
    def test_normal_page(self):
        assert detect.page_challenge_type("<html><body>额度充值</body></html>", "控制台") is None
        assert detect.looks_like_challenge_page("<html>ok</html>", "首页") is False

    def test_title_drives_detection(self):
        assert detect.page_challenge_type(CHALLENGE_HTML, "Just a moment...") == \
            detect.MANAGED_CHALLENGE

    def test_turnstile_page(self):
        assert detect.page_challenge_type(TURNSTILE_HTML, "Just a moment...") == detect.TURNSTILE

    def test_waf_page(self):
        assert detect.page_challenge_type(WAF_HTML, "Attention Required! | Cloudflare") == \
            detect.WAF_BLOCK

    def test_body_only_detection(self):
        assert detect.page_challenge_type(JS_HTML, "") == detect.JS_CHALLENGE

    def test_login_path_is_not_treated_as_normal_page(self):
        html = '<form><input type="password"><button>登录</button></form>'
        assert detect.page_challenge_type(html, "GoRouter", "https://example.com/sign-in") == \
            detect.LOGIN_REQUIRED

    def test_login_form_is_detected_without_login_url(self):
        html = '<form><label>用户名或电子邮件</label><input type="password"></form>'
        assert detect.page_challenge_type(html, "GoRouter") == detect.LOGIN_REQUIRED

    def test_landing_page_login_link_is_not_enough(self):
        html = '<a href="/sign-in">登录</a><main>首页内容</main>'
        assert detect.page_challenge_type(html, "GoRouter", "https://example.com/") is None
