"""
tests/test_xray_wiring.py
测试：VLESS 节点接进代理池消费链路的接线
职责：
- 守「节点标识」与「本地入口」两个键不许混用：记账/缓存判定按标识，拨号按本地地址
- 守 dial_target 的三态：普通代理原样、VLESS 有入口转本地、VLESS 无入口给 None
- 守没有本地入口的节点会被拉黑并换下一个，而不是把 None 塞进 account.proxy 变直连
- 守日志与反馈里不出现 vless 的 uuid

这里的断言全是「曾经写错过或极易写错」的点，尤其是端口漂移导致过盾缓存误命中：
本地端口从固定起点分配，不同节点很可能拿到同一个端口，按拨号地址比对会把 A 节点的
cf_clearance 判给 B 节点用，然后静默被盾拦 —— 这种失败在日志上看不出原因。
"""

import threading

from newapi_checkin import config as cfgmod
from newapi_checkin import client as api
from newapi_checkin import logger as log
from newapi_checkin import proxy_pool as pp
from newapi_checkin import runner as runner_mod
from newapi_checkin import xray
from newapi_checkin.cf import session_store as ss

NODE_A = "vless://11111111-1111-1111-1111-111111111111@a.example.com:443?security=reality&type=tcp&pbk=K1&sid=ab&sni=a.example.com#A"
NODE_B = "vless://22222222-2222-2222-2222-222222222222@b.example.com:443?security=reality&type=tcp&pbk=K2&sid=cd&sni=b.example.com#B"


class _FakeXray:
    """只实现 proxy_for。mapping 里没有的节点返回空串，模拟「没起成入站」。"""

    def __init__(self, mapping):
        self.mapping = dict(mapping)
        self.asked: list = []

    def proxy_for(self, node_addr):
        self.asked.append(node_addr)
        return self.mapping.get(node_addr, "")


def _pool(proxies, xray_manager=None):
    cfg = cfgmod.ProxyPoolConfig(enabled=True)
    pool = pp.ProxyPool(cfg, preset=list(proxies))
    pool._available = list(proxies)
    if xray_manager is not None:
        pool.attach_xray(xray_manager)
    return pool


def _runner(tmp_path, monkeypatch, pool):
    cfg = cfgmod.build_config(
        {
            "defaults": {"retry": 1, "interval_seconds": [0, 0]},
            "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
    runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_ai=False))
    # exit_ip 会真去连代理探出口 IP。这里的节点全是编的，不打桩会白等好几秒超时
    monkeypatch.setattr(runner, "exit_ip", lambda proxy=None: None)
    runner._pool = pool
    return runner, cfg.accounts[0]


def _pool_runner(tmp_path, monkeypatch, preset):
    """走真实 init_proxy_pool 的 runner。refresh 打桩成「直接采纳预置清单」，
    这样能守到「初始化代理池 → 起 xray」那一段接线，而不用碰网络。"""
    cfg = cfgmod.build_config(
        {
            "defaults": {"retry": 1, "interval_seconds": [0, 0]},
            "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
            "proxy_pool": {"enabled": True},
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)

    def fake_refresh(self, desired=None):
        self._available = list(self._preset)
        return len(self._available)

    monkeypatch.setattr(pp.ProxyPool, "refresh", fake_refresh)
    options = runner_mod.RunOptions(use_ai=False, proxy_list=list(preset))
    return runner_mod.Runner(cfg, options)


class TestDialTarget:
    """标识 → 可拨号地址的翻译。翻错方向会把整池统计塌到 127.0.0.1 上。"""

    def test_plain_proxies_pass_through_untouched(self):
        pool = _pool(["1.2.3.4:8080", "socks5://5.6.7.8:1080", "http://u:p@9.9.9.9:3128"])
        for addr in ["1.2.3.4:8080", "socks5://5.6.7.8:1080", "http://u:p@9.9.9.9:3128"]:
            assert pool.dial_target(addr) == addr

    def test_plain_proxies_need_no_xray_at_all(self):
        """没挂 xray 也不能影响普通代理 —— 绝大多数用户根本不用 VLESS。"""
        pool = _pool(["1.2.3.4:8080"])
        assert pool._xray is None
        assert pool.dial_target("1.2.3.4:8080") == "1.2.3.4:8080"

    def test_vless_is_translated_to_the_local_inbound(self):
        fake = _FakeXray({NODE_A: "socks5://127.0.0.1:22801"})
        pool = _pool([NODE_A], fake)
        assert pool.dial_target(NODE_A) == "socks5://127.0.0.1:22801"
        assert fake.asked == [NODE_A]

    def test_vless_without_xray_is_none_not_the_raw_uri(self):
        """回落原样返回等于把 vless:// 塞给 curl_cffi，那是必然的连接错误 + 满屏噪音。"""
        pool = _pool([NODE_A])
        assert pool.dial_target(NODE_A) is None

    def test_vless_without_an_inbound_is_none(self):
        pool = _pool([NODE_A, NODE_B], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        assert pool.dial_target(NODE_B) is None

    def test_empty_input_stays_empty(self):
        pool = _pool([NODE_A], _FakeXray({}))
        assert pool.dial_target(None) is None
        assert pool.dial_target("") is None


class TestAssignKeepsTheTwoKeysApart:
    """分配时 account.proxy 必须是本地地址，记账键必须是原始节点。"""

    def test_account_dials_local_but_is_accounted_by_node(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        assert account.proxy == "socks5://127.0.0.1:22801"
        assert account.proxy_identity == NODE_A
        assert account.proxy_key == NODE_A
        assert runner._pooled_proxies["A"] == NODE_A

    def test_failure_feedback_lands_on_the_node_not_on_localhost(self, tmp_path, monkeypatch):
        """这条是整次改动的核心：反馈记到 127.0.0.1 上平台侧优选就废了。"""
        pool = _pool([NODE_A], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        pool.mark_bad(runner._pooled_proxies["A"], "net")
        snapshot = pool.feedback_snapshot()
        keys = [item["addr"] for item in snapshot]
        assert keys == [NODE_A]
        assert not any("127.0.0.1" in k for k in keys)

    def test_success_feedback_also_lands_on_the_node(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        pool.mark_ok(runner._pooled_proxies["A"])
        assert [item["addr"] for item in pool.feedback_snapshot()] == [NODE_A]

    def test_plain_proxy_sets_both_keys_to_the_same_value(self, tmp_path, monkeypatch):
        pool = _pool(["1.2.3.4:8080"])
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        assert account.proxy == "1.2.3.4:8080"
        assert account.proxy_key == "1.2.3.4:8080"

    def test_a_node_without_an_inbound_is_blacklisted_and_skipped(self, tmp_path, monkeypatch):
        """起不来的节点要换掉，不能把 None 写进 account.proxy 变成直连。"""
        pool = _pool([NODE_A, NODE_B], _FakeXray({NODE_B: "socks5://127.0.0.1:22802"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        assert account.proxy == "socks5://127.0.0.1:22802"
        assert account.proxy_identity == NODE_B
        assert NODE_A in pool._bad

    def test_all_nodes_dead_leaves_the_account_without_a_proxy(self, tmp_path, monkeypatch):
        """一个入口都起不来时不能瞎给地址：宁可报错也不要静默直连。"""
        pool = _pool([NODE_A, NODE_B], _FakeXray({}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        assert account.proxy is None
        assert account.proxy_identity is None
        assert "A" not in runner._pooled_proxies

    def test_manual_proxy_is_never_replaced(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        account.proxy = "http://manual:9999"
        runner._assign_proxy(account)
        assert account.proxy == "http://manual:9999"
        assert account.proxy_key == "http://manual:9999"


class TestSwapKeepsTheTwoKeysApart:
    """换 IP 路径独立实现过一次，必须单独守 —— 只改 _assign_proxy 会漏掉这条。"""

    def test_swap_blacklists_the_old_node_by_identity(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A, NODE_B], _FakeXray({
            NODE_A: "socks5://127.0.0.1:22801",
            NODE_B: "socks5://127.0.0.1:22802",
        }))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        runner._swap_proxy(account, "net")
        assert NODE_A in pool._bad
        assert not any("127.0.0.1" in addr for addr in pool._bad)

    def test_swap_moves_both_keys_together(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A, NODE_B], _FakeXray({
            NODE_A: "socks5://127.0.0.1:22801",
            NODE_B: "socks5://127.0.0.1:22802",
        }))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        runner._swap_proxy(account, "net")
        assert account.proxy == "socks5://127.0.0.1:22802"
        assert account.proxy_identity == NODE_B
        assert runner._pooled_proxies["A"] == NODE_B

    def test_swap_returns_the_identity_so_callers_account_correctly(self, tmp_path, monkeypatch):
        """返回值被上层拿去记 _proven_proxies，给本地地址会让复用逻辑认错出口。"""
        pool = _pool([NODE_A, NODE_B], _FakeXray({
            NODE_A: "socks5://127.0.0.1:22801",
            NODE_B: "socks5://127.0.0.1:22802",
        }))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        assert runner._swap_proxy(account, "net") == NODE_B

    def test_swap_skips_nodes_without_an_inbound(self, tmp_path, monkeypatch):
        """中间那个起不来的节点不能把这次换 IP 白白耗掉。"""
        pool = _pool([NODE_A, NODE_B, "1.2.3.4:8080"], _FakeXray({
            NODE_A: "socks5://127.0.0.1:22801",
        }))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        assert runner._swap_proxy(account, "net") == "1.2.3.4:8080"
        assert NODE_B in pool._bad

    def test_swap_with_nothing_left_keeps_the_old_dialable_address(self, tmp_path, monkeypatch):
        """拿不到替代品时保留原样，绝不清空成直连。"""
        pool = _pool([NODE_A], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        assert runner._swap_proxy(account, "net") is None
        assert account.proxy == "socks5://127.0.0.1:22801"


# SPLICE_SWAP
# SPLICE_SWAP
class TestShieldCacheIsKeyedByNode:
    """过盾缓存按节点标识比对。按本地端口比对会误判，而且是两个方向都错。"""

    def test_same_node_on_a_new_local_port_still_hits(self):
        """重跑一轮 xray 端口会变。按本地地址比对会把每次都判成「代理已变更」，
        cf_clearance 全部作废、每个账号重新过盾 —— 白白多花几十秒还多招质询。"""
        sess = ss.CFSession(
            cookies={"cf_clearance": "x"}, user_agent="UA", accept_language="en",
            exit_ip=None, proxy=NODE_A, expires_at=0.0, saved_at=0.0,
        )
        usable, reason = sess.check(None, NODE_A)
        assert usable, reason

    def test_a_different_node_reusing_the_same_port_is_rejected(self):
        """这条最危险：端口从固定起点分配，两个节点很容易先后拿到 22801。
        按本地地址比对时缓存会被判「有效」，然后拿 A 的 cf_clearance 走 B 的出口。"""
        sess = ss.CFSession(
            cookies={"cf_clearance": "x"}, user_agent="UA", accept_language="en",
            exit_ip=None, proxy=NODE_A, expires_at=0.0, saved_at=0.0,
        )
        usable, reason = sess.check(None, NODE_B)
        assert not usable
        assert "代理已变更" in reason

    def test_runner_feeds_the_identity_into_check(self, tmp_path, monkeypatch):
        """接线本身：runner 传给 check 的必须是标识。"""
        pool = _pool([NODE_A], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        seen: list = []

        class _Spy(ss.CFSession):
            def check(self, current_ip, proxy):
                seen.append(proxy)
                return False, "stub"

        record = ss.AccountSession()
        record.cf = _Spy(cookies={"cf_clearance": "x"}, user_agent="UA",
                         accept_language="en", exit_ip=None, proxy=NODE_A,
                         expires_at=0.0, saved_at=0.0)
        # 缓存判完就会往下走 S1 真发 HTTP。节点是编的，不打桩要白等两秒超时
        monkeypatch.setattr(runner, "_api_call",
                            lambda *a, **k: api.ApiResult(kind=api.FAILED, message="stub"))
        runner._attempt(account, record)
        assert seen == [NODE_A]
        assert not any("127.0.0.1" in str(p) for p in seen)

    def test_harvest_stores_the_node_not_the_local_port(self):
        """存与判必须用同一个键。存本地地址、判标识（或反过来）会让缓存永不命中。"""
        from newapi_checkin.cf import solver

        class _Driver:
            def cookie_dict(self):
                return {"cf_clearance": "x"}

            def user_agent(self):
                return "UA"

            def accept_language(self):
                return "en"

            def cookies(self):
                return []

        account = cfgmod.Account(
            name="A", url="https://a.example.com", cookie="c",
            proxy="socks5://127.0.0.1:22801", proxy_identity=NODE_A,
        )
        sess = solver._harvest(_Driver(), account, None)
        assert sess.proxy == NODE_A
        # 存进去的键要能被 check 认出来，这才是闭环
        usable, reason = sess.check(None, account.proxy_key)
        assert usable, reason


class TestXrayLifecycle:
    """起进程 → 挂池子 → 收尾。漏掉停止会留孤儿进程占着端口，下一轮起不来。"""

    def test_pool_without_vless_never_starts_xray(self, tmp_path, monkeypatch):
        """绝大多数用户只用 http/socks5，不该为他们白起一个进程。"""
        pool = _pool(["1.2.3.4:8080", "socks5://5.6.7.8:1080"])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        started: list = []
        monkeypatch.setattr(xray.XrayManager, "start",
                            lambda self: started.append(self))
        runner._start_xray()
        assert started == []
        assert runner._xray is None
        assert pool._xray is None

    def test_vless_in_the_pool_starts_and_attaches(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A, "1.2.3.4:8080", NODE_B])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        seen: list = []

        def fake_start(self):
            seen.append([n.host for n in self.nodes])
            self.bindings = [
                xray.Binding(node_addr=NODE_A, local_proxy="socks5://127.0.0.1:22801",
                             inbound_tag="in-0"),
                xray.Binding(node_addr=NODE_B, local_proxy="socks5://127.0.0.1:22802",
                             inbound_tag="in-1"),
            ]
            self._by_node = {b.node_addr: b.local_proxy for b in self.bindings}

        monkeypatch.setattr(xray.XrayManager, "start", fake_start)
        runner._start_xray()
        assert seen == [["a.example.com", "b.example.com"]]
        assert runner._xray is not None
        assert pool._xray is runner._xray
        # 挂上之后池子才能把节点翻译成本地入口
        assert pool.dial_target(NODE_A) == "socks5://127.0.0.1:22801"

    def test_start_failure_degrades_instead_of_crashing(self, tmp_path, monkeypatch):
        """xray 没装、版本不对、端口全被占 —— 都不能让整轮签到崩掉。"""
        pool = _pool([NODE_A, "1.2.3.4:8080"])
        runner, _ = _runner(tmp_path, monkeypatch, pool)

        def boom(self):
            raise xray.XrayUnavailable("找不到 xray 可执行文件")

        monkeypatch.setattr(xray.XrayManager, "start", boom)
        runner._start_xray()
        assert runner._xray is None
        assert pool._xray is None
        # 普通代理必须完全不受影响
        assert pool.dial_target("1.2.3.4:8080") == "1.2.3.4:8080"
        assert pool.dial_target(NODE_A) is None

    def test_unparseable_nodes_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        pool = _pool(["vless://garbage", NODE_A])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        seen: list = []
        monkeypatch.setattr(xray.XrayManager, "start",
                            lambda self: seen.append([n.host for n in self.nodes]))
        runner._start_xray()
        assert seen == [["a.example.com"]]

    def test_all_nodes_unparseable_starts_nothing(self, tmp_path, monkeypatch):
        pool = _pool(["vless://garbage", "vless://also-bad"])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        started: list = []
        monkeypatch.setattr(xray.XrayManager, "start",
                            lambda self: started.append(self))
        runner._start_xray()
        assert started == []
        assert runner._xray is None

    def test_stop_releases_the_process_and_detaches(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        monkeypatch.setattr(xray.XrayManager, "start", lambda self: None)
        stopped: list = []
        monkeypatch.setattr(xray.XrayManager, "stop", lambda self: stopped.append(self))
        runner._start_xray()
        runner._stop_xray()
        assert len(stopped) == 1
        assert runner._xray is None
        assert pool._xray is None

    def test_stop_is_idempotent(self, tmp_path, monkeypatch):
        """run 的 finally 可能和别处的收尾撞上，停两次不能报错也不能重复 kill。"""
        pool = _pool([NODE_A])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        monkeypatch.setattr(xray.XrayManager, "start", lambda self: None)
        stopped: list = []
        monkeypatch.setattr(xray.XrayManager, "stop", lambda self: stopped.append(self))
        runner._start_xray()
        runner._stop_xray()
        runner._stop_xray()
        assert len(stopped) == 1

    def test_stop_survives_a_failing_process(self, tmp_path, monkeypatch):
        """收尾失败只记一笔 —— 签到结果已经拿到了，不能被 kill 失败抹掉。"""
        pool = _pool([NODE_A])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        monkeypatch.setattr(xray.XrayManager, "start", lambda self: None)

        def boom(self):
            raise OSError("access denied")

        monkeypatch.setattr(xray.XrayManager, "stop", boom)
        runner._start_xray()
        runner._stop_xray()
        assert runner._xray is None

    def test_run_always_stops_xray_even_when_the_round_explodes(self, tmp_path, monkeypatch):
        """挂在 finally 上才算数：中途抛异常也必须回收。"""
        pool = _pool([NODE_A])
        runner, _ = _runner(tmp_path, monkeypatch, pool)
        monkeypatch.setattr(xray.XrayManager, "start", lambda self: None)
        stopped: list = []
        monkeypatch.setattr(xray.XrayManager, "stop", lambda self: stopped.append(self))
        runner._start_xray()
        monkeypatch.setattr(runner, "_run_serial",
                            lambda accounts: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(runner, "_run_parallel",
                            lambda accounts, workers: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(runner, "init_proxy_pool", lambda **kw: None)
        monkeypatch.setattr(runner, "_wait_for_keepalive", lambda accounts: None)
        monkeypatch.setattr(runner, "_start_run_report", lambda accounts: None)
        monkeypatch.setattr(runner, "_stop_run_report", lambda: None)
        monkeypatch.setattr(runner, "_report_proxy_feedback", lambda: None)
        try:
            runner.run()
        except RuntimeError:
            pass
        assert len(stopped) == 1
        assert runner._xray is None

    def test_init_proxy_pool_actually_starts_xray(self, tmp_path, monkeypatch):
        """守「初始化代理池时会起 xray」这条接线本身。

        漏掉这一步的话 xray 永远不启动、所有 VLESS 节点静默不可用，而上面那些
        直接调 _start_xray 的用例照样全绿 —— 这个缺口是变异测试抓出来的。
        """
        runner = _pool_runner(tmp_path, monkeypatch, [NODE_A, "1.2.3.4:8080"])
        started: list = []
        monkeypatch.setattr(xray.XrayManager, "start",
                            lambda self: started.append([n.host for n in self.nodes]))
        runner.init_proxy_pool(desired=2, accounts=1)
        assert started == [["a.example.com"]]
        assert runner._xray is not None
        assert runner._pool._xray is runner._xray
        assert runner._pool.dial_target("1.2.3.4:8080") == "1.2.3.4:8080"

    def test_init_proxy_pool_without_vless_starts_nothing(self, tmp_path, monkeypatch):
        runner = _pool_runner(tmp_path, monkeypatch, ["1.2.3.4:8080"])
        started: list = []
        monkeypatch.setattr(xray.XrayManager, "start", lambda self: started.append(self))
        runner.init_proxy_pool(desired=1, accounts=1)
        assert started == []
        assert runner._xray is None


class TestSweepDoesNotSlanderNodes:
    """体检回传直接决定平台的优选排序，记错一次会长期影响所有 runner。"""

    def _sweep_pool(self, monkeypatch, addrs, xray_ok):
        pool = _pool([])
        monkeypatch.setattr(pp.ProxyPool, "_fetch_remote", lambda self: list(addrs))
        if xray_ok:
            def fake_start(self):
                self.bindings = [
                    xray.Binding(node_addr=n.raw,
                                 local_proxy=f"socks5://127.0.0.1:{22801 + i}",
                                 inbound_tag=f"in-{i}")
                    for i, n in enumerate(self.nodes)
                ]
                self._by_node = {b.node_addr: b.local_proxy for b in self.bindings}
        else:
            def fake_start(self):
                raise xray.XrayUnavailable("找不到 xray 可执行文件")
        monkeypatch.setattr(xray.XrayManager, "start", fake_start)
        monkeypatch.setattr(xray.XrayManager, "stop", lambda self: None)
        return pool

    def test_missing_xray_skips_nodes_instead_of_failing_them(self, monkeypatch):
        """核心：没装 xray 时 VLESS 不能被记成 net_fail 回传，那是拿本地原因诬陷节点。"""
        pool = self._sweep_pool(monkeypatch, [NODE_A, NODE_B, "1.2.3.4:8080"], xray_ok=False)
        monkeypatch.setattr(pp.ProxyPool, "_test_one", lambda self, proxy: 0.1)
        stats = pool.sweep_remote(minutes=1)
        assert stats["skipped"] == 2
        assert stats["tested"] == 1
        keys = [item["addr"] for item in pool.feedback_snapshot()]
        assert keys == ["1.2.3.4:8080"]
        assert NODE_A not in keys and NODE_B not in keys

    def test_with_xray_the_nodes_are_accounted_by_their_uri(self, monkeypatch):
        pool = self._sweep_pool(monkeypatch, [NODE_A, "1.2.3.4:8080"], xray_ok=True)
        dialed: list = []

        def fake_test(self, proxy):
            dialed.append(self.dial_target(proxy))
            return 0.1

        monkeypatch.setattr(pp.ProxyPool, "_test_one", fake_test)
        stats = pool.sweep_remote(minutes=1)
        assert stats["skipped"] == 0
        assert stats["tested"] == 2
        keys = sorted(item["addr"] for item in pool.feedback_snapshot())
        assert keys == sorted([NODE_A, "1.2.3.4:8080"])
        # 真正拨号用的是本地入口
        assert "socks5://127.0.0.1:22801" in dialed

    def test_sweep_stops_xray_when_done(self, monkeypatch):
        pool = self._sweep_pool(monkeypatch, [NODE_A], xray_ok=True)
        stopped: list = []
        monkeypatch.setattr(xray.XrayManager, "stop", lambda self: stopped.append(self))
        monkeypatch.setattr(pp.ProxyPool, "_test_one", lambda self, proxy: 0.1)
        pool.sweep_remote(minutes=1)
        assert len(stopped) == 1
        assert pool._xray is None

    def test_test_one_translates_before_dialing(self):
        """_test_one 拿到的是标识，必须自己翻译；直接把 vless:// 喂给 curl 是必错的。"""
        pool = _pool([NODE_A], _FakeXray({}))
        assert pool._test_one(NODE_A) is None

    def test_preflight_leaves_vless_alone(self, monkeypatch):
        """preflight 跑在 xray 之前，硬测会把整池机场节点当场清零。

        必须混一个测得通的普通代理进来：preflight 有「无一测通就整体放弃自筛」的保护，
        只放 VLESS 的话那条保护会替我们兜住，测试看着是绿的却什么都没守到 —— 这个
        假绿是变异测试抓出来的。
        """
        pool = _pool([NODE_A, NODE_B, "1.2.3.4:8080"])
        real_test = pp.ProxyPool._test_one

        def part_stub(self, proxy):
            # 普通代理直接算通（离线，不发请求）；VLESS 走真实逻辑，
            # 没有本地入口时它会返回 None，也就是「若被纳入就会被拉黑」
            if proxy.lower().startswith("vless://"):
                return real_test(self, proxy)
            return 0.1

        monkeypatch.setattr(pp.ProxyPool, "_test_one", part_stub)
        assert pool.preflight() == 0
        assert NODE_A not in pool._bad
        assert NODE_B not in pool._bad
        assert pool.feedback_snapshot() == []


class TestLogsNeverLeakTheUuid:
    """vless 的 uuid 是接入凭据。日志会进 Actions 产物，泄了等于把节点送人。"""

    def test_proxy_label_masks_the_uuid(self):
        label = xray.proxy_label(NODE_A)
        assert "11111111-1111-1111-1111-111111111111" not in label
        assert "a.example.com:443" in label

    def test_proxy_label_leaves_plain_proxies_readable(self):
        assert xray.proxy_label("1.2.3.4:8080") == "1.2.3.4:8080"
        assert xray.proxy_label("socks5://5.6.7.8:1080") == "socks5://5.6.7.8:1080"

    def test_proxy_label_handles_garbage_without_echoing_it(self):
        assert xray.proxy_label("vless://not-a-valid-uri") == "vless://<无法解析>"

    def test_assign_log_does_not_contain_the_uuid(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A], _FakeXray({NODE_A: "socks5://127.0.0.1:22801"}))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        lines: list = []
        monkeypatch.setattr(log, "info", lambda msg, *a, **k: lines.append(str(msg)))
        runner._assign_proxy(account)
        assert lines
        assert not any("11111111-1111-1111-1111-111111111111" in ln for ln in lines)

    def test_swap_log_does_not_contain_the_uuid(self, tmp_path, monkeypatch):
        pool = _pool([NODE_A, NODE_B], _FakeXray({
            NODE_A: "socks5://127.0.0.1:22801",
            NODE_B: "socks5://127.0.0.1:22802",
        }))
        runner, account = _runner(tmp_path, monkeypatch, pool)
        runner._assign_proxy(account)
        lines: list = []
        monkeypatch.setattr(log, "warn", lambda msg, *a, **k: lines.append(str(msg)))
        runner._swap_proxy(account, "net")
        assert lines
        joined = " ".join(lines)
        assert "11111111-1111-1111-1111-111111111111" not in joined
        assert "22222222-2222-2222-2222-222222222222" not in joined
