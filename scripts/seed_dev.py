"""Dev seed (HIA-51)

触发 ``ensure_bootstrap_admin``，并（可选）建一个 demo 项目 + 把 admin 设为 OWNER。

用法:
    python -m scripts.seed_dev            # 仅创建 admin
    python -m scripts.seed_dev --demo     # 额外创建一个 demo 项目
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本能从仓库根目录直接 ``python scripts/seed_dev.py`` 运行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))

from sqlalchemy import select  # noqa: E402

from src.api.auth import ensure_bootstrap_admin  # noqa: E402
from src.db.connection import async_session_factory  # noqa: E402
from src.db.identity import Membership, Role  # noqa: E402
from src.db.project import Project, ProjectStatus  # noqa: E402


async def seed(demo: bool) -> None:
    admin = await ensure_bootstrap_admin()
    print(f"✔ admin: {admin.email} ({admin.id}) global_role={admin.global_role}")

    if not demo:
        return

    async with async_session_factory() as session:
        existing = await session.execute(
            select(Project).where(Project.name == "Demo Project")
        )
        if existing.scalar_one_or_none():
            print("• demo project already exists, skip")
            return
        p = Project(
            name="Demo Project",
            description="HIA-51 seed demo project",
            status=ProjectStatus.DISCOVERY,
            owner_id=admin.id,
            created_by=admin.id,
            customer_name="Acme Manufacturing",
            target_environment="local",
            owner_name=admin.display_name,
        )
        session.add(p)
        await session.flush()
        session.add(
            Membership(
                user_id=admin.id,
                project_id=p.id,
                role=Role.OWNER.value,
                invited_by=admin.id,
                is_active=True,
            )
        )
        await session.commit()
        print(f"✔ demo project created: {p.id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dev seed")
    parser.add_argument("--demo", action="store_true", help="create demo project")
    args = parser.parse_args()
    asyncio.run(seed(args.demo))


if __name__ == "__main__":
    main()
