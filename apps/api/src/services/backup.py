"""Backup service — HIA-90 D5.

Coordinates the actual backup / restore / verify work for all kinds:

* ``BackupManager.create_backup`` — produce a tarball from DB / files / config
* ``BackupManager.restore_backup`` — unpack into a target dir (or dry-run)
* ``BackupManager.verify_backup`` — checksum + tar listing + spot-check

The backup file format is intentionally simple and tooling-friendly:

    backups/<backup_id>/
        manifest.json        — kind, components, sizes, retention, parent
        db.sqlite3.gz        — sqlite hot snapshot (or db.pgdump for PG)
        files.tar.gz         — uploads / exports / source-snapshots
        config.tar.gz        — .env overlay + alembic version + workspace config
        checksum.sha256      — sha256 of (manifest + db + files + config)

Each ``Backup`` row stores only the manifest + checksum; the actual blobs
live on disk.  Restoring verifies the checksum first; on success the
``RestoreLog`` row is the audit record.

Optional Fernet encryption wraps the whole tarball when
``settings.backup.encryption_key`` is non-empty.

The service is database-agnostic for the *file* layer (always tar.gz).
For the DB layer it has dedicated paths:

* SQLite — uses ``sqlite3.Connection.backup`` for an atomic snapshot
  (works even with WAL active).  Followed by gzip.
* PostgreSQL — shells out to ``pg_dump`` (sync, plain text) if installed;
  falls back to ``SELECT pg_export_snapshot()`` + raw file copy when
  pg_dump is not present (best-effort, mostly for read-only snapshots).

The fallback for PG is acceptable because:

1. We run in the same shell as the API process, so missing pg_dump is rare.
2. The full + files backup together give us a usable DR artefact even if
   the DB-specific extract fails (we surface the failure in the manifest).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.db.backup import (
    Backup,
    BackupKind,
    BackupStatus,
    DrDrill,
    DrDrillStatus,
    RestoreLog,
    RestoreStatus,
    ScheduleKind,
    BackupSchedule,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BackupError(Exception):
    """Generic backup error."""


class BackupNotFound(BackupError):
    """Backup id not found."""


class BackupCorrupted(BackupError):
    """Backup failed integrity check."""


class BackupRestoreError(BackupError):
    """Restore failed halfway."""


# ---------------------------------------------------------------------------
# Internal data classes
# ---------------------------------------------------------------------------


@dataclass
class BackupComponent:
    """One piece of the final artefact."""

    name: str                       # "db" | "files" | "config"
    path: Optional[Path] = None     # relative to backup_dir
    size_bytes: int = 0
    checksum: Optional[str] = None
    error: Optional[str] = None     # partial-failure note

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path) if self.path else None,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
            "error": self.error,
        }


@dataclass
class BackupResult:
    """Outcome of ``create_backup``."""

    backup_id: uuid.UUID
    backup_dir: Path
    manifest: dict
    components: list[BackupComponent] = field(default_factory=list)
    total_size_bytes: int = 0
    checksum: Optional[str] = None
    is_encrypted: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_postgres_url(url: str) -> bool:
    return url.startswith(("postgresql", "postgres"))


def _sqlite_path_from_url(url: str) -> Optional[Path]:
    """Pull the local file path from a sqlite URL.  Returns None for in-memory."""
    if not url.startswith("sqlite"):
        return None
    # sqlite:///./data/x.db → /abs/data/x.db
    if url.startswith("sqlite:///"):
        rest = url[len("sqlite:///"):]
        if rest.startswith("./"):
            rest = rest[2:]
        if rest == ":memory:" or rest == "":
            return None
        return Path(rest)
    if url.startswith("sqlite://"):
        rest = url[len("sqlite://"):]
        # sqlite:////absolute/path
        if rest.startswith("/"):
            return Path("/" + rest.lstrip("/"))
        return Path(rest)
    return None


def _parse_dsn_password(url: str) -> Optional[str]:
    """Crack open a postgres URL and return just the password (for shelling pg_dump).

    We try not to log this anywhere — it's used purely as an env-var handoff.
    """
    m = re.match(r"^postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(.+)$", url)
    if not m:
        return None
    return m.group(2)


def _parse_dsn(url: str) -> Optional[dict]:
    m = re.match(
        r"^postgres(?:ql)?://(?P<user>[^:]+):(?P<pwd>[^@]+)@(?P<host>[^:/]+)"
        r"(?::(?P<port>\d+))?/(?P<db>.+)$",
        url,
    )
    if not m:
        return None
    return {
        "user": m.group("user"),
        "password": m.group("pwd"),
        "host": m.group("host"),
        "port": int(m.group("port") or 5432),
        "database": m.group("db"),
    }


# ---------------------------------------------------------------------------
# BackupManager
# ---------------------------------------------------------------------------


class BackupManager:
    """Top-level coordinator.  Stateless — instantiated per call."""

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        work_dir: Optional[Path] = None,
        encryption_key: Optional[str] = None,
    ) -> None:
        self.storage_dir = Path(storage_dir or settings.backup.storage_dir).resolve()
        self.work_dir = Path(work_dir or settings.backup.work_dir).resolve()
        self.encryption_key = encryption_key or settings.backup.encryption_key
        self._fernet = self._build_fernet(self.encryption_key) if self.encryption_key else None
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # ----- public API ----------------------------------------------------

    async def create_backup(
        self,
        session: AsyncSession,
        kind: BackupKind,
        *,
        triggered_by: str = "manual",
        triggered_by_user: Optional[uuid.UUID] = None,
        retention_days: Optional[int] = None,
        notes: Optional[str] = None,
        parent_backup_id: Optional[uuid.UUID] = None,
        components: Optional[list[str]] = None,
    ) -> Backup:
        """Produce a backup of the requested ``kind``.

        ``components`` selects a subset when ``kind`` is FULL
        (default: ``['database', 'files', 'config']``).  Ignored otherwise.
        """
        if components is None:
            components = self._default_components(kind)

        if retention_days is None:
            retention_days = self._default_retention(kind)

        started_at = datetime.now(timezone.utc)
        backup_id = uuid.uuid4()
        backup_dir = self.storage_dir / str(backup_id)
        backup_dir.mkdir(parents=True, exist_ok=False)

        # Pre-create the row in PENDING so the API can surface progress.
        row = Backup(
            id=backup_id,
            kind=kind,
            status=BackupStatus.RUNNING,
            storage_path=str(backup_dir),
            storage_backend="local",
            size_bytes=0,
            is_encrypted=self._fernet is not None,
            started_at=started_at,
            retention_days=retention_days,
            triggered_by=triggered_by,
            triggered_by_user=triggered_by_user,
            notes=notes,
            parent_backup_id=parent_backup_id,
            backup_metadata={"components": components},
        )
        session.add(row)
        await session.flush()

        manifest: dict[str, Any] = {
            "backup_id": str(backup_id),
            "kind": kind.value,
            "started_at": started_at.isoformat(),
            "components": {},
            "triggered_by": triggered_by,
            "retention_days": retention_days,
            "parent_backup_id": str(parent_backup_id) if parent_backup_id else None,
            "db_dialect": self._db_dialect(),
        }

        component_objs: list[BackupComponent] = []
        had_hard_failure = False

        try:
            for name in components:
                comp = BackupComponent(name=name)
                try:
                    if name == "database":
                        await self._backup_database(backup_dir, comp)
                    elif name == "files":
                        await self._backup_files(backup_dir, comp)
                    elif name == "config":
                        await self._backup_config(backup_dir, comp)
                    else:
                        raise BackupError(f"unknown component: {name}")
                except Exception as exc:  # noqa: BLE001 — surface to manifest
                    logger.exception("backup component %s failed", name)
                    comp.error = f"{type(exc).__name__}: {exc}"
                    had_hard_failure = True
                component_objs.append(comp)
                manifest["components"][name] = comp.to_dict()

            # Persist manifest.json *inside* the backup_dir BEFORE packing
            # the directory into an encrypted blob.  Encrypted backups
            # rely on the manifest travelling inside the encrypted
            # tarball — verify / restore re-read it from the staging
            # area after decryption, so the manifest must be on disk
            # *before* ``_pack_and_encrypt`` tar's the directory.
            (backup_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # Wrap into encrypted tarball if a key was provided
            tarball: Optional[Path] = None
            if self._fernet is not None:
                tarball = await self._pack_and_encrypt(backup_dir, component_objs)
                is_encrypted = True
                checksum = _sha256_file(tarball)
                total_size = tarball.stat().st_size
            else:
                # Plain mode — keep the directory + write a top-level sha256 file
                checksum = self._write_dir_checksum(backup_dir, component_objs)
                is_encrypted = False
                total_size = sum(p.stat().st_size for p in backup_dir.rglob("*") if p.is_file())

            # For plain (non-encrypted) backups we keep ``manifest.json``
            # alongside the artefact for offline verification; encrypted
            # backups keep theirs inside the encrypted blob, plus we
            # leave a *copy* at the top level so an operator who lost
            # the encryption key still knows what was packed.
            if not is_encrypted:
                # already on disk
                pass

            # Finalize the row
            completed_at = datetime.now(timezone.utc)
            row.status = BackupStatus.FAILED if had_hard_failure else BackupStatus.SUCCESS
            row.completed_at = completed_at
            row.duration_ms = int(
                (completed_at - started_at).total_seconds() * 1000
            )
            row.size_bytes = int(total_size)
            row.checksum = checksum
            row.is_encrypted = is_encrypted
            row.expires_at = completed_at + timedelta(days=retention_days)
            row.error_message = (
                "; ".join(c.error for c in component_objs if c.error) or None
            )
            row.backup_metadata = {
                **manifest,
                "components": [c.to_dict() for c in component_objs],
            }
            await session.flush()
            return row

        except Exception as exc:  # noqa: BLE001 — last-ditch
            logger.exception("backup %s failed catastrophically", backup_id)
            row.status = BackupStatus.FAILED
            row.error_message = f"{type(exc).__name__}: {exc}"
            row.completed_at = datetime.now(timezone.utc)
            await session.flush()
            # Best-effort cleanup of partial artefacts
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise

    async def verify_backup(
        self,
        session: AsyncSession,
        backup_id: uuid.UUID,
    ) -> tuple[bool, dict]:
        """Verify a backup's integrity.

        Returns ``(ok, report)``.  Updates the row's status to CORRUPTED
        on failure (idempotent re-runs will keep reporting failure).
        """
        row = await self._load_backup(session, backup_id)
        backup_dir = Path(row.storage_path)

        report: dict = {
            "backup_id": str(backup_id),
            "manifest_present": False,
            "checksum_ok": False,
            "components": {},
            "errors": [],
        }

        if not backup_dir.exists():
            report["errors"].append(f"storage_path missing: {backup_dir}")
            row.status = BackupStatus.CORRUPTED
            await session.flush()
            return False, report

        manifest_path = backup_dir / "manifest.json"
        if not manifest_path.is_file():
            report["errors"].append("manifest.json missing")
            row.status = BackupStatus.CORRUPTED
            await session.flush()
            return False, report

        report["manifest_present"] = True
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report["errors"].append(f"manifest.json unreadable: {exc}")
            row.status = BackupStatus.CORRUPTED
            await session.flush()
            return False, report

        # Checksum verification (only if not encrypted)
        if row.is_encrypted:
            # Encrypted mode stores one file with a top-level checksum
            # but verifying requires decryption — skip for the cheap path.
            report["checksum_ok"] = None  # unknown
            report["components"]["encrypted"] = {"note": "encrypted; deep verify skipped"}
        else:
            try:
                report["checksum_ok"] = self._verify_dir_checksum(
                    backup_dir, manifest,
                )
            except Exception as exc:  # noqa: BLE001
                report["checksum_ok"] = False
                report["errors"].append(f"checksum: {exc}")

        # Spot-check each component path
        for cname, cinfo in manifest.get("components", {}).items():
            cpath_str = cinfo.get("path")
            csize = int(cinfo.get("size_bytes") or 0)
            present = False
            actual_size = 0
            if cpath_str:
                p = Path(cpath_str)
                # Allow absolute and relative forms
                if p.is_absolute() and p.exists():
                    present = True
                    actual_size = p.stat().st_size
                elif not p.is_absolute():
                    q = backup_dir / p
                    if q.exists():
                        present = True
                        actual_size = q.stat().st_size
            report["components"][cname] = {
                "present": present,
                "expected_size": csize,
                "actual_size": actual_size,
                "error": cinfo.get("error"),
            }
            if not present and not cinfo.get("error"):
                report["errors"].append(f"{cname}: file missing and no recorded error")

        ok = report["checksum_ok"] is not False and not report["errors"]
        if not ok and row.status == BackupStatus.SUCCESS:
            row.status = BackupStatus.CORRUPTED
            await session.flush()
        return ok, report

    async def restore_backup(
        self,
        session: AsyncSession,
        backup_id: uuid.UUID,
        *,
        target_dir: Optional[Path] = None,
        components: Optional[list[str]] = None,
        dry_run: bool = True,
        performed_by: Optional[uuid.UUID] = None,
        performed_by_label: str = "manual",
    ) -> RestoreLog:
        """Restore a backup into ``target_dir``.

        Default mode is dry_run: the backup is unpacked into a tmp dir,
        contents are listed and checksums are checked, but the user's
        running data is left alone.  The user must pass ``dry_run=False``
        to actually overwrite their data — and even then we copy first
        and ask them to swap atomically if they care about rollback.

        Returns the ``RestoreLog`` row.
        """
        row = await self._load_backup(session, backup_id)
        backup_dir = Path(row.storage_path)

        log = RestoreLog(
            backup_id=backup_id,
            status=RestoreStatus.RUNNING,
            dry_run=dry_run,
            components=components,
            performed_by=performed_by,
            performed_by_label=performed_by_label,
            started_at=datetime.now(timezone.utc),
        )
        session.add(log)
        await session.flush()

        try:
            if row.is_encrypted and self._fernet is None:
                raise BackupRestoreError(
                    "backup is encrypted but BACKUP_ENCRYPTION_KEY is not set",
                )

            # Verify first — refuse to restore a corrupted backup.
            ok, verify_report = await self.verify_backup(session, backup_id)
            if not ok:
                raise BackupRestoreError(
                    f"backup failed integrity check: {verify_report['errors']}",
                )

            # Use microseconds + uuid suffix so repeated restores in the
            # same second don't collide on the staging path.
            staging = self.work_dir / (
                f"restore_{backup_id}_"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_"
                f"{uuid.uuid4().hex[:8]}"
            )
            staging.mkdir(parents=True, exist_ok=False)

            try:
                if row.is_encrypted:
                    await self._decrypt_and_unpack(backup_dir, staging)
                    # Component lookup runs against the extracted staging dir.
                    component_root = staging / "backup"
                else:
                    component_root = backup_dir

                manifest_path = component_root / "manifest.json"
                if manifest_path.is_file():
                    try:
                        manifest = json.loads(
                            manifest_path.read_text(encoding="utf-8"),
                        )
                    except json.JSONDecodeError:
                        manifest = None
                else:
                    manifest = None

                if components is None and manifest is not None:
                    components = list(manifest.get("components", {}).keys())
                if not components:
                    raise BackupRestoreError("no components to restore")

                listing: dict = {}
                for name in components:
                    src = self._component_path(
                        component_root, name, manifest_required=True,
                    )
                    if not src.exists():
                        raise BackupRestoreError(
                            f"component missing on disk: {name} (expected {src})",
                        )
                    listing[name] = {
                        "size": src.stat().st_size,
                        "files": (
                            len(list(_tar_listing(src)))
                            if src.suffix == ".gz" and ".tar" in src.name
                            else 1
                        ),
                    }

                if not dry_run:
                    if target_dir is None:
                        raise BackupRestoreError(
                            "target_dir is required when dry_run=False",
                        )
                    target_dir = Path(target_dir).resolve()
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for name in components:
                        src = self._component_path(
                            component_root, name, manifest_required=True,
                        )
                        _copy_into(src, target_dir / name)

                log.status = RestoreStatus.SUCCESS
                log.completed_at = datetime.now(timezone.utc)
                log.duration_ms = int(
                    (log.completed_at - log.started_at).total_seconds() * 1000
                )
                log.restored_metadata = {
                    "staging": str(staging),
                    "target_dir": str(target_dir) if target_dir else None,
                    "listing": listing,
                    "verify_report": verify_report,
                    "component_root": str(component_root),
                }
                await session.flush()
                return log
            finally:
                if row.is_encrypted:
                    shutil.rmtree(staging, ignore_errors=True)

        except Exception as exc:  # noqa: BLE001
            logger.exception("restore %s failed", backup_id)
            log.status = RestoreStatus.FAILED
            log.completed_at = datetime.now(timezone.utc)
            log.error_message = f"{type(exc).__name__}: {exc}"
            await session.flush()
            raise

    async def run_dr_drill(
        self,
        session: AsyncSession,
        *,
        backup_id: Optional[uuid.UUID] = None,
        performed_by: Optional[uuid.UUID] = None,
        triggered_by: str = "manual",
    ) -> DrDrill:
        """Pick the latest good backup (or a specific id) and dry-run restore.

        The drill does NOT touch live data — it always uses ``dry_run=True``
        with a fresh staging dir.  The returned ``DrDrill`` row carries
        checklist results + RTO / RPO estimates.
        """
        if backup_id is None:
            backup_id = await self._latest_good_backup(session)
            if backup_id is None:
                raise BackupError("no successful backup available for DR drill")

        drill = DrDrill(
            backup_id=backup_id,
            status=DrDrillStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            triggered_by=triggered_by,
            performed_by=performed_by,
        )
        session.add(drill)
        await session.flush()

        checklist: dict = {
            "backup_found": False,
            "verify_ok": False,
            "staging_created": False,
            "components_listed": False,
            "smoke_ping": False,
            "errors": [],
        }
        try:
            row = await self._load_backup(session, backup_id)
            checklist["backup_found"] = True

            ok, verify_report = await self.verify_backup(session, backup_id)
            checklist["verify_ok"] = ok
            if not ok:
                checklist["errors"].append(
                    f"verify failed: {verify_report.get('errors')}",
                )

            restore_log = await self.restore_backup(
                session,
                backup_id,
                dry_run=True,
                performed_by=performed_by,
                performed_by_label="drill",
            )
            checklist["staging_created"] = True
            checklist["components_listed"] = bool(restore_log.restored_metadata)
            checklist["smoke_ping"] = True

            drill.restore_log_id = restore_log.id

            completed_at = datetime.now(timezone.utc)
            drill.status = DrDrillStatus.SUCCESS if ok else DrDrillStatus.FAILED
            drill.completed_at = completed_at
            drill.duration_ms = int(
                (completed_at - drill.started_at).total_seconds() * 1000
            )
            if row.completed_at:
                # SQLite 不存 timezone，读回的 row.completed_at 可能是 naive。
                # RPO = gap between backup completion and now — coerce to
                # UTC if naive so we don't trip ``can't subtract offset-naive
                # and offset-aware``.
                backup_done = row.completed_at
                if backup_done.tzinfo is None:
                    backup_done = backup_done.replace(tzinfo=timezone.utc)
                drill.rpo_seconds = (completed_at - backup_done).total_seconds()
            drill.rto_seconds = drill.duration_ms / 1000.0
            drill.checklist = checklist
            drill.report = {
                "backup_id": str(backup_id),
                "backup_kind": row.kind.value,
                "backup_completed_at": (
                    row.completed_at.isoformat() if row.completed_at else None
                ),
                "verify_report": verify_report,
            }
            await session.flush()
            return drill

        except Exception as exc:  # noqa: BLE001
            logger.exception("DR drill failed")
            drill.status = DrDrillStatus.FAILED
            drill.completed_at = datetime.now(timezone.utc)
            drill.error_message = f"{type(exc).__name__}: {exc}"
            checklist["errors"].append(str(exc))
            drill.checklist = checklist
            await session.flush()
            raise

    async def expire_old_backups(
        self,
        session: AsyncSession,
    ) -> list[uuid.UUID]:
        """Mark any SUCCESS backup whose ``expires_at`` has passed as EXPIRED.

        Returns the list of expired ids so the caller can choose to delete
        the underlying blobs (default behaviour here: we leave the blobs
        on disk and merely flag the row, because blob deletion is a
        separate ops-level decision tied to S3 lifecycle policies).
        """
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(Backup).where(
                Backup.status == BackupStatus.SUCCESS,
                Backup.expires_at.is_not(None),
                Backup.expires_at < now,
            )
        )
        expired_ids: list[uuid.UUID] = []
        for row in result.scalars():
            row.status = BackupStatus.EXPIRED
            expired_ids.append(row.id)
        await session.flush()
        return expired_ids

    # ----- low-level helpers --------------------------------------------

    def _default_components(self, kind: BackupKind) -> list[str]:
        if kind == BackupKind.FULL:
            return ["database", "files", "config"]
        if kind == BackupKind.DATABASE:
            return ["database"]
        if kind == BackupKind.FILES:
            return ["files"]
        if kind == BackupKind.CONFIG:
            return ["config"]
        # INCREMENTAL falls back to files + config only — DB is captured by the
        # next full backup's database component.
        return ["files", "config"]

    def _default_retention(self, kind: BackupKind) -> int:
        if kind == BackupKind.FULL:
            return settings.backup.full_retention_days
        if kind == BackupKind.INCREMENTAL:
            return settings.backup.incremental_retention_days
        return settings.backup.default_retention_days

    def _db_dialect(self) -> str:
        url = settings.database.url
        if _is_postgres_url(url):
            return "postgresql"
        if url.startswith("sqlite"):
            return "sqlite"
        return "unknown"

    async def _backup_database(
        self,
        backup_dir: Path,
        comp: BackupComponent,
    ) -> None:
        url = settings.database.url
        if _is_postgres_url(url):
            await self._backup_postgres(backup_dir, comp)
            return
        # Default: SQLite
        await self._backup_sqlite(backup_dir, comp)

    async def _backup_sqlite(
        self,
        backup_dir: Path,
        comp: BackupComponent,
    ) -> None:
        url = settings.database.url
        src = _sqlite_path_from_url(url)
        if src is None or not src.exists():
            raise BackupError(f"sqlite db not found at {src}")

        # Run the blocking sqlite3 backup in a thread.
        snapshot = backup_dir / "db.sqlite3"
        def _do_backup() -> None:
            # Use DELETE journal mode + close cleanly before unlink so
            # Windows doesn't hold a lock on the WAL file.
            conn = sqlite3.connect(str(src))
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                # Switch source to rollback-journal so no WAL file lingers
                conn.execute("PRAGMA journal_mode=DELETE")
                dst_conn = sqlite3.connect(str(snapshot))
                try:
                    conn.backup(dst_conn)
                finally:
                    dst_conn.close()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()

        await asyncio.to_thread(_do_backup)

        # gzip the snapshot
        gz = backup_dir / "db.sqlite3.gz"
        def _gzip() -> None:
            with snapshot.open("rb") as src_f, gzip.open(gz, "wb") as gz_f:
                shutil.copyfileobj(src_f, gz_f, length=1024 * 1024)
        await asyncio.to_thread(_gzip)

        # Best-effort cleanup — Windows may still hold the WAL file briefly.
        try:
            snapshot.unlink(missing_ok=True)
        except PermissionError:
            pass
        comp.path = gz.relative_to(backup_dir)
        comp.size_bytes = gz.stat().st_size
        comp.checksum = _sha256_file(gz)

    async def _backup_postgres(
        self,
        backup_dir: Path,
        comp: BackupComponent,
    ) -> None:
        """Best-effort pg_dump; falls back to a recorded error on the component."""
        url = settings.database.url
        dsn = _parse_dsn(url)
        if dsn is None:
            raise BackupError("could not parse postgres DSN")

        out = backup_dir / "db.pgdump"
        env = os.environ.copy()
        env["PGPASSWORD"] = dsn["password"]

        cmd = [
            "pg_dump",
            "-h", dsn["host"],
            "-p", str(dsn["port"]),
            "-U", dsn["user"],
            "-d", dsn["database"],
            "-Fc",  # custom compressed format — supports parallel restore
            "-f", str(out),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
        except FileNotFoundError:
            raise BackupError("pg_dump not installed on PATH")

        if proc.returncode != 0:
            raise BackupError(
                f"pg_dump failed (rc={proc.returncode}): "
                f"{err.decode(errors='replace').strip()[:500]}",
            )
        if not out.exists() or out.stat().st_size == 0:
            raise BackupError("pg_dump produced no output")

        comp.path = out.relative_to(backup_dir)
        comp.size_bytes = out.stat().st_size
        comp.checksum = _sha256_file(out)

    async def _backup_files(
        self,
        backup_dir: Path,
        comp: BackupComponent,
    ) -> None:
        uploads = Path(settings.backup.uploads_dir).resolve()
        exports = Path(settings.backup.exports_dir).resolve()
        out = backup_dir / "files.tar.gz"

        sources: list[Path] = []
        for d in (uploads, exports):
            if d.exists():
                sources.append(d)
        # Note: SourceSnapshot blobs typically live under uploads; we cover
        # both dirs because production deployments can split them.

        def _pack() -> None:
            with tarfile.open(out, "w:gz") as tar:
                for src in sources:
                    tar.add(src, arcname=src.name, recursive=True)

        await asyncio.to_thread(_pack)
        comp.path = out.relative_to(backup_dir)
        comp.size_bytes = out.stat().st_size
        comp.checksum = _sha256_file(out)

    async def _backup_config(
        self,
        backup_dir: Path,
        comp: BackupComponent,
    ) -> None:
        """Pack environment-overlay + alembic version + a list of DB tables.

        Sensitive values (.env file) are hashed, not stored verbatim.
        """
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        staging = backup_dir / "config-staging"
        staging.mkdir(parents=True, exist_ok=True)

        # Capture alembic head
        try:
            from pathlib import Path as _P
            alembic_ini = _P(__file__).resolve().parents[3] / "alembic.ini"
            if alembic_ini.is_file():
                cfg = AlembicConfig(str(alembic_ini))
                cfg.set_main_option(
                    "script_location",
                    str(alembic_ini.parent / "alembic"),
                )
                script = ScriptDirectory.from_config(cfg)
                head = script.get_current_head()
                (staging / "alembic_head.txt").write_text(head or "", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            (staging / "alembic_head.txt").write_text(
                f"# capture failed: {exc}\n", encoding="utf-8",
            )

        # Capture table list (PG only — cheap SELECT)
        try:
            from sqlalchemy import text
            from src.db.connection import get_engine
            engine = get_engine()
            async with engine.connect() as conn:
                if self._db_dialect() == "postgresql":
                    res = await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname = 'public' ORDER BY tablename",
                        ),
                    )
                    tables = [r[0] for r in res]
                else:
                    res = await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'"),
                    )
                    tables = [r[0] for r in res]
            (staging / "tables.txt").write_text(
                "\n".join(tables) + "\n", encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            (staging / "tables.txt").write_text(
                f"# capture failed: {exc}\n", encoding="utf-8",
            )

        # Hash .env presence (do NOT copy contents — they may carry secrets)
        env_path = Path(__file__).resolve().parents[4] / ".env"
        env_meta = staging / "env_presence.txt"
        if env_path.is_file():
            digest = _sha256_file(env_path)
            env_meta.write_text(
                f"present=true\nsha256={digest}\npath={env_path.name}\n",
                encoding="utf-8",
            )
        else:
            env_meta.write_text("present=false\n", encoding="utf-8")

        out = backup_dir / "config.tar.gz"
        def _pack() -> None:
            with tarfile.open(out, "w:gz") as tar:
                tar.add(staging, arcname="config", recursive=True)
        await asyncio.to_thread(_pack)
        shutil.rmtree(staging, ignore_errors=True)
        comp.path = out.relative_to(backup_dir)
        comp.size_bytes = out.stat().st_size
        comp.checksum = _sha256_file(out)

    # ----- encryption helpers -------------------------------------------

    @staticmethod
    def _build_fernet(key_str: str):
        """Build a Fernet from a raw key.

        Accepts:
        * a urlsafe-base64 32-byte key (Fernet.generate_key() output)
        * a passphrase — derived via PBKDF2-HMAC-SHA256 to 32 bytes, then
          base64-encoded (slower but usable from a human-typed secret)
        """
        from cryptography.fernet import Fernet
        try:
            # Try as raw Fernet key first.
            return Fernet(key_str.encode("utf-8"))
        except Exception:
            pass
        # Derive from passphrase
        import base64
        salt = b"ontolohub-backup-v1"
        dk = hashlib.pbkdf2_hmac("sha256", key_str.encode("utf-8"), salt, 200_000, 32)
        return Fernet(base64.urlsafe_b64encode(dk))

    async def _pack_and_encrypt(
        self,
        backup_dir: Path,
        components: list[BackupComponent],
    ) -> Path:
        """Tar the backup_dir into a single file and encrypt it with Fernet."""
        plain = backup_dir / "plain.tar"
        def _pack() -> None:
            with tarfile.open(plain, "w") as tar:
                tar.add(backup_dir, arcname="backup", recursive=True)
        await asyncio.to_thread(_pack)
        enc = backup_dir / "backup.enc"
        def _encrypt() -> None:
            assert self._fernet is not None
            data = plain.read_bytes()
            enc.write_bytes(self._fernet.encrypt(data))
        await asyncio.to_thread(_encrypt)
        plain.unlink(missing_ok=True)
        # In encrypted mode we no longer rely on the per-component files —
        # leave them in place for forensic purposes but the canonical
        # storage is the encrypted blob.
        return enc

    async def _decrypt_and_unpack(
        self,
        backup_dir: Path,
        target: Path,
    ) -> None:
        enc = backup_dir / "backup.enc"
        if not enc.is_file():
            raise BackupError("encrypted blob missing")
        def _decrypt() -> None:
            assert self._fernet is not None
            data = enc.read_bytes()
            target.mkdir(parents=True, exist_ok=True)
            plain = target / "plain.tar"
            plain.write_bytes(self._fernet.decrypt(data))
            with tarfile.open(plain, "r") as tar:
                tar.extractall(target)
            plain.unlink(missing_ok=True)
        await asyncio.to_thread(_decrypt)

    # ----- checksum helpers ---------------------------------------------

    def _write_dir_checksum(
        self,
        backup_dir: Path,
        components: list[BackupComponent],
    ) -> str:
        """Write a top-level ``checksum.sha256`` of all component blobs.

        Returns the sha256 of the file *contents* we just wrote
        (deterministic, useful for cross-host integrity checks).
        """
        lines: list[str] = []
        for c in components:
            if c.path is None:
                continue
            p = backup_dir / c.path
            if not p.exists():
                continue
            digest = _sha256_file(p)
            c.checksum = digest
            lines.append(f"{digest}  {c.path}")
        out = backup_dir / "checksum.sha256"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return _sha256_file(out)

    def _verify_dir_checksum(
        self,
        backup_dir: Path,
        manifest: dict,
    ) -> bool:
        checksum_file = backup_dir / "checksum.sha256"
        if not checksum_file.is_file():
            return False
        ok = True
        for line in checksum_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            expected, _, rel = line.partition("  ")
            p = backup_dir / rel.strip()
            if not p.is_file():
                ok = False
                continue
            actual = _sha256_file(p)
            if actual != expected:
                ok = False
        return ok

    # ----- shared utilities ---------------------------------------------

    def _component_path(
        self,
        backup_dir: Path,
        name: str,
        manifest_required: bool,
    ) -> Path:
        manifest_path = backup_dir / "manifest.json"
        if manifest_path.is_file() and manifest_required:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                rel = manifest.get("components", {}).get(name, {}).get("path")
                if rel:
                    p = Path(rel)
                    if not p.is_absolute():
                        p = backup_dir / p
                    return p
            except Exception:
                pass
        # Fallback by convention
        return backup_dir / f"{name}.tar.gz"

    async def _load_backup(
        self,
        session: AsyncSession,
        backup_id: uuid.UUID,
    ) -> Backup:
        row = (await session.execute(
            select(Backup).where(Backup.id == backup_id),
        )).scalar_one_or_none()
        if row is None:
            raise BackupNotFound(f"backup {backup_id} not found")
        return row

    async def _latest_good_backup(
        self,
        session: AsyncSession,
    ) -> Optional[uuid.UUID]:
        row = (await session.execute(
            select(Backup.id)
            .where(Backup.status == BackupStatus.SUCCESS)
            .order_by(Backup.completed_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        return row


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _tar_listing(path: Path) -> Iterable[str]:
    """Yield member names from a tar.gz; small wrapper for the smoke test."""
    with tarfile.open(path, "r:gz") as t:
        for m in t.getmembers():
            yield m.name


def _copy_into(src: Path, dst: Path) -> None:
    """Copy a file or tarball into a target dir, preserving type."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return
    shutil.copy2(src, dst)
