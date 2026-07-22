"""실제 자료 기반 heartbeat TUI 시연 모드의 로컬 표시 테스트."""

from __future__ import annotations

import unittest

from app.schemas.runtime import RuntimeEvent, RuntimeEventType
from scripts.verify_live_heartbeat_alert import HeartbeatAlertPreviewApp, preview_message


def deadline_event(event_type: RuntimeEventType, *, title: str, hours: int) -> RuntimeEvent:
    return RuntimeEvent(
        event_type=event_type,
        payload={
            "verification_mode": "time_shift_no_external_write",
            "items": [
                {
                    "title": title,
                    "threshold_hours": hours,
                    "deadline": "2026-05-17T23:59:00+09:00",
                }
            ],
        },
    )


class LiveHeartbeatPreviewTest(unittest.IsolatedAsyncioTestCase):
    def test_preview_message_distinguishes_assignment_and_lecture(self) -> None:
        assignment = preview_message(
            deadline_event(RuntimeEventType.DEADLINE_WARNING, title="보고서", hours=6)
        )
        lecture = preview_message(
            deadline_event(RuntimeEventType.ATTENDANCE_WARNING, title="3주차 강의", hours=1)
        )

        self.assertIn("미제출 과제", assignment)
        self.assertIn("6시간 이내", assignment)
        self.assertIn("미수강 강의", lecture)
        self.assertIn("1시간 이내", lecture)

    async def test_preview_app_appends_alert_without_user_input(self) -> None:
        event = deadline_event(RuntimeEventType.DEADLINE_WARNING, title="보고서", hours=6)
        app = HeartbeatAlertPreviewApp([event])

        async with app.run_test() as pilot:
            # 예약 타이머를 기다리지 않고 같은 선제 알림 메서드를 직접 실행해 표시 계약을 검증한다.
            await app._show_preview_alerts()
            await pilot.pause()

            self.assertTrue(any("미제출 과제" in row for row in app.transcript))
            self.assertTrue(any(row.startswith("SYSTEM >") for row in app.transcript))


if __name__ == "__main__":
    unittest.main()
