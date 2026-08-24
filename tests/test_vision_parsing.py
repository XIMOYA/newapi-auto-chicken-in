"""AI 输出的 JSON 容错解析、坐标归一化、代理解析、拒答识别。"""

from newapi_checkin import utils
from newapi_checkin.ai.vision import VisionClient, _looks_refused


class TestLooseJson:
    def test_plain(self):
        assert utils.loose_json('{"state":"passed"}') == {"state": "passed"}

    def test_markdown_fence(self):
        raw = '```json\n{"state":"turnstile_checkbox","confidence":0.9}\n```'
        assert utils.loose_json(raw)["state"] == "turnstile_checkbox"

    def test_fence_without_language(self):
        assert utils.loose_json('```\n{"text":"AB3D"}\n```') == {"text": "AB3D"}

    def test_prose_around_json(self):
        raw = '好的，我判断如下：\n{"state":"passed","confidence":0.8}\n希望有帮助。'
        assert utils.loose_json(raw)["confidence"] == 0.8

    def test_nested_object(self):
        raw = 'text {"a": {"b": [1,2,3]}, "c": 1} tail'
        assert utils.loose_json(raw)["a"]["b"] == [1, 2, 3]

    def test_brace_inside_string_literal(self):
        raw = 'noise {"x": "} not the end", "y": 2} tail'
        assert utils.loose_json(raw) == {"x": "} not the end", "y": 2}

    def test_escaped_quote_in_string(self):
        raw = r'{"reason": "他说\"过了\"", "ok": true}'
        assert utils.loose_json(raw)["ok"] is True

    def test_returns_none_for_garbage(self):
        assert utils.loose_json("完全没有 JSON") is None
        assert utils.loose_json("") is None
        assert utils.loose_json(None) is None

    def test_unbalanced_returns_none(self):
        assert utils.loose_json('{"a": 1') is None

    def test_array_is_rejected(self):
        assert utils.loose_json("[1,2,3]") is None

    def test_dict_passthrough(self):
        payload = {"already": "parsed"}
        assert utils.loose_json(payload) is payload


class TestProxy:
    def test_full_url(self):
        assert utils.parse_proxy("http://user:pw@1.2.3.4:8080") == {
            "server": "http://1.2.3.4:8080", "username": "user", "password": "pw",
        }

    def test_without_scheme(self):
        assert utils.parse_proxy("1.2.3.4:8080")["server"] == "http://1.2.3.4:8080"

    def test_socks5(self):
        assert utils.parse_proxy("socks5://127.0.0.1:1080")["server"] == "socks5://127.0.0.1:1080"

    def test_none(self):
        assert utils.parse_proxy(None) is None
        assert utils.parse_proxy("") is None


class TestClamp:
    def test_bounds(self):
        assert utils.clamp(-1) == 0.0
        assert utils.clamp(2) == 1.0
        assert utils.clamp(0.5) == 0.5


class TestCoordinateNormalisation:
    """模型经常无视归一化要求直接给像素值，两种都要能吃。"""

    def test_normalised_input(self):
        assert VisionClient._point({"x": 0.25, "y": 0.5}, 800, 400) == (0.25, 0.5)

    def test_pixel_input_is_converted(self):
        assert VisionClient._point({"x": 200, "y": 100}, 800, 400) == (0.25, 0.25)

    def test_out_of_range_is_clamped(self):
        assert VisionClient._point({"x": 9999, "y": 9999}, 800, 400) == (1.0, 1.0)

    def test_missing_field(self):
        assert VisionClient._point({"x": 0.5}, 800, 400) is None

    def test_non_numeric(self):
        assert VisionClient._point({"x": "左边", "y": 0.5}, 800, 400) is None


class TestContentExtraction:
    class Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    def test_plain_content(self):
        resp = self.Resp({"choices": [{"message": {"content": '{"state":"passed"}'}}]})
        assert VisionClient._content_of(resp) == '{"state":"passed"}'

    def test_content_as_parts(self):
        resp = self.Resp({"choices": [{"message": {"content": [
            {"type": "text", "text": '{"state":'}, {"type": "text", "text": '"passed"}'},
        ]}}]})
        assert VisionClient._content_of(resp) == '{"state":"passed"}'

    def test_missing_choices(self):
        assert VisionClient._content_of(self.Resp({"error": {"message": "no model"}})) == ""

    def test_broken_json(self):
        assert VisionClient._content_of(self.Resp(None)) == ""


class TestRefusalDetection:
    """模型用自然语言拒答 vs 输出被截断 —— 两者处置相反，必须分得开。"""

    def test_real_case_dashboard_not_a_captcha(self):
        # 线上实际收到的回复，就是它把重试烧光了
        raw = ("I can't help with this one. The image isn't a tile-selection captcha "
               "— it's a dashboard screenshot with a Cloudflare \"verify you are human\" "
               "checkbox, and identifying...")
        assert _looks_refused(raw) is True

    def test_chinese_refusal(self):
        assert _looks_refused("抱歉，这张图里没有点选题，只有一个真人验证复选框。") is True

    def test_truncated_json_is_not_refusal(self):
        # thinking 吃光 max_tokens 导致的截断，重试有救，不能当拒答
        assert _looks_refused('{"found": true, "points": [{"x": 0.1, "y": 0.2}') is False

    def test_partial_json_with_prose_is_not_refusal(self):
        assert _looks_refused('我无法完全确定，不过：{"found": false') is False

    def test_normal_prose_without_markers_is_not_refusal(self):
        assert _looks_refused("图中有九个方格，分别是……") is False

    def test_empty(self):
        assert _looks_refused("") is False

    def test_case_insensitive(self):
        assert _looks_refused("I CANNOT HELP WITH THAT REQUEST") is True


class TestLocateGridVerdict:
    """locate_grid 的三态：坐标 / [] 可重试 / None 画面里就没有点选题。"""

    @staticmethod
    def _client(reply):
        client = VisionClient.__new__(VisionClient)
        client._ask = lambda *a, **k: reply  # type: ignore[method-assign]
        return client

    def test_refused_returns_none(self):
        client = self._client({"found": False, "_refused": True})
        assert client.locate_grid(b"png", 800, 600, "图块") is None

    def test_explicit_not_found_returns_none(self):
        # 模型守规矩地回 found=false，语义和拒答一样：它看过了，没有点选题
        client = self._client({"found": False})
        assert client.locate_grid(b"png", 800, 600, "图块") is None

    def test_ask_failure_returns_empty_list(self):
        # 请求层全败（超时、HTTP 错误）与「确认无题」不同，还可以再试
        client = self._client(None)
        assert client.locate_grid(b"png", 800, 600, "图块") == []

    def test_found_without_points_returns_empty_list(self):
        client = self._client({"found": True})
        assert client.locate_grid(b"png", 800, 600, "图块") == []

    def test_points_are_normalised(self):
        client = self._client({"found": True, "points": [
            {"x": 400, "y": 300},        # 像素值，要归一化
            {"x": 0.25, "y": 0.5},       # 已经是比例
            {"x": "bad", "y": 0.1},      # 脏数据，丢掉
        ]})
        points = client.locate_grid(b"png", 800, 600, "图块")
        assert points == [(0.5, 0.5), (0.25, 0.5)]
