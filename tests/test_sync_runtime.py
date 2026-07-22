"""6단계 동기화·마감·TUI heartbeat 계약 테스트."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from textual.widgets import Input

from app.config import Settings
from app.schemas.domain import Assignment, Course, EntityStatus, Lecture
from app.schemas.runtime import RuntimeEvent, RuntimeEventType
from app.schemas.workflow import ErrorCode
from app.sync.deadline import DeadlineService
from app.sync.persistence import PersistedSync, SyncPersistence
from app.sync.schemas import SyncResult, SyncStatus, SyncTrigger
from app.sync.service import SyncService
from app.storage.models import (
    ChangeEventModel,
    EntitySnapshotModel,
    NotificationHistoryModel,
)
from app.storage.snapshot_repository import SnapshotRepository
from app.tui.app import EclassQuestApp
from app.tui.events import UiOperationState
from mcp_server.schemas import (
    DashboardSnapshotData,
    DashboardSnapshotResult,
    McpErrorCode,
    McpOutcomeStatus,
    McpToolError,
    SelectedTerm,
)


TERM = SelectedTerm(year=2026, semester=3, selection_source="eclass_default")


class FakeDatabase:
    def __init__(self) -> None:
        self.disposed = False

    @asynccontextmanager
    async def session(self):
        yield object()

    async def dispose(self) -> None:
        self.disposed = True


class InMemorySnapshotSession:
    """SnapshotRepository의 쿼리 순서를 실제 저장 상태로 흉내 내는 최소 세션.

    MySQL이 없는 단위 테스트에서도 Repository 자체의 fingerprint·중복 방지 분기를
    실행한다. SQL 문장을 다시 구현하지 않고 조회 대상 모델만 구분한다.
    """

    def __init__(self) -> None:
        self.snapshots: list[EntitySnapshotModel] = []
        self.change_events: list[ChangeEventModel] = []

    async def scalar(self, statement):
        description = statement.column_descriptions[0]
        entity = description["entity"]
        expression = description["expr"]
        params = statement.compile().params
        user_id = params.get("user_id_1")
        entity_type = params.get("entity_type_1")
        entity_id = params.get("entity_id_1")
        if entity is EntitySnapshotModel:
            matching = [
                row
                for row in self.snapshots
                if row.user_id == user_id
                and row.entity_type == entity_type
                and row.entity_id == entity_id
            ]
            # select(EntitySnapshotModel)은 최신 관측값 조회다.
            if expression is EntitySnapshotModel:
                return matching[-1] if matching else None
            fingerprint = params.get("fingerprint_1")
            return next(
                (row.id for row in matching if row.fingerprint == fingerprint),
                None,
            )
        if entity is ChangeEventModel:
            fingerprint = params.get("fingerprint_1")
            return next(
                (
                    row.id
                    for row in self.change_events
                    if row.user_id == user_id
                    and row.entity_type == entity_type
                    and row.entity_id == entity_id
                    and row.fingerprint == fingerprint
                ),
                None,
            )
        raise AssertionError(f"예상하지 못한 조회 모델입니다: {entity}")

    def add(self, row: object) -> None:
        if isinstance(row, EntitySnapshotModel):
            row.id = len(self.snapshots) + 1
            self.snapshots.append(row)
            return
        if isinstance(row, ChangeEventModel):
            row.id = len(self.change_events) + 1
            row.runtime_event_id = f"event-{row.id}"
            self.change_events.append(row)
            return
        raise AssertionError(f"예상하지 못한 저장 모델입니다: {type(row)}")

    async def flush(self) -> None:
        return None


class InMemoryNotificationSession:
    """동일 dedupe key가 이미 예약됐는지 기억하는 최소 알림 세션."""

    def __init__(self) -> None:
        self.notifications: list[NotificationHistoryModel] = []

    async def scalar(self, statement):
        description = statement.column_descriptions[0]
        if description["entity"] is not NotificationHistoryModel:
            raise AssertionError("알림 중복 조회 외의 SQL은 실행하면 안 됩니다.")
        params = statement.compile().params
        return next(
            (
                row.id
                for row in self.notifications
                if row.user_id == params.get("user_id_1")
                and row.dedupe_key == params.get("dedupe_key_1")
            ),
            None,
        )

    def add(self, row: object) -> None:
        if not isinstance(row, NotificationHistoryModel):
            raise AssertionError(f"예상하지 못한 저장 모델입니다: {type(row)}")
        row.id = len(self.notifications) + 1
        self.notifications.append(row)

    async def flush(self) -> None:
        return None


def empty_reader() -> Mock:
    reader = Mock()
    reader.get_dashboard_snapshot = AsyncMock(
        return_value=DashboardSnapshotResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=TERM,
            data=DashboardSnapshotData(),
        )
    )
    return reader


class SnapshotRepositoryDedupeTest(unittest.IsolatedAsyncioTestCase):
    async def test_identical_payload_is_not_stored_or_emitted_twice(self) -> None:
        """같은 정규화 payload 재수집은 snapshot·change event를 늘리지 않는다."""

        session = InMemorySnapshotSession()
        repository = SnapshotRepository(session)  # type: ignore[arg-type]
        baseline_payload = {
            "id": "assignment-1",
            "title": "중복 방지 과제",
            "submitted": False,
            "metadata": {"week": 7, "course": "데이터마이닝"},
        }

        baseline = await repository.record_snapshot(
            user_id="local-user",
            entity_type="assignment",
            entity_id="assignment-1",
            payload=baseline_payload,
        )
        # JSON key 순서만 다른 payload도 같은 fingerprint여야 한다.
        identical = await repository.record_snapshot(
            user_id="local-user",
            entity_type="assignment",
            entity_id="assignment-1",
            payload={
                "metadata": {"course": "데이터마이닝", "week": 7},
                "submitted": False,
                "title": "중복 방지 과제",
                "id": "assignment-1",
            },
        )

        self.assertEqual(baseline.status, "baseline")
        self.assertEqual(identical.status, "unchanged")
        self.assertEqual(baseline.fingerprint, identical.fingerprint)
        self.assertEqual(len(session.snapshots), 1)
        self.assertEqual(session.change_events, [])

        # 실제 값이 바뀌었을 때에는 snapshot과 event가 각각 한 번만 생기고, 같은 변경을
        # 다시 읽으면 둘 다 더 늘어나지 않는 계약까지 함께 검증한다.
        changed_payload = {**baseline_payload, "submitted": True}
        changed = await repository.record_snapshot(
            user_id="local-user",
            entity_type="assignment",
            entity_id="assignment-1",
            payload=changed_payload,
        )
        repeated_change = await repository.record_snapshot(
            user_id="local-user",
            entity_type="assignment",
            entity_id="assignment-1",
            payload=changed_payload,
        )

        self.assertTrue(changed.change_event_created)
        self.assertEqual(repeated_change.status, "unchanged")
        self.assertEqual(len(session.snapshots), 2)
        self.assertEqual(len(session.change_events), 1)


class SyncServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = FakeDatabase()
        self.reader = empty_reader()
        self.persistence = Mock()
        self.persistence.start_history = AsyncMock(return_value=1)
        self.persistence.finish_history = AsyncMock()
        self.persistence.persist_entities = AsyncMock(
            return_value=PersistedSync(changes=[], change_event_ids=[], deadlines=[])
        )
        self.settings = Settings(_env_file=None, mysql_url="mysql+asyncmy://unused")

    async def test_unchanged_sync_creates_no_runtime_event(self) -> None:
        service = SyncService(
            self.settings,
            database=self.database,  # type: ignore[arg-type]
            reader=self.reader,  # type: ignore[arg-type]
        )
        with patch("app.sync.service.SyncPersistence", return_value=self.persistence):
            result = await service.sync(SyncTrigger.HEARTBEAT)

        self.assertEqual(result.status, SyncStatus.COMPLETED)
        self.assertEqual(result.events, [])
        self.assertEqual(result.selected_term.semester_name, "여름학기")  # type: ignore[union-attr]
        self.assertEqual(result.lecture_checklist, [])
        self.assertEqual(result.course_count, 0)
        self.reader.get_dashboard_snapshot.assert_awaited_once_with()

    async def test_database_start_failure_returns_failed_instead_of_hanging(self) -> None:
        """MySQL이 꺼져 있어도 TUI에 반환할 실패 결과를 즉시 만든다."""

        service = SyncService(
            self.settings,
            database=self.database,  # type: ignore[arg-type]
            reader=self.reader,  # type: ignore[arg-type]
        )
        self.persistence.start_history.side_effect = ConnectionError("mysql unavailable")
        with patch("app.sync.service.SyncPersistence", return_value=self.persistence):
            result = await service.sync(SyncTrigger.STARTUP)

        self.assertEqual(result.status, SyncStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.TEMPORARY_FAILURE)
        self.reader.get_dashboard_snapshot.assert_not_awaited()

    async def test_assignments_due_within_seven_days_are_grouped_with_course_name(self) -> None:
        """왼쪽 과제 패널에는 기본 학기의 가까운 과제와 정확한 강좌명을 제공한다."""

        now = datetime.now(timezone.utc)
        self.reader.get_dashboard_snapshot.return_value = DashboardSnapshotResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=TERM,
            data=DashboardSnapshotData(
                courses=[
                    Course(
                        id="10",
                        name="인공지능 개론",
                        professor="홍길동",
                        url="https://example.test/course/10",
                        year=2026,
                        semester=3,
                    )
                ],
                assignments=[
                    Assignment(
                        id="a1",
                        course_id="10",
                        title="이번 주 보고서",
                        url="https://example.test/a1",
                        due_at=now + timedelta(days=2),
                        submitted=False,
                        status=EntityStatus.INCOMPLETE,
                    ),
                    Assignment(
                        id="a2",
                        course_id="10",
                        title="다음 달 보고서",
                        url="https://example.test/a2",
                        due_at=now + timedelta(days=20),
                        submitted=False,
                        status=EntityStatus.INCOMPLETE,
                    ),
                ],
            ),
        )
        service = SyncService(
            self.settings,
            database=self.database,  # type: ignore[arg-type]
            reader=self.reader,  # type: ignore[arg-type]
        )
        with patch("app.sync.service.SyncPersistence", return_value=self.persistence):
            result = await service.sync(SyncTrigger.HEARTBEAT)

        self.assertEqual(len(result.assignment_checklist), 1)
        self.assertEqual(result.assignment_checklist[0].course_name, "인공지능 개론")
        self.assertEqual(result.assignment_checklist[0].title, "이번 주 보고서")

    async def test_open_lectures_are_exposed_as_verified_progress_checklist(self) -> None:
        now = datetime.now(timezone.utc)
        self.reader.get_dashboard_snapshot.return_value = DashboardSnapshotResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=TERM,
            data=DashboardSnapshotData(
                lectures=[
                    Lecture(
                        id="101",
                        course_id="10",
                        title="이번 주 강의",
                        url="https://example.test/101",
                        progress_percent=35,
                        available_from=now - timedelta(days=1),
                        available_until=now + timedelta(days=2),
                        status=EntityStatus.INCOMPLETE,
                        attendance_status=EntityStatus.INCOMPLETE,
                    ),
                    Lecture(
                        id="102",
                        course_id="10",
                        title="아직 안 열린 강의",
                        url="https://example.test/102",
                        progress_percent=0,
                        available_from=now + timedelta(days=1),
                        available_until=now + timedelta(days=5),
                        status=EntityStatus.INCOMPLETE,
                        attendance_status=EntityStatus.INCOMPLETE,
                    ),
                ],
            ),
        )
        service = SyncService(
            self.settings,
            database=self.database,  # type: ignore[arg-type]
            reader=self.reader,  # type: ignore[arg-type]
        )
        with patch("app.sync.service.SyncPersistence", return_value=self.persistence):
            result = await service.sync(SyncTrigger.HEARTBEAT)

        self.assertEqual(len(result.lecture_checklist), 1)
        self.assertEqual(result.lecture_checklist[0].title, "이번 주 강의")
        self.assertEqual(result.lecture_checklist[0].progress_percent, 35)

    async def test_auth_failure_pauses_heartbeat_until_manual_retry(self) -> None:
        self.reader.get_dashboard_snapshot.return_value = DashboardSnapshotResult(
            ok=False,
            status=McpOutcomeStatus.AUTH_REQUIRED,
            error=McpToolError(
                code=McpErrorCode.AUTH_REQUIRED,
                message="로그인 필요",
            ),
        )
        service = SyncService(
            self.settings,
            database=self.database,  # type: ignore[arg-type]
            reader=self.reader,  # type: ignore[arg-type]
        )
        with patch("app.sync.service.SyncPersistence", return_value=self.persistence):
            first = await service.sync(SyncTrigger.STARTUP)
            second = await service.sync(SyncTrigger.HEARTBEAT)

        self.assertEqual(first.status, SyncStatus.AUTH_REQUIRED)
        self.assertEqual(first.events[0].event_type, RuntimeEventType.SESSION_EXPIRED)
        self.assertEqual(second.status, SyncStatus.AUTH_REQUIRED)
        self.reader.get_dashboard_snapshot.assert_awaited_once()

    async def test_overlapping_sync_is_skipped_instead_of_running_twice(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_snapshot(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return DashboardSnapshotResult(
                ok=True,
                status=McpOutcomeStatus.FOUND,
                selected_term=TERM,
                data=DashboardSnapshotData(),
            )

        self.reader.get_dashboard_snapshot.side_effect = delayed_snapshot
        service = SyncService(
            self.settings,
            database=self.database,  # type: ignore[arg-type]
            reader=self.reader,  # type: ignore[arg-type]
        )
        with patch("app.sync.service.SyncPersistence", return_value=self.persistence):
            first_task = asyncio.create_task(service.sync(SyncTrigger.HEARTBEAT))
            await entered.wait()
            second = await service.sync(SyncTrigger.MANUAL)
            release.set()
            first = await first_task

        self.assertEqual(first.status, SyncStatus.COMPLETED)
        self.assertEqual(second.status, SyncStatus.SKIPPED)


class DeadlineServiceTest(unittest.TestCase):
    def test_due_thresholds_exclude_completed_entities(self) -> None:
        now = datetime(2026, 7, 21, tzinfo=timezone.utc)
        assignments = [
            Assignment(
                id="a1",
                course_id="c1",
                title="미제출 과제",
                url="https://example.test/a1",
                due_at=now + timedelta(hours=5),
                submitted=False,
                status=EntityStatus.INCOMPLETE,
            ),
            Assignment(
                id="a2",
                course_id="c1",
                title="제출 완료",
                url="https://example.test/a2",
                due_at=now + timedelta(minutes=30),
                submitted=True,
                status=EntityStatus.COMPLETE,
            ),
        ]
        lectures = [
            Lecture(
                id="l1",
                course_id="c1",
                title="미시청 강의",
                url="https://example.test/l1",
                available_until=now + timedelta(minutes=30),
                status=EntityStatus.INCOMPLETE,
                attendance_status=EntityStatus.INCOMPLETE,
            )
        ]

        candidates = DeadlineService().evaluate(assignments, lectures, now=now)

        self.assertEqual({item.notification_type for item in candidates}, {"assignment_due_6h", "attendance_due_1h"})
        self.assertEqual(len({item.dedupe_key for item in candidates}), 2)


class ProactiveNotificationDedupeTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_deadline_is_reserved_and_emitted_only_once(self) -> None:
        """같은 dedupe key 재평가는 DB 예약과 Runtime 능동 이벤트를 반복하지 않는다."""

        now = datetime(2026, 7, 21, tzinfo=timezone.utc)
        assignment = Assignment(
            id="assignment-1",
            course_id="course-1",
            title="마감 임박 과제",
            url="https://example.test/assignment-1",
            due_at=now + timedelta(hours=5),
            submitted=False,
            status=EntityStatus.INCOMPLETE,
        )
        first_candidates = DeadlineService().evaluate([assignment], [], now=now)
        second_candidates = DeadlineService().evaluate([assignment], [], now=now)
        self.assertEqual(
            [item.dedupe_key for item in first_candidates],
            [item.dedupe_key for item in second_candidates],
        )

        session = InMemoryNotificationSession()
        persistence = SyncPersistence(session, user_id="local-user")  # type: ignore[arg-type]
        first_reserved = await persistence._reserve_deadlines(first_candidates, now=now)
        second_reserved = await persistence._reserve_deadlines(second_candidates, now=now)

        self.assertEqual(len(session.notifications), 1)
        self.assertEqual(len(first_reserved), 1)
        self.assertEqual(second_reserved, [])

        service = SyncService(
            Settings(_env_file=None, mysql_url="mysql+asyncmy://unused"),
            database=FakeDatabase(),  # type: ignore[arg-type]
            reader=empty_reader(),  # type: ignore[arg-type]
        )
        first_events = service._build_events(
            trigger=SyncTrigger.HEARTBEAT,
            selected_term=TERM,
            changes=[],
            deadlines=first_reserved,
            assignments=[],
            lectures=[],
            now=now,
        )
        second_events = service._build_events(
            trigger=SyncTrigger.HEARTBEAT,
            selected_term=TERM,
            changes=[],
            deadlines=second_reserved,
            assignments=[],
            lectures=[],
            now=now,
        )

        self.assertEqual([event.event_type for event in first_events], [RuntimeEventType.DEADLINE_WARNING])
        self.assertEqual(second_events, [])


class FakeTuiSyncService:
    def __init__(self) -> None:
        self.sync = AsyncMock(side_effect=self._sync)
        self.close = AsyncMock()
        self.resume_authentication = Mock()

    async def _sync(self, trigger: SyncTrigger) -> SyncResult:
        now = datetime.now(timezone.utc)
        return SyncResult(
            status=SyncStatus.COMPLETED,
            trigger=trigger,
            selected_term=TERM,
            started_at=now,
            finished_at=now,
        )

    async def mark_change_events_processed(self, *_args, **_kwargs) -> None:
        return None


class TimerProbeApp(EclassQuestApp):
    """실제 Textual clock Timer 호출 횟수만 관찰하는 테스트용 App."""

    def __init__(self, *args, **kwargs) -> None:
        self.clock_tick_count = 0
        super().__init__(*args, **kwargs)

    def _update_clock(self) -> None:
        self.clock_tick_count += 1
        super()._update_clock()


class TuiSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_timer_callback_runs_heartbeat_sync(self) -> None:
        """Textual 주기 타이머의 콜백이 실제 HEARTBEAT 동기화 Worker를 실행한다."""

        fake = FakeTuiSyncService()
        settings = Settings(
            _env_file=None,
            openai_api_key="test-key",
            mysql_url="mysql+asyncmy://unused",
            eclass_sync_on_startup=False,
        )
        app = EclassQuestApp(settings, sync_service=fake)  # type: ignore[arg-type]

        async with app.run_test() as pilot:
            app.runtime.handle_system_event = AsyncMock()
            app._on_sync_heartbeat()
            await pilot.pause()

            fake.sync.assert_awaited_once_with(SyncTrigger.HEARTBEAT)
            self.assertEqual(app.operation_state, UiOperationState.SYNCING)
            self.assertEqual(app.operation_progress, 100)
            self.assertIn("E-Class MCP", app.current_tool)

    async def test_manual_refresh_uses_same_sync_service(self) -> None:
        fake = FakeTuiSyncService()
        settings = Settings(
            _env_file=None,
            openai_api_key="test-key",
            mysql_url="mysql+asyncmy://unused",
            eclass_sync_on_startup=False,
        )
        app = EclassQuestApp(settings, sync_service=fake)  # type: ignore[arg-type]
        app.runtime.handle_user_request = AsyncMock()

        async with app.run_test() as pilot:
            app.runtime.handle_system_event = AsyncMock()
            self.assertIsNotNone(app._sync_timer)
            self.assertEqual(app._sync_timer._interval, 30 * 60)  # type: ignore[union-attr]
            field = app.query_one("#request", Input)
            field.value = "지금 한번 다시 이클래스 정보 업데이트 해줘"
            field.focus()
            await pilot.press("enter")
            await pilot.pause()

            self.assertIn(
                "USER > 지금 한번 다시 이클래스 정보 업데이트 해줘",
                app.transcript,
            )
            self.assertIn(
                "SYSTEM > E-Class 정보 업데이트를 완료했습니다.",
                app.transcript,
            )

        fake.resume_authentication.assert_called_once()
        fake.sync.assert_awaited_once_with(SyncTrigger.MANUAL)
        app.runtime.handle_user_request.assert_not_awaited()
        app.runtime.handle_system_event.assert_not_awaited()
        fake.close.assert_awaited_once()

    async def test_repeated_timers_and_sync_service_are_cleaned_up_on_exit(self) -> None:
        """긴 TUI 실행을 짧은 주기로 압축해 반복 heartbeat와 종료 정리를 함께 검증한다."""

        fake = FakeTuiSyncService()
        settings = Settings(
            _env_file=None,
            openai_api_key="test-key",
            mysql_url="mysql+asyncmy://unused",
            eclass_sync_on_startup=False,
            eclass_sync_interval_minutes=5,
        )
        # 운영 설정 검증은 그대로 두고, 테스트 인스턴스의 5분을 0.12초로만 압축한다.
        settings.eclass_sync_interval_minutes = 0.002  # type: ignore[assignment]
        app = TimerProbeApp(settings, sync_service=fake)  # type: ignore[arg-type]

        async with app.run_test() as pilot:
            app.runtime.handle_system_event = AsyncMock()
            sync_timer = app._sync_timer
            clock_timer = app._clock_timer
            self.assertIsNotNone(sync_timer)
            self.assertIsNotNone(clock_timer)

            # clock(1초)도 한 번 이상, 압축한 heartbeat도 여러 번 실행될 만큼 기다린다.
            await pilot.pause(1.1)
            self.assertGreaterEqual(fake.sync.await_count, 2)
            self.assertTrue(
                all(call.args == (SyncTrigger.HEARTBEAT,) for call in fake.sync.await_args_list)
            )
            self.assertGreaterEqual(app.clock_tick_count, 2)

        calls_at_shutdown = fake.sync.await_count
        await asyncio.sleep(0.25)

        self.assertFalse(sync_timer._active.is_set())  # type: ignore[union-attr]
        self.assertFalse(clock_timer._active.is_set())  # type: ignore[union-attr]
        self.assertEqual(fake.sync.await_count, calls_at_shutdown)
        fake.close.assert_awaited_once()
        self.assertTrue(app.runtime._closed)

    def test_manual_refresh_intent_accepts_commands_but_not_questions(self) -> None:
        matcher = EclassQuestApp._is_manual_sync_request

        for message in (
            "지금 한번 다시 이클래스 정보 업데이트 해줘",
            "이클래스 업데이트해줘",
            "E-Class 정보 최신화해줘",
            "이클래스 다시 읽어줘",
            "지금 다시 확인해",
        ):
            with self.subTest(message=message):
                self.assertTrue(matcher(message))

        for message in (
            "이클래스 업데이트 주기가 뭐야?",
            "업데이트된 과제 내용 알려줘",
            "파이썬 업데이트 방법 알려줘",
        ):
            with self.subTest(message=message):
                self.assertFalse(matcher(message))
