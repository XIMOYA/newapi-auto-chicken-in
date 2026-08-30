"""
tests/test_remail.py
ReMail 客户端的纯逻辑测试：不联网，只测判定与提取。

守的都是踩过的坑：
- 命中判定必须本地严格校验邮箱前缀，不能信服务端 search 语义
- 同名多订单必须取 createdAt 最新的，旧订单的取件凭证可能已失效
- 多封验证码邮件必须取最新那封，填到旧码就是「验证码不对」且看不出原因
- verificationCode 是选填字段，正则兜底不能少
"""

from datetime import datetime, timedelta, timezone

import pytest

from newapi_checkin.remail import (
    EmailHit,
    Remail,
    RemailError,
    extract_github_code,
    is_retryable,
    parse_rfc3339,
    pick_usable_order,
)

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def _order(email, status="completed", created="2026-08-31T00:00:00Z", token="st_x", no="o1"):
    return {"deliveryEmail": email, "status": status, "createdAt": created,
            "serviceToken": token, "orderNo": no}


class TestPickUsableOrder:
    def test_prefix_must_match_exactly(self):
        """服务端 search 匹配哪些字段没文档，命中必须本地严格校验前缀。"""
        items = [
            _order("someone-else@mail.com"),
            _order("shiao1974extra@mail.com"),  # 前缀是超集，不算命中
            _order("shiao1974@mail.com", no="hit"),
        ]
        got = pick_usable_order(items, "shiao1974")
        assert got is not None and got["orderNo"] == "hit"

    def test_case_insensitive(self):
        got = pick_usable_order([_order("Shiao1974@mail.com", no="hit")], "shiao1974")
        assert got is not None and got["orderNo"] == "hit"

    def test_skips_unusable_status(self):
        """pending/expired 订单拿不到能用的取件凭证。"""
        for status in ("pending", "expired", "refunded", ""):
            assert pick_usable_order([_order("a@mail.com", status=status)], "a") is None

    def test_newest_wins(self):
        """同名多订单取最新：旧订单的取件凭证可能已失效。"""
        items = [
            _order("a@mail.com", created="2026-01-01T00:00:00Z", no="old"),
            _order("a@mail.com", created="2026-08-30T00:00:00Z", no="new"),
            _order("a@mail.com", created="不是时间", no="broken"),
        ]
        got = pick_usable_order(items, "a")
        assert got is not None and got["orderNo"] == "new"

    def test_empty_inputs(self):
        assert pick_usable_order([], "a") is None
        assert pick_usable_order([_order("a@mail.com")], "") is None
        assert pick_usable_order(None, "a") is None


class TestExtractGithubCode:
    def test_exact_format_wins(self):
        """精确格式优先于服务端解析的字段，也优先于通用正则。"""
        preview = "Hi, your verification code: 228311 . Order 999999 from 2026"
        assert extract_github_code(preview, "111111") == "228311"

    def test_falls_back_to_field(self):
        """verificationCode 是选填字段，有值时它比通用正则可靠。"""
        assert extract_github_code("请查看邮件", "654321") == "654321"

    def test_generic_regex_is_last_resort(self):
        assert extract_github_code("your code is 445566 today") == "445566"

    def test_nothing_found(self):
        assert extract_github_code("", "") == ""
        assert extract_github_code("no digits here", "") == ""
        # 位数不够的不算验证码
        assert extract_github_code("code 123", "") == ""


class TestRetryClassification:
    @pytest.mark.parametrize("msg", [
        "取件网络错误: ConnectionError: boom",
        "搜订单响应非 JSON",
        "取件失败 HTTP 502",
    ])
    def test_retryable(self, msg):
        assert is_retryable(msg) is True

    @pytest.mark.parametrize("msg", [
        "key#1 搜订单认证失败（HTTP 401）",
        "取件失败 HTTP 404",
        "取件失败 HTTP 400",
        "__rate_limited__:5.0",
    ])
    def test_not_retryable(self, msg):
        """限流不占重试次数（由轮询按 Retry-After 处理），4xx 重试无意义。"""
        assert is_retryable(msg) is False


def test_parse_rfc3339():
    assert parse_rfc3339("2026-08-31T00:00:00Z") == datetime(
        2026, 8, 31, tzinfo=timezone.utc)
    assert parse_rfc3339("") is None
    assert parse_rfc3339(None) is None
    assert parse_rfc3339("不是时间") is None


class TestPickCodeFromPickup:
    """从取件结果里挑验证码：只测挑选逻辑，正文接口用打桩替掉。"""

    def _client(self, body_text=""):
        client = Remail("https://remail.example.com", ["rk-1"])
        client.message_body = lambda email, token, mid: body_text
        return client

    def test_newest_mail_wins(self):
        """多次登录会攒多封验证码邮件，取旧的那封就是「验证码不对」。"""
        body = {"items": [
            {"id": "1", "sender": "noreply@github.com", "receivedAt": "2026-08-31T12:01:00Z",
             "bodyPreview": "Verification code: 111111"},
            {"id": "2", "sender": "noreply@github.com", "receivedAt": "2026-08-31T12:05:00Z",
             "bodyPreview": "Verification code: 222222"},
        ]}
        code, source = self._client().pick_code_from_pickup(body, NOW)
        assert code == "222222"
        assert "mail#2" in source

    def test_ignores_mail_before_since(self):
        """登录动作之前的邮件一律不算 —— 那是上一轮的码。"""
        body = {"items": [
            {"id": "old", "sender": "noreply@github.com",
             "receivedAt": (NOW - timedelta(minutes=5)).isoformat(),
             "bodyPreview": "Verification code: 999999"},
        ]}
        code, _ = self._client().pick_code_from_pickup(body, NOW)
        assert code == ""

    def test_ignores_non_github_sender(self):
        body = {"items": [
            {"id": "x", "sender": "billing@example.com",
             "receivedAt": "2026-08-31T12:05:00Z",
             "bodyPreview": "Verification code: 123456"},
        ]}
        code, _ = self._client().pick_code_from_pickup(body, NOW)
        assert code == ""

    def test_full_body_preferred(self):
        """正文精确格式比 preview 可靠：preview 可能被截断在验证码中间。"""
        body = {"items": [
            {"id": "1", "sender": "noreply@github.com", "receivedAt": "2026-08-31T12:05:00Z",
             "bodyPreview": "your code is 777777"},
        ]}
        client = self._client(body_text="Verification code: 888888")
        code, source = client.pick_code_from_pickup(body, NOW, "a@mail.com", "st_x")
        assert code == "888888"
        assert source.endswith(".body")

    def test_empty_items(self):
        code, source = self._client().pick_code_from_pickup({}, NOW)
        assert (code, source) == ("", "")


class TestPollForCode:
    def test_raises_after_max_tries(self, monkeypatch):
        """取满次数没等到码必须抛错，不能静默返回空串让上层填个空验证码。"""
        client = Remail("https://remail.example.com", ["rk-1"])
        calls = {"n": 0}

        def fake_pickup(email, token):
            calls["n"] += 1
            return {"items": []}

        client._pickup_once = fake_pickup
        monkeypatch.setattr("newapi_checkin.remail.time.sleep", lambda s: None)
        with pytest.raises(RemailError) as exc:
            client.poll_for_code(EmailHit(1, "a@mail.com", "st_x"), NOW,
                                 max_tries=3, fallback_poll_sec=1)
        assert "3 次" in str(exc.value)
        assert calls["n"] == 3

    def test_returns_on_first_hit(self, monkeypatch):
        client = Remail("https://remail.example.com", ["rk-1"])
        client._pickup_once = lambda email, token: {"items": [
            {"id": "1", "sender": "noreply@github.com",
             "receivedAt": "2026-08-31T12:05:00Z",
             "bodyPreview": "Verification code: 424242"},
        ]}
        client.message_body = lambda *a: ""
        monkeypatch.setattr("newapi_checkin.remail.time.sleep", lambda s: None)
        code, _ = client.poll_for_code(EmailHit(1, "a@mail.com", "st_x"), NOW, max_tries=5)
        assert code == "424242"
