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
            captured["accounts"] = options.account_names
            return SimpleNamespace(run=lambda: 0)

        # 带 4 个账号：--shard 现在也要据此切出本片名单，配置里没有账号会直接返回 0
        accs = [SimpleNamespace(name=f"A{i}", enabled=True) for i in range(1, 5)]
        monkeypatch.setattr(main, "load_config", lambda _p: SimpleNamespace(
            migrated_from=None, source=None,
            accounts=accs,
            select=lambda picked=None: accs if not picked else [a for a in accs if a.name in set(picked)],
            browser=SimpleNamespace(headless="virtual", driver="camoufox", humanize=False),
            http=SimpleNamespace(impersonate="chrome"),
            ai=SimpleNamespace(enabled=False),
            config_sync=SimpleNamespace(enabled=False),
        ))
        monkeypatch.setattr(main, "Runner", fake_runner)
        assert main.main(["--shard", "2/2", "--headless"]) == 0
        assert captured["shard"] == (2, 2)
        # 第 2 片拿后一半
        assert captured["accounts"] == ["A3", "A4"]

    def test_empty_shard_exits_without_running(self, monkeypatch):
        """空片必须直接退出：账号名单为空等于「不过滤」，会让这个 job 把全部账号又跑一遍。"""
        started = []
        accs = [SimpleNamespace(name="A1", enabled=True)]
        monkeypatch.setattr(main, "load_config", lambda _p: SimpleNamespace(
            migrated_from=None, source=None,
            accounts=accs,
            select=lambda picked=None: accs if not picked else [a for a in accs if a.name in set(picked)],
            browser=SimpleNamespace(headless="virtual", driver="camoufox", humanize=False),
            http=SimpleNamespace(impersonate="chrome"),
            ai=SimpleNamespace(enabled=False),
            config_sync=SimpleNamespace(enabled=False),
        ))
        monkeypatch.setattr(main, "Runner",
                            lambda cfg, options: SimpleNamespace(run=lambda: started.append(1) or 0))
        assert main.main(["--shard", "3/3", "--headless"]) == 0
        assert started == []

    def test_default_is_none(self):
        from newapi_checkin.runner import RunOptions

        assert RunOptions().proxy_shard is None


class TestAccountSharding:
    """--shard 同时决定「本片跑哪些账号」。

    账号名不能经 job output 传递：它来自 secret 解出的配置，GitHub 会判定 output
    「可能含 secret」而整个跳过，下游 fromJson('') 就报 empty input。所以名单由各 job
    自己切，切法必须自洽 —— 各片不重叠、合起来覆盖全部，否则会漏签或重复签。
    """

    @staticmethod
    def _cfg(n):
        accs = [SimpleNamespace(name=f"A{i}", enabled=True) for i in range(1, n + 1)]

        def select(picked=None):
            if not picked:
                return accs
            wanted = set(picked)
            return [a for a in accs if a.name in wanted]

        return SimpleNamespace(accounts=accs, select=select)

    def _split(self, count, total):
        args = SimpleNamespace(account=None)
        return [main._shard_account_names(self._cfg(count), args, (i, total))
                for i in range(1, total + 1)]

    @pytest.mark.parametrize("count,total,sizes", [
        (64, 3, [22, 22, 20]),
        (60, 3, [20, 20, 20]),
        (64, 5, [13, 13, 13, 13, 12]),
        (5, 2, [3, 2]),
        (1, 1, [1]),
    ])
    def test_split_sizes(self, count, total, sizes):
        assert [len(p) for p in self._split(count, total)] == sizes

    @pytest.mark.parametrize("count,total", [(64, 3), (97, 7), (10, 4), (1, 1)])
    def test_covers_all_without_overlap(self, count, total):
        parts = self._split(count, total)
        flat = [n for p in parts for n in p]
        assert len(flat) == count            # 不漏
        assert len(set(flat)) == count       # 不重
        assert flat == [f"A{i}" for i in range(1, count + 1)]  # 顺序也保持

    def test_more_shards_than_accounts_yields_empty_tail(self):
        parts = self._split(2, 3)
        assert [len(p) for p in parts] == [1, 1, 0]

    def test_account_filter_applies_before_sharding(self):
        """--account 与 --shard 一起用时，先过滤再切分。"""
        args = SimpleNamespace(account=["A1,A2,A3,A4"])
        parts = [main._shard_account_names(self._cfg(10), args, (i, 2)) for i in (1, 2)]
        assert parts == [["A1", "A2"], ["A3", "A4"]]

    def test_single_shard_returns_everything(self):
        args = SimpleNamespace(account=None)
        assert len(main._shard_account_names(self._cfg(7), args, (1, 1))) == 7
