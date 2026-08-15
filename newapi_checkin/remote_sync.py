"""远程配置 API 获取、AES-GCM 解密和本地保存（保留本地加密状态）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

from curl_cffi import requests as cffi

from . import logger as log
from .config import (
    ConfigError,
    ConfigSyncConfig,
    migrate_legacy,
    load_config,
)
from .config_store import load_document, save_document
from .secure_config import ConfigEncryptionError, config_key_from_environment, decrypt_json
from .utils import sanitize_header_value


class RemoteSyncError(ValueError):
    """远程配置同步失败。"""


# 远端永远不能覆盖的本地模块：同步设置和密钥必须由本地控制，
# 否则覆盖后可能再也无法同步/解密。
_LOCAL_CONTROLLED_KEYS = frozenset({"security", "config_sync"})
# 参与合并的业务模块（缺失告警用）。
_SYNC_MODULES = ("accounts", "ai", "browser", "http", "defaults", "proxy_pool", "notify")


def _merge_payload(local_raw: dict, payload: dict) -> dict:
    """把远端 payload 合并到本地配置。

    - 远端没带的顶级模块保留本地（缺键不动）。
    - 远端显式给出的值（包括显式空数组）按远端意图覆盖。
    - security / config_sync 永远由本地控制。
    """
    merged = copy.deepcopy(local_raw)
    for key, value in payload.items():
        if key in _LOCAL_CONTROLLED_KEYS:
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def _missing_modules(local_raw: dict, payload: dict) -> list:
    """本地存在但远端没提供的业务模块（保留本地并告警）。"""
    return [key for key in _SYNC_MODULES if key in local_raw and key not in payload]


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _dig(value: Any, path: str) -> Any:
    current = value
    for part in (item.strip() for item in path.split(".") if item.strip()):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise RemoteSyncError(f"response_field 找不到字段: {path}")
    return current


def _is_envelope(value: Any) -> bool:
    return isinstance(value, dict) and value.get("algorithm") == "AES-256-GCM"


def _looks_like_config(value: Any) -> bool:
    return isinstance(value, dict) and any(
        key in value for key in ("accounts", "ai", "browser", "http", "defaults", "security", "config_sync")
    )


def _select_payload(value: Any, response_field: str = "") -> Any:
    value = _json_value(value)
    if response_field:
        return _json_value(_dig(value, response_field))
    if _is_envelope(value) or _looks_like_config(value):
        return value
    if isinstance(value, dict):
        for key in ("data", "payload", "config", "result", "encrypted"):
            if key in value:
                candidate = _select_payload(value[key])
                if _is_envelope(candidate) or _looks_like_config(candidate) or isinstance(candidate, list):
                    return candidate
    return value


def _decode_payload(value: Any, sync: ConfigSyncConfig, key: str) -> tuple[dict, bool]:
    candidate = _select_payload(value, sync.response_field)
    encrypted = False
    if _is_envelope(candidate):
        if not key:
            raise RemoteSyncError("远程响应是密文，但 security.config_key 为空")
        try:
            candidate = decrypt_json(candidate, key)
        except ConfigEncryptionError as exc:
            raise RemoteSyncError(str(exc)) from exc
        encrypted = True
        candidate = _select_payload(candidate)
    candidate = _json_value(candidate)
    if isinstance(candidate, list):
        candidate = migrate_legacy(candidate)
    if not isinstance(candidate, dict):
        raise RemoteSyncError("远程响应未解析出 JSON 配置对象")
    return copy.deepcopy(candidate), encrypted


def _headers(sync: ConfigSyncConfig) -> dict[str, str]:
    headers = {"Accept": "application/json, text/plain, */*"}
    for key, value in sync.headers.items():
        key = sanitize_header_value(key)
        if key:
            headers[key] = sanitize_header_value(value)
    if sync.token:
        prefix = sync.token_prefix.strip()
        value = f"{prefix} {sync.token}".strip()
        token_header = sanitize_header_value(sync.token_header)
        if token_header:
            headers[token_header] = sanitize_header_value(value)
    return headers


def _request(sync: ConfigSyncConfig):
    if not sync.url:
        raise RemoteSyncError("未配置远程配置 API URL")
    kwargs: dict[str, Any] = {
        "headers": _headers(sync),
        "timeout": sync.timeout,
    }
    if sync.method == "POST":
        if sync.body is not None:
            kwargs["json"] = sync.body
    elif isinstance(sync.body, dict) and sync.body:
        kwargs["params"] = sync.body
    try:
        response = cffi.request(sync.method, sync.url, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        raise RemoteSyncError(f"远程配置请求失败: {type(exc).__name__}: {exc}") from exc
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        text = str(getattr(response, "text", "") or "")[:200]
        raise RemoteSyncError(f"远程配置 API 返回 HTTP {status}: {text}")
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        text = str(getattr(response, "text", "") or "")[:500]
        try:
            return json.loads(text)
        except json.JSONDecodeError as json_exc:
            raise RemoteSyncError(f"远程配置 API 未返回合法 JSON: {text}") from json_exc


def sync_remote_config(
    path: Optional[Path] = None,
    *,
    force: bool = False,
    auto_only: bool = False,
) -> dict:
    """请求远程配置，解密后校验并按本地加密状态写回 config.json。"""
    try:
        document = load_document(path)
        sync = ConfigSyncConfig.from_raw(document.raw.get("config_sync"))
        if not force and (not sync.enabled or (auto_only and not sync.auto_before_checkin)):
            return {"ok": True, "operation": "sync_config", "skipped": True, "message": "远程配置自动同步未启用"}
        if not sync.url:
            return {"ok": False, "operation": "sync_config", "error": "未配置远程配置 API URL"}

        response_data = _request(sync)
        key = config_key_from_environment(document.security.config_key)
        payload, encrypted = _decode_payload(response_data, sync, key)

        merged = _merge_payload(document.raw, payload)
        missing = _missing_modules(document.raw, payload)
        if missing:
            log.warn(f"远程配置缺少本地模块: {', '.join(missing)}；保留本地设置")
        # 同步设置和密钥由本地控制，避免远端配置覆盖后无法再次同步。
        merged["config_sync"] = copy.deepcopy(document.raw.get("config_sync") or sync.to_dict())
        # 保留本地 security 加密状态：原配置加密时用原密钥继续加密保存，
        # 禁止同步后明文落盘或删除密文文件。
        saved = save_document(
            merged,
            document.path,
            encryption_enabled=document.security.encryption_enabled,
        )
        cfg = load_config(saved.path)
        return {
            "ok": True,
            "operation": "sync_config",
            "skipped": False,
            "encrypted_response": encrypted,
            "path": str(saved.path),
            "account_count": len(cfg.accounts),
            "message": "远程配置已获取并保存到本地",
        }
    except (ConfigError, OSError, ValueError) as exc:
        return {"ok": False, "operation": "sync_config", "error": str(exc)}
