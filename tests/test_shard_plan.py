"""tests/test_shard_plan.py

--shard-plan：把启用账号切成 Actions matrix 用的分片名单。

这一步错了整轮就白跑：漏掉的账号今天不会被签，重叠的账号会被两个 job 同时签而撞上
彼此的凭据轮转。所以边界都要盯住 —— 停用账号、余数分片、空名单、名字里带逗号。
"""
import json
from types import SimpleNamespace

import pytest

import main


def _cfg(names, enabled_all=True):
    """造一个只带 accounts 的假配置，select() 行为与真 Config 一致。"""
    accounts = [
        SimpleNamespace(name=n, enabled=(enabled_all or not n.endswith("_off")))
        for n in names
    ]

    def select(picked=None):
        if not picked:
            return [a for a in accounts if a.enabled]
        wanted = {p for p in picked}
        return [a for a in accounts if a.name in wanted]

    return SimpleNamespace(accounts=accounts, select=select)


def _run(monkeypatch, tmp_path, cfg, size, account=None):
    """把落盘位置指到 tmp_path，返回 (退出码, 计划字典或 None)。"""
    out = tmp_path / "shard-plan.json"
    monkeypatch.setattr(main, "SHARD_PLAN_PATH", out)
    args = SimpleNamespace(account=account)
    code = main._write_shard_plan(cfg, args, size)
    if not out.exists():
        return code, None
    return code, json.loads(out.read_text(encoding="utf-8"))


class TestGrouping:
    def test_splits_by_size_with_remainder(self, monkeypatch, tmp_path):
        code, plan = _run(monkeypatch, tmp_path, _cfg([f"A{i}" for i in range(1, 70)]), 30)
        assert code == 0
        assert plan["count"] == 69
        assert plan["total"] == 3
        sizes = [len(s["accounts"].split(",")) for s in plan["shards"]]
        assert sizes == [30, 30, 9]

    def test_exact_multiple_has_no_empty_tail(self, monkeypatch, tmp_path):
        code, plan = _run(monkeypatch, tmp_path, _cfg([f"A{i}" for i in range(60)]), 30)
        assert code == 0
        assert plan["total"] == 2
        assert [len(s["accounts"].split(",")) for s in plan["shards"]] == [30, 30]

    def test_fewer_accounts_than_size(self, monkeypatch, tmp_path):
        code, plan = _run(monkeypatch, tmp_path, _cfg(["A", "B"]), 30)
        assert code == 0
        assert plan["total"] == 1
        assert plan["shards"][0]["accounts"] == "A,B"

    def test_shards_are_indexed_from_one(self, monkeypatch, tmp_path):
        """matrix 里的 index 直接拿来做 job 名和 artifact 后缀，从 1 起算更好读。"""
        _, plan = _run(monkeypatch, tmp_path, _cfg([f"A{i}" for i in range(5)]), 2)
        assert [s["index"] for s in plan["shards"]] == [1, 2, 3]

    def test_no_account_is_lost_or_duplicated(self, monkeypatch, tmp_path):
        names = [f"A{i}" for i in range(97)]
        _, plan = _run(monkeypatch, tmp_path, _cfg(names), 30)
        flat = [n for s in plan["shards"] for n in s["accounts"].split(",")]
        assert flat == names          # 顺序也要保持，便于对着配置排查
        assert len(set(flat)) == 97

    def test_disabled_accounts_are_excluded(self, monkeypatch, tmp_path):
        cfg = _cfg(["A", "B_off", "C"], enabled_all=False)
        _, plan = _run(monkeypatch, tmp_path, cfg, 30)
        assert plan["count"] == 2
        assert plan["shards"][0]["accounts"] == "A,C"

    def test_account_filter_narrows_the_plan(self, monkeypatch, tmp_path):
        """--account 和 --shard-plan 一起用时，只对被选中的那批账号分片。"""
        cfg = _cfg(["A", "B", "C", "D"])
        _, plan = _run(monkeypatch, tmp_path, cfg, 2, account=["A,C"])
        assert plan["count"] == 2
        assert plan["shards"][0]["accounts"] == "A,C"


class TestRejections:
    def test_comma_in_name_is_rejected(self, monkeypatch, tmp_path):
        """名单是靠 --account A,B 传到各 job 的，名字里带逗号会在途中被拆散。

        与其让某个 job 在半夜报「找不到账号」，不如在算计划这一步就停下来。
        """
        cfg = _cfg(["正常", "带,逗号"])
        code, plan = _run(monkeypatch, tmp_path, cfg, 30)
        assert code == 2
        assert plan is None      # 拒绝时不该留下半成品计划文件

    @pytest.mark.parametrize("size", [0, -1])
    def test_non_positive_size_is_rejected(self, monkeypatch, tmp_path, size):
        code, plan = _run(monkeypatch, tmp_path, _cfg(["A"]), size)
        assert code == 2
        assert plan is None


class TestEmptyRoster:
    def test_no_enabled_account_writes_empty_plan(self, monkeypatch, tmp_path):
        """一个账号都没启用不算错误：workflow 会靠 count == 0 跳过签到 job。

        这里仍然要把文件写出来，否则 plan job 读不到文件会直接报错，
        看上去像配置坏了。
        """
        cfg = _cfg(["A_off", "B_off"], enabled_all=False)
        code, plan = _run(monkeypatch, tmp_path, cfg, 30)
        assert code == 0
        assert plan == {"count": 0, "total": 0, "shards": []}


class TestParser:
    def test_shard_plan_parses_as_int(self):
        args = main.build_parser().parse_args(["--shard-plan", "30"])
        assert args.shard_plan == 30

    def test_absent_by_default(self):
        """不传就必须是 None：签到路径靠 `is not None` 判断，0 也得是合法输入。"""
        assert main.build_parser().parse_args([]).shard_plan is None
