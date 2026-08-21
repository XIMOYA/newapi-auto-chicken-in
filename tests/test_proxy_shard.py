"""tests/test_proxy_shard.py

代理分片：几个 job 并行时各领一份互不重叠的代理。

服务端按轮转发牌，客户端只负责把「我是第几片」如实带上去。这里盯的是客户端这一半 ——
参数解析要严（写错宁可不跑）、URL 拼接要对（remote_url 可能自带查询串）、以及分片号要
真的从 CLI 一路透传到预取请求上。
"""
from types import SimpleNamespace

import pytest

import main
from newapi_checkin.config import ConfigError
from newapi_checkin.proxy_pool import ProxyPool, ProxyPoolConfig


class TestParseShard:
    def test_none_means_no_sharding(self):
        assert main._parse_shard(None) is None

    @pytest.mark.parametrize("raw,want", [
        ("1/3", (1, 3)),
        ("3/3", (3, 3)),
        ("1/1", (1, 1)),
        (" 2 / 4 ", (2, 4)),
    ])
    def test_valid_forms(self, raw, want):
        assert main._parse_shard(raw) == want

    @pytest.mark.parametrize("raw", ["bad", "1", "1/", "/3", "0/3", "4/3", "-1/3", "1/0", "1.5/3"])
    def test_invalid_forms_raise(self, raw):
        """写错不能静默忽略：那会让这个 job 以为自己独占一批代理，实际和别人撞了。"""
        with pytest.raises(ConfigError):
            main._parse_shard(raw)


class TestRemoteURL:
    @staticmethod
    def _pool(shard=None, url="https://cfg.example.com/api/proxies/available"):
        return ProxyPool(ProxyPoolConfig(remote_url=url, preflight_check=False), shard=shard)

    def test_without_shard_url_is_untouched(self):
        assert self._pool()._remote_url() == "https://cfg.example.com/api/proxies/available"

    def test_appends_shard_query(self):
        got = self._pool(shard=(2, 5))._remote_url()
        assert got == "https://cfg.example.com/api/proxies/available?shard=2/5"

    def test_uses_ampersand_when_url_already_has_query(self):
        pool = self._pool(shard=(1, 3),
                          url="https://cfg.example.com/api/proxies/available?limit=50")
        assert pool._remote_url().endswith("?limit=50&shard=1/3")

    def test_single_shard_is_a_noop(self):
        """只有一片就等于没分片，没必要给平台多带一个参数。"""
        assert self._pool(shard=(1, 1))._remote_url().endswith("/available")


class TestFetchCarriesShard:
    def test_prefetch_request_hits_sharded_url(self, monkeypatch):
        seen = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"proxies": ["a:80", "b:80"], "count": 2}

        def fake_get(url, **kwargs):
            seen["url"] = url
            return _Resp()

        monkeypatch.setattr("curl_cffi.requests.get", fake_get)
        pool = ProxyPool(
            ProxyPoolConfig(
                remote_url="https://cfg.example.com/api/proxies/available",
                remote_token="ncf_x",
                preflight_check=False,
            ),
            shard=(3, 4),
        )
        assert pool.refresh() == 2
        assert seen["url"] == "https://cfg.example.com/api/proxies/available?shard=3/4"


class TestRunOptionsWiring:
    def test_cli_shard_reaches_run_options(self, monkeypatch, tmp_path):
        """--shard 要一路透传到 RunOptions，中间断了就等于没分片。"""
        from newapi_checkin.runner import RunOptions

        captured = {}

        def fake_runner(cfg, options):
            captured["shard"] = options.proxy_shard
            return SimpleNamespace(run=lambda: 0)

        monkeypatch.setattr(main, "load_config", lambda _p: SimpleNamespace(
            migrated_from=None, source=None,
            browser=SimpleNamespace(headless="virtual", driver="camoufox", humanize=False),
            http=SimpleNamespace(impersonate="chrome"),
            ai=SimpleNamespace(enabled=False),
            config_sync=SimpleNamespace(enabled=False),
        ))
        monkeypatch.setattr(main, "Runner", fake_runner)
        assert main.main(["--shard", "2/7", "--headless"]) == 0
        assert captured["shard"] == (2, 7)

    def test_default_is_none(self):
        from newapi_checkin.runner import RunOptions

        assert RunOptions().proxy_shard is None
