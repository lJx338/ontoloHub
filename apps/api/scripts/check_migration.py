"""Quick smoke-test: run alembic upgrade head against a fresh sqlite DB."""
import os
import sys
import tempfile
from pathlib import Path

# Setup
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"

from src.core import config  # noqa: E402

config.get_settings.cache_clear()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

alembic_cfg = Config(str(ROOT / "alembic.ini"))
alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
command.upgrade(alembic_cfg, "head")
print("migration upgrade OK")

import sqlite3  # noqa: E402

con = sqlite3.connect(db)
tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()]
print("\nTables:")
for t in tables:
    print(f"  {t}")

# Verify new workflow columns exist
print("\nworkflows columns:")
for r in con.execute("PRAGMA table_info(workflows)").fetchall():
    print(f"  {r[1]}: {r[2]}")

print("\nworkflow_executions columns:")
for r in con.execute("PRAGMA table_info(workflow_executions)").fetchall():
    print(f"  {r[1]}: {r[2]}")

print("\ntrigger_configs columns (workflow_id added?):")
cols = [r[1] for r in con.execute("PRAGMA table_info(trigger_configs)").fetchall()]
print(f"  has workflow_id? {'workflow_id' in cols}")

con.close()
os.unlink(db)
