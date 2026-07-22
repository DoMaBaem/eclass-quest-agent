"""async MySQL 드라이버 실패를 다른 smoke 항목과 격리하는 일회성 DB probe."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.storage.database import Database


async def probe() -> int:
    database = Database.from_settings(get_settings())
    try:
        async with database.engine.connect() as connection:
            if await connection.scalar(text("SELECT 1")) != 1:
                return 1
            current = await connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        if current not in set(ScriptDirectory.from_config(config).get_heads()):
            return 2
        print("connection=ok; migration=head")
        return 0
    except Exception:
        # 실제 DB 예외 문자열에는 호스트·계정이 포함될 수 있으므로 출력하지 않는다.
        return 3
    finally:
        await database.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe()))
