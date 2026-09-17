"""debug coerce_diff"""
import sys
sys.path.insert(0, "D:/qiushi/ontoloHub/apps/api")

import asyncio
from src.db.connection import async_session_factory
from src.db.project import Project, ProjectStatus
from src.db.identity import User, Membership, Role
from src.api.auth import coerce_diff

async def main():
    async with async_session_factory() as s:
        # create a project
        p = Project(name="Debug", status=ProjectStatus.DISCOVERY)
        s.add(p)
        await s.flush()
        await s.refresh(p)
        d = coerce_diff(p)
        print("DIFF:", d)
        print("---")
        for k, v in d.items():
            print(f"  {k}: {type(v).__name__} = {v!r}")

asyncio.run(main())
