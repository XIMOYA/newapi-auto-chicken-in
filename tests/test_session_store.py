"""会话缓存：cf_clearance 与 IP/UA/代理的绑定校验，以及原子落盘。"""

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


def _missing_path():
    from pathlib import Path
    import tempfile

    return Path(tempfile.mkdtemp()) / "sessions.json"
