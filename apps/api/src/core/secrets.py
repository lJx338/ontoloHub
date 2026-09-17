"""对称加密工具 — 用于连接器 ``config`` 中敏感字段落盘加密。

设计要点：

- 使用 :class:`cryptography.fernet.Fernet`（AES-128-CBC + HMAC-SHA256，标准做法）。
- ``Fernet`` 需要的 32 字节 url-safe base64 key 从 ``Settings.security.secret_key``
  通过 SHA-256 派生。生产环境应使用 32 字节随机 key（通过 ``Fernet.generate_key()``
  生成后写到 KMS / 环境变量）。
- 加密结果带 ``enc:v1:`` 前缀，方便以后切换算法时识别版本。
- ``encrypt_secret_fields(config, fields)`` 只加密指定字段；返回值是**新** dict，
  不会 mutate 入参。
"""
from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache
from typing import Any, Iterable, Mapping

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings

logger = logging.getLogger(__name__)

_VERSION = "v1"
_PREFIX = f"enc:{_VERSION}:"


def _derive_key(secret: str) -> bytes:
    """把任意长度的 ``secret`` 派生为 Fernet 兼容的 32 字节 url-safe base64 key。"""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(_derive_key(settings.security.secret_key))


def encrypt_value(plaintext: str) -> str:
    """加密一个字符串，返回带 ``enc:v1:`` 前缀的密文。"""
    if plaintext is None:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext  # 已经是密文，避免重复加密
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return f"{_PREFIX}{token.decode('ascii')}"


def decrypt_value(ciphertext: str) -> str:
    """解密带 ``enc:v1:`` 前缀的密文；未带前缀视为明文（向前兼容）。"""
    if ciphertext is None:
        return ciphertext
    if not ciphertext.startswith(_PREFIX):
        return ciphertext
    token = ciphertext[len(_PREFIX):].encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except InvalidToken as e:
        # 密钥变了或密文损坏 — 返回原文并打 warning，避免整个请求 500
        logger.warning("failed to decrypt field (key rotated?): %s", e)
        return ciphertext


def encrypt_secret_fields(
    config: Mapping[str, Any],
    fields: Iterable[str],
) -> dict[str, Any]:
    """只加密 ``fields`` 里列出的字段。返回值是新 dict。"""
    out = dict(config or {})
    for name in fields or ():
        if name in out and out[name] is not None:
            out[name] = encrypt_value(str(out[name]))
    return out


def decrypt_secret_fields(
    config: Mapping[str, Any],
    fields: Iterable[str],
) -> dict[str, Any]:
    """``encrypt_secret_fields`` 的逆操作。"""
    out = dict(config or {})
    for name in fields or ():
        if name in out and isinstance(out[name], str) and out[name].startswith(_PREFIX):
            out[name] = decrypt_value(out[name])
    return out


def mask_secret_fields(
    config: Mapping[str, Any],
    fields: Iterable[str],
) -> dict[str, Any]:
    """把 secret 字段替换成 ``***``（API 返回给前端用，避免泄漏）。"""
    out = dict(config or {})
    for name in fields or ():
        if name in out and out[name] is not None:
            out[name] = "***"
    return out
