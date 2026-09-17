"""OntoloHub CLI 入口（typer）。

子命令：
    ontolohub version           打印版本
    ontolohub doctor           环境自检
    ontolohub db status        查看 DB 连接状态
    ontolohub db migrate       运行 Alembic 升级
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import create_engine, text

from ontolohub_cli import __version__

# cli/src/ontolohub_cli/main.py -> parents[4] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[4]

app = typer.Typer(
    name="ontolohub",
    help="OntoloHub 管理 CLI",
    add_completion=False,
    invoke_without_command=True,
)

# 让 Windows GBK 控制台也能正常输出（避免 rich UnicodeEncodeError）。
# 重新配置 stdout/stderr 为 UTF-8（Python 3.7+）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

# Rich console 启用 force_terminal 但禁用终端颜色宽度问题；safe_box 避免方框字
# 在某些 legacy 终端上的乱码。
console = Console(force_terminal=False, soft_wrap=True)
db_app = typer.Typer(help="数据库相关")
app.add_typer(db_app, name="db")


# -----------------------------------------------------------------------------
@app.command()
def version() -> None:
    """打印 CLI 与运行信息。"""
    table = Table(show_header=False, box=None)
    table.add_row("ontolohub-cli", __version__)
    table.add_row("Python", sys.version.split()[0])
    table.add_row("Platform", platform.platform())
    console.print(table)


# -----------------------------------------------------------------------------
@app.command()
def doctor() -> None:
    """环境自检：python / node / git / 可写目录。"""
    rows: list[tuple[str, str, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        rows.append((label, "OK" if ok else "FAIL", detail or ("found" if ok else "missing")))

    py = shutil.which("python") or shutil.which("python3")
    check("python", bool(py), py or "not found")

    node = shutil.which("node")
    check("node", bool(node), node or "not found")

    git = shutil.which("git")
    check("git", bool(git), git or "not found")

    cwd = Path.cwd()
    check("cwd is writable", os.access(cwd, os.W_OK), str(cwd))

    table = Table(title="OntoloHub doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for row in rows:
        table.add_row(*row)
    console.print(table)

    if any(r[1] == "FAIL" for r in rows):
        raise typer.Exit(code=1)


# -----------------------------------------------------------------------------
@db_app.command("status")
def db_status(
    url: Optional[str] = typer.Option(None, "--url", help="Override DATABASE_URL"),
) -> None:
    """Probe the configured database connection."""
    db_url = url or os.environ.get("DATABASE_URL", "")

    # 若 env 没设，尝试从 api 的 settings 读默认 URL
    if not db_url:
        try:
            from src.core.config import settings  # type: ignore[import-not-found]

            db_url = settings.database.url
        except Exception:  # noqa: BLE001
            db_url = "sqlite+aiosqlite:///./data/ontolohub.db"

    # 同步驱动 URL 探测：剥掉 +asyncpg / +aiosqlite
    sync_url = db_url
    for _drv in ("+asyncpg", "+aiosqlite"):
        sync_url = sync_url.replace(_drv, "")
    try:
        engine = create_engine(sync_url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        console.print(f"[green]connected[/green]  {db_url}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{type(exc).__name__}[/red] {exc}  ({db_url})")
        raise typer.Exit(code=1) from None


# -----------------------------------------------------------------------------
@db_app.command("migrate")
def db_migrate(
    revision: str = typer.Option("head", "--to", help="Target revision"),
) -> None:
    """Run Alembic migrations from apps/api."""
    api_dir = REPO_ROOT / "apps" / "api"
    if not (api_dir / "alembic.ini").exists():
        console.print(f"[red]alembic.ini not found at {api_dir}[/red]")
        raise typer.Exit(code=1)

    cmd = [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", revision]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]  (cwd={api_dir})")
    result = subprocess.run(cmd, cwd=api_dir, check=False)
    raise SystemExit(result.returncode)


# -----------------------------------------------------------------------------
@db_app.command("seed")
def db_seed() -> None:
    """Seed demo data (M0: project + sample ontology + 1 evidence)."""
    console.print("[yellow]seed not implemented yet - see HIA-66 (A18 E2E + seed)[/yellow]")
    raise typer.Exit(code=0)


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app()