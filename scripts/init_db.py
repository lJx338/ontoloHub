"""初始化数据库。

通过 Alembic 把数据库升级到最新版本。等价于::

    cd apps/api && alembic upgrade head

如果数据库是空的，0001_baseline 会创建所有业务表。
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_DIR = PROJECT_ROOT / "apps" / "api"


def main() -> None:
    if not (API_DIR / "alembic.ini").is_file():
        print(f"ERROR: 找不到 {API_DIR / 'alembic.ini'}", file=sys.stderr)
        sys.exit(1)

    print(f"Upgrading database to head under {API_DIR}...")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=API_DIR,
        check=False,
    )

    if result.returncode != 0:
        print(
            "Alembic upgrade failed. "
            "If 'alembic' module is missing, run `pip install -r requirements.txt` first.",
            file=sys.stderr,
        )
        sys.exit(result.returncode)

    print("Database upgraded to head successfully.")


if __name__ == "__main__":
    main()