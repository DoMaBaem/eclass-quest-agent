"""Mission Service가 자연어 재분류 없이 typed 계약을 실행하는지 검증한다."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.mission_handler import MissionServiceHandler
from app.config import Settings
from app.schemas.manager import (
    ExecutionTargetName,
    ManagerAction,
    ManagerEntityKind,
    ManagerTask,
    ManagerTaskSlots,
    SpecialistStatus,
)
from app.schemas.workflow import CapabilityCode, ErrorCode


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class MissionServiceHandlerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(mysql_url="mysql+asyncmy://unused")
        self.database = SimpleNamespace(
            session=lambda: _SessionContext(),
            dispose=AsyncMock(),
        )
        self.repository = SimpleNamespace(
            list_today=AsyncMock(return_value=[]),
            list_weekly=AsyncMock(return_value=[]),
            mark_completed=AsyncMock(),
            update_mission=AsyncMock(),
        )

    def _task(
        self,
        *,
        action: ManagerAction,
        instruction: str,
        slots: ManagerTaskSlots,
    ) -> ManagerTask:
        return ManagerTask(
            agent=ExecutionTargetName.MISSION_SERVICE,
            capability=CapabilityCode.MISSION_MANAGEMENT,
            entity=ManagerEntityKind.MISSION,
            action=action,
            slots=slots,
            instruction=instruction,
        )

    async def _run(self, task: ManagerTask):
        with (
            patch("app.agent.mission_handler.Database", return_value=self.database),
            patch(
                "app.agent.mission_handler.MissionRepository",
                return_value=self.repository,
            ),
        ):
            return await MissionServiceHandler(self.settings)(task)

    async def test_list_action_is_not_overridden_by_misleading_instruction(self) -> None:
        """instruction에 완료 표현이 있어도 typed LIST_MISSIONS 계약을 따른다."""

        task = self._task(
            action=ManagerAction.LIST_MISSIONS,
            instruction="미션 #7을 완료했다고 적힌 오늘 목록을 보여준다.",
            slots=ManagerTaskSlots(filter="오늘", mission_id=7),
        )

        result = await self._run(task)

        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.repository.list_today.assert_awaited_once_with()
        self.repository.list_weekly.assert_not_awaited()
        self.repository.mark_completed.assert_not_awaited()

    async def test_complete_action_uses_typed_mission_id_only(self) -> None:
        """instruction이 목록처럼 보여도 COMPLETE와 mission_id가 실제 작업을 정한다."""

        mission = SimpleNamespace(
            id=17,
            priority="LOW",
            title="과제 제출",
            status="COMPLETED",
            due_at=None,
        )
        self.repository.mark_completed.return_value = mission
        task = self._task(
            action=ManagerAction.COMPLETE,
            instruction="이번 주 미션 목록을 보여준다.",
            slots=ManagerTaskSlots(mission_id=17),
        )

        result = await self._run(task)

        self.repository.mark_completed.assert_awaited_once_with(17)
        self.repository.list_weekly.assert_not_awaited()
        self.assertEqual(result.status, SpecialistStatus.COMPLETED)
        self.assertIn("#17", result.verified_display_text or "")

    async def test_complete_without_typed_mission_id_is_rejected(self) -> None:
        """자연어에 숫자가 있어도 typed slot이 없으면 저장소를 변경하지 않는다."""

        task = self._task(
            action=ManagerAction.COMPLETE,
            instruction="미션 #23을 완료해줘.",
            slots=ManagerTaskSlots(),
        )

        result = await self._run(task)

        self.assertEqual(result.status, SpecialistStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.INVALID_REQUEST)
        self.repository.mark_completed.assert_not_awaited()

    async def test_update_uses_typed_id_and_query_as_new_title(self) -> None:
        mission = SimpleNamespace(
            id=4,
            priority="NORMAL",
            title="새 제목",
            status="PENDING",
            due_at=None,
        )
        self.repository.update_mission.return_value = mission
        task = self._task(
            action=ManagerAction.UPDATE,
            instruction="오늘 미션 목록을 조회한다.",
            slots=ManagerTaskSlots(mission_id=4, query="새 제목"),
        )

        result = await self._run(task)

        self.repository.update_mission.assert_awaited_once_with(4, title="새 제목")
        self.repository.list_today.assert_not_awaited()
        self.assertEqual(result.status, SpecialistStatus.COMPLETED)

    def test_legacy_instruction_is_converted_once_into_typed_mission_slots(self) -> None:
        """이전 호출자는 Pydantic 호환 변환 뒤 동일한 typed 실행 경로를 사용한다."""

        task = ManagerTask(
            agent=ExecutionTargetName.MISSION_SERVICE,
            capability=CapabilityCode.MISSION_MANAGEMENT,
            instruction="미션 #9 완료",
        )

        self.assertIs(task.action, ManagerAction.COMPLETE)
        self.assertEqual(task.slots.mission_id, 9)


if __name__ == "__main__":
    unittest.main()
