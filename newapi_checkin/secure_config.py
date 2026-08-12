"""JSON 配置的 AES-GCM 加密、解密和密钥轮换。"""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ENVELOPE_VERSION = 1
ALGORITHM = "AES-256-GCM"
KDF_NAME = "PBKDF2-HMAC-SHA256"
DEFAULT_ITERATIONS = 390_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
AAD = f"newapi-checkin-config:v{ENVELOPE_VERSION}".encode("ascii")


class ConfigEncryptionError(ValueError):
    """配置加解密失败。"""


def _require_key(key: str) -> bytes:
    if not isinstance(key, str) or len(key.strip()) < 8:
        raise ConfigEncryptionError("配置密钥至少需要 8 个字符")
    return key.encode("utf-8")


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64_decode(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ConfigEncryptionError(f"加密配置缺少 {field}")
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigEncryptionError(f"加密配置字段 {field} 不是合法 base64") from exc


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    if iterations < 100_000 or iterations > 5_000_000:
        raise ConfigEncryptionError("加密配置的 PBKDF2 iterations 不在允许范围")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(_require_key(password))


def encrypt_json(payload: Any, key: str, iterations: int = DEFAULT_ITERATIONS) -> dict:
    """把 JSON 可序列化对象封装成加密 envelope。"""
    try:
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConfigEncryptionError(f"配置无法序列化为 JSON: {exc}") from exc

    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    derived = _derive_key(key, salt, iterations)
    ciphertext = AESGCM(derived).encrypt(nonce, plaintext, AAD)
    return {
        "version": ENVELOPE_VERSION,
        "algorithm": ALGORITHM,
        "kdf": KDF_NAME,
        "iterations": iterations,
        "salt": _b64_encode(salt),
        "nonce": _b64_encode(nonce),
        "ciphertext": _b64_encode(ciphertext),
    }


def decrypt_json(envelope: dict, key: str) -> Any:
    """解密并解析 JSON envelope。"""
    if not isinstance(envelope, dict):
        raise ConfigEncryptionError("加密配置根节点必须是对象")
    if envelope.get("version") != ENVELOPE_VERSION:
        raise ConfigEncryptionError(f"不支持的加密配置版本: {envelope.get('version')!r}")
    if envelope.get("algorithm") != ALGORITHM or envelope.get("kdf") != KDF_NAME:
        raise ConfigEncryptionError("不支持的加密配置算法")
    try:
        iterations = int(envelope.get("iterations"))
    except (TypeError, ValueError) as exc:
        raise ConfigEncryptionError("加密配置 iterations 无效") from exc
    salt = _b64_decode(envelope.get("salt"), "salt")
    nonce = _b64_decode(envelope.get("nonce"), "nonce")
    ciphertext = _b64_decode(envelope.get("ciphertext"), "ciphertext")
    if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES or len(ciphertext) < 16:
        raise ConfigEncryptionError("加密配置字段长度不正确")
    try:
        derived = _derive_key(key, salt, iterations)
        plaintext = AESGCM(derived).decrypt(nonce, ciphertext, AAD)
        return json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigEncryptionError("配置密钥错误，或加密 JSON 已被篡改/损坏") from exc


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def encrypt_file(payload: Any, path: Path, key: str, iterations: int = DEFAULT_ITERATIONS) -> None:
    _write_json_atomic(path, encrypt_json(payload, key, iterations=iterations))


def decrypt_file(path: Path, key: str) -> Any:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigEncryptionError(f"读取加密配置失败: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigEncryptionError(f"加密配置不是合法 JSON: {path}") from exc
    return decrypt_json(envelope, key)


def rotate_key(source_path: Path, old_key: str, new_key: str, target_path: Path | None = None) -> None:
    """使用新密钥重加密，默认原子替换原文件。"""
    payload = decrypt_file(source_path, old_key)
    encrypt_file(payload, target_path or source_path, new_key)


def config_key_from_environment(default: str = "") -> str:
    return os.environ.get("CHECKIN_CONFIG_KEY", default).strip()
