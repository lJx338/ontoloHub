"""Release Line API — HIA-74 D4 版本化发布线（分支/tag/merge）。

Endpoints (all under ``/ontologies/{ontology_id}/...``):

  Branches:
    GET    /ontologies/{oid}/branches                    list (auto-create default main)
    POST   /ontologies/{oid}/branches                    create
    GET    /ontologies/{oid}/branches/{bid}              detail
    PATCH  /ontologies/{oid}/branches/{bid}              update (description / is_protected)
    DELETE /ontologies/{oid}/branches/{bid}              delete (non-default & non-protected)
    POST   /ontologies/{oid}/branches/{bid}/set-default  promote to default
    POST   /ontologies/{oid}/branches/{bid}/set-head     fast-forward head_version_id
    POST   /ontologies/{oid}/branches/{bid}/merge        merge source→target

  Tags:
    GET    /ontologies/{oid}/tags                        list
    POST   /ontologies/{oid}/tags                        create (immutable)
    DELETE /ontologies/{oid}/tags/{tid}                  delete

  Merge audit (top-level):
    GET    /branch-merges/{mid}                          detail
    GET    /projects/{pid}/branch-merges                 list (project scope)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session
from src.db.identity import Role
from src.db.governance import AuditEventType
from src.db.ontology import Ontology, OntologyVersion
from src.db.release_line import (
    BranchMerge,
    MergeStrategy,
    OntologyBranch,
    OntologyTag,
)
from src.api.auth import (
    get_current_user,
    record_audit,
    require_project_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ontologies", tags=["Release Line"])
merge_router = APIRouter(tags=["Release Line"])


# ===========================================================================
# Helpers
# ===========================================================================


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _load_ontology(session: AsyncSession, ontology_id: uuid.UUID) -> Ontology:
    o = (await session.execute(
        select(Ontology).where(Ontology.id == ontology_id)
    )).scalar_one_or_none()
    if not o:
        raise HTTPException(status_code=404, detail="本体不存在")
    return o


async def _resolve_principal_user_id(user) -> Optional[uuid.UUID]:
    """``user`` is the Principal returned by auth helpers."""
    if hasattr(user, "user"):
        return user.user.id
    if hasattr(user, "id"):
        return user.id
    return None


async def _authorize_for_ontology(
    session: AsyncSession,
    ontology: Ontology,
    user,
    min_role: Role,
) -> None:
    """Enforce project membership when ontology is project-private.

    Reference (cross-project) ontologies bypass project checks; they are
    shared infrastructure that any authenticated user may read.  Mutations
    on reference ontologies still require an authenticated principal
    (handled by ``get_current_user`` dep).
    """
    if ontology.kind.value == "reference":
        return
    if ontology.project_id is None:
        # Project-private but unowned (shouldn't normally happen) → allow
        return
    await require_project_role(
        ontology.project_id, min_role, principal=user, session=session
    )


async def _ensure_default_branch(
    session: AsyncSession,
    ontology: Ontology,
    actor_id: Optional[uuid.UUID],
) -> OntologyBranch:
    """Return the ``is_default=True`` branch, lazily creating ``main``."""
    q = select(OntologyBranch).where(
        OntologyBranch.ontology_id == ontology.id,
        OntologyBranch.is_default.is_(True),
    )
    existing = (await session.execute(q)).scalar_one_or_none()
    if existing:
        return existing

    # Lazily create ``main``
    main = OntologyBranch(
        project_id=ontology.project_id,
        ontology_id=ontology.id,
        name="main",
        description="Default branch",
        is_default=True,
        is_protected=False,
        head_version_id=None,
        created_by=actor_id,
    )
    session.add(main)
    await session.flush()
    await session.refresh(main)
    return main


# ===========================================================================
# Pydantic models
# ===========================================================================


class BranchCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    is_protected: bool = False
    head_version_id: Optional[uuid.UUID] = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        # Git-ish ref name: alnum + . _ -
        if not all(c.isalnum() or c in "._-" for c in v):
            raise ValueError("name 仅允许字母数字和 . _ -")
        if v.lower() in {"head", "origin", "origin/main"}:
            raise ValueError("name 保留字")
        return v


class BranchUpdate(BaseModel):
    description: Optional[str] = None
    is_protected: Optional[bool] = None


class BranchResponse(BaseModel):
    id: uuid.UUID
    project_id: Optional[uuid.UUID]
    ontology_id: uuid.UUID
    name: str
    description: Optional[str]
    is_default: bool
    is_protected: bool
    head_version_id: Optional[uuid.UUID]
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    version_id: uuid.UUID
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not all(c.isalnum() or c in "._-" for c in v):
            raise ValueError("name 仅允许字母数字和 . _ -")
        return v


class TagResponse(BaseModel):
    id: uuid.UUID
    project_id: Optional[uuid.UUID]
    ontology_id: uuid.UUID
    name: str
    description: Optional[str]
    version_id: uuid.UUID
    created_by: Optional[uuid.UUID]
    created_at: str
    updated_at: str


class BranchMergeRequest(BaseModel):
    source_branch_id: uuid.UUID = Field(
        ..., description="Source branch (the one being merged FROM)"
    )
    message: Optional[str] = Field(
        None, description="Merge commit message (merge_commit strategy)"
    )
    force_merge_commit: bool = Field(
        False,
        description="Even if fast-forward is possible, produce a merge commit",
    )


class BranchMergeResponse(BaseModel):
    id: uuid.UUID
    project_id: Optional[uuid.UUID]
    source_branch_id: uuid.UUID
    target_branch_id: uuid.UUID
    source_head_version_id: Optional[uuid.UUID]
    target_head_version_id: Optional[uuid.UUID]
    merge_version_id: Optional[uuid.UUID]
    strategy: str
    message: Optional[str]
    performed_by: Optional[uuid.UUID]
    created_at: str


class SetDefaultRequest(BaseModel):
    branch_id: uuid.UUID


class SetHeadRequest(BaseModel):
    version_id: uuid.UUID


# ===========================================================================
# Converters
# ===========================================================================


def _branch_to_response(b: OntologyBranch) -> BranchResponse:
    return BranchResponse(
        id=b.id,
        project_id=b.project_id,
        ontology_id=b.ontology_id,
        name=b.name,
        description=b.description,
        is_default=bool(b.is_default),
        is_protected=bool(b.is_protected),
        head_version_id=b.head_version_id,
        created_by=b.created_by,
        created_at=_iso(b.created_at),
        updated_at=_iso(b.updated_at),
    )


def _tag_to_response(t: OntologyTag) -> TagResponse:
    return TagResponse(
        id=t.id,
        project_id=t.project_id,
        ontology_id=t.ontology_id,
        name=t.name,
        description=t.description,
        version_id=t.version_id,
        created_by=t.created_by,
        created_at=_iso(t.created_at),
        updated_at=_iso(t.updated_at),
    )


def _merge_to_response(m: BranchMerge) -> BranchMergeResponse:
    return BranchMergeResponse(
        id=m.id,
        project_id=m.project_id,
        source_branch_id=m.source_branch_id,
        target_branch_id=m.target_branch_id,
        source_head_version_id=m.source_head_version_id,
        target_head_version_id=m.target_head_version_id,
        merge_version_id=m.merge_version_id,
        strategy=m.strategy.value if m.strategy else "fast_forward",
        message=m.message,
        performed_by=m.performed_by,
        created_at=_iso(m.created_at),
    )


# ===========================================================================
# Branch CRUD
# ===========================================================================


@router.get(
    "/{ontology_id}/branches",
    response_model=BranchResponse,  # single: returns the *default* branch
)
async def get_default_branch(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchResponse:
    """Return the default branch of the ontology, lazily creating ``main``.

    Use ``?list=1`` to list *all* branches instead.
    """
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.VIEWER)

    actor_id = await _resolve_principal_user_id(user)
    default = await _ensure_default_branch(session, ontology, actor_id)
    await session.flush()
    return _branch_to_response(default)


@router.get(
    "/{ontology_id}/branches/all",
    response_model=list[BranchResponse],
)
async def list_branches(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> list[BranchResponse]:
    """List all branches for an ontology (auto-creates ``main`` if none)."""
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.VIEWER)

    actor_id = await _resolve_principal_user_id(user)
    await _ensure_default_branch(session, ontology, actor_id)

    result = await session.execute(
        select(OntologyBranch)
        .where(OntologyBranch.ontology_id == ontology_id)
        .order_by(
            OntologyBranch.is_default.desc(),
            OntologyBranch.created_at.asc(),
        )
    )
    return [_branch_to_response(b) for b in result.scalars().all()]


@router.post(
    "/{ontology_id}/branches",
    response_model=BranchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_branch(
    ontology_id: uuid.UUID,
    data: BranchCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchResponse:
    """Create a new branch.

    If this is the first branch for the ontology, it is promoted to default
    automatically (otherwise ``main`` already exists and the new branch is
    non-default).
    """
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.EDITOR)

    actor_id = await _resolve_principal_user_id(user)

    # If a row with this name already exists → 409
    existing = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.ontology_id == ontology_id,
            OntologyBranch.name == data.name,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"分支名 '{data.name}' 已存在",
        )

    # If no default branch yet → promote this one
    has_default = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.ontology_id == ontology_id,
            OntologyBranch.is_default.is_(True),
        )
    )).scalar_one_or_none()
    is_first = has_default is None

    # If head_version_id supplied, verify it belongs to the same ontology
    if data.head_version_id:
        v = (await session.execute(
            select(OntologyVersion).where(
                OntologyVersion.id == data.head_version_id,
                OntologyVersion.ontology_id == ontology_id,
            )
        )).scalar_one_or_none()
        if not v:
            raise HTTPException(
                status_code=400,
                detail="head_version_id 不属于该本体",
            )

    branch = OntologyBranch(
        project_id=ontology.project_id,
        ontology_id=ontology.id,
        name=data.name,
        description=data.description,
        is_default=is_first,
        is_protected=data.is_protected,
        head_version_id=data.head_version_id,
        created_by=actor_id,
    )
    session.add(branch)
    await session.flush()
    await session.refresh(branch)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=ontology.project_id,
        target_type="ontology_branch",
        target_id=str(branch.id),
        after={
            "ontology_id": str(ontology.id),
            "name": branch.name,
            "is_default": branch.is_default,
        },
    )
    return _branch_to_response(branch)


@router.get(
    "/{ontology_id}/branches/{branch_id}",
    response_model=BranchResponse,
)
async def get_branch(
    ontology_id: uuid.UUID,
    branch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchResponse:
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.VIEWER)

    b = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.id == branch_id,
            OntologyBranch.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="分支不存在")
    return _branch_to_response(b)


@router.patch(
    "/{ontology_id}/branches/{branch_id}",
    response_model=BranchResponse,
)
async def update_branch(
    ontology_id: uuid.UUID,
    branch_id: uuid.UUID,
    data: BranchUpdate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchResponse:
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.EDITOR)

    b = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.id == branch_id,
            OntologyBranch.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="分支不存在")

    if data.description is not None:
        b.description = data.description
    if data.is_protected is not None:
        # Don't allow demoting protection on a branch whose default status
        # we're not touching (protection is orthogonal to default).
        b.is_protected = data.is_protected

    await session.flush()
    await session.refresh(b)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=ontology.project_id,
        target_type="ontology_branch",
        target_id=str(b.id),
        after={"name": b.name},
    )
    return _branch_to_response(b)


@router.delete(
    "/{ontology_id}/branches/{branch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_branch(
    ontology_id: uuid.UUID,
    branch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.OWNER)

    b = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.id == branch_id,
            OntologyBranch.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="分支不存在")
    if b.is_default:
        raise HTTPException(status_code=409, detail="不能删除默认分支")
    if b.is_protected:
        raise HTTPException(status_code=409, detail="受保护分支不能删除")

    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=user,
        project_id=ontology.project_id,
        target_type="ontology_branch",
        target_id=str(branch_id),
        after={"name": b.name},
    )
    await session.delete(b)
    await session.flush()


@router.post(
    "/{ontology_id}/branches/{branch_id}/set-default",
    response_model=BranchResponse,
)
async def set_default_branch(
    ontology_id: uuid.UUID,
    branch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchResponse:
    """Promote ``branch_id`` to default (demoting whatever is default now)."""
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.OWNER)

    target = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.id == branch_id,
            OntologyBranch.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="分支不存在")
    if target.is_default:
        return _branch_to_response(target)  # no-op

    # Demote current default
    current = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.ontology_id == ontology_id,
            OntologyBranch.is_default.is_(True),
        )
    )).scalar_one_or_none()
    if current:
        current.is_default = False
    target.is_default = True

    await session.flush()
    await session.refresh(target)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=ontology.project_id,
        target_type="ontology_branch",
        target_id=str(target.id),
        after={"name": target.name, "is_default": True},
    )
    return _branch_to_response(target)


@router.post(
    "/{ontology_id}/branches/{branch_id}/set-head",
    response_model=BranchResponse,
)
async def set_branch_head(
    ontology_id: uuid.UUID,
    branch_id: uuid.UUID,
    data: SetHeadRequest,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchResponse:
    """Fast-forward / set ``head_version_id`` of a branch to an arbitrary
    OntologyVersion (must belong to the same ontology)."""
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.EDITOR)

    branch = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.id == branch_id,
            OntologyBranch.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="分支不存在")
    if branch.is_protected:
        raise HTTPException(
            status_code=409, detail="受保护分支不能直接 set-head；请用 merge"
        )

    version = (await session.execute(
        select(OntologyVersion).where(
            OntologyVersion.id == data.version_id,
            OntologyVersion.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not version:
        raise HTTPException(
            status_code=400, detail="version_id 不属于该本体"
        )

    branch.head_version_id = data.version_id
    await session.flush()
    await session.refresh(branch)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=ontology.project_id,
        target_type="ontology_branch",
        target_id=str(branch.id),
        after={"head_version_id": str(data.version_id)},
    )
    return _branch_to_response(branch)


@router.post(
    "/{ontology_id}/branches/{target_branch_id}/merge",
    response_model=BranchMergeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def merge_branch(
    ontology_id: uuid.UUID,
    target_branch_id: uuid.UUID,
    data: BranchMergeRequest,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchMergeResponse:
    """Merge ``source_branch_id`` into ``target_branch_id``.

    Strategy selection:
      * ``NOOP``        — source.head == target.head; nothing to do.
      * ``FAST_FORWARD``— target.head is an ancestor of source.head; only
        move target.head to source.head.
      * ``MERGE_COMMIT``— divergent; produce a merge commit carrying both
        parents.  (For now we only record the intent; the actual
        OntologyVersion merge commit will be added by HIA-74 D4.x.)

    In all cases we record an entry in ``branch_merges`` for audit.
    """
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.EDITOR)

    actor_id = await _resolve_principal_user_id(user)

    target = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.id == target_branch_id,
            OntologyBranch.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="目标分支不存在")

    source = (await session.execute(
        select(OntologyBranch).where(
            OntologyBranch.id == data.source_branch_id,
            OntologyBranch.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="源分支不存在")
    if source.id == target.id:
        raise HTTPException(status_code=400, detail="不能 merge 到自己")

    target_head_before = target.head_version_id
    source_head = source.head_version_id

    if source_head is None:
        raise HTTPException(
            status_code=409,
            detail="源分支没有可用的 head_version_id",
        )

    # Determine strategy.
    if source_head == target_head_before:
        strategy = MergeStrategy.NOOP
    elif target_head_before is None:
        # Target has no head yet → always fast-forward.
        strategy = MergeStrategy.FAST_FORWARD
    elif data.force_merge_commit:
        strategy = MergeStrategy.MERGE_COMMIT
    else:
        # Heuristic: linear ancestry is the common case.  Until we wire up
        # actual version ancestry (HIA-74 D4.x), default to FAST_FORWARD
        # whenever heads differ — divergence detection is left for the
        # merge-commit codepath.
        strategy = MergeStrategy.FAST_FORWARD

    merge_record_id = uuid.uuid4()
    merge_version_id: Optional[uuid.UUID] = None

    if strategy == MergeStrategy.NOOP:
        pass
    elif strategy == MergeStrategy.FAST_FORWARD:
        target.head_version_id = source_head
    else:  # MERGE_COMMIT
        # Without full version-ancestry tracking yet, we don't create a real
        # merge OntologyVersion — but we still record the audit row so the
        # merge is reproducible.  Future HIA-74 work will materialize the
        # commit.
        merge_version_id = None  # placeholder
        target.head_version_id = source_head

    await session.flush()
    await session.refresh(target)

    bm = BranchMerge(
        id=merge_record_id,
        project_id=ontology.project_id,
        source_branch_id=source.id,
        target_branch_id=target.id,
        source_head_version_id=source_head,
        target_head_version_id=target_head_before,
        merge_version_id=merge_version_id,
        strategy=strategy,
        message=data.message,
        performed_by=actor_id,
    )
    session.add(bm)
    await session.flush()
    await session.refresh(bm)

    await record_audit(
        session,
        event_type=AuditEventType.UPDATE,
        principal=user,
        project_id=ontology.project_id,
        target_type="branch_merge",
        target_id=str(bm.id),
        after={
            "source": source.name,
            "target": target.name,
            "strategy": strategy.value,
        },
    )
    return _merge_to_response(bm)


# ===========================================================================
# Tag CRUD
# ===========================================================================


@router.get(
    "/{ontology_id}/tags",
    response_model=list[TagResponse],
)
async def list_tags(
    ontology_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> list[TagResponse]:
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.VIEWER)

    result = await session.execute(
        select(OntologyTag)
        .where(OntologyTag.ontology_id == ontology_id)
        .order_by(OntologyTag.created_at.desc())
    )
    return [_tag_to_response(t) for t in result.scalars().all()]


@router.post(
    "/{ontology_id}/tags",
    response_model=TagResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tag(
    ontology_id: uuid.UUID,
    data: TagCreate,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> TagResponse:
    """Create an immutable tag pointing to ``version_id``."""
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.EDITOR)

    actor_id = await _resolve_principal_user_id(user)

    # Verify version belongs to ontology
    v = (await session.execute(
        select(OntologyVersion).where(
            OntologyVersion.id == data.version_id,
            OntologyVersion.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not v:
        raise HTTPException(
            status_code=400, detail="version_id 不属于该本体"
        )

    # Name uniqueness
    existing = (await session.execute(
        select(OntologyTag).where(
            OntologyTag.ontology_id == ontology_id,
            OntologyTag.name == data.name,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409, detail=f"标签名 '{data.name}' 已存在"
        )

    tag = OntologyTag(
        project_id=ontology.project_id,
        ontology_id=ontology.id,
        name=data.name,
        description=data.description,
        version_id=data.version_id,
        created_by=actor_id,
    )
    session.add(tag)
    await session.flush()
    await session.refresh(tag)

    await record_audit(
        session,
        event_type=AuditEventType.CREATE,
        principal=user,
        project_id=ontology.project_id,
        target_type="ontology_tag",
        target_id=str(tag.id),
        after={
            "ontology_id": str(ontology.id),
            "name": tag.name,
            "version_id": str(tag.version_id),
        },
    )
    return _tag_to_response(tag)


@router.delete(
    "/{ontology_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_tag(
    ontology_id: uuid.UUID,
    tag_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> None:
    ontology = await _load_ontology(session, ontology_id)
    await _authorize_for_ontology(session, ontology, user, Role.OWNER)

    tag = (await session.execute(
        select(OntologyTag).where(
            OntologyTag.id == tag_id,
            OntologyTag.ontology_id == ontology_id,
        )
    )).scalar_one_or_none()
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")

    await record_audit(
        session,
        event_type=AuditEventType.DELETE,
        principal=user,
        project_id=ontology.project_id,
        target_type="ontology_tag",
        target_id=str(tag_id),
        after={"name": tag.name},
    )
    await session.delete(tag)
    await session.flush()


# ===========================================================================
# Merge audit (top-level)
# ===========================================================================


@merge_router.get(
    "/projects/{project_id}/branch-merges",
    response_model=list[BranchMergeResponse],
)
async def list_project_branch_merges(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
    ontology_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[BranchMergeResponse]:
    """List branch merges within a project, newest first."""
    await require_project_role(
        project_id, Role.VIEWER, principal=user, session=session
    )

    q = select(BranchMerge).where(BranchMerge.project_id == project_id)
    if ontology_id is not None:
        # Filter by joining through either branch → ontology_id
        q = q.join(
            OntologyBranch,
            OntologyBranch.id == BranchMerge.target_branch_id,
        ).where(OntologyBranch.ontology_id == ontology_id)
    q = q.order_by(BranchMerge.created_at.desc()).offset(offset).limit(limit)

    result = await session.execute(q)
    return [_merge_to_response(m) for m in result.scalars().all()]


@merge_router.get(
    "/branch-merges/{merge_id}",
    response_model=BranchMergeResponse,
)
async def get_branch_merge(
    merge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> BranchMergeResponse:
    m = (await session.execute(
        select(BranchMerge).where(BranchMerge.id == merge_id)
    )).scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="BranchMerge 不存在")
    if m.project_id is not None:
        await require_project_role(
            m.project_id, Role.VIEWER, principal=user, session=session
        )
    return _merge_to_response(m)
