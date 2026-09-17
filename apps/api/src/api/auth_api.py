"""认证引导 API（HIA-51 M1-01）。

``POST /api/auth/bootstrap`` — 一键 bootstrap 管理员（无 header 时的fallback）。
放在独立的 router 以保持 ``/api/auth`` 路径层级。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import User
from src.api.auth import ensure_bootstrap_admin

router = APIRouter(prefix="/auth", tags=["auth"])


class BootstrapResponse(BaseModel):
    id: str
    email: str
    display_name: str
    global_role: str
    is_active: bool

    model_config = {"from_attributes": True}


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap_admin(
    session: AsyncSession = Depends(get_session),
) -> BootstrapResponse:
    """Create the bootstrap admin if missing; idempotent."""
    admin = await ensure_bootstrap_admin()
    return BootstrapResponse(
        id=str(admin.id),
        email=admin.email,
        display_name=admin.display_name,
        global_role=admin.global_role,
        is_active=admin.is_active,
    )
