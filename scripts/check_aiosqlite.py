import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\qiushi\ontoloHub")
sys.path.insert(0, str(ROOT / "apps" / "api"))

import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

async def main():
    tmp = Path(tempfile.gettempdir()) / f"_dbtest_{int(os.environ.get('TEST_ID', '0'))}.db"
    if tmp.exists():
        tmp.unlink()
    print(f"tmp path str: {str(tmp)}")
    url = f"sqlite+aiosqlite:///{tmp}"
    print(f"url: {url}")
    eng = create_async_engine(url)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy import text
        result = await s.execute(text("SELECT 1"))
        print("query OK:", result.scalar())
    print("file exists:", tmp.exists(), "size:", tmp.stat().st_size if tmp.exists() else None)

asyncio.run(main())
