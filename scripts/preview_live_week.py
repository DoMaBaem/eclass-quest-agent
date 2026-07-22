"""실제 E-Class의 지정 학기·주차 데이터로 TUI 왼쪽 패널을 검증한다.

강좌·강의·과제는 저장된 로그인 세션을 사용하는 읽기 전용 MCP 서비스로 조회한다.
영상 재생, 출석 변경, 과제 제출은 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings
from app.schemas.domain import EntityStatus
from app.sync.schemas import (
    AssignmentChecklistItem,
    CourseChecklistItem,
    LectureChecklistItem,
    SyncResult,
    SyncStatus,
    SyncTrigger,
)
from app.tui.app import EclassQuestApp
from mcp_server.schemas import McpResponse, SelectedTerm
from mcp_server.services.eclass_read import EclassReadService


SEOUL = ZoneInfo("Asia/Seoul")


def _require_success(response: McpResponse, operation: str) -> None:
    """MCP 오류를 비밀값 없이 테스트 실패 메시지로 바꾼다."""

    if response.ok:
        return
    code = response.error.code.value if response.error else "UNKNOWN"
    message = response.error.message if response.error else "원인을 확인할 수 없습니다."
    raise RuntimeError(f"{operation} 실패 [{code}]: {message}")


def _aware(value: datetime) -> datetime:
    """LMS의 naive datetime도 서울 시간으로 안전하게 비교한다."""

    if value.tzinfo is None:
        return value.replace(tzinfo=SEOUL)
    return value.astimezone(SEOUL)


def _week_window(lectures: list, *, year: int, week: int) -> tuple[datetime, datetime]:
    """실제 강의 공개일을 우선 사용해 해당 주차의 월요일~다음 월요일을 구한다."""

    opened = [_aware(item.available_from) for item in lectures if item.available_from is not None]
    if opened:
        anchor = min(opened)
    else:
        # 공개일이 없는 강좌도 테스트할 수 있도록 1학기 1주차를 3월 첫 월요일로 계산한다.
        march_first = datetime.combine(datetime(year, 3, 1).date(), time.min, tzinfo=SEOUL)
        first_monday = march_first + timedelta(days=(7 - march_first.weekday()) % 7)
        anchor = first_monday + timedelta(weeks=week - 1)
    start = datetime.combine(
        (anchor - timedelta(days=anchor.weekday())).date(),
        time.min,
        tzinfo=SEOUL,
    )
    return start, start + timedelta(days=7)


async def load_live_week(settings: Settings, *, year: int, semester: int, week: int) -> SyncResult:
    """실제 학기 데이터를 읽고 지정 주차 패널 모델로 변환한다."""

    reader = EclassReadService(settings)
    courses_result = await reader.list_courses(year, semester)
    _require_success(courses_result, "강좌 조회")
    lectures_result = await reader.list_lectures(None, False, year, semester)
    _require_success(lectures_result, "강의 조회")
    assignments_result = await reader.list_assignments(None, False, year, semester)
    _require_success(assignments_result, "과제 조회")

    courses = courses_result.data
    course_names = {course.id: course.name for course in courses}
    week_pattern = re.compile(rf"(?:^|\D){week}\s*주(?:차)?(?:\D|$)")
    week_lectures = [
        lecture
        for lecture in lectures_result.data
        if lecture.week == week or week_pattern.search(lecture.title)
    ]
    start, end = _week_window(week_lectures, year=year, week=week)

    lecture_items = [
        LectureChecklistItem(
            lecture_id=lecture.id,
            course_id=lecture.course_id,
            course_name=course_names.get(lecture.course_id, "강좌명 확인 불가"),
            title=lecture.title,
            week=lecture.week or week,
            progress_percent=lecture.progress_percent,
            completed=(
                lecture.status is EntityStatus.COMPLETE
                or lecture.attendance_status is EntityStatus.COMPLETE
                or (lecture.progress_percent is not None and lecture.progress_percent >= 100)
            ),
            available_from=lecture.available_from,
            available_until=lecture.available_until,
        )
        for lecture in week_lectures
    ]
    lecture_items.sort(key=lambda item: (item.completed, item.course_name, item.title))

    assignment_items = []
    for assignment in assignments_result.data:
        if assignment.due_at is None:
            continue
        due_at = _aware(assignment.due_at)
        if not start <= due_at < end:
            continue
        assignment_items.append(
            AssignmentChecklistItem(
                assignment_id=assignment.id,
                course_name=course_names.get(assignment.course_id, "강좌명 확인 불가"),
                title=assignment.title,
                due_at=assignment.due_at,
                completed=(
                    assignment.submitted is True
                    or assignment.status is EntityStatus.COMPLETE
                ),
            )
        )
    assignment_items.sort(key=lambda item: (item.completed, _aware(item.due_at), item.course_name))

    selected_term = courses_result.selected_term or SelectedTerm(
        year=year,
        semester=semester,
        selection_source="user_request",
    )
    now = datetime.now(SEOUL)
    return SyncResult(
        status=SyncStatus.COMPLETED,
        trigger=SyncTrigger.MANUAL,
        selected_term=selected_term,
        course_count=len(courses),
        observed_count=len(courses) + len(lectures_result.data) + len(assignments_result.data),
        course_checklist=[
            CourseChecklistItem(course_id=course.id, course_name=course.name)
            for course in courses
        ],
        lecture_checklist=lecture_items,
        assignment_checklist=assignment_items,
        started_at=now,
        finished_at=now,
    )


class LiveWeekPreviewApp(EclassQuestApp):
    """조회 완료된 실데이터만 주입하고 자동 현재 학기 동기화는 끈 테스트 화면."""

    def __init__(self, settings: Settings, result: SyncResult, *, week: int) -> None:
        super().__init__(settings, enable_sync=False)
        self.preview_result = result
        self.preview_week = week

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self._render_live_result)

    def _render_live_result(self) -> None:
        result = self.preview_result
        term = result.selected_term
        term_name = f"{term.year}년 {term.semester_name}" if term else "지정 학기"
        self.query_one("#status").update(f"실제 E-Class {term_name} {self.preview_week}주차 읽기 전용 테스트")
        self._render_lecture_checklist(result.lecture_checklist, result=result)
        self._render_assignment_checklist(result.assignment_checklist, result=result)


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 E-Class 주차 데이터로 TUI 패널을 검증합니다.")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--semester", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--week", type=int, default=7)
    args = parser.parse_args()

    settings = get_settings()
    result = asyncio.run(
        load_live_week(
            settings,
            year=args.year,
            semester=args.semester,
            week=args.week,
        )
    )
    print(
        f"실데이터 조회 완료: 강의 {len(result.lecture_checklist)}개, "
        f"과제 {len(result.assignment_checklist)}개",
        flush=True,
    )
    LiveWeekPreviewApp(settings, result, week=args.week).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
