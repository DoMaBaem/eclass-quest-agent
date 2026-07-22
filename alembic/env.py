"""asyncmy 비동기 URL로 Alembic migration을 실행하는 환경 설정.

Alembic 본체는 동기 migration 함수를 기대하므로 async engine 연결을 만든 뒤 ``run_sync``로
동기 migration 문맥에 넘긴다. 테이블 메타데이터는 app.storage.models.Base 한 곳에서 읽는다.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.storage.models import Base

# alembic.ini를 읽어 만들어진 전역 실행 설정이다.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
if settings.mysql_url:
    # alembic.ini의 가짜 기본 URL보다 현재 .env의 MYSQL_URL을 우선한다.
    config.set_main_option("sqlalchemy.url", settings.mysql_url)

target_metadata = Base.metadata


def do_run_migrations(connection: object) -> None:
    """비동기 연결에서 건네받은 동기 facade로 실제 revision을 적용한다."""

    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """migration 전용 연결을 열고 적용 후 엔진까지 정리한다."""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    raise RuntimeError("이 프로젝트의 MySQL 마이그레이션은 온라인 연결로만 실행합니다.")
else:
    run_migrations_online()
