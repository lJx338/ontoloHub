"""Webhook Out & Trigger In API — HIA-75 C3.

Provides:
- WebhookConfig CRUD (outbound webhook subscriptions)
- WebhookDelivery query (delivery history / status)
- TriggerConfig CRUD (inbound webhook / schedule / object-change triggers)
- Inbound webhook receiver: POST /api/webhooks/in/{token}
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.webhook import (
    WebhookConfig,
    WebhookDelivery,
    WebhookEventType,
    WebhookDeliveryStatus,
    TriggerConfig,
    TriggerType,
    TriggerStatus,
)
from src.db.runtime import ActionType, ActionRun
from src.db.project import Project
from src.api.auth import get_current_user, require_project_role
from src.db.identity import Role
from src.services.webhook_dispatcher import (
    generate_webhook_token,
    generate_webhook_secret,
    build_event_payload,
    dispatch_webhook,
)

router = APIRouter(prefix="/projects", tags=["Webhooks"])
trigger_router = APIRouter(tags=["Triggers"])


# =============================================================================
# Helpers
# =============================================================================


def _get_iso(dt) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


async def _verify_project(session: AsyncSession, project_id: uuid.UUID) -> Project:
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


# =============================================================================
# Pydantic models — WebhookConfig
# =============================================================================


class WebhookConfigCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1, max_length=2048)
    events: list[str] = Field(..., description="Event types to subscribe to, e.g. ['cr.merged', 'release.published']")
    filter_expression: Optional[str] = Field(None, description="JSONata filter expression")
    secret: Optional[str] = Field(None, description="HMAC secret (auto-generated if omitted)")
    retry_count: int = Field(default=3, ge=0, le=10)
    retry_delay_seconds: int = Field(default=60, ge=1, le=3600)


class WebhookConfigUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    url: Optional[str] = Field(None, max_length=2048)
    events: Optional[list[str]] = None
    filter_expression: Optional[str] = None
    secret: Optional[str] = None
    retry_count: Optional[int] = Field(None, ge=0, le=10)
    retry_delay_seconds: Optional[int] = Field(None, ge=1, le=3600)
    is_enabled: Optional[bool] = None


class WebhookConfigResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    url: str
    events: list
    filter_expression: Optional[str]
    secret: str  # returned only on create/update, masked in list
    retry_count: int
    retry_delay_seconds: int
    is_enabled: bool
    total_deliveries: int
    failed_deliveries: int
    last_delivered_at: Optional[str]
    last_error: Optional[str]
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class WebhookDeliveryResponse(BaseModel):
    id: uuid.UUID
    webhook_config_id: uuid.UUID
    event_type: str
    status: str
    http_status_code: Optional[int]
    response_body: Optional[str]
    error_message: Optional[str]
    duration_ms: Optional[int]
    attempt: int
    max_attempts: int
    delivered_at: Optional[str]
    target_type: Optional[str]
    target_id: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


# =============================================================================
# WebhookConfig CRUD
# =============================================================================


@router.post(
    "/{project_id}/webhooks",
    response_model=WebhookConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook(
    project_id: uuid.UUID,
    data: WebhookConfigCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WebhookConfigResponse:
    """创建一个出站 Webhook 订阅。

    Webhook 在匹配的事件发生时向 ``url`` 发送 HTTP POST 请求，
    请求头包含 ``X-OntoloHub-Signature`` (HMAC-SHA256)。
    """
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    secret = data.secret or generate_webhook_secret()

    cfg = WebhookConfig(
        project_id=project_id,
        name=data.name,
        url=data.url,
        events=data.events,
        filter_expression=data.filter_expression,
        secret=secret,
        retry_count=data.retry_count,
        retry_delay_seconds=data.retry_delay_seconds,
        is_enabled=True,
        created_by=user.user.id if hasattr(user, "user") else None,
    )
    session.add(cfg)
    await session.flush()
    await session.refresh(cfg)

    return WebhookConfigResponse(
        id=cfg.id,
        project_id=cfg.project_id,
        name=cfg.name,
        url=cfg.url,
        events=cfg.events,
        filter_expression=cfg.filter_expression,
        secret=cfg.secret,
        retry_count=cfg.retry_count,
        retry_delay_seconds=cfg.retry_delay_seconds,
        is_enabled=cfg.is_enabled,
        total_deliveries=cfg.total_deliveries,
        failed_deliveries=cfg.failed_deliveries,
        last_delivered_at=_get_iso(cfg.last_delivered_at),
        last_error=cfg.last_error,
        created_by=cfg.created_by,
        created_at=_get_iso(cfg.created_at),
        updated_at=_get_iso(cfg.updated_at),
    )


@router.get("/{project_id}/webhooks", response_model=list[WebhookConfigResponse])
async def list_webhooks(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    include_disabled: bool = Query(False),
) -> list[WebhookConfigResponse]:
    """列出项目下所有 Webhook 配置（secret 仅在创建后返回一次）。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    query = select(WebhookConfig).where(WebhookConfig.project_id == project_id)
    if not include_disabled:
        query = query.where(WebhookConfig.is_enabled == True)

    result = await session.execute(query)
    configs = result.scalars().all()

    return [
        WebhookConfigResponse(
            id=c.id,
            project_id=c.project_id,
            name=c.name,
            url=c.url,
            events=c.events,
            filter_expression=c.filter_expression,
            secret="***REDACTED***",
            retry_count=c.retry_count,
            retry_delay_seconds=c.retry_delay_seconds,
            is_enabled=c.is_enabled,
            total_deliveries=c.total_deliveries,
            failed_deliveries=c.failed_deliveries,
            last_delivered_at=_get_iso(c.last_delivered_at),
            last_error=c.last_error,
            created_by=c.created_by,
            created_at=_get_iso(c.created_at),
            updated_at=_get_iso(c.updated_at),
        )
        for c in configs
    ]


@router.get("/{project_id}/webhooks/{webhook_id}", response_model=WebhookConfigResponse)
async def get_webhook(
    project_id: uuid.UUID,
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WebhookConfigResponse:
    """获取单个 Webhook 配置（secret 被遮蔽）。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    result = await session.execute(
        select(WebhookConfig).where(
            WebhookConfig.id == webhook_id,
            WebhookConfig.project_id == project_id,
        )
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="Webhook 不存在")

    return WebhookConfigResponse(
        id=cfg.id,
        project_id=cfg.project_id,
        name=cfg.name,
        url=cfg.url,
        events=cfg.events,
        filter_expression=cfg.filter_expression,
        secret="***REDACTED***",
        retry_count=cfg.retry_count,
        retry_delay_seconds=cfg.retry_delay_seconds,
        is_enabled=cfg.is_enabled,
        total_deliveries=cfg.total_deliveries,
        failed_deliveries=cfg.failed_deliveries,
        last_delivered_at=_get_iso(cfg.last_delivered_at),
        last_error=cfg.last_error,
        created_by=cfg.created_by,
        created_at=_get_iso(cfg.created_at),
        updated_at=_get_iso(cfg.updated_at),
    )


@router.patch("/{project_id}/webhooks/{webhook_id}", response_model=WebhookConfigResponse)
async def update_webhook(
    project_id: uuid.UUID,
    webhook_id: uuid.UUID,
    data: WebhookConfigUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> WebhookConfigResponse:
    """更新 Webhook 配置。更新 secret 时返回新 secret。"""
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    result = await session.execute(
        select(WebhookConfig).where(
            WebhookConfig.id == webhook_id,
            WebhookConfig.project_id == project_id,
        )
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="Webhook 不存在")

    if data.name is not None:
        cfg.name = data.name
    if data.url is not None:
        cfg.url = data.url
    if data.events is not None:
        cfg.events = data.events
    if data.filter_expression is not None:
        cfg.filter_expression = data.filter_expression
    if data.secret is not None:
        cfg.secret = data.secret
    if data.retry_count is not None:
        cfg.retry_count = data.retry_count
    if data.retry_delay_seconds is not None:
        cfg.retry_delay_seconds = data.retry_delay_seconds
    if data.is_enabled is not None:
        cfg.is_enabled = data.is_enabled

    await session.flush()
    await session.refresh(cfg)

    return WebhookConfigResponse(
        id=cfg.id,
        project_id=cfg.project_id,
        name=cfg.name,
        url=cfg.url,
        events=cfg.events,
        filter_expression=cfg.filter_expression,
        secret=cfg.secret,
        retry_count=cfg.retry_count,
        retry_delay_seconds=cfg.retry_delay_seconds,
        is_enabled=cfg.is_enabled,
        total_deliveries=cfg.total_deliveries,
        failed_deliveries=cfg.failed_deliveries,
        last_delivered_at=_get_iso(cfg.last_delivered_at),
        last_error=cfg.last_error,
        created_by=cfg.created_by,
        created_at=_get_iso(cfg.created_at),
        updated_at=_get_iso(cfg.updated_at),
    )


@router.delete("/{project_id}/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    project_id: uuid.UUID,
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    """删除 Webhook 配置。"""
    await require_project_role(project_id, Role.OWNER, principal=user, session=session)

    result = await session.execute(
        select(WebhookConfig).where(
            WebhookConfig.id == webhook_id,
            WebhookConfig.project_id == project_id,
        )
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="Webhook 不存在")

    await session.delete(cfg)


@router.post("/{project_id}/webhooks/{webhook_id}/test", status_code=status.HTTP_200_OK)
async def test_webhook(
    project_id: uuid.UUID,
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> dict:
    """发送一个测试事件到 Webhook URL。"""
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    result = await session.execute(
        select(WebhookConfig).where(
            WebhookConfig.id == webhook_id,
            WebhookConfig.project_id == project_id,
        )
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="Webhook 不存在")

    test_payload = build_event_payload(
        event_type=WebhookEventType.MANUAL_TRIGGER,
        target_type="webhook_test",
        target_id=webhook_id,
        actor_id=user.user.id if hasattr(user, "user") else None,
        actor_name=user.user.email if hasattr(user, "user") else None,
        extra={"message": "This is a test webhook delivery from OntoloHub"},
    )

    delivery_ids = await dispatch_webhook(
        event_type=WebhookEventType.MANUAL_TRIGGER,
        project_id=project_id,
        payload=test_payload,
        target_type="webhook_test",
        target_id=webhook_id,
    )

    return {"message": "Test event dispatched", "delivery_id": str(delivery_ids[0]) if delivery_ids else None}


# =============================================================================
# WebhookDelivery query
# =============================================================================


@router.get(
    "/{project_id}/webhooks/{webhook_id}/deliveries",
    response_model=list[WebhookDeliveryResponse],
)
async def list_deliveries(
    project_id: uuid.UUID,
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[WebhookDeliveryResponse]:
    """查看 Webhook 投递历史。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    # Verify webhook belongs to project
    cfg_result = await session.execute(
        select(WebhookConfig).where(
            WebhookConfig.id == webhook_id,
            WebhookConfig.project_id == project_id,
        )
    )
    if not cfg_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Webhook 不存在")

    query = (
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_config_id == webhook_id)
        .order_by(WebhookDelivery.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if status_filter:
        query = query.where(WebhookDelivery.status == WebhookDeliveryStatus(status_filter))

    result = await session.execute(query)
    deliveries = result.scalars().all()

    return [
        WebhookDeliveryResponse(
            id=d.id,
            webhook_config_id=d.webhook_config_id,
            event_type=d.event_type,
            status=d.status.value if d.status else "",
            http_status_code=d.http_status_code,
            response_body=d.response_body,
            error_message=d.error_message,
            duration_ms=d.duration_ms,
            attempt=d.attempt,
            max_attempts=d.max_attempts,
            delivered_at=_get_iso(d.delivered_at),
            target_type=d.target_type,
            target_id=d.target_id,
            created_at=_get_iso(d.created_at),
        )
        for d in deliveries
    ]


# =============================================================================
# Pydantic models — TriggerConfig
# =============================================================================


class TriggerConfigCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    trigger_type: str = Field(..., pattern="^(inbound_webhook|schedule|object_change)$")
    # trigger_config for inbound_webhook: {token: "..."} or auto-generated
    # for schedule: {cron: "*/5 * * * *"}
    # for object_change: {filter: "..."}
    trigger_config: Optional[dict] = None
    # Exactly one of action_type_id / workflow_id must be set.
    # HIA-76 C4: workflows are now first-class trigger targets.
    action_type_id: Optional[uuid.UUID] = None
    workflow_id: Optional[uuid.UUID] = None
    input_template: Optional[dict] = Field(
        None,
        description="Template merged with event data to form ActionRun input_data. "
                     "Use {{webhook.payload.xxx}} for inbound webhooks, "
                     "{{schedule.fired_at}} for schedules.",
    )

    @model_validator(mode="after")
    def _xor_target(self):
        both_set = self.action_type_id is not None and self.workflow_id is not None
        neither_set = self.action_type_id is None and self.workflow_id is None
        if both_set or neither_set:
            raise ValueError(
                "必须设置 action_type_id 或 workflow_id 之一（不能同时设置，也不能都不设置）"
            )
        return self


class TriggerConfigUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    trigger_config: Optional[dict] = None
    input_template: Optional[dict] = None
    status: Optional[str] = Field(None, pattern="^(active|paused|error)$")


class TriggerConfigResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: Optional[str]
    trigger_type: str
    trigger_config: Optional[dict]
    # HIA-76 C4: either action_type_id or workflow_id is set, never both.
    action_type_id: Optional[uuid.UUID] = None
    workflow_id: Optional[uuid.UUID] = None
    input_template: Optional[dict]
    status: str
    total_runs: int
    failed_runs: int
    last_run_at: Optional[str]
    last_error: Optional[str]
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# =============================================================================
# TriggerConfig CRUD
# =============================================================================


@router.post(
    "/{project_id}/triggers",
    response_model=TriggerConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_trigger(
    project_id: uuid.UUID,
    data: TriggerConfigCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> TriggerConfigResponse:
    """创建触发器配置。

    - ``trigger_type=inbound_webhook``：自动生成 URL ``POST /api/webhooks/in/{token}``
    - ``trigger_type=schedule``：提供 cron 表达式，格式同 Linux cron
    - ``trigger_type=object_change``：对象变更时触发（需配置 filter）
    """
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    # Verify target exists and belongs to project (action_type_id XOR workflow_id).
    if data.action_type_id is not None:
        at_result = await session.execute(
            select(ActionType).where(
                ActionType.id == data.action_type_id,
                ActionType.project_id == project_id,
            )
        )
        action_type = at_result.scalar_one_or_none()
        if not action_type:
            raise HTTPException(status_code=404, detail="ActionType 不存在")
    else:
        action_type = None
        # Lazy import to avoid circular import at module load
        from src.db.workflow import Workflow as _Workflow
        wf_result = await session.execute(
            select(_Workflow).where(
                _Workflow.id == data.workflow_id,
                _Workflow.project_id == project_id,
            )
        )
        workflow = wf_result.scalar_one_or_none()
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow 不存在")

    # Build trigger_config with auto-generated token for inbound_webhook
    trigger_cfg = (data.trigger_config or {}).copy()
    if data.trigger_type == "inbound_webhook" and not trigger_cfg.get("token"):
        trigger_cfg["token"] = generate_webhook_token()

    trigger = TriggerConfig(
        project_id=project_id,
        name=data.name,
        description=data.description,
        trigger_type=TriggerType(data.trigger_type),
        trigger_config=trigger_cfg,
        action_type_id=data.action_type_id,
        workflow_id=data.workflow_id,
        input_template=data.input_template,
        status=TriggerStatus.ACTIVE,
        created_by=user.user.id if hasattr(user, "user") else None,
    )
    session.add(trigger)
    await session.flush()
    await session.refresh(trigger)

    return TriggerConfigResponse(
        id=trigger.id,
        project_id=trigger.project_id,
        name=trigger.name,
        description=trigger.description,
        trigger_type=trigger.trigger_type.value,
        trigger_config=trigger.trigger_config,
        action_type_id=trigger.action_type_id,
        workflow_id=trigger.workflow_id,
        input_template=trigger.input_template,
        status=trigger.status.value,
        total_runs=trigger.total_runs,
        failed_runs=trigger.failed_runs,
        last_run_at=_get_iso(trigger.last_run_at),
        last_error=trigger.last_error,
        created_by=trigger.created_by,
        created_at=_get_iso(trigger.created_at),
        updated_at=_get_iso(trigger.updated_at),
    )


@router.get("/{project_id}/triggers", response_model=list[TriggerConfigResponse])
async def list_triggers(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    trigger_type: Optional[str] = Query(None, alias="trigger_type"),
    include_paused: bool = Query(False),
) -> list[TriggerConfigResponse]:
    """列出项目下所有触发器配置。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    query = select(TriggerConfig).where(TriggerConfig.project_id == project_id)
    if trigger_type:
        query = query.where(TriggerConfig.trigger_type == TriggerType(trigger_type))
    if not include_paused:
        query = query.where(TriggerConfig.status == TriggerStatus.ACTIVE)

    result = await session.execute(query)
    triggers = result.scalars().all()

    return [
        TriggerConfigResponse(
            id=t.id,
            project_id=t.project_id,
            name=t.name,
            description=t.description,
            trigger_type=t.trigger_type.value,
            trigger_config=t.trigger_config,
            action_type_id=t.action_type_id,
            workflow_id=t.workflow_id,
            input_template=t.input_template,
            status=t.status.value,
            total_runs=t.total_runs,
            failed_runs=t.failed_runs,
            last_run_at=_get_iso(t.last_run_at),
            last_error=t.last_error,
            created_by=t.created_by,
            created_at=_get_iso(t.created_at),
            updated_at=_get_iso(t.updated_at),
        )
        for t in triggers
    ]


@router.get("/{project_id}/triggers/{trigger_id}", response_model=TriggerConfigResponse)
async def get_trigger(
    project_id: uuid.UUID,
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> TriggerConfigResponse:
    """获取单个触发器配置。"""
    await require_project_role(project_id, Role.VIEWER, principal=user, session=session)

    result = await session.execute(
        select(TriggerConfig).where(
            TriggerConfig.id == trigger_id,
            TriggerConfig.project_id == project_id,
        )
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在")

    return TriggerConfigResponse(
        id=trigger.id,
        project_id=trigger.project_id,
        name=trigger.name,
        description=trigger.description,
        trigger_type=trigger.trigger_type.value,
        trigger_config=trigger.trigger_config,
        action_type_id=trigger.action_type_id,
        workflow_id=trigger.workflow_id,
        input_template=trigger.input_template,
        status=trigger.status.value,
        total_runs=trigger.total_runs,
        failed_runs=trigger.failed_runs,
        last_run_at=_get_iso(trigger.last_run_at),
        last_error=trigger.last_error,
        created_by=trigger.created_by,
        created_at=_get_iso(trigger.created_at),
        updated_at=_get_iso(trigger.updated_at),
    )


@router.patch("/{project_id}/triggers/{trigger_id}", response_model=TriggerConfigResponse)
async def update_trigger(
    project_id: uuid.UUID,
    trigger_id: uuid.UUID,
    data: TriggerConfigUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> TriggerConfigResponse:
    """更新触发器配置。"""
    await require_project_role(project_id, Role.EDITOR, principal=user, session=session)

    result = await session.execute(
        select(TriggerConfig).where(
            TriggerConfig.id == trigger_id,
            TriggerConfig.project_id == project_id,
        )
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在")

    if data.name is not None:
        trigger.name = data.name
    if data.description is not None:
        trigger.description = data.description
    if data.trigger_config is not None:
        trigger.trigger_config = data.trigger_config
    if data.input_template is not None:
        trigger.input_template = data.input_template
    if data.status is not None:
        trigger.status = TriggerStatus(data.status)

    await session.flush()
    await session.refresh(trigger)

    return TriggerConfigResponse(
        id=trigger.id,
        project_id=trigger.project_id,
        name=trigger.name,
        description=trigger.description,
        trigger_type=trigger.trigger_type.value,
        trigger_config=trigger.trigger_config,
        action_type_id=trigger.action_type_id,
        workflow_id=trigger.workflow_id,
        input_template=trigger.input_template,
        status=trigger.status.value,
        total_runs=trigger.total_runs,
        failed_runs=trigger.failed_runs,
        last_run_at=_get_iso(trigger.last_run_at),
        last_error=trigger.last_error,
        created_by=trigger.created_by,
        created_at=_get_iso(trigger.created_at),
        updated_at=_get_iso(trigger.updated_at),
    )


@router.delete("/{project_id}/triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trigger(
    project_id: uuid.UUID,
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    """删除触发器配置。"""
    await require_project_role(project_id, Role.OWNER, principal=user, session=session)

    result = await session.execute(
        select(TriggerConfig).where(
            TriggerConfig.id == trigger_id,
            TriggerConfig.project_id == project_id,
        )
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在")

    await session.delete(trigger)


# =============================================================================
# Inbound webhook receiver (no auth — uses token)
# =============================================================================


@trigger_router.post("/api/webhooks/in/{token}")
async def receive_webhook(
    token: str,
    request: Request,
) -> dict:
    """入站 Webhook 接收端点。

    认证方式：URL 中的 ``{token}`` 参数。
    请求体必须是 JSON。
    找到匹配的 TriggerConfig 后，创建一个 ActionRun 并异步执行。

    **HIA-75 验收：**
    配置一个 inbound_webhook trigger，POST 到此端点，
    看到 ActionRun 被创建并执行。
    """
    from src.services.webhook_dispatcher import process_inbound_webhook

    # Read body
    try:
        body = await request.body()
        import json as _json
        payload = _json.loads(body) if body else {}
    except Exception:
        payload = {}

    # Extract relevant headers
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    success, error, trigger_id = await process_inbound_webhook(token, payload, headers)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error or "Trigger not found",
        )

    return {
        "status": "accepted",
        "trigger_id": str(trigger_id),
        "message": "Webhook received and trigger fired",
    }


# =============================================================================
# Webhook event types listing
# =============================================================================


@router.get("/webhooks/events", response_model=list[dict])
async def list_webhook_event_types() -> list[dict]:
    """列出所有可用的 Webhook 事件类型。"""
    return [
        {"value": e.value, "label": e.value, "description": _EVENT_DESCRIPTIONS.get(e.value, "")}
        for e in WebhookEventType
    ]


_EVENT_DESCRIPTIONS = {
    "object.created": "对象创建时触发",
    "object.updated": "对象更新时触发",
    "object.deleted": "对象删除时触发",
    "cr.created": "变更请求创建时触发",
    "cr.submitted": "变更请求提交时触发",
    "cr.approved": "变更请求审批通过时触发",
    "cr.rejected": "变更请求被拒绝时触发",
    "cr.merged": "变更请求合并时触发",
    "cr.closed": "变更请求关闭时触发",
    "cr.comment_added": "变更请求有新评论时触发",
    "release.created": "发布创建时触发",
    "release.published": "发布上线时触发",
    "deployment.started": "部署开始时触发",
    "deployment.succeeded": "部署成功时触发",
    "deployment.failed": "部署失败时触发",
    "deployment.rollback": "部署回滚时触发",
    "action_run.started": "Action 执行开始时触发",
    "action_run.completed": "Action 执行完成时触发",
    "manual": "手动触发（用于测试）",
}
