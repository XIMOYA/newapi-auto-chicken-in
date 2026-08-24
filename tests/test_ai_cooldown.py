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

    def test_cooldown_wait_capped_by_remaining_budget(self, monkeypatch):
        """避让等待不能超过任务剩余预算，否则会把整个任务拖过墙钟上限。"""
        monkeypatch.setattr(vision_mod, "TIMEOUT_COOLDOWN", 5.0)
        monkeypatch.setattr(vision_mod, "MIN_TASK_DEADLINE", 1.0)
        monkeypatch.setattr(vision_mod, "TASK_DEADLINE_FACTOR", 0.1)
        client, session = _client([TimeoutLike("t"), TimeoutLike("t")], monkeypatch,
                                  cooldown=5.0, timeout=2, max_retries=2)
        started = time.monotonic()
        assert client.classify_page(b"png").state == "unknown"
        elapsed = time.monotonic() - started
        # 任务预算只有 1s：即使避让期长达 5s，也不能真的等满 5s
        assert elapsed < 2.0
        assert 1 <= len(session.calls) <= 2

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

    def test_per_request_timeout_is_cfg_timeout(self, monkeypatch):
        """cfg.timeout 是单次请求上限；总时长另有墙钟兜底，不再切一半。"""
        client, session = _client([RuntimeError("boom"), FakeResp()], monkeypatch,
                                 timeout=20)
        with client.use_proxy("slow:1"):
            client.classify_page(b"png")
        assert session.calls[0] == 20.0


class TestAIRequiresProxy:
    """AI 强制走代理：代理不通就换下一个 IP，不限次数，绝不退回直连。"""

    @staticmethod
    def _pooled(monkeypatch, script, proxies, *, require=True, timeout=5,
                dropped=None):
        client, session = _client(script, monkeypatch, timeout=timeout)
        supply = list(proxies)
        handed: list = []

        def provider():
            if not supply:
                return None
            proxy = supply.pop(0)
            handed.append(proxy)
            return proxy

        client.set_proxy_source(
            provider, require=require,
            on_failed=(dropped.append if dropped is not None else None),
        )
        return client, session, handed

    def test_swaps_ip_instead_of_going_direct(self, monkeypatch):
        client, session, handed = self._pooled(
            monkeypatch,
            [RuntimeError("proxy down"), RuntimeError("proxy down"), FakeResp()],
            ["p2:80", "p3:80"],
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"
        # 三次请求分别走 p1 / p2 / p3，没有任何一次是直连
        assert [c["http"] for c in session.proxies] == ["p1:80", "p2:80", "p3:80"]
        assert handed == ["p2:80", "p3:80"]

    def test_swaps_are_not_counted_as_retries(self, monkeypatch):
        """max_retries=0 也要能换 IP——换 IP 不占重试次数。"""
        client, session, _handed = self._pooled(
            monkeypatch,
            [RuntimeError("x"), RuntimeError("x"), RuntimeError("x"), FakeResp()],
            ["p2:80", "p3:80", "p4:80"],
            timeout=5,
        )
        client.cfg.max_retries = 0
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"
        assert len(session.calls) == 4

    def test_never_falls_back_to_direct_when_pool_is_dry(self, monkeypatch):
        """换不到新 IP 时沿用当前代理计次重试，仍然不直连。"""
        client, session, _handed = self._pooled(
            monkeypatch, [RuntimeError("x")] * 5, [], timeout=2,
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "unknown"
        assert all(item is not None for item in session.proxies)
        assert {c["http"] for c in session.proxies} == {"p1:80"}

    def test_skips_call_when_no_proxy_available_at_all(self, monkeypatch):
        client, session, _handed = self._pooled(monkeypatch, [FakeResp()], [])
        assert client.classify_page(b"png").state == "unknown"
        assert session.calls == []          # 一次请求都没发

    def test_optional_proxy_still_falls_back_to_direct(self, monkeypatch):
        """require=False（没启用代理池）时保持原来的降级直连行为。"""
        client, session, _handed = self._pooled(
            monkeypatch, [RuntimeError("x"), FakeResp()], [], require=False,
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"
        assert session.proxies[0] is not None
        assert session.proxies[1] is None

    def test_swapped_out_ip_is_reported_as_bad(self, monkeypatch):
        """换掉一个 IP 就要上报它坏了。

        不上报的话它既不在空闲候选里、又不在黑名单里，池子用尽时还会被
        当成共用候选分给别的账号，等于明知连不通还往外发。
        """
        dropped: list = []
        client, _session, _handed = self._pooled(
            monkeypatch,
            [RuntimeError("proxy down"), RuntimeError("proxy down"), FakeResp()],
            ["p2:80", "p3:80"],
            dropped=dropped,
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"
        # 被换掉的 p1/p2 都上报，最后跑通的 p3 不上报
        assert dropped == ["p1:80", "p2:80"]

    def test_thread_local_proxy_updated_after_swap(self, monkeypatch):
        """换代理成功后线程本地代理跟着更新，避免下次请求拿到旧代理。"""
        client, _session, _handed = self._pooled(
            monkeypatch,
            [RuntimeError("proxy down"), FakeResp()],
            ["p2:80"],
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"
            assert client.current_proxy() == "p2:80"
        assert client.current_proxy() is None

    def test_thread_local_proxy_cleared_on_direct_fallback(self, monkeypatch):
        """降级直连时线程本地代理要清掉，current_proxy() 保持一致。"""
        client, _session, _handed = self._pooled(
            monkeypatch, [RuntimeError("x"), FakeResp()], [], require=False,
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"
            assert client.current_proxy() is None
        assert client.current_proxy() is None

    def test_reused_ip_is_not_reported_when_pool_is_dry(self, monkeypatch):
        """池子空了只能沿用当前 IP 重试时不上报——下一轮还得靠它。

        上报会让 Runner 把它拉黑，那就成了「拿一个已拉黑的代理继续用」。
        """
        dropped: list = []
        client, _session, _handed = self._pooled(
            monkeypatch, [RuntimeError("x")] * 5, [], timeout=2, dropped=dropped,
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "unknown"
        assert dropped == []

    def test_direct_fallback_reports_the_dropped_proxy(self, monkeypatch):
        """require=False 降级直连前，被丢掉的代理要上报。"""
        dropped: list = []
        client, _session, _handed = self._pooled(
            monkeypatch, [RuntimeError("x"), FakeResp()], [], require=False,
            dropped=dropped,
        )
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"
        assert dropped == ["p1:80"]

    def test_reporter_error_does_not_break_the_call(self, monkeypatch):
        """上报回调抛错不能连带炸掉视觉调用。"""
        def boom(_proxy):
            raise ValueError("reporter exploded")

        client, _session, _handed = self._pooled(
            monkeypatch,
            [RuntimeError("proxy down"), FakeResp()],
            ["p2:80"],
        )
        client.set_proxy_source(client._proxy_provider, require=True,
                                on_failed=boom)
        with client.use_proxy("p1:80"):
            assert client.classify_page(b"png").state == "passed"

    def test_wall_clock_deadline_stops_endless_swapping(self, monkeypatch):
        """池子一直吐死代理时，靠墙钟上限收口，不会无限打转。"""
        monkeypatch.setattr(vision_mod, "MIN_TASK_DEADLINE", 0.5)
        monkeypatch.setattr(vision_mod, "TASK_DEADLINE_FACTOR", 0.1)
        client, session = _client([RuntimeError("x")] * 500, monkeypatch, timeout=1)
        counter = {"n": 0}

        def provider():
            counter["n"] += 1
            return f"p{counter['n']}:80"

        client.set_proxy_source(provider, require=True)
        started = time.monotonic()
        with client.use_proxy("p0:80"):
            assert client.classify_page(b"png").state == "unknown"
        assert time.monotonic() - started < 3.0
        assert len(session.calls) < 500

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

    @staticmethod
    def _client(monkeypatch, session_factory=None):
        cfg = AIConfig(enabled=True, base_url="https://relay.example.com/v1",
                       api_key="sk-test", model="gpt-4o-mini", timeout=5, max_retries=0)
        client = VisionClient(cfg)
        if session_factory is not None:
            monkeypatch.setattr(client, "_new_session", session_factory)
        return client

    def test_isolation_does_not_key_off_thread_ids(self, monkeypatch):
        """隔离绝不能拿 threading.get_ident() 做 key —— 线程 id 会被系统复用。

        这是平台无关的回归闸门。原实现用 ident 做字典 key，Windows 上 id 复用得慢，
        本地怎么跑都是绿的，只有 Linux CI 会红（那里线程一退出 id 立刻被下一个拿去用），
        复用一发生就是两个后果：新线程摸到上一个线程留下的 session、被顶掉的那个再也
        没人关。这里把 get_ident 钉死成常量，谁改回 ident 方案都会立刻失败。
        """
        created = []
        client = self._client(
            monkeypatch,
            lambda: created.append(FakeSession([FakeResp()])) or created[-1],
        )
        # threading.local 是 C 层按线程对象隔离的，不看 ident，所以这么钉不影响它
        monkeypatch.setattr(vision_mod.threading, "get_ident", lambda: 4242)
        seen = []

        def worker():
            seen.append(id(client._session))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(seen)) == 3           # ident 全一样，也得一线程一个
        assert len(created) == 3

    def test_close_releases_sessions_left_by_dead_threads(self, monkeypatch):
        """线程早就退出了，它创建的 session 仍然必须被关掉。

        threading.local 在线程结束时会丢掉自己那份引用，只靠它收尾就会漏。所以另外
        用一个只增列表登记——这条断言守的正是那个列表。
        """
        closed = []

        class Closable(FakeSession):
            def close(self):
                closed.append(self)

        client = self._client(monkeypatch, lambda: Closable([FakeResp()]))
        threads = [threading.Thread(target=lambda: client._session) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 线程全退了，主线程压根没碰过 _session
        client.close()
        assert len(closed) == 3

    def test_close_lets_the_current_thread_start_over(self, monkeypatch):
        """close 之后当前线程再取要拿到新的，不能继续用已关闭的那个。"""
        client = self._client(monkeypatch, lambda: FakeSession([FakeResp()]))
        first = client._session
        client.close()
        assert client._session is not first

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
