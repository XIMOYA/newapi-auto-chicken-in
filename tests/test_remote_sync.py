"""远程配置 API 同步与解密落盘测试。"""

import json

from newapi_checkin import remote_sync
from newapi_checkin.config_store import load_document, save_document
from newapi_checkin.remote_sync import _merge_payload, sync_remote_config
from newapi_checkin.secure_config import encrypt_json


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self.payload


def base_config(*, sync=None):
    return {
        "security": {
            "encryption_enabled": False,
            "config_key": "config-key-123",
            "encrypted_file": "data/config.encrypted.json",
        },
        "config_sync": sync
        or {
            "enabled": True,
            "url": "https://config.example.com/api",
            "method": "GET",
            "auto_before_checkin": True,
        },
        "accounts": [{"name": "旧账号", "url": "https://old.example.com", "cookie": "old"}],
    }


def test_sync_preserves_local_encryption(tmp_path, monkeypatch):
    """同步后加密状态必须保留：不能明文落盘，也不能删掉密文文件。"""
    path = tmp_path / "config.json"
    raw = base_config()
    save_document(raw, path, encryption_enabled=True, key="config-key-123")
    remote_payload = {
        "accounts": [{"name": "新账号", "url": "https://new.example.com", "cookie": "new"}],
    }
    envelope = encrypt_json(remote_payload, "config-key-123")
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))
    monkeypatch.setattr(
        "newapi_checkin.remote_sync.cffi.request",
        lambda method, url, **kwargs: FakeResponse({"data": envelope}),
    )

    result = sync_remote_config()

    assert result["ok"] is True
    assert result["encrypted_response"] is True
    document = load_document(path)
    assert document.encrypted is True
    assert document.raw["accounts"][0]["name"] == "新账号"
    assert document.raw["security"]["config_key"] == "config-key-123"
    assert document.raw["config_sync"]["url"] == "https://config.example.com/api"
    # config.json 仍只是 security bootstrap，密文文件被更新而不是被删
    bootstrap = json.loads(path.read_text(encoding="utf-8"))
    assert "accounts" not in bootstrap
    assert "cookie" not in path.read_text(encoding="utf-8")
    encrypted_path = tmp_path / "data" / "config.encrypted.json"
    assert encrypted_path.exists()
    assert document.raw["accounts"][0]["cookie"] == "new"


def test_sync_keeps_local_modules_missing_from_remote(tmp_path, monkeypatch):
    """远端没带本地已有的业务模块时，保留本地模块并正常同步账号。"""
    path = tmp_path / "config.json"
    raw = base_config()
    raw["ai"] = {
        "enabled": True,
        "base_url": "https://ai.example.com",
        "api_key": "sk-local-secret",
        "model": "gpt-4o-mini",
    }
    raw["proxy_pool"] = {"enabled": True, "sources": ["http://src.example.com:80"]}
    save_document(raw, path, encryption_enabled=False)
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))
    monkeypatch.setattr(
        "newapi_checkin.remote_sync.cffi.request",
        lambda method, url, **kwargs: FakeResponse(
            {"accounts": [{"name": "新账号", "url": "https://new.example.com", "cookie": "c"}]}
        ),
    )

    result = sync_remote_config()

    assert result["ok"] is True
    document = load_document(path)
    assert document.raw["ai"]["api_key"] == "sk-local-secret"
    assert document.raw["proxy_pool"]["enabled"] is True
    assert document.raw["accounts"][0]["name"] == "新账号"


def test_merge_remote_keeps_local_when_missing_and_applies_empty_arrays():
    """合并语义：远端缺键保留本地；远端显式空数组按远端意图清空。"""
    local = {
        "accounts": [{"name": "A", "url": "https://a.example.com"}],
        "ai": {"enabled": False, "api_key": "local-key"},
        "proxy_pool": {"enabled": True},
    }
    merged = _merge_payload(local, {"accounts": [], "ai": {"enabled": True}})
    assert merged["accounts"] == []
    assert merged["ai"] == {"enabled": True}
    assert merged["proxy_pool"] == {"enabled": True}  # 远端没带 -> 保留本地

    # security / config_sync 永远不能被远端覆盖
    local["security"] = {"encryption_enabled": True}
    merged = _merge_payload(local, {"security": {"encryption_enabled": False}})
    assert merged["security"] == {"encryption_enabled": True}


def test_merge_never_overwrites_local_tabiai_section():
    """tabiai.cdp_url 指向本机 Chrome 调试端口，远端下发不能把它改坏。"""
    local = {
        "accounts": [],
        "tabiai": {"enabled": True, "cdp_url": "http://127.0.0.1:9222"},
    }
    merged = _merge_payload(local, {
        "accounts": [],
        "tabiai": {"enabled": False, "cdp_url": "http://10.0.0.1:9222"},
    })
    assert merged["tabiai"] == {"enabled": True, "cdp_url": "http://127.0.0.1:9222"}


def test_missing_tabiai_is_not_reported_as_missing_module():
    """远端本来就不管 tabiai，不该每次同步都刷一条无意义告警。"""
    from newapi_checkin.remote_sync import _missing_modules

    local = {"accounts": [], "tabiai": {"enabled": True}, "proxy_pool": {"enabled": True}}
    missing = _missing_modules(local, {"accounts": []})
    assert "tabiai" not in missing
    assert "proxy_pool" in missing


def test_sync_sends_token_and_post_body(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    sync = {
        "enabled": True,
        "url": "https://config.example.com/api",
        "method": "POST",
        "token": "secret-token",
        "token_header": "X-Config-Token",
        "token_prefix": "Token",
        "headers": {"X-Client": "checkin"},
        "body": {"site": "demo"},
    }
    save_document(base_config(sync=sync), path, encryption_enabled=False)
    captured = {}
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return FakeResponse({"accounts": [{"name": "更新", "url": "https://new.example.com"}]})

    monkeypatch.setattr("newapi_checkin.remote_sync.cffi.request", fake_request)

    result = sync_remote_config()

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://config.example.com/api"
    assert captured["kwargs"]["headers"]["X-Client"] == "checkin"
    assert captured["kwargs"]["headers"]["X-Config-Token"] == "Token secret-token"
    assert captured["kwargs"]["json"] == {"site": "demo"}


def test_sync_failure_does_not_replace_existing_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_document(base_config(), path, encryption_enabled=False)
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))
    monkeypatch.setattr(
        "newapi_checkin.remote_sync.cffi.request",
        lambda method, url, **kwargs: FakeResponse({"message": "server error"}, status_code=503),
    )

    result = sync_remote_config()

    assert result["ok"] is False
    assert load_document(path).raw["accounts"][0]["name"] == "旧账号"


def test_auto_only_skips_when_auto_sync_is_off(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_document(
        base_config(sync={"enabled": True, "url": "https://config.example.com", "auto_before_checkin": False}),
        path,
        encryption_enabled=False,
    )
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))

    result = sync_remote_config(auto_only=True)

    assert result["ok"] is True
    assert result["skipped"] is True


class TestFetchRunState:
    """查平台运行锁。签到开跑前靠它判断凭据保活是不是正在跑。"""

    @staticmethod
    def _sync(enabled=True, url="https://panel.example.com/api/config/raw"):
        from newapi_checkin.config import ConfigSyncConfig

        return ConfigSyncConfig.from_raw({"enabled": enabled, "url": url, "token": "k" * 20})

    def _capture(self, monkeypatch, response):
        """替掉网络层，记录实际发出的请求。"""
        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update(method=method, url=url, **kwargs)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(remote_sync.cffi, "request", fake_request)
        return seen

    def test_reads_running_source_over_get(self, monkeypatch):
        """必须用 GET：这个端点是只读的，POST 会被平台当成 start 上报。"""
        seen = self._capture(
            monkeypatch, FakeResponse({"running": True, "source": "tabiai-keepalive"})
        )
        ok, state = remote_sync.fetch_run_state(self._sync())
        assert ok is True
        assert state["source"] == "tabiai-keepalive"
        assert seen["method"] == "GET"
        assert seen["url"] == "https://panel.example.com/api/run-state"
        assert "json" not in seen

    def test_carries_the_api_key_header(self, monkeypatch):
        """客户端只有 API Key，没有 JWT —— 认证头漏了就永远查不到锁。"""
        seen = self._capture(monkeypatch, FakeResponse({"running": False}))
        remote_sync.fetch_run_state(self._sync())
        assert any("k" * 20 in str(v) for v in seen["headers"].values())

    def test_disabled_sync_short_circuits(self, monkeypatch):
        called = []
        monkeypatch.setattr(remote_sync.cffi, "request",
                            lambda *a, **kw: called.append(1))
        assert remote_sync.fetch_run_state(self._sync(enabled=False)) == (False, {})
        assert called == []

    def test_unusable_url_reports_failure_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(remote_sync.cffi, "request",
                            lambda *a, **kw: FakeResponse({"running": True}))
        assert remote_sync.fetch_run_state(self._sync(url="")) == (False, {})
        assert remote_sync.fetch_run_state(self._sync(url="不是个地址")) == (False, {})

    def test_network_error_is_swallowed(self, monkeypatch):
        self._capture(monkeypatch, RuntimeError("connection reset"))
        assert remote_sync.fetch_run_state(self._sync()) == (False, {})

    def test_http_error_and_garbage_body_are_not_trusted(self, monkeypatch):
        self._capture(monkeypatch, FakeResponse({"message": "boom"}, status_code=500))
        assert remote_sync.fetch_run_state(self._sync()) == (False, {})

        class NotJson:
            status_code = 200
            text = "<html>"

            def json(self):
                raise ValueError("no json")

        self._capture(monkeypatch, NotJson())
        assert remote_sync.fetch_run_state(self._sync()) == (False, {})


class TestWritebackVerification:
    """凭据回写的验收。

    平台攥着旧代次的后果不是「提示不准」：它的保活协程和网页端检测下次会拿旧代去
    refresh，直接撞重放、整条会话被撤销、所有账号重新签发。而 HTTP 200 并不等于平台
    收下了 —— 网关、鉴权代理、缓存层都可能替它回 200，请求压根没到服务端。所以这里
    认的是响应体里的 ok 字段，而不是状态码。
    """

    @staticmethod
    def _sync():
        from newapi_checkin.config import ConfigSyncConfig

        return ConfigSyncConfig.from_raw({
            "enabled": True,
            "url": "https://panel.example.com/api/config/raw",
            "token": "k" * 20,
        })

    def _replies(self, monkeypatch, responses):
        """按调用次序吐响应，并记录每次实际发出的请求。"""
        sent = []

        def fake_request(method, url, **kwargs):
            sent.append({"method": method, "url": url, **kwargs})
            item = responses[min(len(sent) - 1, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(remote_sync.cffi, "request", fake_request)
        monkeypatch.setattr(remote_sync.time, "sleep", lambda _s: None)
        return sent

    def test_ok_true_is_accepted(self, monkeypatch):
        sent = self._replies(monkeypatch, [FakeResponse({"ok": True})])
        ok, detail = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is True
        # 回写只发一次；后面那条 GET 是读回核实（本例的假响应没有 cookie 字段，
        # 核实拿不到结论，按加固失灵放过）
        assert [item["method"] for item in sent] == ["POST", "GET"]
        assert sent[0]["json"] == {"cookie": "new_api_refresh=sid.gen2"}

    def test_never_goes_through_a_proxy(self, monkeypatch):
        """打的是自己的平台，不该套代理 —— 代理只会给关键链路多一个失败点。"""
        sent = self._replies(monkeypatch, [FakeResponse({"ok": True})])
        remote_sync.writeback_refresh_cookie(self._sync(), "T", "new_api_refresh=sid.gen2")
        assert sent[0].get("proxies") is None

    def test_gateway_answering_200_without_json_is_rejected(self, monkeypatch):
        """最要防的一种：网关代答 200，请求压根没到服务端。"""
        class HtmlPage:
            status_code = 200
            text = "<html>gateway ok</html>"

            def json(self):
                raise ValueError("not json")

        self._replies(monkeypatch, [HtmlPage()])
        ok, detail = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is False
        assert "不是 JSON" in detail

    def test_ok_false_is_rejected(self, monkeypatch):
        self._replies(monkeypatch, [FakeResponse({"ok": False, "error": "库满了"})])
        ok, detail = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is False
        assert "未确认收下" in detail

    def test_missing_ok_field_is_rejected(self, monkeypatch):
        """只有 message 没有 ok 也不算 —— 别的服务也可能回一段 JSON。"""
        self._replies(monkeypatch, [FakeResponse({"message": "received"})])
        ok, _ = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is False

    def test_json_array_is_rejected(self, monkeypatch):
        self._replies(monkeypatch, [FakeResponse([{"ok": True}])])
        ok, detail = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is False
        assert "JSON 对象" in detail

    def test_retries_until_the_platform_confirms(self, monkeypatch):
        """前两次抖了第三次成 —— 漏一次的代价是整条会话，值得多试。"""
        sent = self._replies(monkeypatch, [
            RuntimeError("connection reset"),
            FakeResponse({"ok": False}),
            FakeResponse({"ok": True}),
        ])
        ok, _ = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is True
        # 只数回写本身的次数，读回核实的 GET 不算重试
        assert len([item for item in sent if item["method"] == "POST"]) == 3

    def test_gives_up_after_the_attempt_budget(self, monkeypatch):
        sent = self._replies(monkeypatch, [FakeResponse({"ok": False})])
        ok, detail = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is False
        assert len(sent) == remote_sync.WRITEBACK_ATTEMPTS
        assert f"重试 {remote_sync.WRITEBACK_ATTEMPTS} 次" in detail

    def test_http_error_is_rejected_without_reading_the_body(self, monkeypatch):
        self._replies(monkeypatch, [FakeResponse({"error": "no such account"},
                                                 status_code=404)])
        ok, detail = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is False
        assert "HTTP 404" in detail

    def test_guard_conditions_skip_the_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(remote_sync.cffi, "request", lambda *a, **kw: called.append(1))
        from newapi_checkin.config import ConfigSyncConfig

        disabled = ConfigSyncConfig.from_raw({"enabled": False, "url": "https://x.example.com"})
        assert remote_sync.writeback_refresh_cookie(disabled, "T", "c")[0] is False
        assert remote_sync.writeback_refresh_cookie(self._sync(), "T", "  ")[0] is False
        assert called == []


class TestWritebackReadBack:
    """回写被验收之后的读回核实。

    平台回 `ok: true` 只是它的自述，库里到底存了什么得读回来才知道。这一步专门堵
    「收了但没存」：那种情况下平台仍攥着旧代次，它的保活下次 refresh 就撞重放、
    整条会话被 AUTH_SESSION_REVOKED 报废。

    两条路径必须严格分开：
      - **确认存的不是这一代** → 真故障，和回写失败同一条判负路径
      - **核实拿不到结论**（网络错、老平台没这个端点、响应形状不对）→ 记日志放过。
        回写已经被平台明确确认过了，加固失灵不该把成功的轮次判成失败
    """

    @staticmethod
    def _sync(**overrides):
        from newapi_checkin.config import ConfigSyncConfig

        raw = {
            "enabled": True,
            "url": "https://panel.example.com/api/config/raw",
            "token": "k" * 20,
        }
        raw.update(overrides)
        return ConfigSyncConfig.from_raw(raw)

    def _serve(self, monkeypatch, *, writeback=None, readback=None):
        """按方法分派假响应：POST 是回写，GET 是读回核实。"""
        sent = []

        def fake_request(method, url, **kwargs):
            sent.append({"method": method, "url": url, **kwargs})
            item = writeback if method == "POST" else readback
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(remote_sync.cffi, "request", fake_request)
        monkeypatch.setattr(remote_sync.time, "sleep", lambda _s: None)
        return sent

    def _writeback(self, monkeypatch, readback, cookie="new_api_refresh=sid.gen2"):
        sent = self._serve(monkeypatch,
                           writeback=FakeResponse({"ok": True}),
                           readback=readback)
        ok, detail = remote_sync.writeback_refresh_cookie(self._sync(), "T", cookie)
        return ok, detail, sent

    def test_matching_cookie_is_read_back_from_the_account_endpoint(self, monkeypatch):
        ok, _, sent = self._writeback(monkeypatch, FakeResponse(
            {"name": "T", "cookie": "new_api_refresh=sid.gen2"}))
        assert ok is True
        assert sent[1]["method"] == "GET"
        assert sent[1]["url"] == "https://panel.example.com/api/accounts/T/raw"

    def test_trailing_whitespace_is_not_a_mismatch(self, monkeypatch):
        """一个换行不该让整轮判负。"""
        ok, _, _ = self._writeback(monkeypatch, FakeResponse(
            {"cookie": "\n  new_api_refresh=sid.gen2  \n"}))
        assert ok is True

    def test_platform_side_normalization_is_not_a_mismatch(self, monkeypatch):
        """平台落库前会给裸 sid.secret 补上 new_api_refresh= 前缀，那仍是同一个值。"""
        ok, _, _ = self._writeback(monkeypatch,
                                   FakeResponse({"cookie": "new_api_refresh=sid.gen2"}),
                                   cookie="sid.gen2")
        assert ok is True

    def test_another_generation_in_the_store_is_a_failure(self, monkeypatch):
        """收了但没存 —— 平台还攥着旧代次，必须让调用方按回写失败处理。"""
        ok, detail, sent = self._writeback(monkeypatch, FakeResponse(
            {"cookie": "new_api_refresh=sid.gen1"}))
        assert ok is False
        assert "读回核实不一致" in detail
        # 判负后不该再重试回写：平台确实收下了，重发同一个值不会改变结果
        assert [item["method"] for item in sent] == ["POST", "GET"]

    def test_empty_cookie_in_the_store_is_a_failure(self, monkeypatch):
        """库里是空串同样是「没存住」，不能当成核实不了。"""
        ok, detail, _ = self._writeback(monkeypatch, FakeResponse({"cookie": ""}))
        assert ok is False
        assert "读回核实不一致" in detail

    def test_network_error_while_verifying_is_not_a_verdict(self, monkeypatch):
        ok, _, _ = self._writeback(monkeypatch, RuntimeError("connection reset"))
        assert ok is True

    def test_missing_endpoint_on_an_old_platform_is_not_a_verdict(self, monkeypatch):
        """老版本平台没有这个端点会 404，那只是核实不了。"""
        ok, _, _ = self._writeback(monkeypatch,
                                   FakeResponse({"error": "not found"}, status_code=404))
        assert ok is True

    def test_unexpected_response_shape_is_not_a_verdict(self, monkeypatch):
        """网关代答、端点被改：拿不到 cookie 字段就断言库里存错了会造出一批假故障。"""
        class HtmlPage:
            status_code = 200
            text = "<html>gateway ok</html>"

            def json(self):
                raise ValueError("not json")

        assert self._writeback(monkeypatch, HtmlPage())[0] is True
        assert self._writeback(monkeypatch, FakeResponse({"name": "T"}))[0] is True
        assert self._writeback(monkeypatch, FakeResponse([{"cookie": "x"}]))[0] is True

    def test_verification_never_goes_through_a_proxy(self, monkeypatch):
        """读回打的还是自己的平台，代理只会多一个失败点。"""
        _, _, sent = self._writeback(monkeypatch, FakeResponse(
            {"cookie": "new_api_refresh=sid.gen2"}))
        assert "proxies" in sent[1] and sent[1]["proxies"] is None

    def test_verification_reuses_auth_header_and_timeout(self, monkeypatch):
        sync = self._sync()
        _, _, sent = self._writeback(monkeypatch, FakeResponse(
            {"cookie": "new_api_refresh=sid.gen2"}))
        assert any("k" * 20 in str(v) for v in sent[1]["headers"].values())
        assert sent[1]["timeout"] == sync.timeout

    def test_underivable_readback_url_skips_verification(self, monkeypatch):
        """回写走的是第三方网关模板时推不出读回地址，跳过核实但不影响回写结论。"""
        sent = self._serve(monkeypatch, writeback=FakeResponse({"ok": True}))
        sync = self._sync(url="", writeback_url="https://gw.example.com/hook/{name}")
        ok, _ = remote_sync.writeback_refresh_cookie(sync, "T", "new_api_refresh=sid.gen2")
        assert ok is True
        assert [item["method"] for item in sent] == ["POST"]

    def test_failed_writeback_never_reaches_verification(self, monkeypatch):
        """回写自己都没被确认时不该再去读回：判负原因是回写失败，不是核实。"""
        sent = self._serve(monkeypatch, writeback=FakeResponse({"ok": False}))
        ok, detail = remote_sync.writeback_refresh_cookie(
            self._sync(), "T", "new_api_refresh=sid.gen2")
        assert ok is False
        assert "未确认收下" in detail
        assert all(item["method"] == "POST" for item in sent)
