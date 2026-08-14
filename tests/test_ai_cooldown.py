"""AI 请求超时后的全局避让。"""

import threading
import time

import pytest

from newapi_checkin.ai import vision as vision_mod
from newapi_checkin.ai.vision import VisionClient, is_timeout
from newapi_checkin.config import AIConfig


class FakeResp:
    status_code = 200

    def __init__(self, content='{"state":"passed","confidence":0.9}'):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class FakeSession:
    """按脚本返回响应或抛异常，并记录每次请求的 timeout。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list = []

    def post(self, url, json=None, timeout=None, **_kwargs):
        self.calls.append(timeout)
        item = self.script.pop(0) if self.script else FakeResp()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


def _client(script, monkeypatch, *, cooldown=0.05, timeout=20, max_retries=2):
    monkeypatch.setattr(vision_mod, "TIMEOUT_COOLDOWN", cooldown)
    cfg = AIConfig(enabled=True, base_url="https://relay.example.com/v1",
                   api_key="sk-test", model="gpt-4o-mini",
                   timeout=timeout, max_retries=max_retries)
    client = VisionClient(cfg)
    session = FakeSession(script)
    client._session = session
    return client, session


class TimeoutLike(Exception):
    """带 curl 错误码的假超时，验证按错误码识别的兜底路径。"""

    code = 28


class TestTimeoutDetection:
    def test_detects_curl_timeout_exception(self):
        pytest.importorskip("curl_cffi")
        from curl_cffi.requests.exceptions import Timeout

        assert is_timeout(Timeout("Operation timed out")) is True

    def test_detects_by_curl_error_code(self):
        assert is_timeout(TimeoutLike("something")) is True

    def test_detects_by_message(self):
        assert is_timeout(RuntimeError("Failed to perform, curl: (28) Timed out")) is True

    def test_other_errors_are_not_timeouts(self):
        assert is_timeout(RuntimeError("Connection refused")) is False
        assert is_timeout(ValueError("bad json")) is False


class TestCooldown:
    def test_timeout_triggers_cooldown_then_retries(self, monkeypatch):
        client, session = _client([TimeoutLike("timed out"), FakeResp()], monkeypatch,
                                  cooldown=0.2)
        started = time.monotonic()
        verdict = client.classify_page(b"png")
        elapsed = time.monotonic() - started
        assert verdict.state == "passed"
        assert len(session.calls) == 2
        assert elapsed >= 0.2          # 中间真的停了一下

    def test_cooldown_is_not_charged_to_request_budget(self, monkeypatch):
        """避让是主动让路，不能把请求预算吃掉，否则重试机会被白白抵消。"""
        client, session = _client([TimeoutLike("timed out"), FakeResp()], monkeypatch,
                                  cooldown=0.3, timeout=10)
        client.classify_page(b"png")
        # 第二次请求拿到的超时额度不应该被 0.3s 避让扣掉太多
        assert session.calls[1] > 9.0

    def test_non_timeout_error_does_not_trigger_cooldown(self, monkeypatch):
        client, session = _client([RuntimeError("connection refused"), FakeResp()],
                                  monkeypatch, cooldown=5.0)
        started = time.monotonic()
        assert client.classify_page(b"png").state == "passed"
        assert time.monotonic() - started < 1.0
        assert client._cooldown_left() == 0.0
        assert len(session.calls) == 2

    def test_cooldown_is_shared_across_threads(self, monkeypatch):
        """一个线程超时，其他线程也要一起让——AI 端点是所有账号共用的。"""
        client, _session = _client([TimeoutLike("timed out"), FakeResp()], monkeypatch,
                                   cooldown=0.4)
        client.classify_page(b"png")          # 触发并等满一次避让

        client._session = FakeSession([FakeResp()])
        client._enter_cooldown()              # 模拟另一个线程刚刚超时
        started = time.monotonic()
        client.classify_page(b"png")
        assert time.monotonic() - started >= 0.4

    def test_all_timeouts_give_up_without_hanging(self, monkeypatch):
        client, session = _client([TimeoutLike("t")] * 5, monkeypatch,
                                  cooldown=0.05, timeout=2, max_retries=2)
        assert client.classify_page(b"png").state == "unknown"
        assert 1 <= len(session.calls) <= 3

    def test_cooldown_waits_are_serialised_not_stacked(self, monkeypatch):
        """两个线程同时撞上同一个避让期，总等待时间不应该叠加。"""
        client, _session = _client([FakeResp(), FakeResp()], monkeypatch, cooldown=0.4)
        client._enter_cooldown()
        started = time.monotonic()

        def worker():
            client.classify_page(b"png")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert time.monotonic() - started < 0.9
