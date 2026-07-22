"""로드맵 7·8단계의 행동 권한, 문서 변환, Mission 우선순위, 비밀 경계를 검증한다."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.config import Settings
from app.approval import ApprovalGate
from app.guardrails import GuardrailViolation, contained_path, guard_user_input, require_eclass_url
from app.storage.mission_repository import MissionRepository
from document_mcp_server import server as markitdown_server
from app.agent.eclass_mcp_handler import EclassMcpSpecialistHandler
from mcp_server.schemas import PlaybackInfo
from mcp_server.services.playback import LecturePlaybackService


class GuardrailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            eclass_username="student-number",
            eclass_password="secret-password",
        )

    def test_secret_and_disallowed_actions_are_blocked_before_agent(self) -> None:
        with self.assertRaises(GuardrailViolation) as secret:
            guard_user_input("비밀번호=secret-password", self.settings)
        self.assertEqual(secret.exception.code, "SECRET_DETECTED")
        with self.assertRaises(GuardrailViolation) as action:
            guard_user_input("과제를 대신 제출해줘", self.settings)
        self.assertEqual(action.exception.code, "ACTION_NOT_ALLOWED")

    def test_only_exact_eclass_host_and_contained_path_are_allowed(self) -> None:
        self.assertEqual(
            require_eclass_url("https://learn.hansung.ac.kr/mod/assign/view.php?id=1", self.settings),
            "https://learn.hansung.ac.kr/mod/assign/view.php?id=1",
        )
        with self.assertRaises(GuardrailViolation):
            require_eclass_url("https://evil.example/mod/assign/view.php?id=1", self.settings)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(contained_path(root, root / "safe.txt"), (root / "safe.txt").resolve())
            with self.assertRaises(GuardrailViolation):
                contained_path(root, root / ".." / "outside.txt")

    def test_mutating_tools_cannot_register_without_human_approval(self) -> None:
        with self.assertRaises(ValueError):
            ApprovalGate.validate_registration("submit_assignment", needs_approval=False)
        ApprovalGate.validate_registration("submit_assignment", needs_approval=True)

    def test_natural_korean_playback_words_count_as_explicit_requests(self) -> None:
        """짧은 구어체 명령도 사용자가 직접 요청한 재생·중지로 판정한다."""

        for message in ("2주차 영상 봐", "그거 봐", "강의 멈춰"):
            with self.subTest(message=message):
                guarded = guard_user_input(message, self.settings)
                self.assertTrue(guarded.explicit_playback_request)


class StageSevenServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_stdio_mcp_inherits_only_allowlisted_gui_environment(self) -> None:
        """Agent subprocess가 DISPLAY는 받되 API 키 같은 나머지 환경변수는 직접 전달하지 않는다."""

        with patch.dict(
            os.environ,
            {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "PRIVATE_VALUE": "secret"},
            clear=True,
        ):
            server = EclassMcpSpecialistHandler(Settings(_env_file=None))._new_mcp_server()

        self.assertEqual(server.params.env["DISPLAY"], ":0")
        self.assertEqual(server.params.env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertNotIn("PRIVATE_VALUE", server.params.env)

    def test_playback_supports_hansung_video_viewer_popup_link(self) -> None:
        self.assertIn("/mod/vod/viewer.php", LecturePlaybackService.VIEWER_LINK_SELECTOR)
        self.assertIn("동영상 보기", LecturePlaybackService.VIEWER_LINK_SELECTOR)
        self.assertIn(
            "--autoplay-policy=no-user-gesture-required",
            LecturePlaybackService.CHROMIUM_LAUNCH_ARGS,
        )

    async def test_playback_requires_explicit_user_request_before_browser_launch(self) -> None:
        result = await LecturePlaybackService(Settings(_env_file=None)).play(
            "12345", explicit_user_request=False
        )
        self.assertFalse(result.ok)
        self.assertIn("명시적 요청", result.error.message)  # type: ignore[union-attr]

    async def test_demo_preview_also_requires_request_and_caps_duration(self) -> None:
        service = LecturePlaybackService(Settings(_env_file=None))
        blocked = await service.preview("12345", explicit_user_request=False, seconds=20)
        invalid = await service.preview("12345", explicit_user_request=True, seconds=31)
        self.assertFalse(blocked.ok)
        self.assertFalse(invalid.ok)
        self.assertIn("5~30초", invalid.error.message)  # type: ignore[union-attr]

    async def test_playback_rejects_invalid_media_and_window_settings_before_launch(self) -> None:
        service = LecturePlaybackService(Settings(_env_file=None))

        invalid_volume = await service.play(
            "12345", explicit_user_request=True, volume_percent=101
        )
        invalid_rate = await service.play(
            "12345", explicit_user_request=True, playback_rate=2.5
        )
        invalid_window = await service.play(
            "12345", explicit_user_request=True, window_width=500, window_height=800
        )

        self.assertIn("볼륨", invalid_volume.error.message)  # type: ignore[union-attr]
        self.assertIn("배속", invalid_rate.error.message)  # type: ignore[union-attr]
        self.assertIn("창 크기", invalid_window.error.message)  # type: ignore[union-attr]

    async def test_playback_audit_failure_does_not_change_playback_result(self) -> None:
        """MySQL 감사 기록은 부가 기능이므로 실패해도 재생 성공을 실패로 바꾸지 않는다."""

        service = LecturePlaybackService(Settings(_env_file=None))
        service._record = AsyncMock(side_effect=RuntimeError("db unavailable"))
        info = PlaybackInfo(
            playback_id="00000000-0000-0000-0000-000000000001",
            lecture_id="12345",
            status="PLAYING",
            started_at=datetime.now(timezone.utc),
        )

        recorded = await service._record_safely(info, request_id=info.playback_id)

        self.assertFalse(recorded)

    async def test_markitdown_converts_only_manifest_backed_local_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            download_id = str(uuid4())
            directory = root / download_id
            directory.mkdir()
            source = directory / "assignment.txt"
            source.write_text("과제 제목\n제출 기한: 금요일\n", encoding="utf-8")
            manifest = {
                "download_id": download_id,
                "relative_path": source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
            (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(markitdown_server.settings, "download_root", root):
                result = await markitdown_server.convert_download(download_id)
            self.assertTrue(result.ok)
            self.assertIn("과제 제목", result.markdown)
            self.assertTrue((directory / "converted.md").exists())

    async def test_markitdown_mcp_exposes_only_contained_conversion_tool(self) -> None:
        tools = await markitdown_server.mcp.list_tools()
        self.assertEqual([tool.name for tool in tools], ["convert_download"])
        self.assertTrue(tools[0].outputSchema)

    def test_priority_uses_verified_deadline_only(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertEqual(
            MissionRepository.calculate_priority(due_at=now + timedelta(hours=5), completed=False, now=now),
            "URGENT",
        )
        self.assertEqual(
            MissionRepository.calculate_priority(due_at=now + timedelta(hours=20), completed=False, now=now),
            "HIGH",
        )
        self.assertEqual(
            MissionRepository.calculate_priority(due_at=None, completed=False, now=now),
            "NORMAL",
        )
