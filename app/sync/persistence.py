"""E-Class 최신 상태, snapshot, 변경, 알림 중복 정보를 MySQL에 저장한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.domain import Announcement, Assignment, Course, Grade, Lecture
from app.storage.models import (
    AnnouncementModel,
    AssignmentModel,
    CourseModel,
    GradeModel,
    LectureModel,
    NotificationHistoryModel,
    SyncHistoryModel,
    UserModel,
)
from app.storage.snapshot_repository import SnapshotRepository
from app.storage.mission_repository import MissionRepository
from app.sync.schemas import DeadlineCandidate, SyncTrigger


@dataclass(frozen=True, slots=True)
class PersistedSync:
    changes: list[dict[str, object]]
    change_event_ids: list[str]
    deadlines: list[DeadlineCandidate]


class SyncPersistence:
    """동기화 한 번의 DB 작업을 일관된 트랜잭션 안에서 수행한다."""

    def __init__(self, session: AsyncSession, *, user_id: str) -> None:
        self.session = session
        self.user_id = user_id

    async def ensure_user(self) -> None:
        if await self.session.get(UserModel, self.user_id) is None:
            self.session.add(UserModel(id=self.user_id, settings_json={}))
            await self.session.flush()

    async def start_history(self, trigger: SyncTrigger, started_at: datetime) -> int:
        await self.ensure_user()
        row = SyncHistoryModel(
            user_id=self.user_id,
            trigger_type=trigger.value,
            status="RUNNING",
            started_at=started_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def finish_history(
        self,
        history_id: int,
        *,
        status: str,
        finished_at: datetime,
        change_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        row = await self.session.get(SyncHistoryModel, history_id)
        if row is None:
            return
        row.status = status
        row.finished_at = finished_at
        row.change_count = change_count
        row.error_code = error_code

    async def persist_entities(
        self,
        *,
        history_id: int,
        courses: list[Course],
        announcements: list[Announcement],
        assignments: list[Assignment],
        lectures: list[Lecture],
        grades: list[Grade],
        deadline_candidates: list[DeadlineCandidate],
        finished_at: datetime,
    ) -> PersistedSync:
        await self.ensure_user()
        previous_success = await self.session.scalar(
            select(SyncHistoryModel.id)
            .where(
                SyncHistoryModel.user_id == self.user_id,
                SyncHistoryModel.status == "COMPLETED",
                SyncHistoryModel.id != history_id,
            )
            .limit(1)
        )
        notify_on_first_seen = previous_success is not None
        snapshot_repository = SnapshotRepository(self.session)
        new_change_count = 0

        collections: tuple[tuple[str, list[object]], ...] = (
            ("course", list(courses)),
            ("announcement", list(announcements)),
            ("assignment", list(assignments)),
            ("lecture", list(lectures)),
            ("grade", list(grades)),
        )
        for entity_type, entities in collections:
            for entity in entities:
                database_payload = entity.model_dump(mode="python")  # type: ignore[attr-defined]
                payload = entity.model_dump(mode="json")  # type: ignore[attr-defined]
                await self._upsert_entity(entity_type, database_payload)
                result = await snapshot_repository.record_snapshot(
                    user_id=self.user_id,
                    entity_type=entity_type,
                    entity_id=str(payload["id"]),
                    payload=payload,
                    notify_on_first_seen=notify_on_first_seen,
                )
                if result.change_event_created:
                    new_change_count += 1

        # 동기화된 검증 결과에서만 미션을 만들며 같은 원본은 Repository 고유키로 갱신한다.
        missions = MissionRepository(self.session, user_id=self.user_id)
        for assignment in assignments:
            await missions.create_or_update(
                source_type="ASSIGNMENT",
                source_id=assignment.id,
                title=assignment.title,
                due_at=assignment.due_at,
                completed=assignment.submitted is True or assignment.status.value == "COMPLETE",
            )
        for lecture in lectures:
            await missions.create_or_update(
                source_type="LECTURE",
                source_id=lecture.id,
                title=lecture.title,
                due_at=lecture.available_until,
                completed=lecture.status.value == "COMPLETE",
            )

        # 직전 Manager 호출이 실패한 경우를 포함해 PENDING 이벤트를 다시 전달한다.
        pending_events = await snapshot_repository.get_pending_manager_events(
            user_id=self.user_id,
            limit=50,
        )
        changes = [
            {
                "change_type": event.change_type,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "payload": event.payload,
            }
            for event in pending_events
        ]
        event_ids = [event.event_id for event in pending_events]

        deadlines = await self._reserve_deadlines(deadline_candidates, now=finished_at)
        await self.finish_history(
            history_id,
            status="COMPLETED",
            finished_at=finished_at,
            change_count=new_change_count,
        )
        return PersistedSync(changes=changes, change_event_ids=event_ids, deadlines=deadlines)

    async def _upsert_entity(self, entity_type: str, payload: dict[str, object]) -> None:
        model, values = self._model_values(entity_type, payload)
        row = await self.session.scalar(
            select(model).where(model.user_id == self.user_id, model.eclass_id == str(payload["id"]))
        )
        if row is None:
            self.session.add(model(user_id=self.user_id, eclass_id=str(payload["id"]), **values))
            return
        for key, value in values.items():
            setattr(row, key, value)

    @staticmethod
    def _model_values(entity_type: str, payload: dict[str, object]) -> tuple[type, dict[str, object]]:
        if entity_type == "course":
            return CourseModel, {
                "name": payload["name"], "professor": payload.get("professor"), "url": payload["url"],
                "year": payload["year"], "semester": payload["semester"], "status": payload["status"],
            }
        if entity_type == "assignment":
            return AssignmentModel, {
                "course_eclass_id": payload["course_id"], "title": payload["title"], "url": payload["url"],
                "due_at": payload.get("due_at"), "submitted": payload.get("submitted"),
                "submitted_at": payload.get("submitted_at"), "status": payload["status"],
            }
        if entity_type == "lecture":
            return LectureModel, {
                "course_eclass_id": payload["course_id"], "title": payload["title"], "url": payload["url"],
                "week": payload.get("week"), "progress_percent": payload.get("progress_percent"),
                "available_from": payload.get("available_from"), "available_until": payload.get("available_until"),
                "attendance_status": payload["attendance_status"], "completed_at": payload.get("completed_at"),
                "status": payload["status"],
            }
        if entity_type == "announcement":
            return AnnouncementModel, {
                "course_eclass_id": payload.get("course_id"), "title": payload["title"], "url": payload["url"],
                "posted_at": payload.get("posted_at"), "status": payload["status"],
            }
        if entity_type == "grade":
            return GradeModel, {
                "course_eclass_id": payload["course_id"], "item": payload["item"],
                "score": payload.get("score"), "published_at": payload.get("published_at"),
                "status": payload["status"],
            }
        raise ValueError(f"지원하지 않는 엔터티 유형입니다: {entity_type}")

    async def _reserve_deadlines(
        self, candidates: list[DeadlineCandidate], *, now: datetime
    ) -> list[DeadlineCandidate]:
        reserved: list[DeadlineCandidate] = []
        for candidate in candidates:
            exists = await self.session.scalar(
                select(NotificationHistoryModel.id).where(
                    NotificationHistoryModel.user_id == self.user_id,
                    NotificationHistoryModel.dedupe_key == candidate.dedupe_key,
                )
            )
            if exists is not None:
                continue
            self.session.add(
                NotificationHistoryModel(
                    user_id=self.user_id,
                    entity_type=candidate.entity_type,
                    entity_id=candidate.entity_id,
                    notification_type=candidate.notification_type,
                    dedupe_key=candidate.dedupe_key,
                    notified_at=now,
                )
            )
            reserved.append(candidate)
        await self.session.flush()
        return reserved
