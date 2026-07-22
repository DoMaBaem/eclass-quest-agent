"""현재 TUI가 공통 코드 이동 뒤에도 시작되는지 확인한다."""

import asyncio
import unittest
from unittest.mock import ANY, AsyncMock

from textual.widgets import Input, RichLog, Static

from app.config import Settings
from app.runtime.events import RuntimeProgressEvent
from app.schemas.manager import (
    ManagerPriority,
    ManagerResult,
    ManagerStatus,
    SpecialistAgentName,
)
from app.tui.app import EclassQuestApp
from app.tui.events import UiOperationState
from app.sync.schemas import (
    AssignmentChecklistItem,
    CourseChecklistItem,
    LectureChecklistItem,
    SyncResult,
    SyncStatus,
    SyncTrigger,
)
from datetime import datetime, timezone


class TuiSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_tui_starts_in_fixed_dialogue_state(self) -> None:
        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)

        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("E-CLASS QUEST SYSTEM", str(app.query_one("#system-title", Static).render()))
            self.assertIsNotNone(app.query_one("#lecture-checklist", RichLog))
            self.assertIsNotNone(app.query_one("#assignment-checklist", RichLog))
            self.assertFalse(app.query_one("#result", RichLog).wrap)
            self.assertTrue(app.transcript)
            self.assertTrue(app.transcript[0].startswith("SYSTEM >"))
            status = str(app.query_one("#status", Static).render())
            self.assertIn("READY", status)
            self.assertIn("TOOL:", status)
            self.assertIn("100%", status)

    async def test_chat_result_does_not_enter_task_route(self) -> None:
        """전문 Agent가 없는 완료 결과는 대화 화면에 남아야 한다."""

        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)
        result = ManagerResult(
            status=ManagerStatus.COMPLETED,
            message="안녕하세요.",
            should_notify=True,
            priority=ManagerPriority.NORMAL,
        )
        app.runtime.handle_user_request = AsyncMock(return_value=result)

        async with app.run_test() as pilot:
            field = app.query_one("#request", Input)
            field.value = "안녕"
            field.focus()
            await pilot.press("enter")
            await pilot.pause()

            self.assertIn("E-CLASS QUEST SYSTEM", str(app.query_one("#system-title", Static).render()))
            self.assertEqual(app.transcript[-1], "SYSTEM > 안녕하세요.")
            self.assertEqual(app.operation_state, UiOperationState.USER_TASK)
            self.assertEqual(app.operation_progress, 100)

    async def test_processing_animation_runs_only_until_specialist_finishes(self) -> None:
        """progress callback은 Agent를 지연하지 않고 Runtime 완료 시 애니메이션을 취소한다."""

        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)
        animation_started = asyncio.Event()
        animation_cancelled = asyncio.Event()
        specialist_started = asyncio.Event()
        title_was_preserved = False

        async def fake_animation() -> None:
            animation_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                animation_cancelled.set()

        async def fake_handle_user_request(
            _message: str,
            *,
            on_progress,
            on_manager_delta,
        ) -> ManagerResult:
            nonlocal title_was_preserved
            del on_manager_delta
            await on_progress(RuntimeProgressEvent.AGENT_DELEGATED, SpecialistAgentName.ECLASS.value)
            title_was_preserved = "E-CLASS QUEST SYSTEM" in str(
                app.query_one("#system-title", Static).render()
            )
            await asyncio.wait_for(animation_started.wait(), timeout=0.5)
            specialist_started.set()
            return ManagerResult(
                status=ManagerStatus.COMPLETED,
                message="조회 완료",
                should_notify=True,
                delegated_agents=[SpecialistAgentName.ECLASS],
            )

        app._animate_processing = fake_animation
        app.runtime.handle_user_request = fake_handle_user_request

        async with app.run_test() as pilot:
            field = app.query_one("#request", Input)
            field.value = "공지 확인해줘"
            field.focus()
            await pilot.press("enter")
            await pilot.pause()

            self.assertTrue(specialist_started.is_set())
            self.assertTrue(animation_cancelled.is_set())
            self.assertTrue(title_was_preserved)
            self.assertIn("E-CLASS QUEST SYSTEM", str(app.query_one("#system-title", Static).render()))
            self.assertTrue(app.transcript[0].startswith("SYSTEM >"))
            self.assertIn("USER > 공지 확인해줘", app.transcript)
            self.assertEqual(app.transcript[-1], "SYSTEM > 조회 완료")
            self.assertEqual(app.operation_state, UiOperationState.USER_TASK)
            self.assertEqual(app.current_tool, "E-Class MCP")
            self.assertIn("100%", str(app.query_one("#status", Static).render()))

    async def test_proactive_notice_appends_without_clearing_chat(self) -> None:
        """능동 알림도 이미 표시된 세션 대화를 지우지 않고 마지막에 추가한다."""

        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)
        notice = ManagerResult(
            status=ManagerStatus.COMPLETED,
            message="새 공지가 있습니다.",
            should_notify=True,
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            previous = list(app.transcript)
            await app.show_proactive_result(notice)

            self.assertEqual(app.transcript[:-1], previous)
            self.assertEqual(app.transcript[-1], "ALERT > 새 공지가 있습니다.")
            self.assertEqual(app.operation_state, UiOperationState.PROACTIVE_ALERT)
            self.assertIn(
                "PROACTIVE_ALERT",
                str(app.query_one("#status", Static).render()),
            )
            saved_transcript = list(app.transcript)
            app._return_to_ready()
            self.assertEqual(app.operation_state, UiOperationState.READY)
            self.assertEqual(app.transcript, saved_transcript)

    async def test_auth_required_has_a_dedicated_visible_state(self) -> None:
        """인증 만료는 일반 실패로 뭉개지 않고 사용자가 즉시 알아볼 수 있어야 한다."""

        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)
        app.runtime.handle_user_request = AsyncMock(
            return_value=ManagerResult(
                status=ManagerStatus.AUTH_REQUIRED,
                message="E-Class 로그인이 필요합니다.",
                should_notify=True,
                priority=ManagerPriority.HIGH,
            )
        )

        async with app.run_test() as pilot:
            field = app.query_one("#request", Input)
            field.value = "공지 확인해줘"
            field.focus()
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(app.operation_state, UiOperationState.AUTH_REQUIRED)
            self.assertIn("AUTH_REQUIRED", str(app.query_one("#status", Static).render()))
            self.assertNotIn("ERROR CODE", "\n".join(app.transcript))

    async def test_completed_state_returns_to_ready_without_clearing_chat(self) -> None:
        """완료 상태의 짧은 표시가 끝나면 기록은 보존하고 상태바만 READY로 돌아간다."""

        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)

        async with app.run_test() as pilot:
            saved_transcript = list(app.transcript)
            app._set_operation_state(
                UiOperationState.USER_TASK,
                tool="E-Class MCP",
                progress=100,
                detail="작업 완료",
            )
            app._schedule_ready_reset(0.01)
            await pilot.pause(0.05)

            self.assertEqual(app.operation_state, UiOperationState.READY)
            self.assertEqual(app.transcript, saved_transcript)

    async def test_successful_playback_exposes_state_and_verified_stop_target(self) -> None:
        """실제 재생 결과의 검증 ID만 저장하고 PLAYBACK 상태를 유지한다."""

        playback_id = "00000000-0000-0000-0000-000000000031"
        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)
        app.runtime.handle_user_request = AsyncMock(
            return_value=ManagerResult(
                status=ManagerStatus.COMPLETED,
                message="강의 영상 재생을 시작했습니다.",
                should_notify=True,
                delegated_agents=[SpecialistAgentName.ECLASS],
                evidence_refs=[f"playback:{playback_id}"],
            )
        )

        async with app.run_test() as pilot:
            field = app.query_one("#request", Input)
            field.value = "딥러닝 2주차 영상 재생해줘"
            field.focus()
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(app.operation_state, UiOperationState.PLAYBACK)
            self.assertEqual(app._active_playback_id, playback_id)
            status = str(app.query_one("#status", Static).render())
            self.assertIn("PLAYBACK", status)
            self.assertIn("F2", status)

    async def test_f2_stops_only_the_verified_active_playback(self) -> None:
        """F2는 현재 TUI가 받은 playback ID를 포함한 명시적 중지 요청만 보낸다."""

        playback_id = "00000000-0000-0000-0000-000000000032"
        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)
        app._active_playback_id = playback_id
        app.runtime.stop_verified_playback = AsyncMock(
            return_value=ManagerResult(
                status=ManagerStatus.COMPLETED,
                message="강의 영상 재생을 중지했습니다.",
                should_notify=True,
                delegated_agents=[SpecialistAgentName.ECLASS],
                evidence_refs=[f"playback:{playback_id}"],
            )
        )

        async with app.run_test() as pilot:
            await pilot.press("f2")
            await pilot.pause()

            app.runtime.stop_verified_playback.assert_awaited_once_with(
                playback_id,
                on_progress=ANY,
            )
            self.assertIsNone(app._active_playback_id)
            self.assertIn("USER > [F2] 재생 중인 강의 영상 중지", app.transcript)
            self.assertEqual(app.transcript[-1], "SYSTEM > 강의 영상 재생을 중지했습니다.")
            self.assertEqual(app.operation_state, UiOperationState.PLAYBACK)
            self.assertIn(
                "[F2] STOP VIDEO",
                str(app.query_one("#command-bar", Static).render()),
            )

    async def test_f2_without_active_playback_never_calls_runtime(self) -> None:
        """활성 재생 ID가 없으면 F2가 임의 ID를 만들거나 Agent를 호출하지 않는다."""

        app = EclassQuestApp(Settings(openai_api_key="test-key"), enable_sync=False)
        app.runtime.stop_verified_playback = AsyncMock()

        async with app.run_test() as pilot:
            await pilot.press("f2")
            await pilot.pause()

            app.runtime.stop_verified_playback.assert_not_awaited()
            self.assertEqual(
                app.transcript[-1],
                "SYSTEM > 현재 TUI에서 재생 중인 강의 영상이 없습니다.",
            )

    async def test_empty_term_renders_no_enrolled_lectures_message(self) -> None:
        """방학 기본 학기에 강좌가 0개면 왼쪽 체크리스트가 빈 이유를 명시한다."""

        app = EclassQuestApp(Settings(_env_file=None, openai_api_key="test-key"), enable_sync=False)
        now = datetime.now(timezone.utc)
        empty = SyncResult(
            status=SyncStatus.COMPLETED,
            trigger=SyncTrigger.STARTUP,
            course_count=0,
            started_at=now,
            finished_at=now,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app._render_lecture_checklist([], result=empty)
            checklist = app.query_one("#lecture-checklist", RichLog)
            self.assertIn("수강 강의 없음", checklist.lines[0].text)

    async def test_weekly_assignments_replace_mcp_activity_panel(self) -> None:
        """왼쪽 아래에는 내부 MCP 상태 대신 이번 주 과제를 표시한다."""

        app = EclassQuestApp(Settings(_env_file=None, openai_api_key="test-key"), enable_sync=False)
        due_at = datetime.now(timezone.utc)
        item = AssignmentChecklistItem(
            assignment_id="a1",
            course_name="인공지능 개론",
            title="실습 보고서",
            due_at=due_at,
            completed=False,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app._render_assignment_checklist([item])
            checklist = app.query_one("#assignment-checklist", RichLog)
            rendered = "\n".join(line.text for line in checklist.lines)
            self.assertIn("인공지능 개론", rendered)
            self.assertIn("실습 보고서", rendered)

    async def test_lecture_summary_counts_completed_courses_within_week(self) -> None:
        """9개 영상이 7개 과목에 속하면 요약의 분모는 7이어야 한다."""

        app = EclassQuestApp(Settings(_env_file=None, openai_api_key="test-key"), enable_sync=False)
        items: list[LectureChecklistItem] = []
        for course_number in range(1, 8):
            video_count = 2 if course_number <= 2 else 1
            for video_number in range(1, video_count + 1):
                items.append(
                    LectureChecklistItem(
                        lecture_id=f"lecture-{course_number}-{video_number}",
                        course_id=f"course-{course_number}",
                        course_name=f"과목 {course_number}",
                        title=f"7주차 영상 {video_number}/{video_count}",
                        week=7,
                        progress_percent=100,
                        completed=True,
                    )
                )

        async with app.run_test() as pilot:
            await pilot.pause()
            app._render_lecture_checklist(items)
            summary = str(app.query_one("#lecture-summary", Static).render())
            rendered = "\n".join(
                line.text for line in app.query_one("#lecture-checklist", RichLog).lines
            )

            self.assertEqual(summary, "7주차 - 7 / 7 완료")
            self.assertIn("2 / 2개 수강 · 100%", rendered)
            self.assertNotIn("영상 1/2", rendered)

    async def test_course_without_weekly_lecture_is_completed_without_fake_percentage(self) -> None:
        """주차 영상이 없는 과목은 완료 수에 포함하되 0/0이나 0%를 표시하지 않는다."""

        app = EclassQuestApp(Settings(_env_file=None, openai_api_key="test-key"), enable_sync=False)
        now = datetime.now(timezone.utc)
        item = LectureChecklistItem(
            lecture_id="lecture-1",
            course_id="course-1",
            course_name="딥러닝",
            title="7주차 영상",
            week=7,
            progress_percent=100,
            completed=True,
        )
        result = SyncResult(
            status=SyncStatus.COMPLETED,
            trigger=SyncTrigger.MANUAL,
            course_count=2,
            course_checklist=[
                CourseChecklistItem(course_id="course-1", course_name="딥러닝"),
                CourseChecklistItem(course_id="course-2", course_name="연구프로젝트"),
            ],
            lecture_checklist=[item],
            started_at=now,
            finished_at=now,
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            app._render_lecture_checklist([item], result=result)
            summary = str(app.query_one("#lecture-summary", Static).render())
            rendered = "\n".join(
                line.text for line in app.query_one("#lecture-checklist", RichLog).lines
            )

            self.assertEqual(summary, "7주차 - 2 / 2 완료")
            self.assertIn("연구프로젝트", rendered)
            self.assertIn("강의 없음", rendered)
            self.assertNotIn("0 / 0개", rendered)

if __name__ == "__main__":
    unittest.main()
