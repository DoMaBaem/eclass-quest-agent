"""TUI heartbeat를 E-Class 읽기, MySQL 비교, 능동 이벤트로 연결한다."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.schemas.domain import EntityStatus
from app.schemas.runtime import RuntimeEvent, RuntimeEventType
from app.schemas.workflow import ErrorCode
from app.storage.database import Database
from app.storage.snapshot_repository import SnapshotRepository
from app.sync.deadline import DeadlineService
from app.sync.persistence import SyncPersistence
from app.sync.schemas import (
    AssignmentChecklistItem,
    LectureChecklistItem,
    SyncResult,
    SyncStatus,
    SyncTrigger,
)
from mcp_server.schemas import McpErrorCode, McpResponse, SelectedTerm
from mcp_server.services.eclass_read import EclassReadService


class SyncService:
    """동시 실행을 막고, 변경·마감이 있을 때만 RuntimeEvent를 생성한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        reader: EclassReadService | None = None,
        user_id: str = "local-user",
    ) -> None:
        self.settings = settings
        self.database = database or Database.from_settings(settings)
        self.reader = reader or EclassReadService(settings, user_id=user_id)
        self.user_id = user_id
        self._lock = asyncio.Lock()
        self._paused_for_auth = False
        self._closed = False

    async def sync(self, trigger: SyncTrigger) -> SyncResult:
        """학기 미지정 MCP 호출로 E-Class 기본 학기를 동기화한다."""

        started_at = datetime.now(timezone.utc)
        if self._closed or self._lock.locked():
            return self._result(SyncStatus.SKIPPED, trigger, started_at)
        if self._paused_for_auth and trigger is SyncTrigger.HEARTBEAT:
            return self._result(
                SyncStatus.AUTH_REQUIRED,
                trigger,
                started_at,
                error_code=ErrorCode.AUTH_REQUIRED,
            )

        async with self._lock:
            # DB가 꺼져 있으면 _start_history 자체가 실패한다. 이 호출이 try 밖에 있으면
            # Worker가 결과를 반환하지 못해 TUI가 영원히 '동기화 대기 중'에 남는다.
            try:
                history_id = await asyncio.wait_for(
                    self._start_history(trigger, started_at),
                    timeout=8,
                )
            except Exception:
                return self._result(
                    SyncStatus.FAILED,
                    trigger,
                    started_at,
                    error_code=ErrorCode.TEMPORARY_FAILURE,
                )
            try:
                # Startup과 Heartbeat 모두 같은 업무 단위 계약을 사용한다. MCP 서비스가
                # E-Class 기본 학기를 한 번 확정하고 전체 영역을 모두 읽었을 때만 Snapshot을
                # 반환하므로, 일부 파서 실패를 빈 목록으로 저장하는 삭제 오탐을 막을 수 있다.
                snapshot_result = await self.reader.get_dashboard_snapshot()
                failure = self._failure_code(snapshot_result)
                if failure:
                    return await self._finish_failure(history_id, trigger, started_at, failure)
                selected_term = snapshot_result.selected_term
                snapshot = snapshot_result.data
                if selected_term is None or snapshot is None:
                    return await self._finish_failure(
                        history_id, trigger, started_at, ErrorCode.TEMPORARY_FAILURE
                    )
                courses = snapshot.courses
                announcements = snapshot.announcements
                assignments = snapshot.assignments
                lectures = snapshot.lectures
                grades = snapshot.grades
                now = datetime.now(timezone.utc)
                deadline_candidates = DeadlineService().evaluate(assignments, lectures, now=now)
                async with self.database.session() as session:
                    persisted = await SyncPersistence(session, user_id=self.user_id).persist_entities(
                        history_id=history_id,
                        courses=courses,
                        announcements=announcements,
                        assignments=assignments,
                        lectures=lectures,
                        grades=grades,
                        deadline_candidates=deadline_candidates,
                        finished_at=now,
                    )

                self._paused_for_auth = False
                events = self._build_events(
                    trigger=trigger,
                    selected_term=selected_term,
                    changes=persisted.changes,
                    deadlines=persisted.deadlines,
                    assignments=assignments,
                    lectures=lectures,
                    now=now,
                )
                return SyncResult(
                    status=SyncStatus.COMPLETED,
                    trigger=trigger,
                    selected_term=selected_term,
                    change_count=len(persisted.changes),
                    deadline_count=len(persisted.deadlines),
                    observed_count=sum(
                        map(len, (courses, announcements, assignments, lectures, grades))
                    ),
                    course_count=len(courses),
                    events=events,
                    change_event_ids=persisted.change_event_ids,
                    course_checklist=[
                        {
                            "course_id": course.id,
                            "course_name": course.name,
                        }
                        for course in courses
                    ],
                    lecture_checklist=self._lecture_checklist(
                        lectures,
                        courses=courses,
                        now=now,
                    ),
                    assignment_checklist=self._assignment_checklist(
                        assignments,
                        courses=courses,
                        now=now,
                    ),
                    started_at=started_at,
                    finished_at=now,
                )
            except asyncio.CancelledError:
                await self._finish_failure(
                    history_id,
                    trigger,
                    started_at,
                    ErrorCode.TEMPORARY_FAILURE,
                )
                raise
            except Exception:
                return await self._finish_failure(
                    history_id,
                    trigger,
                    started_at,
                    ErrorCode.TEMPORARY_FAILURE,
                )

    async def mark_change_events_processed(
        self, event_ids: Iterable[str], *, request_id: str
    ) -> None:
        async with self.database.session() as session:
            repository = SnapshotRepository(session)
            for event_id in event_ids:
                await repository.mark_manager_event_processed(
                    event_id=event_id,
                    request_id=request_id,
                )

    def resume_authentication(self) -> None:
        """사용자가 직접 로그인한 뒤 수동 동기화를 다시 시도할 수 있게 한다."""

        self._paused_for_auth = False

    async def close(self) -> None:
        self._closed = True
        # 진행 중인 동기화가 자원을 닫은 후 DB 풀을 정리한다.
        async with self._lock:
            await self.database.dispose()

    async def _start_history(self, trigger: SyncTrigger, started_at: datetime) -> int:
        async with self.database.session() as session:
            return await SyncPersistence(session, user_id=self.user_id).start_history(trigger, started_at)

    async def _finish_failure(
        self,
        history_id: int,
        trigger: SyncTrigger,
        started_at: datetime,
        error_code: ErrorCode,
    ) -> SyncResult:
        finished_at = datetime.now(timezone.utc)
        status = SyncStatus.AUTH_REQUIRED if error_code is ErrorCode.AUTH_REQUIRED else SyncStatus.FAILED
        if status is SyncStatus.AUTH_REQUIRED:
            self._paused_for_auth = True
        async with self.database.session() as session:
            await SyncPersistence(session, user_id=self.user_id).finish_history(
                history_id,
                status=status.value,
                finished_at=finished_at,
                error_code=error_code.value,
            )
        events = []
        if status is SyncStatus.AUTH_REQUIRED:
            events.append(
                RuntimeEvent(
                    event_type=RuntimeEventType.SESSION_EXPIRED,
                    user_id=self.user_id,
                    payload={"action": "run_scripts_login_then_manual_sync"},
                )
            )
        return SyncResult(
            status=status,
            trigger=trigger,
            events=events,
            started_at=started_at,
            finished_at=finished_at,
            error_code=error_code,
        )

    @staticmethod
    def _failure_code(response: McpResponse) -> ErrorCode | None:
        if response.ok:
            return None
        if response.error and response.error.code is McpErrorCode.AUTH_REQUIRED:
            return ErrorCode.AUTH_REQUIRED
        return ErrorCode.TEMPORARY_FAILURE

    def _build_events(
        self,
        *,
        trigger: SyncTrigger,
        selected_term: SelectedTerm,
        changes: list[dict[str, object]],
        deadlines: list,
        assignments: list,
        lectures: list,
        now: datetime,
    ) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        term_payload = selected_term.model_dump(mode="json")
        if changes:
            events.append(
                RuntimeEvent(
                    event_type=RuntimeEventType.LMS_CHANGED,
                    user_id=self.user_id,
                    payload={
                        "selected_term": term_payload,
                        "change_count": len(changes),
                        "changes": [self._manager_change(item) for item in changes[:50]],
                    },
                )
            )

        assignment_deadlines = [item for item in deadlines if item.entity_type == "assignment"]
        attendance_deadlines = [item for item in deadlines if item.entity_type == "lecture"]
        for event_type, items in (
            (RuntimeEventType.DEADLINE_WARNING, assignment_deadlines),
            (RuntimeEventType.ATTENDANCE_WARNING, attendance_deadlines),
        ):
            if items:
                events.append(
                    RuntimeEvent(
                        event_type=event_type,
                        user_id=self.user_id,
                        payload={
                            "selected_term": term_payload,
                            "items": [item.payload for item in items[:50]],
                        },
                    )
                )

        if trigger is SyncTrigger.STARTUP and not events:
            active_assignments = [
                item
                for item in assignments
                if item.submitted is not True
                and item.status is not EntityStatus.COMPLETE
                and item.due_at is not None
                and self._future(item.due_at, now)
            ]
            active_lectures = [
                item
                for item in lectures
                if item.status is not EntityStatus.COMPLETE
                and item.available_until is not None
                and self._future(item.available_until, now)
            ]
            if active_assignments or active_lectures:
                events.append(
                    RuntimeEvent(
                        event_type=RuntimeEventType.STARTUP_BRIEFING,
                        user_id=self.user_id,
                        payload={
                            "selected_term": term_payload,
                            "incomplete_assignment_count": len(active_assignments),
                            "unwatched_lecture_count": len(active_lectures),
                        },
                    )
                )
        return events

    @staticmethod
    def _future(deadline: datetime, now: datetime) -> bool:
        normalized = deadline.replace(tzinfo=timezone.utc) if deadline.tzinfo is None else deadline
        return normalized > now

    @staticmethod
    def _lecture_checklist(
        lectures: list,
        *,
        courses: list | None = None,
        now: datetime,
    ) -> list[LectureChecklistItem]:
        """현재 열려 있는 강의를 완료 항목까지 포함해 미완료·마감순으로 정렬한다."""

        def aware(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        course_names = {course.id: course.name for course in (courses or [])}
        items: list[LectureChecklistItem] = []
        for lecture in lectures:
            opened_at = aware(lecture.available_from)
            closes_at = aware(lecture.available_until)
            if opened_at is not None and opened_at > now:
                continue
            if closes_at is not None and closes_at < now:
                continue
            completed = (
                lecture.status is EntityStatus.COMPLETE
                or lecture.attendance_status is EntityStatus.COMPLETE
                or (lecture.progress_percent is not None and lecture.progress_percent >= 100)
            )
            items.append(
                LectureChecklistItem(
                    lecture_id=lecture.id,
                    course_id=lecture.course_id,
                    course_name=course_names.get(lecture.course_id, "강좌명 확인 불가"),
                    title=lecture.title,
                    week=lecture.week,
                    progress_percent=lecture.progress_percent,
                    completed=completed,
                    available_from=lecture.available_from,
                    available_until=lecture.available_until,
                )
            )
        return sorted(
            items,
            key=lambda item: (
                item.completed,
                aware(item.available_until).timestamp() if item.available_until else float("inf"),
                item.title,
            ),
        )

    @staticmethod
    def _assignment_checklist(
        assignments: list,
        *,
        courses: list,
        now: datetime,
    ) -> list[AssignmentChecklistItem]:
        """기본 학기에서 앞으로 7일 안에 마감되는 과제를 강좌명과 함께 정렬한다."""

        def aware(value: datetime) -> datetime:
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        course_names = {course.id: course.name for course in courses}
        cutoff = now + timedelta(days=7)
        items: list[AssignmentChecklistItem] = []
        for assignment in assignments:
            if assignment.due_at is None:
                continue
            due_at = aware(assignment.due_at)
            if due_at < now or due_at > cutoff:
                continue
            completed = (
                assignment.submitted is True
                or assignment.status is EntityStatus.COMPLETE
            )
            items.append(
                AssignmentChecklistItem(
                    assignment_id=assignment.id,
                    course_name=course_names.get(assignment.course_id, "강좌명 확인 불가"),
                    title=assignment.title,
                    due_at=assignment.due_at,
                    completed=completed,
                )
            )
        return sorted(items, key=lambda item: (item.completed, aware(item.due_at), item.course_name))

    @staticmethod
    def _manager_change(change: dict[str, object]) -> dict[str, object]:
        """Manager에는 변경 판단에 필요한 최소 필드만 전달한다."""

        payload = change.get("payload")
        data = payload if isinstance(payload, dict) else {}
        safe_fields = {
            key: data[key]
            for key in (
                "title",
                "name",
                "item",
                "course_id",
                "due_at",
                "submitted",
                "status",
                "posted_at",
                "week",
                "progress_percent",
                "available_until",
                "attendance_status",
                "score",
            )
            if key in data
        }
        return {
            "change_type": change.get("change_type"),
            "entity_type": change.get("entity_type"),
            "entity_id": change.get("entity_id"),
            "data": safe_fields,
        }

    @staticmethod
    def _result(
        status: SyncStatus,
        trigger: SyncTrigger,
        started_at: datetime,
        *,
        error_code: ErrorCode | None = None,
    ) -> SyncResult:
        return SyncResult(
            status=status,
            trigger=trigger,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            error_code=error_code,
        )
