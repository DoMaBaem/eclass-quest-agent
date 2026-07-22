"""Mission 계획을 MySQL의 결정론적 저장소 작업으로 실행한다."""

from __future__ import annotations

from app.config import Settings
from app.schemas.manager import ManagerAction, ManagerTask, SpecialistResult, SpecialistStatus
from app.schemas.workflow import ErrorCode
from app.storage.database import Database
from app.storage.mission_repository import MissionRepository


class MissionServiceHandler:
    """LLM을 사용하지 않고 생성·조회·수정·완료 규칙만 수행한다."""

    def __init__(self, settings: Settings, *, user_id: str = "local-user") -> None:
        self.settings = settings
        self.user_id = user_id
        self._trace_events: list[tuple[str, str]] = []

    async def __call__(self, task: ManagerTask) -> SpecialistResult:
        self._trace_events = []
        if not self.settings.mysql_url:
            return SpecialistResult(
                status=SpecialistStatus.CAPABILITY_NOT_READY,
                summary="Mission 저장을 위한 MYSQL_URL이 설정되지 않았습니다.",
            )
        database = Database(self.settings.mysql_url)
        try:
            async with database.session() as session:
                repository = MissionRepository(session, user_id=self.user_id)
                self._trace_events.append(("MissionRepository", "STARTED"))
                if task.action is ManagerAction.COMPLETE:
                    if task.slots.mission_id is None:
                        return self._invalid_request("완료할 미션 ID가 필요합니다.")
                    mission = await repository.mark_completed(task.slots.mission_id)
                    if mission is None:
                        return self._not_found()
                    return self._result([mission], "미션을 완료 처리했습니다.")
                if task.action is ManagerAction.UPDATE:
                    if task.slots.mission_id is None:
                        return self._invalid_request("수정할 미션 ID가 필요합니다.")
                    if not task.slots.query:
                        return self._invalid_request("수정할 미션 제목이 필요합니다.")
                    mission = await repository.update_mission(
                        task.slots.mission_id,
                        title=task.slots.query,
                    )
                    if mission is None:
                        return self._not_found()
                    return self._result([mission], "미션을 수정했습니다.")
                # LIST_MISSIONS만 목록을 읽는다. instruction에 "완료" 같은 단어가 있어도
                # action을 바꾸지 않는다. filter가 없는 이전 호출에 한해 자연어를 보수적으로 본다.
                filter_text = task.slots.filter
                if filter_text is None:
                    compact_instruction = "".join(task.instruction.casefold().split())
                    if "오늘" in compact_instruction:
                        filter_text = "오늘"
                    elif any(
                        word in compact_instruction for word in ("이번주", "주간", "일주일")
                    ):
                        filter_text = "이번 주"
                compact_filter = "".join((filter_text or "").casefold().split())
                if "오늘" in compact_filter:
                    return self._result(await repository.list_today(), "오늘의 미션")
                if any(word in compact_filter for word in ("이번주", "주간", "일주일")):
                    return self._result(await repository.list_weekly(), "7일 이내 미션")
                # E-Class 동기화 단계에서 이미 중복 없이 생성하므로 기본 Mission 요청은 주간 목록이다.
                return self._result(await repository.list_weekly(), "7일 이내 미션")
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="Mission 저장소 작업에 실패했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        finally:
            await database.dispose()

    def consume_trace_events(self) -> list[tuple[str, str]]:
        events, self._trace_events = self._trace_events, []
        return events

    @staticmethod
    def _not_found() -> SpecialistResult:
        return SpecialistResult(
            status=SpecialistStatus.FAILED,
            summary="요청한 미션을 찾을 수 없습니다.",
            error_code=ErrorCode.INVALID_REQUEST,
        )

    @staticmethod
    def _invalid_request(summary: str) -> SpecialistResult:
        return SpecialistResult(
            status=SpecialistStatus.FAILED,
            summary=summary,
            error_code=ErrorCode.INVALID_REQUEST,
        )

    @staticmethod
    def _result(missions, heading: str) -> SpecialistResult:
        if not missions:
            display = f"{heading}: 예정된 미션이 없습니다."
        else:
            lines = [heading]
            for mission in missions:
                due = mission.due_at.strftime("%Y-%m-%d %H:%M") if mission.due_at else "기한 없음"
                lines.append(
                    f"- #{mission.id} [{mission.priority}] {mission.title} · {due} · {mission.status}"
                )
            display = "\n".join(lines)
        return SpecialistResult(
            status=SpecialistStatus.COMPLETED,
            summary=display,
            evidence_refs=[f"mission:{mission.id}" for mission in missions],
            verified_display_text=display,
        )
