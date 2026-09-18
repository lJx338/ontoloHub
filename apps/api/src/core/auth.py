"""JWT + API Key 认证工具（HIA-64 B1）。

提供：
- ``hash_password`` / ``verify_password``：bcrypt 包装
- ``create_access_token`` / ``create_refresh_token``：JWT 签发
- ``decode_token``：JWT 验签 + 解析
- ``generate_api_key``：生成 ``ont_xxxxxx...`` 明文 key
- ``hash_api_key``：SHA-256 哈希 key（落盘用）
- ``authenticate_user``：邮箱 + 密码登录

设计原则：
- JWT 短 access + 长 refresh 双 token，refresh 仅用于换 access
- API Key 单独存哈希；前缀 ``ont_`` 方便识别
- 任何认证失败 → ``401 Unauthorized``；不返 403（避免泄漏 email 是否存在）
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.db.identity import User


# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------


def _bcrypt_context() -> CryptContext:
    """Lazy build — 避免 import 时一次性算 cost。"""
    rounds = get_settings().security.bcrypt_rounds
    return CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=rounds)


def hash_password(plain: str) -> str:
    """bcrypt hash；明文密码不入库。"""
    if not plain:
        raise ValueError("password cannot be empty")
    return _bcrypt_context().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码 vs bcrypt hash。"""
    if not plain or not hashed:
        return False
    try:
        return _bcrypt_context().verify(plain, hashed)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT 签发 / 验签
# ---------------------------------------------------------------------------


def create_access_token(*, subject: str, extra: Optional[dict[str, Any]] = None) -> str:
    """签发 access token (短 TTL)。"""
    return _create_token(
        subject=subject,
        extra=extra,
        ttl=get_settings().security.jwt_access_ttl_seconds,
        token_type="access",
    )


def create_refresh_token(*, subject: str) -> str:
    """签发 refresh token (长 TTL)。仅用于换 access，不能直接调用业务 API。"""
    return _create_token(
        subject=subject,
        extra=None,
        ttl=get_settings().security.jwt_refresh_ttl_seconds,
        token_type="refresh",
    )


def _create_token(
    *,
    subject: str,
    extra: Optional[dict[str, Any]],
    ttl: int,
    token_type: str,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,         # user_id (UUID 字符串)
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "type": token_type,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(
        payload,
        get_settings().security.effective_jwt_secret(),
        algorithm=get_settings().security.jwt_algorithm,
    )


def decode_token(token: str) -> dict[str, Any]:
    """验签 + 解码 JWT。

    Raises:
        JWTError: 验签失败 / 过期 / 类型不对
    """
    return jwt.decode(
        token,
        get_settings().security.effective_jwt_secret(),
        algorithms=[get_settings().security.jwt_algorithm],
    )


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str, str]:
    """生成 API Key。

    Returns:
        (plain_key, key_prefix, key_hash)
        - ``plain_key``: 明文 key（只在创建响应中返回一次）
        - ``key_prefix``: 前 ``ont_xxxxxxxx`` 用于列表展示
        - ``key_hash``: SHA-256 哈希（落盘用）
    """
    prefix = get_settings().security.api_key_prefix
    # 32 字节 = 256 bit 随机；总长度约 40 字符
    raw = secrets.token_urlsafe(32)
    plain = f"{prefix}{raw}"
    key_hash = hash_api_key(plain)
    # prefix marker: ``ont_xxxxxx``（前 12 字符）
    key_prefix = plain[:12]
    return plain, key_prefix, key_hash


def hash_api_key(plain: str) -> str:
    """SHA-256 哈希 API Key。"""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    """constant-time comparison — 避免 timing attack。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Authenticate user
# ---------------------------------------------------------------------------


async def authenticate_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
) -> Optional[User]:
    """邮箱 + 密码登录。

    Returns:
        认证成功时返回 User；失败（用户不存在 / 密码错 / 用户未激活）返回 None。
    """
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    if not user.password_hash:
        # bootstrap admin 阶段：用户没设密码不允许走 JWT 登录（强制设置密码流程）
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> Optional[User]:
    """按 ID 取用户。"""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """JWT / API Key 相关的可预期错误。"""

    def __init__(self, detail: str = "invalid credentials"):
        self.detail = detail
        super().__init__(detail)


__all__ = [
    "AuthError",
    "JWTError",
    "authenticate_user",
    "constant_time_eq",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "generate_api_key",
    "get_user_by_id",
    "hash_api_key",
    "hash_password",
    "verify_password",
]
