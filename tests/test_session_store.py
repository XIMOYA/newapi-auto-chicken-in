"""会话缓存：cf_clearance 与 IP/UA/代理的绑定校验，以及原子落盘。"""

import json

from newapi_checkin.cf import session_store as ss
from newapi_checkin.utils import now


def make_session(**overrides):
    payload = {
        "cookies": {"cf_clearance": "abc", "session": "s"},
        "user_agent": "Mozilla/5.0 Firefox/133.0",
        "accept_language": "zh-CN,zh;q=0.9",
        "exit_ip": "1.2.3.4",
        "proxy": None,
        "expires_at": now() + 1800,
        "saved_at": now(),
    }
    payload.update(overrides)
    return ss.CFSession(**payload)


class TestCFSessionCheck:
    def test_valid(self):
        ok, reason = make_session().check("1.2.3.4", None)
        assert ok is True
        assert reason == "缓存有效"

    def test_ip_changed(self):
        ok, reason = make_session().check("9.9.9.9", None)
        assert ok is False
        assert "出口 IP" in reason

    def test_ip_unknown_skips_comparison(self):
        """探测不到 IP 时不该阻断，只是少一层校验。"""
        assert make_session().check(None, None)[0] is True

    def test_proxy_changed(self):
        ok, reason = make_session().check("1.2.3.4", "http://127.0.0.1:1080")
        assert ok is False
        assert "代理" in reason

    def test_expired(self):
        ok, reason = make_session(expires_at=now() - 10).check("1.2.3.4", None)
        assert ok is False
        assert "过期" in reason

    def test_expiry_margin(self):
        """快到期的也算失效，避免请求发出去正好赶上失效。"""
        ok, _ = make_session(expires_at=now() + 5).check("1.2.3.4", None)
        assert ok is False

    def test_missing_user_agent(self):
        ok, reason = make_session(user_agent="").check("1.2.3.4", None)
        assert ok is False
        assert "UA" in reason

    def test_no_cookies(self):
        ok, reason = make_session(cookies={}).check("1.2.3.4", None)
        assert ok is False
        assert "cookie" in reason


class TestCookieExpiry:
    def test_uses_cf_clearance_expires(self):
        target = now() + 3600
        assert ss.cookie_expiry([
            {"name": "session", "value": "x", "expires": -1},
            {"name": "cf_clearance", "value": "y", "expires": target},
        ]) == target

    def test_session_cookie_falls_back_to_default_ttl(self):
        value = ss.cookie_expiry([{"name": "cf_clearance", "expires": -1}])
        assert now() + ss.DEFAULT_TTL - 5 <= value <= now() + ss.DEFAULT_TTL + 5

    def test_no_cf_clearance(self):
        value = ss.cookie_expiry([{"name": "other", "expires": 99}])
        assert value > now()

    def test_empty_list(self):
        assert ss.cookie_expiry([]) > now()


class TestSessionStore:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = ss.SessionStore(path)
        store.update_cf("acct-1", make_session())
        store.remember("acct-1", checkin_path="/api/user/check_in", user_id=7)
        store.flush()
        assert path.exists()

        reloaded = ss.SessionStore(path)
        record = reloaded.get("acct-1")
        assert record.checkin_path == "/api/user/check_in"
        assert record.user_id == 7
        assert record.cf.cookies["cf_clearance"] == "abc"
        assert record.cf.user_agent.endswith("Firefox/133.0")

    def test_clear_cf_keeps_metadata(self):
        """cookie 失效不该丢掉与 IP 无关的 checkin_path / user_id。"""
        store = ss.SessionStore(_missing_path())
        store.update_cf("a", make_session())
        store.remember("a", checkin_path="/api/user/checkin", user_id=3)
        store.clear_cf("a")
        record = store.get("a")
        assert record.cf is None
        assert record.checkin_path == "/api/user/checkin"
        assert record.user_id == 3

    def test_missing_file_is_fine(self, tmp_path):
        store = ss.SessionStore(tmp_path / "nope.json")
        assert store.get("whatever").cf is None

    def test_corrupt_file_is_ignored(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        store = ss.SessionStore(path)
        assert store.get("x").cf is None

    def test_flush_is_noop_without_changes(self, tmp_path):
        path = tmp_path / "sessions.json"
        ss.SessionStore(path).flush()
        assert not path.exists()

    def test_flush_creates_parent_dir(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "sessions.json"
        store = ss.SessionStore(path)
        store.update_cf("a", make_session())
        store.flush()
        assert path.exists()

    def test_concurrent_updates_flush_valid_json(self, tmp_path):
        import json
        import threading

        path = tmp_path / "sessions.json"
        store = ss.SessionStore(path)

        def update(index):
            store.remember(f"acct-{index}", user_id=index)
            store.flush()

        threads = [threading.Thread(target=update, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert {payload[key]["user_id"] for key in payload} == set(range(8))


class TestRefreshInflight:
    """代次悬空记账。

    TaBiAI 旧代重放的安全窗口实测只有 20~45 秒，超窗重放会撤销整条会话。所以「这一代
    悬空了多久」必须跨进程记住 —— Actions 跑超时被平台强杀是常态，纯内存计时挡不住
    「进程死了、下一轮捡起旧代接着刷」这条路径。
    """

    GEN1 = "new_api_refresh=sid-a.secret-one"
    GEN2 = "new_api_refresh=sid-a.secret-two"

    def test_fingerprint_ignores_the_cookie_name_prefix(self):
        """裸 sid.secret 和带 new_api_refresh= 的写法是同一代，不能算成两代。"""
        assert ss.generation_fingerprint(self.GEN1) ==             ss.generation_fingerprint("sid-a.secret-one")
        assert ss.generation_fingerprint(self.GEN1) != ss.generation_fingerprint(self.GEN2)
        assert ss.generation_fingerprint("") == ""

    def test_fingerprint_does_not_leak_the_secret(self):
        """存哈希不存原值：refresh_cookie 已经有完整凭据了，没必要留第二份。"""
        fingerprint = ss.generation_fingerprint(self.GEN1)
        assert "secret-one" not in fingerprint
        assert len(fingerprint) == 12

    def test_no_mark_means_safe_to_use(self):
        store = ss.SessionStore(_missing_path())
        assert store.refresh_inflight_age("a", self.GEN1) is None

    def test_mark_then_age_grows(self):
        store = ss.SessionStore(_missing_path())
        store.mark_refresh_inflight("a", self.GEN1)
        age = store.refresh_inflight_age("a", self.GEN1)
        assert age is not None and 0 <= age < 2

    def test_repeated_mark_does_not_reset_the_clock(self):
        """悬空时长要从第一次送出算起，每次重试都重置的话预算永远用不完。"""
        store = ss.SessionStore(_missing_path())
        store.mark_refresh_inflight("a", self.GEN1)
        store.get("a").refresh_inflight_at = now() - 30
        store.mark_refresh_inflight("a", self.GEN1)
        assert store.refresh_inflight_age("a", self.GEN1) >= 29

    def test_mark_is_bound_to_one_generation(self):
        """别的代次查不到这笔账 —— 平台回写/人工重签换代后旧账自然作废。"""
        store = ss.SessionStore(_missing_path())
        store.mark_refresh_inflight("a", self.GEN1)
        assert store.refresh_inflight_age("a", self.GEN2) is None

    def test_rotating_the_cookie_clears_the_mark(self):
        """换代等于上一代已被站点取代，它再也不会被送去 refresh，账该清零。"""
        store = ss.SessionStore(_missing_path())
        store.mark_refresh_inflight("a", self.GEN1)
        store.remember_refresh_cookie("a", self.GEN2)
        assert store.refresh_inflight_age("a", self.GEN1) is None
        assert store.refresh_inflight_age("a", self.GEN2) is None

    def test_clear_settles_the_account(self):
        store = ss.SessionStore(_missing_path())
        store.mark_refresh_inflight("a", self.GEN1)
        store.clear_refresh_inflight("a")
        assert store.refresh_inflight_age("a", self.GEN1) is None

    def test_mark_survives_a_process_restart(self, tmp_path):
        """这是整个设计的立足点：Actions 被强杀后，下一轮必须还知道这一代危险。"""
        path = tmp_path / "sessions.json"
        first = ss.SessionStore(path)
        first.mark_refresh_inflight("a", self.GEN1)
        first.get("a").refresh_inflight_at = now() - 42
        first.mark_dirty()          # 直接改字段绕过了脏标记，不打上 flush 会空转
        first.flush()

        reloaded = ss.SessionStore(path)
        assert reloaded.refresh_inflight_age("a", self.GEN1) >= 41

    def test_mark_is_flushed_immediately(self, tmp_path):
        """不走节流：节流期间进程被杀就等于没记账，代价是整条会话。"""
        path = tmp_path / "sessions.json"
        store = ss.SessionStore(path)
        store.mark_refresh_inflight("a", self.GEN1)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["a"]["refresh_inflight_gen"] == ss.generation_fingerprint(self.GEN1)

    def test_backward_clock_is_treated_as_no_mark(self):
        """容器里时钟被往回调不罕见，算出负数时宁可当没标记，也不要当「刚刚才用」。"""
        store = ss.SessionStore(_missing_path())
        store.mark_refresh_inflight("a", self.GEN1)
        store.get("a").refresh_inflight_at = now() + 600
        assert store.refresh_inflight_age("a", self.GEN1) is None

    def test_garbage_values_are_dropped_on_load(self):
        """脏值当没标记：拿坏数据去判「还安全」比不判更危险。"""
        for bad in ({"refresh_inflight_at": "later", "refresh_inflight_gen": "abc"},
                    {"refresh_inflight_at": -5, "refresh_inflight_gen": "abc"},
                    {"refresh_inflight_at": 1000.0}):
            record = ss.AccountSession.from_dict(bad)
            assert record.refresh_inflight_at is None
            assert record.refresh_inflight_gen is None

    def test_half_written_pair_is_not_persisted(self):
        """两个字段是一对，只有一个的时候不该写出去。"""
        record = ss.AccountSession(refresh_inflight_at=now())
        assert "refresh_inflight_at" not in record.to_dict()


def _missing_path():
    from pathlib import Path
    import tempfile

    return Path(tempfile.mkdtemp()) / "sessions.json"
