"""tests/test_shard_report.py
分片结果落盘与合并的测试。

覆盖点：
  - 单片往返：写出去的 JSON 读回来字段不丢（含 quota 这种 object 字段）
  - 多片合并：按分片号升序拼接，download-artifact 套的子目录也能递归读到
  - 缺片：missing/complete/note 要如实反映，不能静默少人
  - 坏文件：一片坏了不许带走其他片的结果
  - dry_run：只有所有片都是连通性检查才算，混合时按真实签到处理
全程只碰 tmp_path，不发信、不读项目里的 config.json。
"""

import json

import pytest

from newapi_checkin import shard_report as sr
from newapi_checkin.logger import SummaryRow


def row(name="A", status="success", strategy="S1", detail="", quota=None):
    return SummaryRow(name, status, strategy, detail, quota)


def write(tmp_path, rel, rows, *, shard=None, dry_run=False):
    return sr.dump_shard_summary(tmp_path / rel, rows, shard=shard, dry_run=dry_run)


# --------------------------------------------------------------------------- #
# 单片往返
# --------------------------------------------------------------------------- #


class TestDump:
    def test_creates_parent_dirs(self, tmp_path):
        path = write(tmp_path, "a/b/shard-1.json", [row()], shard=(1, 2))
        assert path.exists()

    def test_payload_shape(self, tmp_path):
        path = write(tmp_path, "s.json", [row(quota="1.5万")], shard=(2, 3), dry_run=True)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"] == sr.SCHEMA_VERSION
        assert raw["shard"] == {"index": 2, "total": 3}
        assert raw["dry_run"] is True
        assert raw["rows"][0]["quota"] == "1.5万"

    def test_shard_none_allowed(self, tmp_path):
        """本地手动跑没有分片号，也要能落盘。"""
        raw = json.loads(write(tmp_path, "s.json", [row()]).read_text(encoding="utf-8"))
        assert raw["shard"] is None

    def test_round_trip_keeps_fields(self, tmp_path):
        write(tmp_path, "s.json", [row(name="站点A", status="failed",
                                       strategy="S3", detail="过盾超时", quota=42)],
              shard=(1, 1))
        merged = sr.merge_shard_summaries(tmp_path)
        got = merged.rows[0]
        assert (got.name, got.status, got.strategy, got.detail, got.quota) == \
            ("站点A", "failed", "S3", "过盾超时", 42)


# --------------------------------------------------------------------------- #
# 多片合并
# --------------------------------------------------------------------------- #


class TestMergeOrder:
    def test_sorted_by_shard_index(self, tmp_path):
        write(tmp_path, "z.json", [row(name="c")], shard=(3, 3))
        write(tmp_path, "a.json", [row(name="a")], shard=(1, 3))
        write(tmp_path, "m.json", [row(name="b")], shard=(2, 3))
        merged = sr.merge_shard_summaries(tmp_path)
        # 按分片号排，不是按文件名排（文件名是 z/a/m）
        assert [r.name for r in merged.rows] == ["a", "b", "c"]
        assert merged.present == [1, 2, 3]

    def test_reads_nested_dirs(self, tmp_path):
        """download-artifact 会按 artifact 名各套一层目录。"""
        write(tmp_path, "checkin-summary-1/s.json", [row(name="a")], shard=(1, 2))
        write(tmp_path, "checkin-summary-2/s.json", [row(name="b")], shard=(2, 2))
        assert [r.name for r in sr.merge_shard_summaries(tmp_path).rows] == ["a", "b"]

    def test_shardless_goes_last(self, tmp_path):
        write(tmp_path, "no-shard.json", [row(name="x")])
        write(tmp_path, "s1.json", [row(name="a")], shard=(1, 1))
        assert [r.name for r in sr.merge_shard_summaries(tmp_path).rows] == ["a", "x"]


# --------------------------------------------------------------------------- #
# 缺片
# --------------------------------------------------------------------------- #


class TestMissingShards:
    def test_complete_when_all_present(self, tmp_path):
        write(tmp_path, "s1.json", [row(name="a")], shard=(1, 2))
        write(tmp_path, "s2.json", [row(name="b")], shard=(2, 2))
        merged = sr.merge_shard_summaries(tmp_path)
        assert merged.complete and merged.missing == []
        assert "2 个分片" in merged.note() and "全部分片" in merged.note()

    def test_detects_missing_middle(self, tmp_path):
        write(tmp_path, "s1.json", [row(name="a")], shard=(1, 3))
        write(tmp_path, "s3.json", [row(name="c")], shard=(3, 3))
        merged = sr.merge_shard_summaries(tmp_path)
        assert merged.missing == [2] and not merged.complete
        note = merged.note()
        # 缺片必须在正文里说清楚，别让人以为这就是全部账号
        assert "缺第 2 片" in note and "没有出现在上表里" in note

    def test_expected_from_max_total(self, tmp_path):
        """只到一片时也要按它自报的 total 判缺，不能拿文件数当预期。"""
        write(tmp_path, "s2.json", [row(name="b")], shard=(2, 4))
        merged = sr.merge_shard_summaries(tmp_path)
        assert merged.expected == 4 and merged.missing == [1, 3, 4]

    def test_expected_falls_back_without_shard_info(self, tmp_path):
        """老格式/本地单跑没有 shard 字段时，不该算出一堆假缺片。"""
        write(tmp_path, "s.json", [row(name="a")])
        merged = sr.merge_shard_summaries(tmp_path)
        assert merged.missing == []

    def test_empty_dir(self, tmp_path):
        merged = sr.merge_shard_summaries(tmp_path / "nope")
        assert merged.rows == [] and merged.note() == ""


# --------------------------------------------------------------------------- #
# 坏文件与 dry_run
# --------------------------------------------------------------------------- #


class TestResilience:
    def test_broken_json_skipped(self, tmp_path):
        write(tmp_path, "s1.json", [row(name="a")], shard=(1, 2))
        (tmp_path / "broken.json").write_text("{不是 JSON", encoding="utf-8")
        merged = sr.merge_shard_summaries(tmp_path)
        assert [r.name for r in merged.rows] == ["a"]

    def test_wrong_shape_skipped(self, tmp_path):
        write(tmp_path, "s1.json", [row(name="a")], shard=(1, 2))
        (tmp_path / "weird.json").write_text('{"rows": "不是数组"}', encoding="utf-8")
        assert len(sr.merge_shard_summaries(tmp_path).rows) == 1

    def test_row_without_name_dropped(self, tmp_path):
        path = write(tmp_path, "s1.json", [row(name="a")], shard=(1, 1))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["rows"].append({"status": "success"})       # 没有 name，展示不了
        raw["rows"].append({"name": "b"})               # 没有 status，同理
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        assert [r.name for r in sr.merge_shard_summaries(tmp_path).rows] == ["a"]

    def test_unknown_field_ignored(self, tmp_path):
        path = write(tmp_path, "s1.json", [row(name="a")], shard=(1, 1))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["rows"][0]["future_field"] = "以后加的字段"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        assert sr.merge_shard_summaries(tmp_path).rows[0].name == "a"

    @pytest.mark.parametrize("flags,expected", [
        ((True, True), True),      # 全是连通性检查
        ((True, False), False),    # 混合：按真实签到算，免得主题误标
        ((False, False), False),
    ])
    def test_dry_run_merge(self, tmp_path, flags, expected):
        for i, flag in enumerate(flags, start=1):
            write(tmp_path, f"s{i}.json", [row(name=f"a{i}")],
                  shard=(i, len(flags)), dry_run=flag)
        assert sr.merge_shard_summaries(tmp_path).dry_run is expected


# --------------------------------------------------------------------------- #
# Runner 侧的交接：落盘模式不许自己发信
#
# 这是「一天只收一封邮件」的关键保证 —— 三个分片各发一封的老行为就是从这里来的。
# --------------------------------------------------------------------------- #


class TestRunnerHandoff:
    def _runner(self, **opts):
        from newapi_checkin import runner as runner_mod
        from newapi_checkin.config import build_config

        cfg = build_config({
            "notify": {"email": {
                "enabled": True, "smtp_host": "smtp.aliyun.com", "smtp_port": 465,
                "username": "u", "password": "p", "from_addr": "f@aliyun.com",
                "to_addrs": ["t@qq.com"],
            }},
            "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
        })
        r = runner_mod.Runner(cfg, runner_mod.RunOptions(
            use_browser=False, use_ai=False, **opts))
        r.summary.add(row(name="A"))
        return r

    @pytest.fixture()
    def spy(self, monkeypatch):
        """拦住真正的发信，只记录被调用了几次。"""
        from newapi_checkin import notify as nt

        calls = []
        monkeypatch.setattr(nt, "send_report",
                            lambda *a, **k: (calls.append(k), (True, "主题"))[1])
        return calls

    def test_no_mail_when_summary_out(self, tmp_path, spy):
        r = self._runner(summary_out=str(tmp_path / "s.json"), proxy_shard=(2, 3))
        r._send_notification()
        assert spy == []                      # 交给汇总步骤发，本片闭嘴

    def test_mail_sent_without_summary_out(self, spy):
        self._runner()._send_notification()
        assert len(spy) == 1                  # 本地/单 job 场景照旧自己发

    def test_dump_records_shard(self, tmp_path):
        path = tmp_path / "out" / "s.json"
        r = self._runner(summary_out=str(path), proxy_shard=(2, 3))
        r._dump_summary()
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["shard"] == {"index": 2, "total": 3}
        assert [x["name"] for x in raw["rows"]] == ["A"]

    def test_dump_noop_without_option(self, tmp_path):
        r = self._runner()
        r._dump_summary()
        assert list(tmp_path.iterdir()) == []

    def test_dump_failure_does_not_raise(self, tmp_path, monkeypatch):
        """落盘失败只能 WARN：这一轮真正的产出是签到，不能被写文件的错抹掉。"""
        from newapi_checkin import shard_report as mod

        monkeypatch.setattr(mod, "dump_shard_summary",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满")))
        self._runner(summary_out=str(tmp_path / "s.json"))._dump_summary()
