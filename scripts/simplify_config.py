"""
scripts/simplify_config.py
把旧版 config.json（config_version <= 2）对齐到当前 v3 结构并瘦身。

做两件事：
1. 结构对齐：版本号升到 3、补上 tabiai 段与 config_sync.writeback_url
2. 瘦身：删掉账号里「与默认值相同」和「对当前登录方式无意义」的字段

只删冗余，不动任何有效值：cookie / user_id / 非默认的 checkin_path 一律原样保留。
顺带体检一遍凭据，把不会签到成功的账号点出来（不自动改动它们）。

用法：
    python scripts/simplify_config.py 输入.json [-o 输出.json]
    python scripts/simplify_config.py 输入.json --check      # 只体检不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CURRENT_VERSION = 3

# 账号级可省略字段：值等于这里的默认值时删掉，两端读取时都会兜底成同一个值
ACCOUNT_DEFAULTS = {
    "browser_path": "/dashboard",
    "proxy": None,
    "checkin_path": None,
}

# GitHub OAuth 已不是登录方式，这两个字段只在 tabiai 下作签发原料；
# 非 tabiai 账号上留着全空的它们纯属噪音
TABIAI_ONLY_FIELDS = ("github_user_session", "github_client_id")

TABIAI_SECTION = {
    "enabled": False,
    "cdp_url": "http://127.0.0.1:9222",
    "token_timeout": 120,
    "token_interval_minutes": 21,
    "keep_page": False,
}


def simplify_account(account: dict, stats: dict) -> dict:
    """瘦身单个账号；返回新 dict，键序保持原样以便 diff 时好读。"""
    method = str(account.get("login_method") or "newapi_cookie").strip().lower()
    # 旧值归一化：v3 起 GitHub OAuth 并入 tabiai
    if method in ("github_cookie", "github", "github-cookie"):
        method = "tabiai"
        stats["login_method_migrated"] += 1

    out: dict = {}
    for key, value in account.items():
        if key == "login_method":
            out[key] = method
            continue
        # 非 tabiai 账号上的空签发原料字段直接扔掉
        if key in TABIAI_ONLY_FIELDS:
            if method != "tabiai" and not str(value or "").strip():
                stats["dropped_fields"] += 1
                continue
            out[key] = value
            continue
        if key in ACCOUNT_DEFAULTS and value == ACCOUNT_DEFAULTS[key]:
            stats["dropped_fields"] += 1
            continue
        out[key] = value
    # 老配置可能整个缺 login_method；显式补上，和平台保存的格式保持一致
    if "login_method" not in out:
        out["login_method"] = method
    return out


def audit_account(account: dict) -> str:
    """凭据体检：返回问题描述，没问题返回空串。不修改配置。"""
    cookie = str(account.get("cookie") or "").strip()
    method = str(account.get("login_method") or "newapi_cookie").strip().lower()
    if not cookie:
        return "凭据为空，本轮会被跳过"
    if method == "tabiai":
        # tabiai 存的是 new_api_refresh 的值，形如 sid.secret
        value = cookie.split("new_api_refresh=", 1)[-1].strip()
        if "." not in value:
            return "不像 new_api_refresh（缺少 sid.secret 的点号分隔）"
        return ""
    # newapi_cookie 必须是 name=value 形式，裸串会被 Cookie 解析器整条丢弃
    if "=" not in cookie:
        return "不是 name=value 形式的 Cookie，解析后为空，等同于没填"
    return ""


def unwrap_payload(value):
    """剥掉平台接口的外层包裹，拿到真正的配置对象。

    GET /api/export 返回 {"json": "<配置的 JSON 字符串>"}，
    导入格式是 {"config": {...}}，网页上复制出来的又是裸配置。
    三种都直接接受，省得用户先手工拆一层。
    """
    for _ in range(3):  # 最多剥三层，避免畸形输入把自己绕进去
        if not isinstance(value, dict):
            return value
        if "accounts" in value or "config_version" in value:
            return value
        inner = value.get("json", value.get("config"))
        if inner is None:
            return value
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                return value
        value = inner
    return value


def simplify(raw: dict) -> tuple[dict, dict]:
    """返回 (简化后的配置, 统计信息)。"""
    stats = {"dropped_fields": 0, "login_method_migrated": 0, "added_sections": []}
    cfg = json.loads(json.dumps(raw))  # 深拷贝，不动入参

    old_version = cfg.get("config_version")
    cfg["config_version"] = CURRENT_VERSION

    accounts = cfg.get("accounts")
    if isinstance(accounts, list):
        cfg["accounts"] = [
            simplify_account(a, stats) if isinstance(a, dict) else a for a in accounts
        ]

    if "tabiai" not in cfg:
        cfg["tabiai"] = dict(TABIAI_SECTION)
        stats["added_sections"].append("tabiai")

    sync = cfg.get("config_sync")
    if isinstance(sync, dict) and "writeback_url" not in sync:
        sync["writeback_url"] = ""
        stats["added_sections"].append("config_sync.writeback_url")

    stats["version"] = f"{old_version} -> {CURRENT_VERSION}"
    return cfg, stats


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，中文输出会抛 UnicodeEncodeError 或被管道截断
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="把旧 config.json 对齐到 v3 并瘦身")
    parser.add_argument("input", help="输入配置文件路径，用 - 表示从标准输入读")
    parser.add_argument("-o", "--output", help="输出路径（默认在输入同目录加 .v3 后缀）")
    parser.add_argument("--check", action="store_true", help="只体检与统计，不写文件")
    args = parser.parse_args(argv)

    from_stdin = args.input == "-"
    if from_stdin and not args.output and not args.check:
        print("从标准输入读时必须用 -o 指定输出路径（或加 --check）", file=sys.stderr)
        return 2

    src = Path(args.input)
    try:
        text_in = sys.stdin.read() if from_stdin else src.read_text(encoding="utf-8")
        raw = unwrap_payload(json.loads(text_in))
    except (OSError, ValueError) as exc:
        print(f"读取失败: {exc}", file=sys.stderr)
        return 2
    if not isinstance(raw, dict):
        print("配置根节点必须是对象", file=sys.stderr)
        return 2

    cfg, stats = simplify(raw)
    accounts = [a for a in cfg.get("accounts", []) if isinstance(a, dict)]

    print(f"config_version: {stats['version']}")
    print(f"账号数: {len(accounts)}")
    print(f"删除冗余字段: {stats['dropped_fields']} 处")
    if stats["login_method_migrated"]:
        print(f"login_method 归一化为 tabiai: {stats['login_method_migrated']} 个")
    if stats["added_sections"]:
        print(f"补齐缺失项: {', '.join(stats['added_sections'])}")

    problems = [(a.get("name", "?"), audit_account(a)) for a in accounts]
    problems = [(n, p) for n, p in problems if p]
    if problems:
        print(f"\n凭据体检：{len(problems)} 个账号当前不会签到成功（未自动改动）")
        for name, problem in problems:
            print(f"  - {name}: {problem}")
    else:
        print("\n凭据体检：全部账号都有可用形态的凭据")

    if args.check:
        return 0

    dst = Path(args.output) if args.output else src.with_suffix(".v3.json")
    before = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")))
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    after = len(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")))
    print(f"\n已写出: {dst}")
    print(f"紧凑体积: {before} -> {after} 字符（省 {before - after}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


