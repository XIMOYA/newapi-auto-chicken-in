"""AI 输出的 JSON 容错解析、坐标归一化、代理解析。"""

from newapi_checkin import utils
from newapi_checkin.ai.vision import VisionClient


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
