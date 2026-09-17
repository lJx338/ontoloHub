"""诊断脚本：直接测试 cfg.get_settings + reinit_engines."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\qiushi\ontoloHub")
sys.path.insert(0, str(ROOT / "apps" / "api"))

# 没设置 env
print(f"initial DATABASE_URL: {os.environ.get('DATABASE_URL')}")

from src.core.config import get_settings
print(f"initial get_settings().database.url: {get_settings().database.url}")

# 假装 monkeypatch
new_path = Path(tempfile.gettempdir()) / "_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{new_path}"
print(f"set DATABASE_URL: {os.environ['DATABASE_URL']}")

# 清缓存
get_settings.cache_clear()
s = get_settings()
print(f"after cache_clear: s.database.url = {s.database.url}")

# 调用 _build_async_engine
from src.db import connection as conn_module
print(f"BEFORE reinit: async_engine.url = {conn_module.async_engine.url}")
asyncio.run(conn_module.reinit_engines())
print(f"AFTER reinit: async_engine.url = {conn_module.async_engine.url}")
print(f"AFTER reinit: async_session_factory = {conn_module.async_session_factory}")
