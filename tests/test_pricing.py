"""tests/test_pricing.py
定价表与额度总览的测试。

覆盖点：
  - 解析 /api/pricing 的真实字段（2026-08 实拉 gorouter.app / tabitoken.com 的结构）
  - opus 过滤：站点以后加了别的模型不该混进来
  - 按次计费（quota_type=1）用 model_price 精确算次数，向下取整
  - 按 token 计费（quota_type=0）不编次数，只标计费方式
  - 按站点聚合余额、同站点只拉一次定价、拉失败时降级不影响发信
全程不联网：定价用注入的 fetcher 假实现。
"""

import pytest

from newapi_checkin import pricing as pr
from newapi_checkin.logger import SummaryRow

# 实拉回来的结构，字段名与顺序照抄，别改成想象的样子
GOROUTER_PAYLOAD = {
    "success": True,
    "group_ratio": {"default": 1},
    "usable_group": {"default": "默认分组"},
    "data": [
        {"model_name": "claude-opus-5-thinking", "quota_type": 1, "model_ratio": 0,
         "model_price": 0.3, "completion_ratio": 0, "enable_groups": ["vip", "default"]},
        {"model_name": "claude-opus-5", "quota_type": 1, "model_ratio": 0,
         "model_price": 0.3, "completion_ratio": 0, "enable_groups": ["vip", "default"]},
        {"model_name": "claude-opus-4-8", "quota_type": 1, "model_ratio": 0,
         "model_price": 0.2, "completion_ratio": 0, "enable_groups": ["vip", "default"]},
    ],
}


def row(name="A", balance=None, unit=500000, site="https://gorouter.app"):
    return SummaryRow(name, "success", balance=balance, quota_per_unit=unit, site=site)


class TestParse:
    def test_real_payload(self):
        table = pr.parse_pricing(GOROUTER_PAYLOAD)
        assert [m.name for m in table.models] == [
            # 按单价升序：便宜的排前面，一眼看到最能撑次数的
            "claude-opus-4-8", "claude-opus-5", "claude-opus-5-thinking",
        ]
        assert table.group_ratio == {"default": 1}
        assert table.usable_groups == {"default": "默认分组"}

    def test_opus_filter_drops_others(self):
        payload = dict(GOROUTER_PAYLOAD)
        payload["data"] = GOROUTER_PAYLOAD["data"] + [
            {"model_name": "gpt-5.6", "quota_type": 1, "model_price": 0.1},
            {"model_name": "gemini-3-pro", "quota_type": 0, "model_ratio": 2},
        ]
        names = [m.name for m in pr.parse_pricing(payload).models]
        assert all("opus" in n for n in names) and len(names) == 3

    def test_custom_filter(self):
        payload = dict(GOROUTER_PAYLOAD)
        payload["data"] = [{"model_name": "gpt-5.6", "quota_type": 1, "model_price": 0.1}]
        assert [m.name for m in pr.parse_pricing(payload, ("gpt",)).models] == ["gpt-5.6"]

    def test_garbage_payload_is_empty_table(self):
        for bad in (None, {}, {"data": "不是数组"}, {"data": [1, "x", None]}):
            assert pr.parse_pricing(bad).models == []

    def test_row_without_name_skipped(self):
        table = pr.parse_pricing({"data": [{"quota_type": 1, "model_price": 0.3}]})
        assert table.models == []


class TestCalls:
    def _model(self, **kw):
        base = {"name": "claude-opus-5", "quota_type": 1, "price": 0.3}
        base.update(kw)
        return pr.ModelPrice(**base)

    @pytest.mark.parametrize("balance,price,expected", [
        (12.34, 0.3, 41),      # 41.13 -> 41，半次请求发不出去
        (12.34, 0.2, 61),
        (0.29, 0.3, 0),        # 不够一次
        (0, 0.3, 0),
        (100.0, 0.5, 200),
    ])
    def test_floor_division(self, balance, price, expected):
        assert self._model(price=price).calls_for(balance) == expected

    def test_group_ratio_multiplies_price(self):
        m = self._model(price=0.3)
        assert m.unit_price(2.0) == 0.6
        assert m.calls_for(12.34, 2.0) == 20

    def test_unknown_balance_is_none(self):
        assert self._model().calls_for(None) is None

    def test_per_token_model_has_no_call_price(self):
        m = self._model(quota_type=0, price=0, model_ratio=2.5)
        assert m.per_call is False
        assert m.unit_price() is None and m.calls_for(100.0) is None

    def test_zero_price_is_not_per_call(self):
        """单价 0 不能当按次计费用，否则要除零。"""
        assert self._model(price=0).calls_for(100.0) is None


class TestSummarizeBySite:
    def _fetcher(self, mapping, calls=None):
        def fetch(base):
            if calls is not None:
                calls.append(base)
            payload = mapping.get(base)
            return pr.parse_pricing(payload) if payload else None
        return fetch

    def test_groups_and_totals(self):
        rows = [
            row("Go-1", balance=6170000),                    # $12.34
            row("Go-2", balance=2500000),                    # $5.00
            row("Tabi-1", balance=1000000, site="https://tabitoken.com"),   # $2.00
        ]
        sites = pr.summarize_by_site(rows, None, fetcher=self._fetcher({}))
        assert [s.label for s in sites] == ["gorouter.app", "tabitoken.com"]
        assert round(sites[0].total_usd, 2) == 17.34
        assert round(sites[1].total_usd, 2) == 2.00

    def test_unknown_balance_counts_account_not_total(self):
        rows = [row("Go-1", balance=6170000), row("Go-2", balance=None)]
        site = pr.summarize_by_site(rows, None, fetcher=self._fetcher({}))[0]
        assert site.accounts == 2 and site.known == 1
        assert site.complete is False
        assert round(site.total_usd, 2) == 12.34

    def test_per_row_quota_per_unit(self):
        """换算率逐行走：同一封邮件里两个站点的换算率可以不同。"""
        rows = [row("A", balance=1000000, unit=250000)]      # 1000000/250000 = $4
        assert pr.summarize_by_site(rows, None, fetcher=self._fetcher({}))[0].total_usd == 4.0

    def test_missing_unit_falls_back_to_default(self):
        rows = [row("A", balance=1000000, unit=None)]        # 默认 500000 -> $2
        assert pr.summarize_by_site(rows, None, fetcher=self._fetcher({}))[0].total_usd == 2.0

    def test_fetches_once_per_site(self):
        calls = []
        rows = [row("Go-1", balance=1), row("Go-2", balance=1), row("Go-3", balance=1)]
        pr.summarize_by_site(rows, None, fetcher=self._fetcher({}, calls))
        assert calls == ["https://gorouter.app"]

    def test_rows_without_site_ignored(self):
        rows = [SummaryRow("无站点", "success", balance=1000000)]
        assert pr.summarize_by_site(rows, None, fetcher=self._fetcher({})) == []

    def test_fetch_failure_keeps_balance(self):
        """拉定价炸了也要保住余额汇总，只是少了次数那几行。"""
        def boom(_base):
            raise RuntimeError("被盾拦了")

        site = pr.summarize_by_site([row("A", balance=6170000)], None, fetcher=boom)[0]
        assert site.table is None and round(site.total_usd, 2) == 12.34
        assert site.rows() == []

    def test_calls_use_site_total(self):
        rows = [row("Go-1", balance=6170000), row("Go-2", balance=2500000)]   # 合计 $17.34
        site = pr.summarize_by_site(
            rows, None, fetcher=self._fetcher({"https://gorouter.app": GOROUTER_PAYLOAD}))[0]
        table = dict((name, calls) for name, _unit, calls in site.rows("default"))
        assert table["claude-opus-4-8"] == "86 次"       # 17.34 / 0.2
        assert table["claude-opus-5"] == "57 次"         # 17.34 / 0.3

    def test_per_token_row_shows_no_count(self):
        payload = {"data": [{"model_name": "claude-opus-x", "quota_type": 0,
                            "model_ratio": 2.5, "completion_ratio": 3}]}
        site = pr.summarize_by_site(
            [row("A", balance=6170000)], None,
            fetcher=self._fetcher({"https://gorouter.app": payload}))[0]
        name, unit, calls = site.rows("default")[0]
        assert "按 token 计费" in unit and calls == "—"
