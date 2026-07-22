"""검증된 과제·강의에서 Mission을 중복 없이 생성하고 조회한다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.domain import Mission
from app.storage.models import MissionModel


class MissionRepository:
    def __init__(self, session: AsyncSession, *, user_id: str) -> None:
        self.session = session
        self.user_id = user_id

    @staticmethod
    def calculate_priority(
        *, due_at: datetime | None, completed: bool, now: datetime | None = None
    ) -> str:
        """검증된 마감과 완료 상태만으로 우선순위를 계산한다."""

        if completed:
            return "LOW"
        if due_at is None:
            return "NORMAL"
        current = now or datetime.now(timezone.utc)
        normalized_due = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
        remaining = normalized_due - current
        if remaining <= timedelta(hours=6):
            return "URGENT"
        if remaining <= timedelta(hours=24):
            return "HIGH"
        return "NORMAL"

    async def create_or_update(
        self,
        *,
        source_type: str,
        source_id: str,
        title: str,
        due_at: datetime | None,
        completed: bool = False,
    ) -> tuple[Mission, bool]:
        """(사용자, 원본 유형, 원본 ID) 고유키로 같은 LMS 미션 생성을 막는다."""

        row = await self.session.scalar(
            select(MissionModel).where(
                MissionModel.user_id == self.user_id,
                MissionModel.source_type == source_type,
                MissionModel.source_id == source_id,
            )
        )
        created = row is None
        if row is None:
            row = MissionModel(
                user_id=self.user_id,
                source_type=source_type,
                source_id=source_id,
                title=title,
                status="COMPLETED" if completed else "PENDING",
                priority=self.calculate_priority(due_at=due_at, completed=completed),
                due_at=due_at,
                completed_at=datetime.now(timezone.utc) if completed else None,
            )
            self.session.add(row)
        else:
            row.title = title
            row.due_at = due_at
            if completed and row.status != "COMPLETED":
                row.status = "COMPLETED"
                row.completed_at = datetime.now(timezone.utc)
            row.priority = self.calculate_priority(
                due_at=due_at,
                completed=row.status == "COMPLETED",
            )
        await self.session.flush()
        return self._to_schema(row), created

    async def create_mission(self, **kwargs) -> tuple[Mission, bool]:
        """Mission Service가 검증된 LMS 항목을 중복 없이 저장하는 생성 진입점."""

        return await self.create_or_update(**kwargs)

    async def list_today_missions(self, *, now: datetime | None = None) -> list[Mission]:
        return await self.list_today(now=now)

    async def list_weekly_missions(self, *, now: datetime | None = None) -> list[Mission]:
        return await self.list_weekly(now=now)

    async def mark_mission_completed(self, mission_id: int) -> Mission | None:
        return await self.mark_completed(mission_id)

    async def update_mission(
        self, mission_id: int, *, title: str | None = None, due_at: datetime | None = None
    ) -> Mission | None:
        row = await self.session.get(MissionModel, mission_id)
        if row is None or row.user_id != self.user_id:
            return None
        if title is not None:
            row.title = title
        if due_at is not None:
            row.due_at = due_at
        row.priority = self.calculate_priority(
            due_at=row.due_at,
            completed=row.status == "COMPLETED",
        )
        await self.session.flush()
        return self._to_schema(row)

    async def mark_completed(self, mission_id: int) -> Mission | None:
        row = await self.session.get(MissionModel, mission_id)
        if row is None or row.user_id != self.user_id:
            return None
        row.status = "COMPLETED"
        row.completed_at = datetime.now(timezone.utc)
        row.priority = "LOW"
        await self.session.flush()
        return self._to_schema(row)

    async def list_today(self, *, now: datetime | None = None) -> list[Mission]:
        current = now or datetime.now(timezone.utc)
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._list_between(start, start + timedelta(days=1))

    async def list_weekly(self, *, now: datetime | None = None) -> list[Mission]:
        current = now or datetime.now(timezone.utc)
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._list_between(start, start + timedelta(days=7))

    async def _list_between(self, start: datetime, end: datetime) -> list[Mission]:
        rows = (
            await self.session.scalars(
                select(MissionModel)
                .where(
                    MissionModel.user_id == self.user_id,
                    MissionModel.status == "PENDING",
                    MissionModel.due_at.is_not(None),
                    MissionModel.due_at >= start,
                    MissionModel.due_at < end,
                )
                .order_by(MissionModel.due_at.asc())
            )
        ).all()
        return [self._to_schema(row) for row in rows]

    @staticmethod
    def _to_schema(row: MissionModel) -> Mission:
        return Mission(
            id=row.id,
            source_type=row.source_type,
            source_id=row.source_id,
            title=row.title,
            status=row.status,
            priority=row.priority,
            due_at=row.due_at,
            completed_at=row.completed_at,
        )
