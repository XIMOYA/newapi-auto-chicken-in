from types import SimpleNamespace

import main


def _fake_cfg():
    return SimpleNamespace(
        migrated_from=None,
        source=None,
        browser=SimpleNamespace(headless="virtual", driver="camoufox", humanize=False),
        http=SimpleNamespace(impersonate="chrome"),
        ai=SimpleNamespace(enabled=False),
    )


def test_parser_supports_explicit_headless_mode():
    args = main.build_parser().parse_args(["--headless", "--account", "站点A", "--parallel", "3"])
    assert args.headless is True
    assert args.headful is False
    assert args.account == ["站点A"]
    assert args.parallel == 3


def test_parser_supports_separate_cookie_test_modes():
    args = main.build_parser().parse_args(["--cookie-test", "github_cookie"])
    assert args.cookie_test == "github_cookie"


def test_headless_and_manual_are_rejected(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda _path: _fake_cfg())
    monkeypatch.setattr(main.log, "setup", lambda **_kwargs: None)
    errors = []
    monkeypatch.setattr(main.log, "err", errors.append)

    assert main.main(["--headless", "--manual"]) == 2
    assert "不能同时使用" in errors[0]


def test_manual_requires_display_on_linux(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda _path: _fake_cfg())
    monkeypatch.setattr(main.log, "setup", lambda **_kwargs: None)
    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    errors = []
    monkeypatch.setattr(main.log, "err", errors.append)

    assert main.main(["--manual"]) == 2
    assert "DISPLAY" in errors[0]


def test_parallel_cli_override(monkeypatch):
    cfg = _fake_cfg()
    monkeypatch.setattr(main, "load_config", lambda _path: cfg)
    monkeypatch.setattr(main.log, "setup", lambda **_kwargs: None)
    monkeypatch.setattr(main.log, "debug", lambda *_args, **_kwargs: None)
    seen = {}

    class FakeRunner:
        def __init__(self, received_cfg, options):
            seen["options"] = options

        def run(self):
            return 0

    monkeypatch.setattr(main, "Runner", FakeRunner)

    assert main.main(["--headless", "--parallel", "4"]) == 0
    assert seen["options"].parallelism == 4


def test_headless_cli_overrides_config(monkeypatch):
    cfg = _fake_cfg()
    monkeypatch.setattr(main, "load_config", lambda _path: cfg)
    monkeypatch.setattr(main.log, "setup", lambda **_kwargs: None)
    monkeypatch.setattr(main.log, "debug", lambda *_args, **_kwargs: None)
    seen = {}

    class FakeRunner:
        def __init__(self, received_cfg, options):
            seen["cfg"] = received_cfg
            seen["options"] = options

        def run(self):
            return 0

    monkeypatch.setattr(main, "Runner", FakeRunner)

    assert main.main(["--headless"]) == 0
    assert seen["cfg"].browser.headless is True
    assert seen["options"].headful is False
    assert seen["options"].manual is False
    assert seen["options"].parallelism == 1


def test_parser_supports_proxy_sweep():
    args = main.build_parser().parse_args(["--proxy-sweep"])
    assert args.proxy_sweep is True
    assert args.proxy_sweep_minutes == 50          # 默认时间盒
    args = main.build_parser().parse_args(["--proxy-sweep", "--proxy-sweep-minutes", "20"])
    assert args.proxy_sweep_minutes == 20


class TestProxySweepEntry:
    """--proxy-sweep 的入口：体检平台上的代理并回传，不签到。

    回传是这趟的唯一产出 —— 测得再准，传不上去平台排序就一点没变，整趟白跑。
    所以回传失败必须以非 0 退出，让 Actions 亮红而不是静静地"成功"。
    """

    @staticmethod
    def _cfg(enabled=True, remote_url="https://panel.example.com/api/proxies/available"):
        from newapi_checkin.config import build_config

        return build_config({
            "proxy_pool": {"enabled": enabled, "remote_url": remote_url},
            "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
        })

    def _fake_pool(self, monkeypatch, stats, feedback=(True, "已回传 3 条")):
        calls = {}

        class FakePool:
            def __init__(self, cfg):
                calls["cfg"] = cfg

            def sweep_remote(self, minutes):
                calls["minutes"] = minutes
                return stats

            def report_feedback(self, source="github-actions"):
                calls["source"] = source
                return feedback

        monkeypatch.setattr("newapi_checkin.proxy_pool.ProxyPool", FakePool)
        return calls

    def test_disabled_pool_is_refused(self):
        assert main._sweep_proxies(self._cfg(enabled=False), 50) == 2

    def test_missing_remote_url_is_refused(self):
        """体检的对象是平台上的代理，拉不到就没得测。"""
        assert main._sweep_proxies(self._cfg(remote_url=""), 50) == 2

    def test_happy_path_reports_and_returns_zero(self, monkeypatch):
        calls = self._fake_pool(monkeypatch, {"total": 9, "tested": 9, "ok": 7,
                                              "fail": 2, "elapsed": 12.0})
        assert main._sweep_proxies(self._cfg(), 30) == 0
        assert calls["minutes"] == 30              # 时间盒透传下去了
        assert calls["source"]                     # 带了来源标注

    def test_failed_feedback_exits_nonzero(self, monkeypatch):
        """传不上去就等于没体检，不能报成功。"""
        self._fake_pool(monkeypatch, {"total": 9, "tested": 9, "ok": 7, "fail": 2},
                        feedback=(False, "HTTP 500"))
        assert main._sweep_proxies(self._cfg(), 50) == 1

    def test_empty_target_list_exits_nonzero(self, monkeypatch):
        self._fake_pool(monkeypatch, {"total": 0, "tested": 0, "ok": 0, "fail": 0,
                                      "reason": "平台没有返回存活代理"})
        assert main._sweep_proxies(self._cfg(), 50) == 1

    def test_source_marks_github_actions(self, monkeypatch):
        calls = self._fake_pool(monkeypatch, {"total": 1, "tested": 1, "ok": 1, "fail": 0})
        monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
        main._sweep_proxies(self._cfg(), 50)
        assert "me/repo" in calls["source"]


class TestProxyListRoundTrip:
    """--proxy-sweep-out 落盘、--proxy-list 读回：签到 workflow 里靠这一对传递清单。"""

    def test_write_then_load_keeps_order(self, tmp_path):
        """按延迟升序写的，读回来顺序不能乱 —— acquire 顺序取优依赖它。"""
        path = tmp_path / "alive.json"
        assert main._write_alive_proxies(path, ["fast:80", "mid:80", "slow:80"]) == 3
        assert main._load_proxy_list(str(path)) == ["fast:80", "mid:80", "slow:80"]

    def test_written_shape_matches_the_platform_response(self, tmp_path):
        """字段名跟 /api/proxies/available 对齐，下游解析能复用同一套。"""
        import json

        path = tmp_path / "alive.json"
        main._write_alive_proxies(path, ["a:80"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["proxies"] == ["a:80"] and raw["count"] == 1
        assert raw["checked_at"] and raw["source"]

    def test_empty_result_is_still_written(self, tmp_path):
        """一条都没测通也要写文件：下游读到空清单才能明确报错，而不是猜上一步崩没崩。"""
        path = tmp_path / "alive.json"
        assert main._write_alive_proxies(path, []) == 0
        assert path.exists()

    def test_missing_file_is_rejected(self, tmp_path):
        assert main._load_proxy_list(str(tmp_path / "nope.json")) is None

    def test_empty_list_is_rejected(self, tmp_path):
        """空清单不能当成"有池子"往下走，否则所有账号都会被判无代理跳过。"""
        path = tmp_path / "empty.json"
        path.write_text('{"proxies": []}', encoding="utf-8")
        assert main._load_proxy_list(str(path)) is None

    def test_bare_array_is_accepted(self, tmp_path):
        """手写清单时直接给数组也认，不强求包一层。"""
        path = tmp_path / "bare.json"
        path.write_text('["a:80", "b:80"]', encoding="utf-8")
        assert main._load_proxy_list(str(path)) == ["a:80", "b:80"]

    def test_garbage_shape_is_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{"proxies": "a:80"}', encoding="utf-8")
        assert main._load_proxy_list(str(path)) is None

    def test_broken_json_is_rejected(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        assert main._load_proxy_list(str(path)) is None
