"""配置文档读写：兼容明文 JSON，并支持完整配置的加密保存。"""

from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import (
    CONFIG_FILE,
    SecurityConfig,
    build_config,
    read_config_document,
    resolve_encrypted_config_path,
)
from .secure_config import ConfigEncryptionError, config_key_from_environment, decrypt_file, encrypt_file


@dataclass
class ConfigDocument:
    path: Path
    bootstrap: dict
    raw: dict
    security: SecurityConfig

    @property
    def encrypted(self) -> bool:
        return self.security.encryption_enabled

    @property
    def encrypted_path(self) -> Path:
        return resolve_encrypted_config_path(self.path, self.security)

    def safe_meta(self) -> dict:
        return {
            "path": str(self.path),
            "encrypted": self.encrypted,
            "encrypted_file": str(self.encrypted_path),
            "key_configured": bool(self.security.config_key or os.environ.get("CHECKIN_CONFIG_KEY")),
            "accounts": [
                {
                    "name": str(item.get("name") or ""),
                    "url": str(item.get("url") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "has_cookie": bool(item.get("cookie")),
                    "has_proxy": bool(item.get("proxy")),
                    "user_id": item.get("user_id", item.get("userId")),
                }
                for item in (self.raw.get("accounts") or [])
                if isinstance(item, dict)
            ],
        }


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _backup(path: Path) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))


def _key(security: SecurityConfig, key: Optional[str] = None) -> str:
    value = (key if key is not None else config_key_from_environment(security.config_key)).strip()
    if len(value) < 8:
        raise ConfigEncryptionError("配置密钥至少需要 8 个字符")
    return value


def _payload_without_security(raw: dict) -> dict:
    payload = copy.deepcopy(raw)
    payload.pop("security", None)
    return payload


def load_document(path: Optional[Path] = None) -> ConfigDocument:
    target = path or Path(os.environ.get("CHECKIN_CONFIG") or CONFIG_FILE)
    bootstrap, raw, security = read_config_document(target)
    return ConfigDocument(path=target, bootstrap=bootstrap, raw=raw, security=security)


def save_document(
    raw: dict,
    path: Optional[Path] = None,
    *,
    encryption_enabled: Optional[bool] = None,
    key: Optional[str] = None,
) -> ConfigDocument:
    """校验并保存配置；加密时 config.json 只保留 security bootstrap。"""
    target = path or Path(os.environ.get("CHECKIN_CONFIG") or CONFIG_FILE)
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是对象")
    candidate = copy.deepcopy(raw)
    existing_security = SecurityConfig.from_raw(candidate.get("security"))
    if target.exists():
        try:
            current = load_document(target)
            if not candidate.get("security"):
                existing_security = current.security
        except Exception:
            current = None
    else:
        current = None

    enabled = existing_security.encryption_enabled if encryption_enabled is None else bool(encryption_enabled)
    candidate_security = SecurityConfig(
        encryption_enabled=enabled,
        config_key=(key if key is not None else existing_security.config_key).strip(),
        encrypted_file=existing_security.encrypted_file,
    )
    candidate["security"] = candidate_security.to_dict()
    # 使用现有解析器校验 URL、账号、AI 配置等业务字段；加密元数据不影响校验。
    build_config(candidate, source=target)

    encrypted_path = resolve_encrypted_config_path(target, candidate_security)
    _backup(target)
    if enabled:
        actual_key = _key(candidate_security, key)
        _backup(encrypted_path)
        encrypt_file(_payload_without_security(candidate), encrypted_path, actual_key)
        bootstrap = {"security": candidate_security.to_dict()}
        _atomic_json(target, bootstrap)
    else:
        _atomic_json(target, candidate)
        if encrypted_path != target and encrypted_path.exists():
            _backup(encrypted_path)
            try:
                encrypted_path.unlink()
            except OSError:
                pass
    return load_document(target)


def set_encryption(path: Optional[Path], enabled: bool, key: Optional[str] = None) -> ConfigDocument:
    document = load_document(path)
    return save_document(document.raw, document.path, encryption_enabled=enabled, key=key)


def change_key(path: Optional[Path], new_key: str, old_key: Optional[str] = None) -> ConfigDocument:
    document = load_document(path)
    if not document.encrypted:
        return save_document(document.raw, document.path, encryption_enabled=True, key=new_key)
    # 先使用当前配置/环境变量成功解密，再用新密钥原子重写。
    _key(document.security, old_key)
    return save_document(document.raw, document.path, encryption_enabled=True, key=new_key)


def export_plain(path: Optional[Path], destination: Path) -> Path:
    document = load_document(path)
    raw = copy.deepcopy(document.raw)
    raw["security"] = {"encryption_enabled": False, "config_key": "", "encrypted_file": document.security.encrypted_file}
    _backup(destination)
    _atomic_json(destination, raw)
    return destination


def import_json(source: Path, target: Optional[Path], *, encrypt: bool, key: Optional[str] = None) -> ConfigDocument:
    try:
        incoming = json.loads(source.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigEncryptionError(f"读取导入文件失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigEncryptionError(f"导入文件不是合法 JSON: {exc}") from exc
    if isinstance(incoming, dict) and incoming.get("algorithm") == "AES-256-GCM":
        if not key:
            raise ConfigEncryptionError("导入的是加密 JSON，请提供解密密钥")
        incoming = decrypt_file(source, key)
    if not isinstance(incoming, dict):
        raise ConfigEncryptionError("导入配置根节点必须是对象")
    return save_document(incoming, target, encryption_enabled=encrypt, key=key)
