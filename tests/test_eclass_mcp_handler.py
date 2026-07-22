"""E-Class Agent handler가 검증된 MCP 본문을 모델 출력과 분리해 보존하는지 테스트한다."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.eclass_mcp_handler import (
    EclassMcpSpecialistHandler,
    _VerifiedMcpOutputCapture,
    _announcement_display_text,
    _announcement_followup_context,
    _announcement_list_display_text,
    _assignment_details_display_text,
    _assignment_followup_context,
    _assignment_list_display_text,
    _attachment_followup_context,
    _attachment_list_display_text,
    _course_followup_context,
    _course_assignment_display_text,
    _course_assignment_followup_context,
    _course_list_display_text,
    _lecture_followup_context,
    _lecture_list_display_text,
    _lecture_resolution_followup_context,
    _mcp_gui_environment,
    _prefer_verified_assignment_list,
    _prefer_verified_lecture_list,
    _safe_playback_arguments,
    _semantic_outcome_contract,
    _split_eclass_course_name,
    _tool_allowlist_for_task,
)
from app.config import Settings
from app.schemas.domain import Announcement, AnnouncementDetails, Assignment, Attachment, Course, Lecture
from app.schemas.manager import (
    ManagerAction,
    ManagerEntityKind,
    ManagerTask,
    ManagerTaskSlots,
    SpecialistAgentName,
    SpecialistResult,
    SpecialistStatus,
    VerifiedAnnouncementTarget,
    VerifiedAttachmentTarget,
    VerifiedLectureTarget,
)
from app.schemas.workflow import CapabilityCode, ErrorCode
from mcp_server.schemas import (
    AnnouncementDetailsResult,
    AnnouncementListResult,
    AssignmentDetailsResult,
    AssignmentListResult,
    AttachmentListResult,
    CourseListResult,
    CourseAnnouncementData,
    CourseAnnouncementResult,
    CourseAssignmentData,
    CourseAssignmentResult,
    DownloadInfo,
    DownloadResult,
    LectureListResult,
    LectureResolutionResult,
    McpOutcomeStatus,
    McpErrorCode,
    McpToolError,
    PlaybackInfo,
    PlaybackResult,
    SelectedTerm,
    VerifiedLectureTarget as McpVerifiedLectureTarget,
    VerifiedCourseReference,
    VerifiedPlaybackResult,
)


class VerifiedMcpOutputCaptureTest(unittest.IsolatedAsyncioTestCase):
    def test_mcp_subprocess_inherits_browser_and_audio_paths(self) -> None:
        """Docker MCP 자식 프로세스가 Chromium과 PulseAudio 경로를 잃지 않는다."""

        with patch.dict(
            "os.environ",
            {
                "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
                "PULSE_RUNTIME_PATH": "/defaults",
            },
            clear=True,
        ):
            self.assertEqual(
                _mcp_gui_environment()["PLAYWRIGHT_BROWSERS_PATH"],
                "/ms-playwright",
            )
            self.assertEqual(
                _mcp_gui_environment()["PULSE_RUNTIME_PATH"],
                "/defaults",
            )

    async def test_announcement_detail_is_not_completed_by_list_only(self) -> None:
        """DETAIL 계약은 공지 목록 조회만으로 완료될 수 없다."""

        response = CourseAnnouncementResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            data=CourseAnnouncementData(
                course=VerifiedCourseReference(
                    course_id="46516",
                    course_name="딥러닝",
                    professor="조혜경",
                    year=2026,
                    semester=1,
                ),
                announcements=[
                    Announcement(
                        id="532941",
                        course_id="46516",
                        title="기말고사 시험지 확인 시간 안내",
                        url="https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1&bwid=532941",
                    )
                ],
            ),
        )
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            await hooks.on_tool_end(
                None,
                None,
                SimpleNamespace(name="list_course_announcements"),
                response.model_dump_json(),
            )
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="공지 상세 조회 완료",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.ANNOUNCEMENT,
            action=ManagerAction.DETAIL,
            slots=ManagerTaskSlots(course_query="딥러닝", ordinal=1),
            instruction="딥러닝 1번 공지 상세 내용",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.TEMPORARY_FAILURE)
        self.assertIn("중간 목록 조회만으로는 완료 처리하지 않습니다", result.summary)
        self.assertIsNone(result.verified_display_text)

    async def test_assignment_detail_is_not_completed_by_list_only(self) -> None:
        """DETAIL 계약은 과제 목록 조회만으로 완료될 수 없다."""

        response = CourseAssignmentResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            data=CourseAssignmentData(
                course=VerifiedCourseReference(
                    course_id="46500",
                    course_name="빅데이터프로그래밍",
                    professor="이청용",
                    year=2026,
                    semester=1,
                ),
                assignments=[
                    Assignment(
                        id="1140975",
                        course_id="46500",
                        course_name="빅데이터프로그래밍",
                        title="실습과제 제출",
                        url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1140975",
                    )
                ],
            ),
        )
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            await hooks.on_tool_end(
                None,
                None,
                SimpleNamespace(name="list_course_assignments"),
                response.model_dump_json(),
            )
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="과제 상세 조회 완료",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.ASSIGNMENT,
            action=ManagerAction.DETAIL,
            slots=ManagerTaskSlots(course_query="빅데이터프로그래밍", ordinal=1),
            instruction="빅데이터프로그래밍 1번 과제 상세 내용",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.TEMPORARY_FAILURE)
        self.assertIn("중간 목록 조회만으로는 완료 처리하지 않습니다", result.summary)
        self.assertIsNone(result.verified_display_text)

    def test_agent_tool_filter_uses_typed_operation_allowlist(self) -> None:
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        assignment_task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.ASSIGNMENT,
            action=ManagerAction.LIST,
            slots=ManagerTaskSlots(course_query="빅데이터프로그래밍"),
            instruction="빅데이터프로그래밍 과제 목록",
        )
        handler._active_tool_allowlist = _tool_allowlist_for_task(assignment_task)
        self.assertTrue(
            handler._filter_mcp_tool(
                None,
                SimpleNamespace(name="list_course_assignments"),
            )
        )
        self.assertFalse(
            handler._filter_mcp_tool(
                None,
                SimpleNamespace(name="list_course_announcements"),
            )
        )
        self.assertFalse(
            handler._filter_mcp_tool(None, SimpleNamespace(name="resolve_lecture"))
        )
        self.assertFalse(
            handler._filter_mcp_tool(
                None,
                SimpleNamespace(name="get_dashboard_snapshot"),
            )
        )
        self.assertFalse(handler._filter_mcp_tool(None, SimpleNamespace(name="play_lecture")))
        self.assertFalse(
            handler._filter_mcp_tool(None, SimpleNamespace(name="preview_lecture"))
        )

        play_task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            entity=ManagerEntityKind.LECTURE,
            action=ManagerAction.PLAY,
            slots=ManagerTaskSlots(course_query="데이터마이닝", week=2),
            instruction="데이터마이닝 2주차 영상 재생",
        )
        handler._active_tool_allowlist = _tool_allowlist_for_task(play_task)
        self.assertTrue(handler._filter_mcp_tool(None, SimpleNamespace(name="resolve_lecture")))
        self.assertTrue(
            handler._filter_mcp_tool(None, SimpleNamespace(name="play_resolved_lecture"))
        )
        self.assertFalse(
            handler._filter_mcp_tool(
                None,
                SimpleNamespace(name="preview_resolved_lecture"),
            )
        )
        self.assertFalse(handler._filter_mcp_tool(None, SimpleNamespace(name="stop_lecture")))
        self.assertFalse(
            handler._filter_mcp_tool(
                None,
                SimpleNamespace(name="get_grades"),
            )
        )

        grade_task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.GRADE,
            action=ManagerAction.LIST,
            slots=ManagerTaskSlots(),
            instruction="성적 목록",
        )
        handler._active_tool_allowlist = _tool_allowlist_for_task(grade_task)
        self.assertTrue(handler._filter_mcp_tool(None, SimpleNamespace(name="get_grades")))
        self.assertFalse(handler._filter_mcp_tool(None, SimpleNamespace(name="list_grades")))

        attachment_download_task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            entity=ManagerEntityKind.ATTACHMENT,
            action=ManagerAction.DOWNLOAD,
            slots=ManagerTaskSlots(query="guide.pdf"),
            instruction="검증된 과제 첨부 guide.pdf를 분석한다.",
        )
        handler._active_tool_allowlist = _tool_allowlist_for_task(
            attachment_download_task
        )
        self.assertTrue(
            handler._filter_mcp_tool(
                None,
                SimpleNamespace(name="list_assignment_attachments"),
            )
        )
        # Agent는 원시 URL·ID 다운로드 Tool을 볼 수 없고 Runtime 검증 경로만 호출한다.
        self.assertFalse(
            handler._filter_mcp_tool(
                None,
                SimpleNamespace(name="download_attachment"),
            )
        )

    def test_semantic_outcome_status_matrix_is_deterministic(self) -> None:
        expected = {
            McpOutcomeStatus.FOUND: (SpecialistStatus.COMPLETED, None),
            McpOutcomeStatus.NOT_FOUND: (SpecialistStatus.COMPLETED, None),
            McpOutcomeStatus.AMBIGUOUS: (SpecialistStatus.COMPLETED, None),
            McpOutcomeStatus.AUTH_REQUIRED: (
                SpecialistStatus.AUTH_REQUIRED,
                ErrorCode.AUTH_REQUIRED,
            ),
            McpOutcomeStatus.PARSER_CHANGED: (
                SpecialistStatus.FAILED,
                ErrorCode.TEMPORARY_FAILURE,
            ),
            McpOutcomeStatus.TEMPORARY_FAILURE: (
                SpecialistStatus.FAILED,
                ErrorCode.TEMPORARY_FAILURE,
            ),
            McpOutcomeStatus.INVALID_REQUEST: (
                SpecialistStatus.FAILED,
                ErrorCode.INVALID_REQUEST,
            ),
        }
        for outcome, contract in expected.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(_semantic_outcome_contract(outcome), contract)

    def test_typed_play_action_is_not_overridden_by_preview_word_in_instruction(self) -> None:
        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            entity=ManagerEntityKind.LECTURE,
            action=ManagerAction.PLAY,
            slots=ManagerTaskSlots(course_query="데이터마이닝", week=2),
            instruction="미리보기라는 표현과 무관하게 검증된 영상을 재생한다.",
        )

        tool_name, arguments = _safe_playback_arguments(
            task,
            "00000000-0000-0000-0000-000000000070",
        )

        self.assertEqual(tool_name, "play_resolved_lecture")
        self.assertEqual(
            arguments["reference_id"],
            "00000000-0000-0000-0000-000000000070",
        )

    async def test_video_task_completes_resolved_target_when_agent_omits_play_call(self) -> None:
        now = datetime.now(timezone.utc)
        target = McpVerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000010",
            lecture_id="1133557",
            course_id="46499",
            course_name="데이터마이닝[A,B]",
            title="[동영상] 02주차_Python 개요 및 가상환경 구축",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now,
        )
        resolution = LectureResolutionResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            data=target,
        )
        playback = VerifiedPlaybackResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            target=target,
            data=PlaybackInfo(
                playback_id="00000000-0000-0000-0000-000000000011",
                lecture_id="1133557",
                status="PLAYING",
                volume_percent=70,
                playback_rate=1.25,
                started_at=now,
            ),
        )
        server = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent=playback.model_dump(mode="json")
                )
            )
        )
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            hooks.lecture_resolution_result = resolution
            hooks.last_data_tool = "resolve_lecture"
            hooks.successful_tools.append("resolve_lecture")
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="강의 대상을 확인했습니다.",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            instruction="데이터마이닝 2주차 영상을 볼륨 70, 1.25배속으로 재생한다.",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        tool_name, arguments = server.call_tool.await_args.args
        self.assertEqual(tool_name, "play_resolved_lecture")
        self.assertEqual(arguments["reference_id"], target.reference_id)
        self.assertEqual(arguments["volume_percent"], 70)
        self.assertEqual(arguments["playback_rate"], 1.25)
        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn(target.title, result.verified_display_text or "")

    async def test_preview_task_completes_safe_preview_when_agent_only_resolves(self) -> None:
        now = datetime.now(timezone.utc)
        target = McpVerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000020",
            lecture_id="1133557",
            course_id="46499",
            course_name="데이터마이닝[A,B]",
            title="[동영상] 02주차_Python 개요 및 가상환경 구축",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now,
        )
        resolution = LectureResolutionResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            data=target,
        )
        playback = VerifiedPlaybackResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            target=target,
            data=PlaybackInfo(
                playback_id="00000000-0000-0000-0000-000000000021",
                lecture_id=target.lecture_id,
                status="PLAYING",
                volume_percent=60,
                playback_rate=1.5,
                started_at=now,
            ),
        )
        server = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent=playback.model_dump(mode="json")
                )
            )
        )
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            hooks.lecture_resolution_result = resolution
            hooks.last_data_tool = "resolve_lecture"
            hooks.successful_tools.append("resolve_lecture")
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="강의 대상을 확인했습니다.",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            instruction="데이터마이닝 2주차 영상을 12초 미리보기, 볼륨 60, 1.5배속으로 실행한다.",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        tool_name, arguments = server.call_tool.await_args.args
        self.assertEqual(tool_name, "preview_resolved_lecture")
        self.assertEqual(arguments["reference_id"], target.reference_id)
        self.assertEqual(arguments["seconds"], 12)
        self.assertEqual(arguments["options"]["volume_percent"], 60)
        self.assertEqual(arguments["options"]["playback_rate"], 1.5)
        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("미리보기", result.verified_display_text or "")

    async def test_verified_playback_success_wins_over_later_unrelated_lookup(self) -> None:
        now = datetime.now(timezone.utc)
        target = McpVerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000040",
            lecture_id="1133557",
            course_id="46499",
            course_name="데이터마이닝[A,B]",
            title="02주차 Python 개요",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now,
        )
        playback = VerifiedPlaybackResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            target=target,
            data=PlaybackInfo(
                playback_id="00000000-0000-0000-0000-000000000041",
                lecture_id=target.lecture_id,
                status="PLAYING",
                started_at=now,
            ),
        )
        unrelated = CourseListResult(
            ok=True,
            data=[
                Course(
                    id="99999",
                    name="무관한 강좌",
                    url="https://learn.hansung.ac.kr/course/view.php?id=99999",
                    year=2026,
                    semester=1,
                )
            ],
        )
        server = SimpleNamespace(call_tool=AsyncMock())
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            await hooks.on_tool_end(
                None,
                None,
                SimpleNamespace(name="play_resolved_lecture"),
                playback.model_dump_json(),
            )
            await hooks.on_tool_end(
                None,
                None,
                SimpleNamespace(name="list_courses"),
                unrelated.model_dump_json(),
            )
            self.assertEqual(hooks.last_data_tool, "list_courses")
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="무관한 마지막 조회",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            entity=ManagerEntityKind.LECTURE,
            action=ManagerAction.PLAY,
            slots=ManagerTaskSlots(course_query="데이터마이닝", week=2),
            instruction="데이터마이닝 2주차 재생",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("02주차 Python 개요", result.verified_display_text or "")
        self.assertNotIn("무관한 강좌", result.verified_display_text or "")

    async def test_verified_playback_failure_wins_over_later_unrelated_lookup(self) -> None:
        now = datetime.now(timezone.utc)
        target = McpVerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000050",
            lecture_id="1133557",
            course_id="46499",
            course_name="데이터마이닝[A,B]",
            title="02주차 Python 개요",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now,
        )
        playback = VerifiedPlaybackResult(
            ok=False,
            status=McpOutcomeStatus.AUTH_REQUIRED,
            target=target,
            error=McpToolError(
                code=McpErrorCode.AUTH_REQUIRED,
                message="E-Class 로그인이 필요합니다.",
                retryable=False,
            ),
        )
        unrelated = CourseListResult(ok=True, data=[])
        server = SimpleNamespace(call_tool=AsyncMock())
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            await hooks.on_tool_end(
                None,
                None,
                SimpleNamespace(name="play_resolved_lecture"),
                playback.model_dump_json(),
            )
            await hooks.on_tool_end(
                None,
                None,
                SimpleNamespace(name="list_courses"),
                unrelated.model_dump_json(),
            )
            self.assertEqual(hooks.last_data_tool, "list_courses")
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="무관한 조회는 성공",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            entity=ManagerEntityKind.LECTURE,
            action=ManagerAction.PLAY,
            slots=ManagerTaskSlots(course_query="데이터마이닝", week=2),
            instruction="데이터마이닝 2주차 재생",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.AUTH_REQUIRED)
        self.assertEqual(result.error_code, ErrorCode.AUTH_REQUIRED)
        self.assertEqual(result.summary, "E-Class 로그인이 필요합니다.")

    async def test_stop_operation_keeps_only_stop_tool_and_returns_captured_result(self) -> None:
        now = datetime.now(timezone.utc)
        stopped = PlaybackResult(
            ok=True,
            data=PlaybackInfo(
                playback_id="00000000-0000-0000-0000-000000000060",
                lecture_id="1133557",
                status="STOPPED",
                started_at=now,
                finished_at=now,
            ),
        )
        server = SimpleNamespace(call_tool=AsyncMock())
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            await hooks.on_tool_end(
                None,
                None,
                SimpleNamespace(name="stop_lecture"),
                stopped.model_dump_json(),
            )
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.COMPLETED,
                    summary="중지 완료",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            entity=ManagerEntityKind.LECTURE,
            action=ManagerAction.STOP,
            slots=ManagerTaskSlots(query=stopped.data.playback_id),
            instruction=f"재생 ID {stopped.data.playback_id} 영상을 멈춰",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("재생을 중지했습니다", result.verified_display_text or "")
        self.assertEqual(
            _tool_allowlist_for_task(task),
            frozenset({"check_session", "stop_lecture"}),
        )

    async def test_verified_stop_calls_existing_mcp_process_without_agent(self) -> None:
        """Runtime 전용 중지는 UUID를 그대로 기존 stdio MCP Tool에 한 번만 전달한다."""

        now = datetime.now(timezone.utc)
        playback_id = "00000000-0000-0000-0000-000000000073"
        stopped = PlaybackResult(
            ok=True,
            data=PlaybackInfo(
                playback_id=playback_id,
                lecture_id="1133557",
                status="STOPPED",
                started_at=now,
                finished_at=now,
            ),
        )
        server = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent=stopped.model_dump(mode="json")
                )
            )
        )
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]

        with patch(
            "app.agent.eclass_mcp_handler.Runner.run",
            new=AsyncMock(side_effect=AssertionError("E-Class Agent를 호출하면 안 됩니다.")),
        ):
            result = await handler.stop_verified_playback(playback_id)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn(f"playback:{playback_id}", result.evidence_refs)
        server.call_tool.assert_awaited_once_with(
            "stop_lecture",
            {"playback_id": playback_id},
        )

    async def test_ambiguous_resolution_preserves_typed_candidates_without_reference(self) -> None:
        response = LectureResolutionResult(
            ok=False,
            status=McpOutcomeStatus.AMBIGUOUS,
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
            candidates=[
                Lecture(
                    id="1133557",
                    course_id="46499",
                    title="02주차 Python 개요 1차시",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1133557",
                    week=2,
                ),
                Lecture(
                    id="1133558",
                    course_id="46499",
                    title="02주차 Python 개요 2차시",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1133558",
                    week=2,
                ),
            ],
        )

        encoded = _lecture_resolution_followup_context(response)

        self.assertIsNotNone(encoded)
        assert encoded is not None
        self.assertIn('"number":1', encoded)
        self.assertIn('"lecture_id":"1133557"', encoded)
        self.assertIn('"course_id":"46499"', encoded)
        self.assertIn('"title":"02주차 Python 개요 1차시"', encoded)
        self.assertIn('"week":2', encoded)
        self.assertNotIn("reference_id", encoded)

    async def test_ambiguous_video_task_returns_candidates_in_followup_context(self) -> None:
        resolution = LectureResolutionResult(
            ok=False,
            status=McpOutcomeStatus.AMBIGUOUS,
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
            candidates=[
                Lecture(
                    id="1133557",
                    course_id="46499",
                    title="02주차 Python 개요 1차시",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1133557",
                    week=2,
                ),
                Lecture(
                    id="1133558",
                    course_id="46499",
                    title="02주차 Python 개요 2차시",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1133558",
                    week=2,
                ),
            ],
        )
        server = SimpleNamespace(call_tool=AsyncMock())
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]

        async def fake_run(_agent, _input, *, hooks, **_kwargs):
            hooks.lecture_resolution_result = resolution
            hooks.last_data_tool = "resolve_lecture"
            hooks.successful_tools.append("resolve_lecture")
            return SimpleNamespace(
                final_output=SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary="후보가 여러 개입니다.",
                )
            )

        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            instruction="데이터마이닝 2주차 영상 미리보기",
        )
        with patch("app.agent.eclass_mcp_handler.Runner.run", new=fake_run):
            result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("1. 2주차", result.verified_display_text or "")
        self.assertIn('"lecture_id":"1133557"', result.verified_followup_context or "")
        self.assertNotIn("reference_id", result.verified_followup_context or "")
        server.call_tool.assert_not_awaited()

    async def test_semantic_course_assignment_tool_is_captured_as_typed_followup(self) -> None:
        response = CourseAssignmentResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=SelectedTerm(
                year=2026,
                semester=1,
                selection_source="user_request",
            ),
            data=CourseAssignmentData(
                course=VerifiedCourseReference(
                    course_id="46500",
                    course_name="빅데이터프로그래밍",
                    professor="이청용",
                    year=2026,
                    semester=1,
                ),
                assignments=[
                    Assignment(
                        id="1140975",
                        course_id="46500",
                        course_name="빅데이터프로그래밍",
                        title="실습과제 제출",
                        url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1140975",
                        week=3,
                    )
                ],
            ),
        )
        capture = _VerifiedMcpOutputCapture()

        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_course_assignments"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.course_assignment_result)
        self.assertEqual(capture.last_data_tool, "list_course_assignments")
        self.assertIn("assignment:1140975", capture.evidence_refs)
        display = _course_assignment_display_text(capture.course_assignment_result)
        context = _course_assignment_followup_context(capture.course_assignment_result)
        self.assertIn("빅데이터프로그래밍", display)
        self.assertIn('"id":"1140975"', context)

    async def test_verified_lecture_playback_re_resolves_and_never_calls_raw_id_tool(self) -> None:
        now = datetime.now(timezone.utc)
        resolved_target = McpVerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000030",
            lecture_id="1133557",
            course_id="46499",
            course_name="데이터마이닝[A,B]",
            title="[동영상] 02주차_Python 개요 및 가상환경 구축",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now,
        )
        resolution = LectureResolutionResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            data=resolved_target,
        )
        playback = VerifiedPlaybackResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            target=resolved_target,
            data=PlaybackInfo(
                playback_id="00000000-0000-0000-0000-000000000001",
                lecture_id="1133557",
                status="PLAYING",
                volume_percent=80,
                playback_rate=1.5,
                started_at=now,
            ),
        )
        server = SimpleNamespace(
            call_tool=AsyncMock(
                side_effect=[
                    SimpleNamespace(structuredContent=resolution.model_dump(mode="json")),
                    SimpleNamespace(structuredContent=playback.model_dump(mode="json")),
                ]
            )
        )
        handler = EclassMcpSpecialistHandler(Settings(_env_file=None, openai_api_key="test-key"))
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]
        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            instruction="2주차 영상을 볼륨 80, 1.5배속으로 재생한다.",
            verified_lecture_target=VerifiedLectureTarget(
                id="1133557",
                course_id="46499",
                course_name="데이터마이닝[A,B]",
                title="[동영상] 02주차_Python 개요 및 가상환경 구축",
                url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1133557",
                week=2,
                year=2026,
                semester=1,
            ),
        )

        result = await handler(task)

        self.assertEqual(server.call_tool.await_count, 2)
        resolve_call, play_call = server.call_tool.await_args_list
        self.assertEqual(resolve_call.args[0], "resolve_lecture")
        self.assertEqual(resolve_call.args[1]["course_query"], "데이터마이닝[A,B]")
        self.assertEqual(resolve_call.args[1]["title_query"], resolved_target.title)
        self.assertEqual(resolve_call.args[1]["week"], 2)
        self.assertEqual(play_call.args[0], "play_resolved_lecture")
        arguments = play_call.args[1]
        self.assertEqual(arguments["reference_id"], resolved_target.reference_id)
        self.assertNotIn("lecture_id", arguments)
        self.assertEqual(arguments["volume_percent"], 80)
        self.assertEqual(arguments["playback_rate"], 1.5)
        called_tool_names = [call.args[0] for call in server.call_tool.await_args_list]
        self.assertNotIn("play_lecture", called_tool_names)
        self.assertNotIn("preview_lecture", called_tool_names)
        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("02주차_Python 개요 및 가상환경 구축", result.verified_display_text or "")

    async def test_verified_lecture_re_resolution_id_mismatch_blocks_playback(self) -> None:
        now = datetime.now(timezone.utc)
        different_target = McpVerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000031",
            lecture_id="DIFFERENT-ID",
            course_id="46499",
            course_name="데이터마이닝[A,B]",
            title="다른 영상",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now,
        )
        resolution = LectureResolutionResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            data=different_target,
        )
        server = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent=resolution.model_dump(mode="json")
                )
            )
        )
        handler = EclassMcpSpecialistHandler(
            Settings(_env_file=None, openai_api_key="test-key")
        )
        handler._ensure_server = AsyncMock(return_value=server)  # type: ignore[method-assign]
        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.VIDEO_PLAY,
            instruction="2주차 영상을 재생한다.",
            verified_lecture_target=VerifiedLectureTarget(
                id="1133557",
                course_id="46499",
                course_name="데이터마이닝[A,B]",
                title="[동영상] 02주차_Python 개요 및 가상환경 구축",
                url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1133557",
                week=2,
                year=2026,
                semester=1,
            ),
        )

        result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_REQUEST)
        server.call_tool.assert_awaited_once()
        self.assertEqual(server.call_tool.await_args.args[0], "resolve_lecture")

    async def test_lecture_list_preserves_real_ids_for_follow_up_playback(self) -> None:
        response = LectureListResult(
            ok=True,
            selected_term=SelectedTerm(year=2026, semester=1, selection_source="user_request"),
            data=[
                Lecture(
                    id="1133557",
                    course_id="46499",
                    title="[동영상] 02주차_Python 개요 및 가상환경 구축",
                    url="https://learn.hansung.ac.kr/mod/vod/view.php?id=1133557",
                    week=2,
                )
            ],
        )
        courses = CourseListResult(
            ok=True,
            data=[
                Course(
                    id="46499",
                    name="데이터마이닝[A,B]",
                    url="https://learn.hansung.ac.kr/course/view.php?id=46499",
                    year=2026,
                    semester=1,
                )
            ],
        )
        capture = _VerifiedMcpOutputCapture()
        capture.course_list = courses

        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_lectures"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.lecture_list)
        display = _lecture_list_display_text(capture.lecture_list, courses)  # type: ignore[arg-type]
        context = _lecture_followup_context(capture.lecture_list, courses)  # type: ignore[arg-type]
        self.assertIn("02주차_Python 개요 및 가상환경 구축", display)
        self.assertIn('"kind":"verified_lecture_candidates"', context)
        self.assertIn('"id":"1133557"', context)
        preferred = _prefer_verified_lecture_list(
            SpecialistResult(status=SpecialistStatus.FAILED, summary="잘못된 강의 ID"),
            capture,
        )
        self.assertEqual(preferred.status, SpecialistStatus.COMPLETED)

    async def test_attachment_list_preserves_download_candidates_for_follow_up(self) -> None:
        response = AttachmentListResult(
            ok=True,
            data=[
                Attachment(
                    id="attachment-1",
                    parent_type="assignment",
                    parent_id="1140975",
                    name="BPM_2026-1_lab1.pdf",
                    url="https://learn.hansung.ac.kr/pluginfile.php/1/BPM_2026-1_lab1.pdf",
                    mime_type="application/pdf",
                )
            ],
        )
        capture = _VerifiedMcpOutputCapture()

        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_assignment_attachments"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.attachment_list)
        display = _attachment_list_display_text(capture.attachment_list)  # type: ignore[arg-type]
        context = _attachment_followup_context(capture.attachment_list)  # type: ignore[arg-type]
        self.assertIn("BPM_2026-1_lab1.pdf", display)
        self.assertIn('"kind":"verified_attachment_candidates"', context)
        self.assertIn('"id":"attachment-1"', context)

    async def test_verified_attachment_target_downloads_without_agent_rewriting_url(self) -> None:
        from datetime import datetime, timezone

        response = DownloadResult(
            ok=True,
            data=DownloadInfo(
                download_id="11111111-1111-1111-1111-111111111111",
                attachment_id="attachment-1",
                filename="BPM_2026-1_lab1.pdf",
                mime_type="application/pdf",
                size_bytes=100,
                sha256="a" * 64,
                expires_at=datetime.now(timezone.utc),
            ),
        )
        fake_server = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(structuredContent=response.model_dump(mode="json"))
            )
        )
        handler = EclassMcpSpecialistHandler(Settings(openai_api_key=None))
        handler._ensure_server = AsyncMock(return_value=fake_server)  # type: ignore[method-assign]
        target = VerifiedAttachmentTarget(
            id="attachment-1",
            parent_id="1140975",
            name="BPM_2026-1_lab1.pdf",
            url="https://learn.hansung.ac.kr/pluginfile.php/1/BPM_2026-1_lab1.pdf",
        )
        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            instruction="검증된 PDF를 다운로드한다.",
            verified_attachment_target=target,
        )

        result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertEqual(
            result.evidence_refs,
            ["download:11111111-1111-1111-1111-111111111111:attachment-1"],
        )
        fake_server.call_tool.assert_awaited_once_with(
            "download_attachment",
            {
                "attachment_url": target.url,
                "attachment_id": target.id,
                "filename": target.name,
            },
        )

    async def test_verified_assignment_list_overrides_agent_failure(self) -> None:
        """MCP 과제 목록이 성공했다면 모델의 잘못된 FAILED 판정을 노출하지 않는다."""

        courses = CourseListResult(
            ok=True,
            selected_term=SelectedTerm(year=2026, semester=1, selection_source="user_request"),
            data=[
                Course(
                    id="46500",
                    name="빅데이터프로그래밍[7,A,N]",
                    url="https://learn.hansung.ac.kr/course/view.php?id=46500",
                    year=2026,
                    semester=1,
                )
            ],
        )
        assignments = AssignmentListResult(
            ok=True,
            selected_term=SelectedTerm(year=2026, semester=1, selection_source="user_request"),
            data=[
                Assignment(
                    id="1001",
                    course_id="46500",
                    title="1주차 과제",
                    url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1001",
                    week=1,
                )
            ],
        )
        capture = _VerifiedMcpOutputCapture()
        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_courses"),
            courses.model_dump_json(),
        )
        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_assignments"),
            assignments.model_dump_json(),
        )
        agent_result = SpecialistResult(
            status=SpecialistStatus.FAILED,
            summary="course_id 46500을 확인할 수 없습니다.",
        )

        result = _prefer_verified_assignment_list(agent_result, capture)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIsNone(result.error_code)
        self.assertIn("빅데이터프로그래밍", result.verified_display_text or "")
        self.assertIn("1주차 과제", result.verified_display_text or "")

    async def test_assignment_details_outputs_only_selected_assignment(self) -> None:
        response = AssignmentDetailsResult(
            ok=True,
            data=Assignment(
                id="1001",
                course_id="46499",
                title="실습 1: KNN 알고리즘",
                url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1001",
                week=6,
                submitted=True,
            ),
        )
        capture = _VerifiedMcpOutputCapture()
        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="get_assignment_details"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.assignment_details)
        display = _assignment_details_display_text(capture.assignment_details)  # type: ignore[arg-type]
        self.assertIn("실습 1: KNN 알고리즘", display)
        self.assertNotIn("과제 2", display)

    async def test_assignment_list_preserves_week_and_submission_status(self) -> None:
        """과제 목록은 모델의 의역 없이 실제 주차·상태를 표시한다."""

        response = AssignmentListResult(
            ok=True,
            selected_term=SelectedTerm(year=2026, semester=1, selection_source="user_request"),
            data=[
                Assignment(
                    id="1001",
                    course_id="46499",
                    title="실습 1: KNN 알고리즘",
                    url="https://learn.hansung.ac.kr/mod/assign/view.php?id=1001",
                    week=6,
                    submitted=True,
                )
            ],
        )
        courses = CourseListResult(
            ok=True,
            data=[
                Course(
                    id="46499",
                    name="데이터마이닝[A,B]",
                    url="https://learn.hansung.ac.kr/course/view.php?id=46499",
                    year=2026,
                    semester=1,
                )
            ],
        )
        capture = _VerifiedMcpOutputCapture()
        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_assignments"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.assignment_list)
        display = _assignment_list_display_text(capture.assignment_list, courses)  # type: ignore[arg-type]
        context = _assignment_followup_context(capture.assignment_list)  # type: ignore[arg-type]
        self.assertIn("데이터마이닝 · 6주차", display)
        self.assertIn("실습 1: KNN 알고리즘", display)
        self.assertIn("제출 완료", display)
        self.assertIn('"kind":"verified_assignment_candidates"', context)
        self.assertIn('"number":1', context)
        self.assertIn('"id":"1001"', context)

    async def test_verified_notice_target_bypasses_agent_and_calls_detail_service(self) -> None:
        """Runtime이 붙인 URL은 Agent 재검색 없이 E-Class MCP Tool에 직접 전달한다."""

        url = "https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1104303&bwid=534552"
        response = AnnouncementDetailsResult(
            ok=True,
            selected_term=SelectedTerm(year=2026, semester=1, selection_source="user_request"),
            data=AnnouncementDetails(
                id="534552",
                course_id="46516",
                title="기말고사 시험지 확인 시간 안내",
                url=url,
                author="조혜경",
                content="시험지 확인 안내 원문",
            ),
        )
        class FakeMcpServer:
            def __init__(self) -> None:
                self.call_tool = AsyncMock(
                    return_value=SimpleNamespace(structuredContent=response.model_dump(mode="json"))
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def connect(self):
                return None

            async def cleanup(self):
                return None

        fake_server = FakeMcpServer()
        handler = EclassMcpSpecialistHandler(Settings(openai_api_key=None))
        handler._new_mcp_server = lambda: fake_server  # type: ignore[method-assign]
        task = ManagerTask(
            agent=SpecialistAgentName.ECLASS,
            capability=CapabilityCode.ECLASS_QUERY,
            instruction="1번 공지 세부 내용을 조회한다.",
            verified_announcement_target=VerifiedAnnouncementTarget(
                id="534552",
                title="기말고사 시험지 확인 시간 안내",
                url=url,
                course_id="46516",
                year=2026,
                semester=1,
            ),
        )

        result = await handler(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("시험지 확인 안내 원문", result.verified_display_text or "")
        fake_server.call_tool.assert_awaited_once_with(
            "get_announcement_details",
            {
                "announcement_url": url,
                "course_id": "46516",
                "year": 2026,
                "semester": 1,
            },
        )

    async def test_captures_structured_announcement_body_verbatim(self) -> None:
        body = "중간 및 최종발표에서 산업체 전문가 또는 교수자가 제시한 피드백"
        response = AnnouncementDetailsResult(
            ok=True,
            data=AnnouncementDetails(
                id="532941",
                course_id="46500",
                title="결과보고서 작성 관련 안내사항",
                url="https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1102436&bwid=532941",
                content=body,
            ),
        )
        capture = _VerifiedMcpOutputCapture()

        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="get_announcement_details"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.announcement_details)
        self.assertIn(body, _announcement_display_text(capture.announcement_details))  # type: ignore[arg-type]

    async def test_captures_exact_announcement_candidates_for_follow_up(self) -> None:
        """공지 목록의 번호·제목·URL은 모델 요약이 아니라 MCP 원본으로 보존한다."""

        response = AnnouncementListResult(
            ok=True,
            selected_term=SelectedTerm(year=2026, semester=1, selection_source="user_request"),
            data=[
                Announcement(
                    id="532001",
                    course_id="46516",
                    title="딥러닝 수업 운영 안내",
                    url="https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1&bwid=532001",
                ),
                Announcement(
                    id="532002",
                    course_id="46516",
                    title="프로젝트 제출 안내",
                    url="https://learn.hansung.ac.kr/mod/ubboard/article.php?id=1&bwid=532002",
                ),
            ],
        )
        capture = _VerifiedMcpOutputCapture()

        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_announcements"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.announcement_list)
        display = _announcement_list_display_text(capture.announcement_list)  # type: ignore[arg-type]
        context = _announcement_followup_context(capture.announcement_list)  # type: ignore[arg-type]
        self.assertIn("1. [날짜 없음] 딥러닝 수업 운영 안내", display)
        self.assertIn('"number":1', context)
        self.assertIn('"id":"532001"', context)
        self.assertIn('"url":"https://learn.hansung.ac.kr/', context)

    async def test_course_list_preserves_professor_and_separates_eclass_groups(self) -> None:
        """모델을 거치지 않고 교수명 원문과 강좌명 뒤 그룹 코드를 구분해 표시한다."""

        response = CourseListResult(
            ok=True,
            selected_term=SelectedTerm(year=2026, semester=1, selection_source="user_request"),
            data=[
                Course(
                    id="46545",
                    name="데이터베이스[9,A,B,C]",
                    professor="장재영",
                    url="https://learn.hansung.ac.kr/course/view.php?id=46545",
                    year=2026,
                    semester=1,
                ),
                Course(
                    id="46516",
                    name="딥러닝[A,B,N]",
                    professor="조혜경",
                    url="https://learn.hansung.ac.kr/course/view.php?id=46516",
                    year=2026,
                    semester=1,
                ),
            ],
        )
        capture = _VerifiedMcpOutputCapture()

        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_courses"),
            response.model_dump_json(),
        )

        self.assertIsNotNone(capture.course_list)
        self.assertEqual(capture.last_data_tool, "list_courses")
        display = _course_list_display_text(capture.course_list)  # type: ignore[arg-type]
        context = _course_followup_context(capture.course_list)  # type: ignore[arg-type]
        self.assertIn("1. 데이터베이스\n", display)
        self.assertNotIn("[9,A,B,C]", display)
        self.assertNotIn("E-Class 그룹", display)
        self.assertIn("담당자: 장재영", display)
        self.assertIn("2. 딥러닝\n", display)
        self.assertIn("담당자: 조혜경", display)
        self.assertIn('"professor":"조혜경"', context)
        self.assertEqual(_split_eclass_course_name("컴퓨터비전[7]"), ("컴퓨터비전", ["7"]))

        # 강좌 목록 뒤 실제 강의 영상 Tool을 호출했다면 중간 list_courses를 최종 결과로 쓰지 않는다.
        await capture.on_tool_end(
            None,
            None,
            SimpleNamespace(name="list_lectures"),
            "{}",
        )
        self.assertEqual(capture.last_data_tool, "list_lectures")

    async def test_ensure_server_reconnects_after_first_connect_failure(self) -> None:
        """최초 connect 실패가 난 MCP 객체를 다음 요청에서 재사용하지 않는다."""

        failed_server = SimpleNamespace(
            connect=AsyncMock(side_effect=RuntimeError("stdio connect failed")),
            cleanup=AsyncMock(return_value=None),
        )
        recovered_server = SimpleNamespace(
            connect=AsyncMock(return_value=None),
            cleanup=AsyncMock(return_value=None),
        )
        servers = iter((failed_server, recovered_server))
        handler = EclassMcpSpecialistHandler(Settings(_env_file=None))
        handler._new_mcp_server = lambda: next(servers)  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "stdio connect failed"):
            await handler._ensure_server()

        self.assertIsNone(handler._server)
        self.assertIsNone(handler._server_lifecycle_task)
        failed_server.cleanup.assert_awaited_once()

        server = await handler._ensure_server()

        self.assertIs(server, recovered_server)
        failed_server.connect.assert_awaited_once()
        recovered_server.connect.assert_awaited_once()
        await handler.close()
        recovered_server.cleanup.assert_awaited_once()

    async def test_ensure_server_does_not_reuse_dead_lifecycle(self) -> None:
        """필드에 서버가 남아도 lifecycle이 끝났다면 새 서버를 연결한다."""

        dead_server = SimpleNamespace(
            connect=AsyncMock(return_value=None),
            cleanup=AsyncMock(return_value=None),
        )
        fresh_server = SimpleNamespace(
            connect=AsyncMock(return_value=None),
            cleanup=AsyncMock(return_value=None),
        )
        servers = iter((dead_server, fresh_server))
        handler = EclassMcpSpecialistHandler(Settings(_env_file=None))
        handler._new_mcp_server = lambda: next(servers)  # type: ignore[method-assign]

        first = await handler._ensure_server()
        self.assertIs(first, dead_server)
        assert handler._server_close_event is not None
        assert handler._server_lifecycle_task is not None
        handler._server_close_event.set()
        await handler._server_lifecycle_task
        self.assertTrue(handler._server_lifecycle_task.done())

        second = await handler._ensure_server()

        self.assertIs(second, fresh_server)
        self.assertIsNot(first, second)
        dead_server.cleanup.assert_awaited_once()
        fresh_server.connect.assert_awaited_once()
        await handler.close()
        fresh_server.cleanup.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
