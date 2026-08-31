"""
tests/test_github_login.py
GitHub 登录的判定逻辑测试（全离线，不起浏览器）。

守的都是会导致「静默错」的点：
- 成功的唯一信号是 user_session 下发；按 URL 判会把设备验证码页误判成成功
- 登出占位值 user_session=deleted 不能当成功，否则写一条废凭据进池子
- 停用/封禁的判定必须与 Go 侧 github_status.go 同口径，否则两边结论会打架
"""

import pytest

from newapi_checkin.github_login import (
    classify_account_page,
    classify_login_stage,
    extract_user_session,
    human_delays,
    read_stage,
)


class FakeLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class FakePage:
    """只实现判定需要的那几个方法。"""

    def __init__(self, url="", counts=None):
        self.url = url
        self._counts = counts or {}

    def locator(self, selector):
        return FakeLocator(self._counts.get(selector, 0))


class TestExtractUserSession:
    def test_takes_value(self):
        got = extract_user_session([
            "logged_in=yes; Path=/",
            "user_session=abcdef1234567890; Path=/; HttpOnly",
        ])
        assert got == "abcdef1234567890"

    def test_rejects_logout_placeholder(self):
        """登出会下发 user_session=deleted，当成功就是写一条废凭据进池子。"""
        assert extract_user_session(["user_session=deleted; Path=/"]) == ""

    def test_rejects_too_short(self):
        assert extract_user_session(["user_session=x; Path=/"]) == ""

    def test_no_cookie(self):
        assert extract_user_session([]) == ""
        assert extract_user_session(["other=1"]) == ""
        assert extract_user_session(None) == ""


class TestClassifyLoginStage:
    def _stage(self, **kw):
        base = dict(url="https://github.com/", session="", has_flash_error=False,
                    has_captcha=False, has_otp_field=False)
        base.update(kw)
        return classify_login_stage(**base)

    def test_session_wins(self):
        """session 一到手就是成功，其它信号都不重要。"""
        stage = self._stage(session="abcdef1234567890", url="https://github.com/login",
                            has_flash_error=True, has_captcha=True)
        assert stage.kind == "success"
        assert stage.session == "abcdef1234567890"

    def test_device_code_before_credential_error(self):
        """verified-device 页也是 github.com 域且不含 /login，必须先判它。"""
        stage = self._stage(url="https://github.com/sessions/verified-device")
        assert stage.kind == "device_code"

    def test_otp_field_alone_is_enough(self):
        """URL 可能还没变，但 #otp 已经渲染出来了。"""
        stage = self._stage(url="https://github.com/", has_otp_field=True)
        assert stage.kind == "device_code"

    def test_credential_error_needs_both_signals(self):
        """只回 /login 不算错（可能还在跳转中），必须同时有错误横幅。"""
        assert self._stage(url="https://github.com/login").kind == "pending"
        stage = self._stage(url="https://github.com/login", has_flash_error=True)
        assert stage.kind == "credential_error"

    def test_captcha(self):
        assert self._stage(has_captcha=True).kind == "captcha"

    def test_pending_by_default(self):
        assert self._stage().kind == "pending"


class TestClassifyAccountPage:
    @pytest.mark.parametrize("body,want", [
        ("Your account has been suspended", "suspended"),
        ("this account has been suspended.", "suspended"),
        ("账号已被暂停", "suspended"),
        ("Your account has been terminated", "banned"),
        ("account was disabled", "banned"),
        ("permanently suspended", "banned"),
        ("<html>normal profile page</html>", "active"),
        ("", "active"),
    ])
    def test_markers(self, body, want):
        assert classify_account_page(body) == want

    def test_banned_wins_over_suspended(self):
        """两种特征同时出现时按更重的判：banned 基本没有恢复可能。"""
        assert classify_account_page(
            "account has been suspended and permanently suspended") == "banned"


class TestReadStage:
    def test_reads_from_page(self):
        page = FakePage(url="https://github.com/login", counts={".flash-error": 1})
        assert read_stage(page, {}).kind == "credential_error"

    def test_session_holder_wins(self):
        page = FakePage(url="https://github.com/login", counts={".flash-error": 1})
        stage = read_stage(page, {"value": "abcdef1234567890"})
        assert stage.kind == "success"

    def test_broken_page_is_pending(self):
        """读现场失败不能判死，交给外层超时收口。"""

        class Broken:
            @property
            def url(self):
                raise RuntimeError("boom")

        assert read_stage(Broken(), {}).kind == "pending"

    def test_locator_failure_tolerated(self):
        class BadLocator:
            def locator(self, selector):
                raise RuntimeError("detached")

            url = "https://github.com/"

        assert read_stage(BadLocator(), {}).kind == "pending"


def test_human_delays():
    """逐字符停顿必须与文本等长：GitHub 表单带 timestamp 检测，瞬填是机器特征。"""
    delays = human_delays("abcde")
    assert len(delays) == 5
    assert all(60 <= d <= 180 for d in delays)
    assert human_delays("") == []
