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
