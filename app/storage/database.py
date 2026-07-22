"""SQLAlchemy 비동기 엔진, 세션 수명, 사용자별 브라우저 작업 잠금.

``Database``는 연결 풀과 트랜잭션 경계를 관리하고, Repository는 전달받은 AsyncSession으로
쿼리만 수행한다. 이 분리 덕분에 commit/rollback 책임이 한곳에 모인다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.storage.models import UserModel


class Database:
    """애플리케이션 전역에서 재사용하는 비동기 MySQL 연결 묶음."""

    def __init__(self, mysql_url: str) -> None:
        # pool_pre_ping은 풀에서 꺼낸 오래된 연결이 살아 있는지 먼저 확인한다.
        # Docker/MySQL이 꺼졌을 때 TUI가 영원히 '동기화 대기 중'에 머물지 않도록
        # 연결 생성과 풀 대기를 각각 5초로 제한한다.
        self.engine = create_async_engine(
            mysql_url,
            pool_pre_ping=True,
            pool_timeout=5,
            connect_args={"connect_timeout": 5},
        )
        # commit 뒤에도 이미 읽은 속성을 사용할 수 있게 expire_on_commit=False를 둔다.
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        if not settings.mysql_url:
            raise ValueError("MYSQL_URL이 필요합니다.")
        return cls(settings.mysql_url)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """세션 블록이 성공하면 commit, 예외가 나면 rollback한다."""

        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()


@asynccontextmanager
async def user_browser_lock(session: AsyncSession, user_id: str) -> AsyncIterator[None]:
    """동일 사용자의 Playwright 작업을 MySQL 행 잠금으로 직렬화한다.

    사용자 행이 없으면 먼저 만든 뒤 다시 잠근다. 호출자는 반드시 Database.session() 안에서
    사용해야 하며, 블록이 끝나면 해당 트랜잭션의 commit/rollback과 함께 잠금이 풀린다.
    """

    user = await session.get(UserModel, user_id)
    if user is None:
        # SELECT FOR UPDATE는 존재하지 않는 행을 잠글 수 없으므로 사용자 행을 먼저 보장한다.
        session.add(UserModel(id=user_id))
        await session.flush()
    # 트랜잭션이 끝날 때까지 같은 user_id의 다른 브라우저 작업이 여기서 기다린다.
    await session.execute(select(UserModel).where(UserModel.id == user_id).with_for_update())
    yield
