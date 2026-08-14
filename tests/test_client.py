"""签到结果分类、cookie 处理、指纹族匹配。"""

from newapi_checkin import client


class TestClassifySelf:
    def test_success(self):
        data = {"success": True, "data": {"id": 42, "username": "kiro", "quota": 500}}
        result = client.classify_self(200, data)
        assert result.kind == client.SUCCESS
        assert result.user_id == 42
        assert result.message == "kiro"

    def test_401_is_auth_failed(self):
        result = client.classify_self(401, {"success": False, "message": "无权进行此操作"})
        assert result.kind == client.AUTH_FAILED

    def test_success_false_with_auth_marker(self):
        result = client.classify_self(200, {"success": False, "message": "unauthorized"})
        assert result.kind == client.AUTH_FAILED

    def test_success_false_other(self):
        result = client.classify_self(200, {"success": False, "message": "服务器繁忙"})
        assert result.kind == client.FAILED

    def test_missing_id(self):
        result = client.classify_self(200, {"success": True, "data": {}})
        assert result.kind == client.FAILED
        assert "data.id" in result.message

    def test_non_json(self):
        result = client.classify_self(200, None, "<html>nope</html>")
        assert result.kind == client.UNKNOWN
        assert "非 JSON" in result.message


class TestClassifyCheckin:
    def test_success_with_quota(self):
        data = {"success": True, "message": "签到成功", "data": {"quota_awarded": 1000}}
        result = client.classify_checkin(200, data, path="/api/user/checkin")
        assert result.kind == client.SUCCESS
        assert result.quota == 1000
        assert result.ok is True

    def test_quota_from_flat_field(self):
        result = client.classify_checkin(200, {"success": True, "quota": 66})
        assert result.quota == 66

    def test_already_done_chinese(self):
        result = client.classify_checkin(200, {"success": False, "message": "今日已签到"})
        assert result.kind == client.ALREADY_DONE
        assert result.ok is True

    def test_already_done_english(self):
        result = client.classify_checkin(400, {"success": False,
                                               "message": "You have already checked in"})
        assert result.kind == client.ALREADY_DONE

    def test_success_true_but_already_message(self):
        """success=true 优先，仍然算成功。"""
        result = client.classify_checkin(200, {"success": True, "message": "已签到过了"})
        assert result.kind == client.SUCCESS

    def test_auth_failed(self):
        result = client.classify_checkin(401, {"success": False, "message": "请先登录"})
        assert result.kind == client.AUTH_FAILED

    def test_turnstile_token_empty_is_not_auth_failure(self):
        result = client.classify_checkin(
            200, {"success": False, "message": "Turnstile token 为空"}
        )
        assert result.kind == client.TURNSTILE_REQUIRED
        assert result.kind != client.AUTH_FAILED

    def test_generic_token_word_is_not_auth_failure(self):
        result = client.classify_checkin(
            200, {"success": False, "message": "请求参数 token 缺失"}
        )
        assert result.kind == client.FAILED

    def test_plain_failure(self):
        result = client.classify_checkin(200, {"success": False, "message": "签到功能未开启"})
        assert result.kind == client.FAILED
        assert result.ok is False

    def test_empty_message_falls_back_to_status(self):
        result = client.classify_checkin(500, {"success": False})
        assert result.kind == client.FAILED
        assert "500" in result.message


class TestCookies:
    def test_parse(self):
        parsed = client.parse_cookie_header("a=1; session=abc; empty=; broken")
        assert parsed == {"a": "1", "session": "abc", "empty": ""}

    def test_parse_value_with_equals(self):
        assert client.parse_cookie_header("token=aa=bb==")["token"] == "aa=bb=="

    def test_merge_prefers_harvested(self):
        merged = client.merge_cookies("session=old; keep=1",
                                      {"session": "new", "cf_clearance": "zzz"})
        assert merged == {"session": "new", "keep": "1", "cf_clearance": "zzz"}

    def test_merge_ignores_empty_values(self):
        merged = client.merge_cookies("session=old", {"session": ""})
        assert merged["session"] == "old"

    def test_build_header_roundtrip(self):
        header = client.build_cookie_header({"a": "1", "b": "2"})
        assert client.parse_cookie_header(header) == {"a": "1", "b": "2"}


class TestPickImpersonate:
    def test_firefox_ua_switches_family(self):
        """Camoufox 是 Firefox 系，配 Chrome 的 JA3 会让 cf_clearance 立刻失效。"""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
        assert client.pick_impersonate("chrome", ua) == "firefox"

    def test_chrome_ua_keeps_chrome(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"
        assert client.pick_impersonate("chrome131", ua) == "chrome131"

    def test_chrome_ua_with_firefox_config(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"
        assert client.pick_impersonate("firefox133", ua) == "chrome"

    def test_no_ua_keeps_config(self):
        assert client.pick_impersonate("chrome131", "") == "chrome131"

    def test_empty_config_defaults_to_chrome(self):
        assert client.pick_impersonate("", "") == "chrome"


class TestSanitizeHeaderValue:
    """非 latin-1 字符会导致 curl_cffi headers.update 抛 UnicodeEncodeError
    （koqj 事故根因）。清洗后必须能安全进入 header。"""

    def test_ascii_passthrough(self):
        assert client.sanitize_header_value("session=abc; x=1") == "session=abc; x=1"

    def test_emoji_removed(self):
        value = "session=abc🎉def"
        cleaned = client.sanitize_header_value(value)
        assert "🎉" not in cleaned
        assert cleaned == "session=abcdef"

    def test_chinese_removed(self):
        value = "token=你好world"
        assert client.sanitize_header_value(value) == "token=world"

    def test_control_chars_removed(self):
        value = "a=1\r\nSet-Cookie: evil=1\r\nb=2"
        cleaned = client.sanitize_header_value(value)
        assert "\r" not in cleaned and "\n" not in cleaned
        # 清洗只剔除控制字符，不删可打印文本——没有 \r\n 就无法构成新头注入
        assert all(ord(c) >= 0x20 for c in cleaned)

    def test_latin1_high_bytes_kept(self):
        # 0xA0-0xFF 属于 latin-1 可打印区，应保留
        value = "token=café-ñ"
        assert client.sanitize_header_value(value) == "token=café-ñ"

    def test_none_to_empty(self):
        assert client.sanitize_header_value(None) == ""

    def test_build_cookie_header_sanitizes(self):
        header = client.build_cookie_header({"session": "abc🎉", "keep": "ok"})
        assert "🎉" not in header
        assert client.parse_cookie_header(header) == {"session": "abc", "keep": "ok"}

    def test_build_cookie_header_skips_emptied_name(self):
        header = client.build_cookie_header({"🎉": "x", "keep": "ok"})
        assert client.parse_cookie_header(header) == {"keep": "ok"}
