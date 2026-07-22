"""실제 E-Class 자료를 이용해 heartbeat 마감 알림을 안전하게 검증한다.

방학에는 현재 학기에 임박한 과제·강의가 없으므로, 지정한 과거 학기의 실제 자료를 읽은 뒤
기준 시각만 실제 마감 직전으로 이동한다. E-Class의 제출·출석 상태나 MySQL 데이터는 변경하지
않는다. 실제 LMS 제목을 외부 모델로 전송하지 않고 로컬의 마감 판정과 이벤트 생성까지만 확인한다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.schemas.manager import ManagerPriority, ManagerResult, ManagerStatus
from app.schemas.domain import EntityStatus
from app.schemas.runtime import RuntimeEvent, RuntimeEventType
from app.sync.deadline import DeadlineService
from app.tui.app import EclassQuestApp
from mcp_server.schemas import McpResponse
from mcp_server.services.eclass_read import EclassReadService


def require_ok(label: str, result: McpResponse) -> None:
    """MCP 오류 원문이나 인증정보를 출력하지 않고 검증 실패만 명확히 알린다."""

    if result.ok:
        return
    code = result.error.code.value if result.error else "UNKNOWN"
    raise RuntimeError(f"{label} 조회 실패: {code}")


async def verify(year: int, semester: int) -> list[RuntimeEvent]:
    settings = get_settings()
    reader = EclassReadService(settings)

    print(f"[1/4] 실제 E-Class {year}년 {semester}학기 자료 조회 중...", flush=True)
    courses_result = await reader.list_courses(year, semester)
    assignments_result = await reader.list_assignments(None, False, year, semester)
    lectures_result = await reader.list_lectures(None, False, year, semester)
    require_ok("강좌", courses_result)
    require_ok("과제", assignments_result)
    require_ok("강의", lectures_result)

    courses = courses_result.data
    assignments = assignments_result.data
    lectures = lectures_result.data
    print(
        f"      강좌 {len(courses)}개 · 과제 {len(assignments)}개 · 강의 {len(lectures)}개",
        flush=True,
    )

    # 실제 제목과 마감 시각은 그대로 사용한다. 완료 여부만 '아직 하지 않았다면'이라는 가정으로
    # 바꾸고 기준 시각을 마감 직전으로 이동해 현재 방학에도 경계값을 재현한다.
    assignment = next((item for item in assignments if item.due_at is not None), None)
    lecture = next((item for item in lectures if item.available_until is not None), None)
    if assignment is None and lecture is None:
        raise RuntimeError("선택한 학기에 마감 시각이 있는 과제나 강의가 없습니다.")

    detector = DeadlineService()
    candidates = []
    if assignment is not None and assignment.due_at is not None:
        pending_assignment = assignment.model_copy(
            update={"submitted": False, "status": EntityStatus.INCOMPLETE}
        )
        candidates.extend(
            detector.evaluate(
                [pending_assignment],
                [],
                now=assignment.due_at - timedelta(hours=5),
            )
        )
    if lecture is not None and lecture.available_until is not None:
        pending_lecture = lecture.model_copy(
            update={
                "status": EntityStatus.INCOMPLETE,
                "attendance_status": EntityStatus.INCOMPLETE,
            }
        )
        candidates.extend(
            detector.evaluate(
                [],
                [pending_lecture],
                now=lecture.available_until - timedelta(minutes=30),
            )
        )

    print("[2/4] 마감 직전 시간 이동 판정 완료", flush=True)
    types = {candidate.notification_type for candidate in candidates}
    if assignment is not None and "assignment_due_6h" not in types:
        raise RuntimeError("미제출 과제의 6시간 전 알림이 생성되지 않았습니다.")
    if lecture is not None and "attendance_due_1h" not in types:
        raise RuntimeError("미수강 강의의 1시간 전 알림이 생성되지 않았습니다.")

    events: list[RuntimeEvent] = []
    term = courses_result.selected_term
    term_payload = term.model_dump(mode="json") if term is not None else {
        "year": year,
        "semester": semester,
        "selection_source": "user_request",
    }
    assignment_candidates = [item for item in candidates if item.entity_type == "assignment"]
    lecture_candidates = [item for item in candidates if item.entity_type == "lecture"]
    if assignment_candidates:
        events.append(
            RuntimeEvent(
                event_type=RuntimeEventType.DEADLINE_WARNING,
                payload={
                    "verification_mode": "time_shift_no_external_write",
                    "selected_term": term_payload,
                    "items": [item.payload for item in assignment_candidates],
                },
            )
        )
    if lecture_candidates:
        events.append(
            RuntimeEvent(
                event_type=RuntimeEventType.ATTENDANCE_WARNING,
                payload={
                    "verification_mode": "time_shift_no_external_write",
                    "selected_term": term_payload,
                    "items": [item.payload for item in lecture_candidates],
                },
            )
        )

    print(f"[3/4] heartbeat 구조화 알림 이벤트 {len(events)}개 생성", flush=True)
    for candidate in candidates:
        threshold = candidate.payload["threshold_hours"]
        label = "과제" if candidate.entity_type == "assignment" else "강의"
        print(f"      {label}: {candidate.payload['title']} → {threshold}시간 전 알림", flush=True)

    print("[4/4] 검증 성공: 실제 E-Class 읽기 → 마감 판정 → heartbeat 이벤트 생성", flush=True)
    return events


def preview_message(event: RuntimeEvent) -> str:
    """외부 모델 없이 구조화된 heartbeat 이벤트를 TUI 표시 문장으로 바꾼다."""

    items = event.payload.get("items", [])
    if not isinstance(items, list) or not items:
        return "[시간 이동 검증] 표시할 마감 항목이 없습니다."
    item = items[0] if isinstance(items[0], dict) else {}
    title = str(item.get("title", "제목 확인 불가"))
    hours = int(item.get("threshold_hours", 0) or 0)
    deadline = str(item.get("deadline", "마감 시각 확인 불가"))
    if event.event_type is RuntimeEventType.DEADLINE_WARNING:
        return (
            "[시간 이동 검증] 미제출 과제 마감이 임박했습니다.\n"
            f"과제: {title}\n마감 단계: {hours}시간 이내\n마감: {deadline}"
        )
    return (
        "[시간 이동 검증] 미수강 강의의 출석 인정 종료가 임박했습니다.\n"
        f"강의: {title}\n마감 단계: {hours}시간 이내\n종료: {deadline}"
    )


class HeartbeatAlertPreviewApp(EclassQuestApp):
    """실제 자료로 만든 검증 이벤트를 기존 TUI 대화창에 선제 표시한다."""

    def __init__(self, events: list[RuntimeEvent]) -> None:
        super().__init__(get_settings(), enable_sync=False)
        self.preview_events = events
        # 테스트가 같은 메서드를 직접 호출하는 동안 예약 timer가 겹쳐도 알림을 두 번
        # 표시하거나 화면 종료 뒤 위젯을 조회하지 않도록 한 번만 실행한다.
        self._preview_shown = False

    def on_mount(self) -> None:
        super().on_mount()
        # 첫 화면 렌더링 뒤 사용자 입력 없이 SYSTEM 알림을 발생시킨다.
        self.set_timer(0.8, self._show_preview_alerts, name="heartbeat-alert-preview")

    async def _show_preview_alerts(self) -> None:
        if self._preview_shown:
            return
        self._preview_shown = True
        for event in self.preview_events:
            await self.show_proactive_result(
                ManagerResult(
                    status=ManagerStatus.COMPLETED,
                    message=preview_message(event),
                    should_notify=True,
                    priority=ManagerPriority.HIGH,
                )
            )
            await asyncio.sleep(0.7)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="실제 과거 학기 자료와 시간 이동으로 heartbeat 알림을 검증합니다."
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--semester", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument(
        "--tui",
        action="store_true",
        help="검증된 과제·강의 경고를 실제 TUI 대화창에서 선제 알림으로 보여줍니다.",
    )
    args = parser.parse_args()
    events = asyncio.run(verify(args.year, args.semester))
    if args.tui:
        HeartbeatAlertPreviewApp(events).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
