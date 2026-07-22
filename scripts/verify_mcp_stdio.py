"""MCP stdio 서버를 실제로 띄워 E-Class 읽기 Tool을 검증한다.

강좌명·과제명·첨부파일명 같은 학생 정보는 터미널에 출력하지 않고 성공 여부와
건수만 표시한다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # `python scripts/verify_mcp_stdio.py`로 실행해도 프로젝트 패키지를 찾게 한다.
    sys.path.insert(0, str(PROJECT_ROOT))

EXPECTED_TOOLS = {
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


def structured(result: Any) -> dict[str, Any]:
    """SDK 버전과 무관하게 구조화 MCP 결과를 dict로 읽는다."""

    value = result.structuredContent
    if not isinstance(value, dict):
        raise RuntimeError("MCP Tool이 구조화 결과를 반환하지 않았습니다.")
    return value


def require_ok(name: str, payload: dict[str, Any]) -> Any:
    """오류 내부정보를 출력하지 않고 Tool 실패만 알린다."""

    if payload.get("ok") is not True:
        code = (payload.get("error") or {}).get("code", "UNKNOWN")
        raise RuntimeError(f"{name} 검증 실패: {code}")
    return payload.get("data")


async def verify() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=PROJECT_ROOT,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            registered = {tool.name for tool in (await session.list_tools()).tools}
            if registered != EXPECTED_TOOLS:
                missing = sorted(EXPECTED_TOOLS - registered)
                unexpected = sorted(registered - EXPECTED_TOOLS)
                raise RuntimeError(
                    f"MCP Tool 등록 목록이 ROADMAP과 다릅니다. "
                    f"누락={missing}, 추가={unexpected}"
                )
            print(f"Tool 등록: {len(registered)}개 성공", flush=True)

            session_data = require_ok("check_session", structured(await session.call_tool("check_session")))
            if not session_data.get("authenticated"):
                raise RuntimeError("E-Class 세션이 인증되지 않았습니다.")
            print("세션: 인증 성공", flush=True)

            default_result = structured(await session.call_tool("list_courses", {}))
            default_courses = require_ok("list_courses(default)", default_result)
            default_term = default_result.get("selected_term") or {}
            if default_term.get("selection_source") != "eclass_default":
                raise RuntimeError("E-Class 기본 학기가 응답에 보존되지 않았습니다.")
            print(
                f"E-Class 기본 학기: {default_term.get('year')}년 "
                f"{default_term.get('semester_name')}, 강좌 {len(default_courses)}개",
                flush=True,
            )

            dashboard_result = structured(await session.call_tool("get_dashboard_snapshot", {}))
            dashboard = require_ok("get_dashboard_snapshot", dashboard_result)
            dashboard_term = dashboard_result.get("selected_term") or {}
            if dashboard_term.get("selection_source") != "eclass_default":
                raise RuntimeError("Dashboard가 E-Class 기본 학기를 보존하지 않았습니다.")
            counts = {
                name: len(dashboard.get(name) or [])
                for name in ("courses", "announcements", "assignments", "lectures", "grades")
            }
            print(
                "Dashboard Snapshot: "
                + ", ".join(f"{name} {count}개" for name, count in counts.items()),
                flush=True,
            )

            # 방학이어도 실제 콘텐츠가 있는 2026년 1학기를 명시해 파서를 검증한다.
            courses_result = structured(
                await session.call_tool(
                    "list_courses",
                    {"year": 2026, "semester": 1},
                )
            )
            courses = require_ok(
                "list_courses",
                courses_result,
            )
            if not courses:
                raise RuntimeError("2026년 1학기 강좌가 0개입니다.")
            selected_term = courses_result.get("selected_term") or {}
            if any(
                (
                    selected_term.get("year") != 2026,
                    selected_term.get("semester") != 1,
                    selected_term.get("selection_source") != "user_request",
                )
            ):
                raise RuntimeError("사용자 지정 학기가 정확히 적용되지 않았습니다.")
            course_id = courses[0]["id"]
            print(f"강좌: {len(courses)}개 구조화 성공", flush=True)

            announcements = require_ok(
                "list_announcements",
                structured(
                    await session.call_tool(
                        "list_announcements",
                        {"course_id": None, "limit": 5, "year": 2026, "semester": 1},
                    )
                ),
            )
            print(f"공지: {len(announcements)}개 구조화 성공", flush=True)

            assignments = require_ok(
                "list_assignments",
                structured(
                    await session.call_tool(
                        "list_assignments",
                        {
                            "days": None,
                            "only_incomplete": False,
                            "year": 2026,
                            "semester": 1,
                        },
                    )
                ),
            )
            print(f"과제: {len(assignments)}개 구조화 성공", flush=True)
            if assignments:
                assignment_id = assignments[0]["id"]
                require_ok(
                    "get_assignment_details",
                    structured(
                        await session.call_tool(
                            "get_assignment_details",
                            {"assignment_id": assignment_id, "year": 2026, "semester": 1},
                        )
                    ),
                )
                attachments = require_ok(
                    "list_assignment_attachments",
                    structured(
                        await session.call_tool(
                            "list_assignment_attachments",
                            {"assignment_id": assignment_id, "year": 2026, "semester": 1},
                        )
                    ),
                )
                print(f"과제 상세·첨부: 성공(첨부 {len(attachments)}개)", flush=True)

            lectures = require_ok(
                "list_lectures",
                structured(
                    await session.call_tool(
                        "list_lectures",
                        {
                            "course_id": None,
                            "only_unwatched": False,
                            "year": 2026,
                            "semester": 1,
                        },
                    )
                ),
            )
            print(f"강의: {len(lectures)}개 구조화 성공", flush=True)
            if lectures:
                require_ok(
                    "get_lecture_status",
                    structured(
                        await session.call_tool(
                            "get_lecture_status",
                            {"lecture_id": lectures[0]["id"], "year": 2026, "semester": 1},
                        )
                    ),
                )
                print("강의 시청 상태: 구조화 성공", flush=True)

            grades = require_ok(
                "get_grades",
                structured(
                    await session.call_tool(
                        "get_grades",
                        {"course_id": course_id, "year": 2026, "semester": 1},
                    )
                ),
            )
            print(f"성적: 공개 항목 {len(grades)}개 구조화 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
