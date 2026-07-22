"""FastMCP에 약속한 읽기 Tool이 모두 등록되는지 확인한다."""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from app.schemas.domain import utc_now
from mcp_server.schemas import (
    DashboardSnapshotData,
    DashboardSnapshotResult,
    McpOutcomeStatus,
    PlaybackInfo,
    PlaybackResult,
    SelectedTerm,
    VerifiedLectureTarget,
)
from mcp_server.server import (
    get_dashboard_snapshot,
    mcp,
    play_resolved_lecture,
    preview_resolved_lecture,
)


class EclassMcpServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_eclass_tools_have_structured_output(self) -> None:
        expected = {
            "check_session",
            "list_courses",
            "get_dashboard_snapshot",
            "resolve_course",
            "list_announcements",
            "list_course_announcements",
            "get_announcement_details",
            "list_assignments",
            "list_course_assignments",
            "get_assignment_details",
            "list_assignment_attachments",
            "list_lectures",
            "list_course_lectures",
            "resolve_lecture",
            "get_lecture_status",
            "get_grades",
            "play_lecture",
            "play_resolved_lecture",
            "preview_resolved_lecture",
            "stop_lecture",
            "preview_lecture",
            "download_attachment",
        }

        tools = await mcp.list_tools()

        self.assertEqual({tool.name for tool in tools}, expected)
        self.assertTrue(all(tool.outputSchema for tool in tools))
        dashboard_tool = next(
            tool for tool in tools if tool.name == "get_dashboard_snapshot"
        )
        self.assertEqual(dashboard_tool.inputSchema.get("properties"), {})
        self.assertNotIn("attachment_url", dashboard_tool.inputSchema.get("properties", {}))
        assignment_tool = next(tool for tool in tools if tool.name == "list_assignments")
        self.assertIn("course_id", assignment_tool.inputSchema["properties"])
        self.assertIn("course_query", assignment_tool.inputSchema["properties"])
        semantic_lecture_tool = next(
            tool for tool in tools if tool.name == "list_course_lectures"
        )
        self.assertIn("course_query", semantic_lecture_tool.inputSchema["properties"])
        self.assertIn("week", semantic_lecture_tool.inputSchema["properties"])
        self.assertNotIn("course_id", semantic_lecture_tool.inputSchema["properties"])
        safe_playback_tool = next(
            tool for tool in tools if tool.name == "play_resolved_lecture"
        )
        self.assertIn("reference_id", safe_playback_tool.inputSchema["properties"])
        self.assertNotIn("lecture_id", safe_playback_tool.inputSchema["properties"])
        safe_preview_tool = next(
            tool for tool in tools if tool.name == "preview_resolved_lecture"
        )
        self.assertIn("reference_id", safe_preview_tool.inputSchema["properties"])
        self.assertIn("seconds", safe_preview_tool.inputSchema["properties"])
        self.assertIn("options", safe_preview_tool.inputSchema["properties"])
        self.assertNotIn("lecture_id", safe_preview_tool.inputSchema["properties"])

    async def test_dashboard_tool_delegates_to_atomic_service_contract(self) -> None:
        response = DashboardSnapshotResult(
            ok=True,
            status=McpOutcomeStatus.FOUND,
            selected_term=SelectedTerm(
                year=2026,
                semester=3,
                selection_source="eclass_default",
            ),
            data=DashboardSnapshotData(),
        )
        with patch(
            "mcp_server.server.service.get_dashboard_snapshot",
            new=AsyncMock(return_value=response),
        ) as snapshot:
            result = await get_dashboard_snapshot()

        self.assertEqual(result, response)
        snapshot.assert_awaited_once_with()

    async def test_safe_playback_uses_server_verified_target_id(self) -> None:
        now = utc_now()
        target = VerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000001",
            lecture_id="verified-lecture-id",
            course_id="46500",
            course_name="빅데이터프로그래밍",
            title="02주차 가상환경",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        playback = PlaybackResult(
            ok=True,
            data=PlaybackInfo(
                playback_id="00000000-0000-0000-0000-000000000002",
                lecture_id="verified-lecture-id",
                status="PLAYING",
                started_at=now,
            ),
        )
        with (
            patch(
                "mcp_server.server.service.get_verified_lecture_target",
                return_value=target,
            ),
            patch(
                "mcp_server.server.playback_service.play",
                new=AsyncMock(return_value=playback),
            ) as play,
        ):
            result = await play_resolved_lecture(
                target.reference_id,
                explicit_user_request=True,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        play.assert_awaited_once()
        self.assertEqual(play.await_args.args[0], "verified-lecture-id")

    async def test_safe_playback_rejects_reference_not_issued_by_server(self) -> None:
        with (
            patch(
                "mcp_server.server.service.get_verified_lecture_target",
                return_value=None,
            ),
            patch(
                "mcp_server.server.playback_service.play",
                new=AsyncMock(),
            ) as play,
        ):
            result = await play_resolved_lecture(
                "00000000-0000-0000-0000-000000000099",
                explicit_user_request=True,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.NOT_FOUND)
        play.assert_not_awaited()

    async def test_safe_playback_keeps_explicit_user_request_guard(self) -> None:
        now = utc_now()
        target = VerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000001",
            lecture_id="302",
            course_id="46500",
            course_name="빅데이터프로그래밍",
            title="02주차 가상환경",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        with patch(
            "mcp_server.server.service.get_verified_lecture_target",
            return_value=target,
        ):
            result = await play_resolved_lecture(target.reference_id)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.INVALID_REQUEST)

    async def test_safe_preview_uses_verified_target_and_nested_options(self) -> None:
        now = utc_now()
        target = VerifiedLectureTarget(
            reference_id="00000000-0000-0000-0000-000000000003",
            lecture_id="1133557",
            course_id="46499",
            course_name="데이터마이닝[A,B]",
            title="02주차 Python 개요",
            week=2,
            year=2026,
            semester=1,
            verified_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        playback = PlaybackResult(
            ok=True,
            data=PlaybackInfo(
                playback_id="00000000-0000-0000-0000-000000000004",
                lecture_id=target.lecture_id,
                status="PLAYING",
                volume_percent=70,
                playback_rate=1.25,
                window_width=1280,
                window_height=720,
                started_at=now,
            ),
        )
        with (
            patch(
                "mcp_server.server.service.get_verified_lecture_target",
                return_value=target,
            ),
            patch(
                "mcp_server.server.playback_service.preview",
                new=AsyncMock(return_value=playback),
            ) as preview,
        ):
            result = await preview_resolved_lecture(
                target.reference_id,
                explicit_user_request=True,
                seconds=15,
                options={
                    "volume_percent": 70,
                    "playback_rate": 1.25,
                    "window_width": 1280,
                    "window_height": 720,
                },
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.FOUND)
        preview.assert_awaited_once_with(
            "1133557",
            explicit_user_request=True,
            seconds=15,
            volume_percent=70,
            playback_rate=1.25,
            window_width=1280,
            window_height=720,
        )

    async def test_safe_preview_rejects_unverified_reference(self) -> None:
        with (
            patch(
                "mcp_server.server.service.get_verified_lecture_target",
                return_value=None,
            ),
            patch(
                "mcp_server.server.playback_service.preview",
                new=AsyncMock(),
            ) as preview,
        ):
            result = await preview_resolved_lecture(
                "00000000-0000-0000-0000-000000000099",
                explicit_user_request=True,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, McpOutcomeStatus.NOT_FOUND)
        preview.assert_not_awaited()
