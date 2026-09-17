import sqlite3
import os, tempfile
from pathlib import Path

test_dir = Path(tempfile.gettempdir())

# Try to find recent pytest temp dirs
for sub in ['pytest-11', 'pytest-12', 'pytest-13']:
    base = test_dir / 'pytest-of-liuxi' / sub
    if not base.exists():
        continue
    for test_dir_inner in base.iterdir():
        db = test_dir_inner / 'test.db'
        if db.exists():
            try:
                c = sqlite3.connect(str(db))
                tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                print(f"{db.name}: size={db.stat().st_size}, tables={tables}")
                c.close()
            except Exception as e:
                print(f"{db.name}: error {e}")
