"""실제 E-Class 2026년 1학기 7주차 데이터를 TUI 체크리스트까지 검증한다.

mock이나 DB snapshot을 사용하지 않는다. E-Class MCP stdio 서버에서 강좌·과제·강의를 직접
읽지만 영상 재생, 제출, 출석 변경 같은 행동 Tool은 호출하지 않는다.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from textual.widgets import RichLog, Static

from app.config import Settings
from app.schemas.domain import Assignment, Course, Lecture
from app.sync.schemas import CourseChecklistItem, SyncResult, SyncStatus, SyncTrigger
from app.sync.service import SyncService
from app.tui.app import EclassQuestApp


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def structured(result: Any) -> dict[str, Any]:
    payload = result.structuredContent
    if not isinstance(payload, dict):
        raise RuntimeError("MCP Tool이 구조화 결과를 반환하지 않았습니다.")
    if payload.get("ok") is not True:
        error = payload.get("error") or {}
        raise RuntimeError(f"MCP 조회 실패: {error.get('code', 'UNKNOWN')}")
    return payload


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def week_seven_reference(lectures: list[Lecture]) -> datetime:
    """여러 강좌의 7주차가 함께 열린 시각을 실제 공개 시작일로 계산한다."""

    opened = [aware(item.available_from) for item in lectures if item.available_from]
    if not opened:
        # 현재 VOD 목록에는 출석 기간이 없어 역사적 화면 검증의 기준일만 학사 7주차로 둔다.
        # 강의·진도·과제 데이터 자체는 모두 실제 MCP 응답이다.
        return datetime(2026, 4, 13, tzinfo=ZoneInfo("Asia/Seoul"))
    reference = max(opened)
    closes = [aware(item.available_until) for item in lectures if item.available_until]
    if closes and min(closes) < reference:
        raise RuntimeError("7주차 강의들의 출석 인정 기간이 서로 겹치지 않습니다.")
    return reference


async def verify() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=PROJECT_ROOT,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            term = {"year": 2026, "semester": 1}
            courses_payload = structured(await session.call_tool("list_courses", term))
            assignments_payload = structured(
                await session.call_tool(
                    "list_assignments",
                    {"days": None, "only_incomplete": False, **term},
                )
            )
            lectures_payload = structured(
                await session.call_tool(
                    "list_lectures",
                    {"course_id": None, "only_unwatched": False, **term},
                )
            )

    selected_term = lectures_payload.get("selected_term") or {}
    if selected_term.get("year") != 2026 or selected_term.get("semester") != 1:
        raise RuntimeError("MCP가 요청한 2026년 1학기를 적용하지 않았습니다.")

    courses = [Course.model_validate(item) for item in courses_payload["data"]]
    assignments = [Assignment.model_validate(item) for item in assignments_payload["data"]]
    lectures = [Lecture.model_validate(item) for item in lectures_payload["data"]]
    week_seven = [
        item
        for item in lectures
        if item.week == 7 or re.search(r"(?:^|\D)7\s*주차", item.title)
    ]
    if not week_seven:
        raise RuntimeError("실제 2026년 1학기 데이터에서 7주차 강의를 찾지 못했습니다.")

    reference_at = week_seven_reference(week_seven)
    lecture_checklist = SyncService._lecture_checklist(
        week_seven,
        courses=courses,
        now=reference_at,
    )
    assignment_checklist = SyncService._assignment_checklist(
        assignments,
        courses=courses,
        now=reference_at,
    )
    if not lecture_checklist:
        raise RuntimeError("7주차 강의가 TUI ACTIVE LECTURES 변환에서 모두 누락됐습니다.")

    result = SyncResult(
        status=SyncStatus.COMPLETED,
        trigger=SyncTrigger.MANUAL,
        selected_term=lectures_payload["selected_term"],
        course_count=len(courses),
        course_checklist=[
            CourseChecklistItem(course_id=course.id, course_name=course.name)
            for course in courses
        ],
        lecture_checklist=lecture_checklist,
        assignment_checklist=assignment_checklist,
        started_at=reference_at,
        finished_at=reference_at,
    )
    app = EclassQuestApp(
        Settings(_env_file=None, openai_api_key="live-verification-only"),
        enable_sync=False,
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app._render_lecture_checklist(result.lecture_checklist, result=result)
        app._render_assignment_checklist(result.assignment_checklist, result=result)
        lecture_summary = str(app.query_one("#lecture-summary", Static).render())
        lecture_log = app.query_one("#lecture-checklist", RichLog)
        assignment_log = app.query_one("#assignment-checklist", RichLog)
        lecture_lines = [line.text for line in lecture_log.lines]
        assignment_lines = [line.text for line in assignment_log.lines]

    print("실제 E-Class 2026년 1학기 7주차 검증 성공", flush=True)
    print(f"기준 시각: {reference_at.astimezone().isoformat(timespec='minutes')}", flush=True)
    print(f"ACTIVE LECTURES: {lecture_summary} / {len(lecture_checklist)}개", flush=True)
    for line in lecture_lines:
        print(f"  {line}", flush=True)
    print(f"THIS WEEK ASSIGNMENTS: {len(assignment_checklist)}개", flush=True)
    if assignment_lines:
        for line in assignment_lines:
            print(f"  {line}", flush=True)
    else:
        print("  이번 주 과제 없음", flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
