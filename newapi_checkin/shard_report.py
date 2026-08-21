"""newapi_checkin/shard_report.py
分片结果的落盘与合并。

Actions 把账号切成多片并行签到时，每片是独立进程，各自只知道自己那几个账号的结果。
如果每片跑完都自己发一封邮件，一天就会收到 N 封各含一部分账号的信 —— 于是改成：
  1. 每片用 --summary-out 把自己的汇总行写成一个 JSON；
  2. 汇总 job 下载全部 JSON，用 --send-summary 合并后只发一封完整的邮件。

分片号记在文件内容里而不是只靠文件名：artifact 下载后目录结构可能被套一层，
靠内容认片比靠路径解析稳。合并时还会算出「预期 N 片、实到哪几片」，缺片直接写进
邮件正文 —— 分片 job 被 OOM/超时打断时不能让那批账号悄无声息地消失。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import logger as log
from .logger import SummaryRow

# 文件格式版本。以后加字段时靠它判断兼容性，读到未来的版本只 WARN 不炸
SCHEMA_VERSION = 1
# SummaryRow 的字段白名单：反序列化只认这几个，多出来的字段直接丢。
# balance/quota_per_unit 必须在列表里 —— 少一个，分片汇总出来的邮件额度列就全是 -
_ROW_FIELDS = ("name", "status", "strategy", "detail", "quota", "balance", "quota_per_unit")


def _row_to_dict(row) -> dict:
    return {f: getattr(row, f, None) for f in _ROW_FIELDS}


def _row_from_dict(raw: dict) -> SummaryRow:
    """只取白名单字段，缺的用 SummaryRow 的默认值补。

    name/status 是必填：缺了这两个的行没法展示，交给调用方过滤掉。
    """
    kwargs = {f: raw[f] for f in _ROW_FIELDS if f in raw and raw[f] is not None}
    return SummaryRow(**kwargs)


def dump_shard_summary(path, rows: list, *, shard: Optional[tuple] = None,
                       dry_run: bool = False) -> Path:
    """把一片的汇总行写成 JSON，返回真实写入路径。父目录不存在会自动建。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCHEMA_VERSION,
        "shard": {"index": shard[0], "total": shard[1]} if shard else None,
        "dry_run": bool(dry_run),
        "rows": [_row_to_dict(r) for r in rows],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


class MergedSummary:
    """合并后的结果：拼好的 rows + 够不够齐的元信息。"""

    def __init__(self, rows: list, *, expected: int, present: list, dry_run: bool):
        self.rows = rows
        self.expected = expected          # 预期片数（取各片自报 total 的最大值）
        self.present = present            # 实到的分片号，已升序
        self.dry_run = dry_run

    @property
    def missing(self) -> list:
        """缺了哪几片。

        present 为空意味着没有任何一片自报过分片号（本地单跑、或老格式文件），
        这时无从判断谁缺席，返回空列表而不是凭 expected 硬算 —— 否则本地跑一次
        就会被说成「缺第 1 片」。
        """
        if self.expected <= 0 or not self.present:
            return []
        return [i for i in range(1, self.expected + 1) if i not in set(self.present)]

    @property
    def complete(self) -> bool:
        return not self.missing

    def note(self) -> str:
        """给邮件正文用的一句话说明。缺片时把话说重一点，别让人以为这就是全部。"""
        if self.expected <= 1 and not self.present:
            return ""
        if self.complete:
            return f"本次由 {self.expected} 个分片并行完成，上表已合并全部分片的结果。"
        missing = "、".join(str(i) for i in self.missing)
        return (
            f"注意：本次共 {self.expected} 个分片，只收到其中 {len(self.present)} 片的结果，"
            f"缺第 {missing} 片。缺失分片的账号没有出现在上表里（可能是该分片超时、"
            f"被中断或上传失败），请到 Actions 对应分片的日志里确认。"
        )


def _read_one(path: Path) -> Optional[dict]:
    """读一个分片文件。坏文件只 WARN 并跳过 —— 一片坏了不该带走其他片的结果。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 坏 JSON / 编码问题统一跳过
        log.warn(f"分片结果 {path.name} 读取失败，已跳过: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        log.warn(f"分片结果 {path.name} 结构不认识，已跳过")
        return None
    if raw.get("version", SCHEMA_VERSION) > SCHEMA_VERSION:
        log.warn(f"分片结果 {path.name} 来自更新的版本（v{raw.get('version')}），尽力解析")
    return raw


def merge_shard_summaries(directory) -> MergedSummary:
    """递归读目录下所有 *.json，按分片号升序合并成一份结果。

    递归是因为 download-artifact 会按 artifact 名再套一层目录；用 rglob 就不用关心
    到底套了几层。没有分片信息的文件排在最后，仍然计入 rows —— 本地手动跑出来的
    单份结果也能用这个入口发信。
    """
    root = Path(directory)
    files = sorted(root.rglob("*.json")) if root.exists() else []
    if not files:
        log.warn(f"{root} 下没有找到任何分片结果 JSON")
        return MergedSummary([], expected=0, present=[], dry_run=False)

    loaded = []
    for path in files:
        raw = _read_one(path)
        if raw is not None:
            loaded.append(raw)

    # 排序键：有分片号的按号排，没有的丢到最后，保证邮件里账号顺序稳定可预期
    def sort_key(item):
        shard = item.get("shard") or {}
        index = shard.get("index")
        return (0, int(index)) if isinstance(index, int) else (1, 0)

    loaded.sort(key=sort_key)

    rows, present, expected = [], [], 0
    for raw in loaded:
        shard = raw.get("shard") or {}
        index, total = shard.get("index"), shard.get("total")
        if isinstance(index, int):
            present.append(index)
        if isinstance(total, int):
            expected = max(expected, total)
        for item in raw["rows"]:
            if not isinstance(item, dict) or not item.get("name") or not item.get("status"):
                continue
            try:
                rows.append(_row_from_dict(item))
            except Exception as exc:  # noqa: BLE001 - 单行坏了不影响其他行
                log.warn(f"跳过一条无法解析的结果行: {type(exc).__name__}: {exc}")

    # 各片都没自报 total（老格式或本地单跑）时，用实到的片数兜底，免得算出一堆假缺片
    if expected <= 0:
        expected = len(present) or len(loaded)
    # dry_run 只在**所有**片都是连通性检查时才算：混合时按真实签到处理，
    # 否则会把真实签到的结果在主题里标成「连通性检查」，比漏标更误导人
    dry_run = bool(loaded) and all(bool(raw.get("dry_run")) for raw in loaded)
    return MergedSummary(rows, expected=expected, present=sorted(present), dry_run=dry_run)
