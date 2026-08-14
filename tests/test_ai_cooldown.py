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
    """按脚本返回响应或抛异常，并记录每次请求的 timeout 与 proxies。

    生产代码里每个线程各有一个 session，测试为了断言方便共用一个假对象，
    所以这里自己加锁保护脚本队列。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list = []          # 每次请求的 timeout
        self.proxies: list = []        # 每次请求的 proxies
        self._lock = threading.Lock()

    def _next(self, timeout, proxies):
        with self._lock:
            self.calls.append(timeout)
            self.proxies.append(proxies)
            item = self.script.pop(0) if self.script else FakeResp()
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url, json=None, timeout=None, **kwargs):
        return self._next(timeout, kwargs.get("proxies"))

    def get(self, url, **kwargs):
        return self._next(None, kwargs.get("proxies"))

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


class TestAIProxy:
    """AI 视觉请求跟着账号走代理，别让它暴露真实出口 IP。"""

    def test_no_proxy_by_default(self, monkeypatch):
        client, session = _client([FakeResp()], monkeypatch)
        assert client.classify_page(b"png").state == "passed"
        assert session.proxies == [None]

    def test_uses_bound_proxy(self, monkeypatch):
        client, session = _client([FakeResp()], monkeypatch)
        with client.use_proxy("10.0.0.1:8080"):
            client.classify_page(b"png")
        assert session.proxies[0] == {
            "http": "10.0.0.1:8080", "https": "10.0.0.1:8080",
        }

    def test_binding_is_restored_on_exit(self, monkeypatch):
        client, _session = _client([FakeResp()], monkeypatch)
        with client.use_proxy("a:1"):
            with client.use_proxy("b:2"):
                assert client.current_proxy() == "b:2"
            assert client.current_proxy() == "a:1"
        assert client.current_proxy() is None

    def test_falls_back_to_direct_when_proxy_fails(self, monkeypatch):
        """代理挂了不能让 AI 变成单点故障，要自动改走直连。"""
        client, session = _client([RuntimeError("proxy connect failed"), FakeResp()],
                                 monkeypatch)
        with client.use_proxy("dead:9999"):
            assert client.classify_page(b"png").state == "passed"
        assert session.proxies[0] is not None       # 第一次走代理
        assert session.proxies[1] is None          # 第二次直连

    def test_proxied_attempt_leaves_budget_for_fallback(self, monkeypatch):
        """代理不通会一直挂到超时，所以只能占用一半预算。"""
        client, session = _client([RuntimeError("timeout"), FakeResp()], monkeypatch,
                                 timeout=20)
        with client.use_proxy("slow:1"):
            client.classify_page(b"png")
        assert session.calls[0] <= 10.0 + 0.01
        assert session.calls[1] > 10.0

    def test_proxy_failure_does_not_trigger_ai_cooldown(self, monkeypatch):
        """经代理超时说明代理有问题，不能据此判定 AI 端点过载。"""
        client, _session = _client([TimeoutLike("timed out"), FakeResp()], monkeypatch,
                                   cooldown=5.0)
        started = time.monotonic()
        with client.use_proxy("dead:1"):
            assert client.classify_page(b"png").state == "passed"
        assert time.monotonic() - started < 1.0
        assert client._cooldown_left() == 0.0

    def test_binding_is_thread_local(self, monkeypatch):
        client, session = _client([FakeResp(), FakeResp()], monkeypatch)
        barrier = threading.Barrier(2)

        def worker(proxy):
            with client.use_proxy(proxy):
                barrier.wait()
                client.classify_page(b"png")

        threads = [threading.Thread(target=worker, args=(p,))
                   for p in ("p1:80", "p2:80")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(item["http"] for item in session.proxies) == ["p1:80", "p2:80"]

    def test_ping_honours_bound_proxy(self, monkeypatch):
        client, session = _client([FakeResp()], monkeypatch)
        with client.use_proxy("p:80"):
            ok, _message = client.ping()
        assert ok is True
        assert session.proxies[0] == {"http": "p:80", "https": "p:80"}


class TestPerThreadSession:
    """一线程一 session：并行签到时几个账号的视觉调用不该互相排队。"""

    def test_each_thread_gets_its_own_session(self, monkeypatch):
        monkeypatch.setattr(vision_mod, "TIMEOUT_COOLDOWN", 0.01)
        cfg = AIConfig(enabled=True, base_url="https://relay.example.com/v1",
                       api_key="sk-test", model="gpt-4o-mini", timeout=5, max_retries=0)
        client = VisionClient(cfg)
        created = []
        monkeypatch.setattr(client, "_new_session",
                            lambda: created.append(FakeSession([FakeResp()])) or created[-1])
        seen = []

        def worker():
            seen.append(id(client._session))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(seen)) == 4          # 四个线程四个不同 session
        assert len(created) == 4

    def test_same_thread_reuses_its_session(self, monkeypatch):
        cfg = AIConfig(enabled=True, base_url="https://relay.example.com/v1",
                       api_key="sk-test", model="gpt-4o-mini", timeout=5, max_retries=0)
        client = VisionClient(cfg)
        monkeypatch.setattr(client, "_new_session", lambda: FakeSession([FakeResp()]))
        assert client._session is client._session

    def test_close_closes_every_thread_session(self, monkeypatch):
        cfg = AIConfig(enabled=True, base_url="https://relay.example.com/v1",
                       api_key="sk-test", model="gpt-4o-mini", timeout=5, max_retries=0)
        client = VisionClient(cfg)
        closed = []

        class Closable(FakeSession):
            def close(self):
                closed.append(self)

        monkeypatch.setattr(client, "_new_session", lambda: Closable([FakeResp()]))
        threads = [threading.Thread(target=lambda: client._session) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        client.close()
        assert len(closed) == 3

    def test_concurrent_calls_are_not_serialised(self, monkeypatch):
        """三个线程各发一次请求，总耗时应接近单次，而不是三次相加。"""
        monkeypatch.setattr(vision_mod, "TIMEOUT_COOLDOWN", 0.01)
        cfg = AIConfig(enabled=True, base_url="https://relay.example.com/v1",
                       api_key="sk-test", model="gpt-4o-mini", timeout=10, max_retries=0)
        client = VisionClient(cfg)

        class SlowSession(FakeSession):
            def post(self, url, json=None, timeout=None, **kwargs):
                time.sleep(0.3)
                return FakeResp()

        monkeypatch.setattr(client, "_new_session", lambda: SlowSession([]))
        started = time.monotonic()
        threads = [threading.Thread(target=lambda: client.classify_page(b"png"))
                   for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - started
        assert elapsed < 0.8               # 串行的话至少 0.9s
