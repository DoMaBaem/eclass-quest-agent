"""E-Class Agent의 실행 동안 로컬 MCP stdio 서버를 연결하는 Runtime handler."""

from __future__ import annotations

import json
import os
import re
import sys
import asyncio
from pathlib import Path
from uuid import UUID

from agents import RunHooks, Runner, set_default_openai_key
from agents.mcp import MCPServerStdio

from app.agent.eclass_agent import build_eclass_agent
from app.agent.errors import OpenAiApiKeyRequiredError
from app.agent.run_config import privacy_safe_run_config
from app.config import Settings
from app.schemas.domain import Attachment
from app.schemas.manager import (
    ManagerAction,
    ManagerEntityKind,
    ManagerTask,
    SpecialistResult,
    SpecialistStatus,
    VerifiedAttachmentTarget,
)
from app.schemas.workflow import CapabilityCode, ErrorCode
from mcp_server.schemas import (
    AnnouncementDetailsResult,
    AnnouncementListResult,
    AttachmentListResult,
    AssignmentDetailsResult,
    AssignmentListResult,
    CourseAnnouncementResult,
    CourseAssignmentResult,
    CourseLectureResult,
    CourseListResult,
    CourseResolutionResult,
    DownloadResult,
    GradeListResult,
    LectureListResult,
    LectureResolutionResult,
    LectureStatusResult,
    McpErrorCode,
    McpOutcomeStatus,
    PlaybackResult,
    VerifiedPlaybackResult,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# MCP SDK는 보안을 위해 stdio subprocess에 HOME/PATH 등 일부 환경변수만 기본 상속한다.
# headed Chromium에 필요한 GUI 변수는 그 기본 목록에서 제외되므로, 비밀값 전체를 넘기지 않고
# 화면·오디오 연결에 필요한 이름만 명시적으로 허용한다.
MCP_GUI_ENV_KEYS = (
    # Docker 이미지에 설치된 Playwright Chromium 위치다. MCP SDK에 ``env``를 명시하면
    # 이 값은 자동 상속되지 않으므로 누락 시 Agent가 띄운 MCP에서만 브라우저 실행이 실패한다.
    "PLAYWRIGHT_BROWSERS_PATH",
    # Linux/WSL GUI·오디오
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XAUTHORITY",
    "PULSE_SERVER",
    # LinuxServer Webtop은 PULSE_SERVER 대신 이 경로의 native 소켓을 사용한다.
    # 누락하면 headed Chromium 영상은 재생돼도 PulseAudio 출력 스트림이 생기지 않는다.
    "PULSE_RUNTIME_PATH",
    "DBUS_SESSION_BUS_ADDRESS",
    # Windows에서 Chromium subprocess가 사용자 프로필과 시스템 실행 파일을 찾는 데 필요하다.
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)


# Manager가 확정한 ``entity/action``마다 E-Class Agent에 노출할 수 있는 MCP Tool을
# 명시한다. 한 요청에서 불필요한 도메인의 Tool을 감추면 모델이 공지 요청 중 과제를
# 조회하거나, 목록 요청 중 영상 제어를 실행하는 등의 범위 이탈을 구조적으로 막을 수 있다.
# ``check_session``은 어느 업무에서든 인증 문제를 설명할 수 있는 공통 읽기 Tool이다.
_COMMON_AGENT_TOOLS = frozenset({"check_session"})
_OPERATION_TOOL_ALLOWLIST: dict[
    tuple[ManagerEntityKind, ManagerAction], frozenset[str]
] = {
    (ManagerEntityKind.COURSE, ManagerAction.LIST): frozenset({"list_courses"}),
    (ManagerEntityKind.COURSE, ManagerAction.DETAIL): frozenset(
        {"list_courses", "resolve_course"}
    ),
    (ManagerEntityKind.ANNOUNCEMENT, ManagerAction.LIST): frozenset(
        {"list_announcements", "list_course_announcements"}
    ),
    (ManagerEntityKind.ANNOUNCEMENT, ManagerAction.DETAIL): frozenset(
        {
            "list_announcements",
            "list_course_announcements",
            "get_announcement_details",
        }
    ),
    (ManagerEntityKind.ASSIGNMENT, ManagerAction.LIST): frozenset(
        {"list_assignments", "list_course_assignments"}
    ),
    (ManagerEntityKind.ASSIGNMENT, ManagerAction.DETAIL): frozenset(
        {
            "list_assignments",
            "list_course_assignments",
            "get_assignment_details",
        }
    ),
    (ManagerEntityKind.ATTACHMENT, ManagerAction.LIST): frozenset(
        {"list_assignment_attachments"}
    ),
    (ManagerEntityKind.ATTACHMENT, ManagerAction.DETAIL): frozenset(
        {"list_assignment_attachments"}
    ),
    (ManagerEntityKind.ATTACHMENT, ManagerAction.DOWNLOAD): frozenset(
        # 원시 URL·ID를 받는 download_attachment는 Agent에 노출하지 않는다. Runtime이
        # typed Snapshot에서 단 하나를 검증한 뒤 전용 경로에서만 직접 호출한다.
        {"list_assignment_attachments"}
    ),
    (ManagerEntityKind.LECTURE, ManagerAction.LIST): frozenset(
        {"list_lectures", "list_course_lectures"}
    ),
    (ManagerEntityKind.LECTURE, ManagerAction.DETAIL): frozenset(
        {"list_lectures", "list_course_lectures", "get_lecture_status"}
    ),
    (ManagerEntityKind.GRADE, ManagerAction.LIST): frozenset({"get_grades"}),
    (ManagerEntityKind.GRADE, ManagerAction.DETAIL): frozenset({"get_grades"}),
    (ManagerEntityKind.LECTURE, ManagerAction.PLAY): frozenset(
        {"resolve_lecture", "play_resolved_lecture"}
    ),
    (ManagerEntityKind.LECTURE, ManagerAction.PREVIEW): frozenset(
        {"resolve_lecture", "preview_resolved_lecture"}
    ),
    (ManagerEntityKind.LECTURE, ManagerAction.STOP): frozenset({"stop_lecture"}),
}

# 이 두 호환 Tool은 서버 API에 남아 있어도 Agent에게는 절대 노출하지 않는다. 재생은
# 반드시 resolve_lecture가 발급한 불투명 reference_id를 받는 안전 Tool로만 실행한다.
_RAW_PLAYBACK_TOOLS = frozenset({"play_lecture", "preview_lecture"})


def _tool_allowlist_for_task(task: ManagerTask) -> frozenset[str]:
    """typed 업무 계약을 Agent가 사용할 수 있는 정확한 Tool 집합으로 변환한다."""

    return _COMMON_AGENT_TOOLS | _OPERATION_TOOL_ALLOWLIST.get(
        (task.entity, task.action),
        frozenset(),
    )


def _semantic_outcome_contract(
    status: McpOutcomeStatus,
) -> tuple[SpecialistStatus, ErrorCode | None]:
    """고수준 MCP 상태를 모델 판단 없이 Runtime 공개 상태로 변환한다."""

    if status in {
        McpOutcomeStatus.FOUND,
        McpOutcomeStatus.NOT_FOUND,
        McpOutcomeStatus.AMBIGUOUS,
    }:
        return SpecialistStatus.COMPLETED, None
    if status is McpOutcomeStatus.AUTH_REQUIRED:
        return SpecialistStatus.AUTH_REQUIRED, ErrorCode.AUTH_REQUIRED
    if status is McpOutcomeStatus.INVALID_REQUEST:
        return SpecialistStatus.FAILED, ErrorCode.INVALID_REQUEST
    return SpecialistStatus.FAILED, ErrorCode.TEMPORARY_FAILURE


def _mcp_gui_environment() -> dict[str, str]:
    """현재 터미널의 GUI 연결 정보 중 headed Playwright에 필요한 값만 반환한다."""

    return {
        key: value
        for key in MCP_GUI_ENV_KEYS
        if (value := os.environ.get(key))
    }


class _VerifiedMcpOutputCapture(RunHooks):
    """LLM이 의역하면 안 되는 검증된 MCP 상세 결과를 실행 중 별도로 보존한다."""

    def __init__(self) -> None:
        self.announcement_details: AnnouncementDetailsResult | None = None
        self.announcement_list: AnnouncementListResult | None = None
        self.assignment_list: AssignmentListResult | None = None
        self.assignment_details: AssignmentDetailsResult | None = None
        self.attachment_list: AttachmentListResult | None = None
        self.course_list: CourseListResult | None = None
        self.course_resolution: CourseResolutionResult | None = None
        self.lecture_list: LectureListResult | None = None
        self.lecture_status: LectureStatusResult | None = None
        self.grade_list: GradeListResult | None = None
        self.course_announcement_result: CourseAnnouncementResult | None = None
        self.course_assignment_result: CourseAssignmentResult | None = None
        self.course_lecture_result: CourseLectureResult | None = None
        self.lecture_resolution_result: LectureResolutionResult | None = None
        self.verified_playback_result: VerifiedPlaybackResult | None = None
        self.verified_playback_tool: str | None = None
        self.playback_result: PlaybackResult | None = None
        self.playback_tool: str | None = None
        self.downloads: list[DownloadResult] = []
        self.download_result: DownloadResult | None = None
        self.successful_tools: list[str] = []
        self.evidence_refs: list[str] = []
        self.tool_events: list[tuple[str, str]] = []
        # 사용자 문장 키워드가 아니라 Agent가 실제로 마지막에 사용한 데이터 Tool을 기준으로
        # 최종 표시 대상을 결정한다. "강의 목록"처럼 표현이 달라도 list_courses면 강좌 목록이다.
        self.last_data_tool: str | None = None

    async def on_tool_end(self, context, agent, tool, result: object) -> None:  # type: ignore[override]
        del context, agent
        try:
            raw_payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(raw_payload, dict) and "result" in raw_payload:
                raw_payload = raw_payload["result"]
        except (json.JSONDecodeError, TypeError):
            raw_payload = None
        completed_outcomes = {"FOUND", "NOT_FOUND", "AMBIGUOUS"}
        if isinstance(raw_payload, dict) and (
            raw_payload.get("ok") is True
            or raw_payload.get("status") in completed_outcomes
        ):
            self.tool_events.append((tool.name, "COMPLETED"))
            self.successful_tools.append(tool.name)
            entity_type = {
                "list_courses": "course",
                "list_announcements": "announcement",
                "list_course_announcements": "announcement",
                "get_announcement_details": "announcement",
                "list_assignments": "assignment",
                "list_course_assignments": "assignment",
                "get_assignment_details": "assignment",
                "list_assignment_attachments": "attachment",
                "list_lectures": "lecture",
                "list_course_lectures": "lecture",
                "resolve_lecture": "lecture",
                "get_lecture_status": "lecture",
                "get_grades": "grade",
                "play_lecture": "playback",
                "stop_lecture": "playback",
                "preview_lecture": "playback",
                "play_resolved_lecture": "playback",
                "preview_resolved_lecture": "playback",
            }.get(tool.name)
            data = raw_payload.get("data")
            items = data if isinstance(data, list) else [data]
            if entity_type:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_id = item.get("id") or item.get("playback_id")
                    if item_id is not None:
                        self.evidence_refs.append(f"{entity_type}:{item_id}")
        elif tool.name:
            self.tool_events.append((tool.name, "FAILED"))
        if tool.name in {
            "list_courses",
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
            "download_attachment",
            "play_lecture",
            "stop_lecture",
            "preview_lecture",
            "play_resolved_lecture",
            "preview_resolved_lecture",
        }:
            self.last_data_tool = tool.name
        if tool.name == "download_attachment":
            try:
                parsed_download = DownloadResult.model_validate(raw_payload)
                self.download_result = parsed_download
                if parsed_download.ok and parsed_download.data is not None:
                    self.downloads.append(parsed_download)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            return
        if tool.name in {"play_resolved_lecture", "preview_resolved_lecture"}:
            self.verified_playback_tool = tool.name
            try:
                self.verified_playback_result = VerifiedPlaybackResult.model_validate(raw_payload)
                target = self.verified_playback_result.target
                playback = self.verified_playback_result.data
                if target is not None:
                    self.evidence_refs.append(f"lecture:{target.lecture_id}")
                if playback is not None:
                    self.evidence_refs.append(f"playback:{playback.playback_id}")
            except (TypeError, ValueError):
                self.verified_playback_result = None
            return
        if tool.name in {"play_lecture", "stop_lecture", "preview_lecture"}:
            self.playback_tool = tool.name
            try:
                self.playback_result = PlaybackResult.model_validate(raw_payload)
            except (TypeError, ValueError):
                self.playback_result = None
            return
        if tool.name not in {
            "list_courses",
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
        }:
            return
        try:
            payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(payload, dict) and "result" in payload:
                payload = payload["result"]
            if tool.name == "list_courses":
                parsed_courses = CourseListResult.model_validate(payload)
                self.course_list = parsed_courses
                return
            if tool.name == "resolve_course":
                parsed_resolution = CourseResolutionResult.model_validate(payload)
                self.course_resolution = parsed_resolution
                if parsed_resolution.ok and parsed_resolution.data is not None:
                    course = parsed_resolution.data.course
                    if course is not None:
                        self.evidence_refs.append(f"course:{course.id}")
                return
            if tool.name == "list_announcements":
                parsed_list = AnnouncementListResult.model_validate(payload)
                self.announcement_list = parsed_list
                return
            if tool.name == "list_course_announcements":
                semantic_announcements = CourseAnnouncementResult.model_validate(payload)
                self.course_announcement_result = semantic_announcements
                if semantic_announcements.ok and semantic_announcements.data is not None:
                    self.evidence_refs.append(
                        f"course:{semantic_announcements.data.course.course_id}"
                    )
                    self.evidence_refs.extend(
                        f"announcement:{item.id}"
                        for item in semantic_announcements.data.announcements
                    )
                return
            if tool.name == "list_assignments":
                parsed_assignments = AssignmentListResult.model_validate(payload)
                self.assignment_list = parsed_assignments
                return
            if tool.name == "list_course_assignments":
                semantic_assignments = CourseAssignmentResult.model_validate(payload)
                self.course_assignment_result = semantic_assignments
                if semantic_assignments.ok and semantic_assignments.data is not None:
                    self.evidence_refs.append(
                        f"course:{semantic_assignments.data.course.course_id}"
                    )
                    self.evidence_refs.extend(
                        f"assignment:{item.id}"
                        for item in semantic_assignments.data.assignments
                    )
                return
            if tool.name == "get_assignment_details":
                parsed_assignment = AssignmentDetailsResult.model_validate(payload)
                self.assignment_details = parsed_assignment
                return
            if tool.name == "list_assignment_attachments":
                parsed_attachments = AttachmentListResult.model_validate(payload)
                self.attachment_list = parsed_attachments
                return
            if tool.name == "list_lectures":
                parsed_lectures = LectureListResult.model_validate(payload)
                self.lecture_list = parsed_lectures
                return
            if tool.name == "list_course_lectures":
                semantic_lectures = CourseLectureResult.model_validate(payload)
                self.course_lecture_result = semantic_lectures
                if semantic_lectures.ok and semantic_lectures.data is not None:
                    self.evidence_refs.append(
                        f"course:{semantic_lectures.data.course.course_id}"
                    )
                    self.evidence_refs.extend(
                        f"lecture:{item.id}"
                        for item in semantic_lectures.data.lectures
                    )
                return
            if tool.name == "resolve_lecture":
                resolution = LectureResolutionResult.model_validate(payload)
                self.lecture_resolution_result = resolution
                if resolution.ok and resolution.data is not None:
                    self.evidence_refs.extend(
                        [
                            f"course:{resolution.data.course_id}",
                            f"lecture:{resolution.data.lecture_id}",
                        ]
                    )
                return
            if tool.name == "get_lecture_status":
                self.lecture_status = LectureStatusResult.model_validate(payload)
                return
            if tool.name == "get_grades":
                self.grade_list = GradeListResult.model_validate(payload)
                return
            parsed = AnnouncementDetailsResult.model_validate(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        self.announcement_details = parsed


def _announcement_display_text(result: AnnouncementDetailsResult) -> str:
    """공지 본문을 모델 재작성 없이 사용자에게 보여 줄 텍스트로 만든다."""

    assert result.data is not None
    details = result.data
    metadata = [f"[{details.title}]"]
    if details.author:
        metadata.append(f"작성자: {details.author}")
    if details.posted_at:
        metadata.append(f"작성일: {details.posted_at.strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(metadata) + f"\n\n{details.content}\n\n출처: {details.url}"


def _announcement_list_display_text(result: AnnouncementListResult) -> str:
    """모델의 제목 재작성 없이 검증된 공지 목록을 번호와 함께 표시한다."""

    if not result.data:
        return "조회된 공지사항이 없습니다."
    term = result.selected_term
    heading = (
        f"{term.year}년 {term.semester_name} 공지사항 {len(result.data)}건"
        if term is not None
        else f"공지사항 {len(result.data)}건"
    )
    rows = [heading]
    for index, item in enumerate(result.data, start=1):
        posted = item.posted_at.strftime("%Y-%m-%d %H:%M") if item.posted_at else "날짜 없음"
        rows.append(f"{index}. [{posted}] {item.title}\n   {item.url}")
    return "\n".join(rows)


def _announcement_followup_context(result: AnnouncementListResult) -> str:
    """Manager가 번호·대명사를 정확한 공지 URL로 복원할 수 있는 JSON 문맥을 만든다."""

    payload = {
        "kind": "verified_announcement_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": item.id,
                "course_id": item.course_id,
                "title": item.title,
                "url": item.url,
                "posted_at": item.posted_at.isoformat() if item.posted_at else None,
            }
            for index, item in enumerate(result.data, start=1)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 제목이나 URL이 비정상적으로 길어도 JSON 앞부분을 잘라 문법을 깨뜨리지 않는다.
    while len(encoded) > 12_000 and payload["items"]:
        payload["items"].pop()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _course_announcement_display_text(result: CourseAnnouncementResult) -> str:
    """업무 단위 Tool의 강좌·공지 묶음을 재작성 없이 표시한다."""

    assert result.data is not None
    data = result.data
    if not data.announcements:
        return f"{data.course.course_name}: 조회된 공지사항이 없습니다."
    term = result.selected_term
    heading = (
        f"{term.year}년 {term.semester_name} {data.course.course_name} 공지사항 "
        f"{len(data.announcements)}건"
        if term is not None
        else f"{data.course.course_name} 공지사항 {len(data.announcements)}건"
    )
    rows = [heading]
    for index, item in enumerate(data.announcements, start=1):
        posted = item.posted_at.strftime("%Y-%m-%d %H:%M") if item.posted_at else "날짜 없음"
        rows.append(f"{index}. [{posted}] {item.title}\n   {item.url}")
    return "\n".join(rows)


def _course_announcement_followup_context(result: CourseAnnouncementResult) -> str:
    assert result.data is not None
    payload = {
        "kind": "verified_announcement_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": item.id,
                "course_id": result.data.course.course_id,
                "course_name": result.data.course.course_name,
                "title": item.title,
                "url": item.url,
                "posted_at": item.posted_at.isoformat() if item.posted_at else None,
            }
            for index, item in enumerate(result.data.announcements, start=1)
        ],
    }
    return _bounded_followup_json(payload)


def _assignment_list_display_text(
    result: AssignmentListResult,
    courses: CourseListResult | None = None,
) -> str:
    """과제 주차·제목·마감·제출 상태를 MCP 원문 그대로 표시한다."""

    if not result.data:
        return "조회된 과제가 없습니다."
    course_names = {
        course.id: _split_eclass_course_name(course.name)[0]
        for course in courses.data
    } if courses is not None else {}
    term = result.selected_term
    heading = (
        f"{term.year}년 {term.semester_name} 과제 {len(result.data)}건"
        if term is not None
        else f"과제 {len(result.data)}건"
    )
    rows = [heading]
    for index, assignment in enumerate(result.data, start=1):
        course_name = course_names.get(
            assignment.course_id,
            assignment.course_name or f"강좌 ID {assignment.course_id}",
        )
        week = f"{assignment.week}주차" if assignment.week is not None else "주차 확인 불가"
        due = assignment.due_at.strftime("%Y-%m-%d %H:%M") if assignment.due_at else "마감 없음"
        submission = (
            "제출 완료"
            if assignment.submitted is True
            else "미제출"
            if assignment.submitted is False
            else "제출 상태 확인 불가"
        )
        rows.append(
            f"{index}. [{course_name} · {week}] {assignment.title}\n"
            f"   마감: {due} · {submission}"
        )
    return "\n".join(rows)


def _assignment_followup_context(result: AssignmentListResult) -> str:
    """다음 요청의 `첫 번째/1번/그 과제`를 실제 과제 ID와 연결할 검증 JSON을 만든다."""

    payload = {
        "kind": "verified_assignment_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": assignment.id,
                "course_id": assignment.course_id,
                "course_name": assignment.course_name,
                "title": assignment.title,
                "url": assignment.url,
            }
            for index, assignment in enumerate(result.data, start=1)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encoded) > 12_000 and payload["items"]:
        payload["items"].pop()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _course_assignment_display_text(result: CourseAssignmentResult) -> str:
    assert result.data is not None
    data = result.data
    if not data.assignments:
        return f"{data.course.course_name}: 조회된 과제가 없습니다."
    term = result.selected_term
    heading = (
        f"{term.year}년 {term.semester_name} {data.course.course_name} 과제 {len(data.assignments)}건"
        if term is not None
        else f"{data.course.course_name} 과제 {len(data.assignments)}건"
    )
    rows = [heading]
    for index, assignment in enumerate(data.assignments, start=1):
        week = f"{assignment.week}주차" if assignment.week is not None else "주차 확인 불가"
        due = assignment.due_at.strftime("%Y-%m-%d %H:%M") if assignment.due_at else "마감 없음"
        submission = (
            "제출 완료"
            if assignment.submitted is True
            else "미제출"
            if assignment.submitted is False
            else "제출 상태 확인 불가"
        )
        rows.append(f"{index}. [{week}] {assignment.title}\n   마감: {due} · {submission}")
    return "\n".join(rows)


def _course_assignment_followup_context(result: CourseAssignmentResult) -> str:
    assert result.data is not None
    payload = {
        "kind": "verified_assignment_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": item.id,
                "course_id": result.data.course.course_id,
                "course_name": result.data.course.course_name,
                "title": item.title,
                "url": item.url,
            }
            for index, item in enumerate(result.data.assignments, start=1)
        ],
    }
    return _bounded_followup_json(payload)


def _lecture_list_display_text(
    result: LectureListResult,
    courses: CourseListResult | None = None,
) -> str:
    """강의 주차·제목·출석 상태를 MCP 원문 그대로 번호와 함께 표시한다."""

    if not result.data:
        return "조회된 강의 영상이 없습니다."
    course_names = {
        course.id: _split_eclass_course_name(course.name)[0]
        for course in courses.data
    } if courses is not None else {}
    term = result.selected_term
    heading = (
        f"{term.year}년 {term.semester_name} 강의 영상 {len(result.data)}개"
        if term is not None
        else f"강의 영상 {len(result.data)}개"
    )
    rows = [heading]
    for index, lecture in enumerate(result.data, start=1):
        course_name = course_names.get(lecture.course_id, f"강좌 ID {lecture.course_id}")
        week = f"{lecture.week}주차" if lecture.week is not None else "주차 확인 불가"
        status = (
            "수강 완료"
            if lecture.attendance_status.value == "COMPLETE"
            else "미수강"
            if lecture.attendance_status.value == "INCOMPLETE"
            else "출석 상태 확인 불가"
        )
        progress = (
            f" · 진도 {lecture.progress_percent:g}%"
            if lecture.progress_percent is not None
            else ""
        )
        rows.append(
            f"{index}. [{course_name} · {week}] {lecture.title}\n"
            f"   {status}{progress}"
        )
    return "\n".join(rows)


def _lecture_followup_context(
    result: LectureListResult,
    courses: CourseListResult | None = None,
) -> str:
    """후속 재생 요청을 실제 lecture_id로 연결할 검증 JSON을 만든다."""

    course_names = {
        course.id: course.name for course in courses.data
    } if courses is not None else {}
    payload = {
        "kind": "verified_lecture_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": lecture.id,
                "course_id": lecture.course_id,
                "course_name": course_names.get(lecture.course_id),
                "title": lecture.title,
                "url": lecture.url,
                "week": lecture.week,
            }
            for index, lecture in enumerate(result.data, start=1)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encoded) > 12_000 and payload["items"]:
        payload["items"].pop()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _course_lecture_display_text(result: CourseLectureResult) -> str:
    assert result.data is not None
    data = result.data
    if not data.lectures:
        return f"{data.course.course_name}: 조회된 강의 영상이 없습니다."
    term = result.selected_term
    heading = (
        f"{term.year}년 {term.semester_name} {data.course.course_name} 강의 영상 {len(data.lectures)}개"
        if term is not None
        else f"{data.course.course_name} 강의 영상 {len(data.lectures)}개"
    )
    rows = [heading]
    for index, lecture in enumerate(data.lectures, start=1):
        week = f"{lecture.week}주차" if lecture.week is not None else "주차 확인 불가"
        status = (
            "수강 완료"
            if lecture.attendance_status.value == "COMPLETE"
            else "미수강"
            if lecture.attendance_status.value == "INCOMPLETE"
            else "출석 상태 확인 불가"
        )
        progress = (
            f" · 진도 {lecture.progress_percent:g}%"
            if lecture.progress_percent is not None
            else ""
        )
        rows.append(f"{index}. [{week}] {lecture.title}\n   {status}{progress}")
    return "\n".join(rows)


def _course_lecture_followup_context(result: CourseLectureResult) -> str:
    assert result.data is not None
    payload = {
        "kind": "verified_lecture_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": item.id,
                "course_id": result.data.course.course_id,
                "course_name": result.data.course.course_name,
                "title": item.title,
                "url": item.url,
                "week": item.week,
            }
            for index, item in enumerate(result.data.lectures, start=1)
        ],
    }
    return _bounded_followup_json(payload)


def _bounded_followup_json(payload: dict[str, object]) -> str:
    """후속 후보 JSON을 문법이 깨지지 않는 범위에서 12,000자로 제한한다."""

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    items = payload.get("items")
    if not isinstance(items, list):
        return encoded[:12_000]
    while len(encoded) > 12_000 and items:
        items.pop()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _lecture_resolution_display_text(result: LectureResolutionResult) -> str:
    """검증 참조 발급 결과 또는 실제 복수 후보를 표시한다."""

    if result.data is not None:
        target = result.data
        week = f"{target.week}주차 · " if target.week is not None else ""
        return f"강의 확인: {target.course_name}\n{week}{target.title}"
    if result.candidates:
        rows = ["일치하는 강의 영상이 여러 개입니다."]
        for index, lecture in enumerate(result.candidates, start=1):
            week = f"{lecture.week}주차 · " if lecture.week is not None else ""
            rows.append(
                f"{index}. {week}{lecture.title} · "
                f"강의 ID: {lecture.id} · 강좌 ID: {lecture.course_id}"
            )
        return "\n".join(rows)
    if result.course_candidates:
        rows = ["일치하는 강좌가 여러 개입니다."]
        rows.extend(
            f"{index}. {course.name} · 강좌 ID: {course.id}"
            for index, course in enumerate(result.course_candidates, start=1)
        )
        return "\n".join(rows)
    return result.error.message if result.error is not None else "요청한 강의 영상을 찾지 못했습니다."


def _lecture_resolution_followup_context(
    result: LectureResolutionResult,
) -> str | None:
    """resolve 결과를 다음 번호 선택에 쓸 검증 JSON으로 보존한다.

    복수 후보에는 재생 권한처럼 사용될 수 있는 ``reference_id``를 절대 넣지 않는다.
    단일 FOUND에서만 서버가 발급한 불투명 참조를 포함할 수 있다. ``id``는 기존 Runtime
    계약을 위한 호환 필드이고 ``lecture_id``가 도메인 의미를 명확히 드러내는 필드다.
    """

    selected_term = (
        result.selected_term.model_dump(mode="json")
        if result.selected_term is not None
        else None
    )
    if result.data is not None:
        target = result.data
        payload: dict[str, object] = {
            "kind": "verified_lecture_candidates",
            "selected_term": selected_term,
            "items": [
                {
                    "number": 1,
                    "id": target.lecture_id,
                    "lecture_id": target.lecture_id,
                    "course_id": target.course_id,
                    "course_name": target.course_name,
                    "title": target.title,
                    "week": target.week,
                    "reference_id": target.reference_id,
                }
            ],
        }
        return _bounded_followup_json(payload)
    if not result.candidates:
        return None
    payload = {
        "kind": "verified_lecture_candidates",
        "selected_term": selected_term,
        "items": [
            {
                "number": index,
                "id": lecture.id,
                "lecture_id": lecture.id,
                "course_id": lecture.course_id,
                "title": lecture.title,
                "url": lecture.url,
                "week": lecture.week,
            }
            for index, lecture in enumerate(result.candidates, start=1)
        ],
    }
    return _bounded_followup_json(payload)


def _semantic_course_outcome_display_text(
    result: CourseAnnouncementResult | CourseAssignmentResult | CourseLectureResult,
    subject: str,
) -> str:
    """강좌 해석이 단일 결과가 아닐 때 실제 후보 또는 검증 오류를 표시한다."""

    if result.candidates:
        rows = [f"'{subject}' 요청과 일치하는 강좌가 여러 개입니다."]
        rows.extend(
            f"{index}. {course.name} · 강좌 ID: {course.id}"
            for index, course in enumerate(result.candidates, start=1)
        )
        return "\n".join(rows)
    return result.error.message if result.error is not None else f"{subject}을 찾지 못했습니다."


def _verified_playback_display_text(
    result: VerifiedPlaybackResult,
    *,
    preview: bool = False,
) -> str:
    """검증된 참조로 시작한 player 상태를 Tool 원문 값만 사용해 표시한다."""

    assert result.data is not None and result.target is not None
    playback = result.data
    target = result.target
    action = {
        "PLAYING": (
            "강의 영상 미리보기를 시작했습니다."
            if preview
            else "강의 영상 재생을 시작했습니다."
        ),
        "STOPPED": "강의 영상 재생을 중지했습니다.",
        "TIMED_OUT": "강의 영상 재생 시간이 끝나 자동으로 중지했습니다.",
    }.get(playback.status, "강의 영상 상태를 확인했습니다.")
    window_text = (
        f"{playback.window_width}x{playback.window_height}"
        if playback.window_width is not None and playback.window_height is not None
        else "E-Class 기본 크기"
    )
    week = f"{target.week}주차 · " if target.week is not None else ""
    return (
        f"{action}\n"
        f"대상: {target.course_name} · {week}{target.title}\n"
        f"설정: 볼륨 {playback.volume_percent}% · {playback.playback_rate:g}배속 · {window_text}\n"
        f"재생 ID: {playback.playback_id}"
    )


def _semantic_failure_text(
    result: LectureResolutionResult | VerifiedPlaybackResult,
    fallback: str,
) -> str:
    """고수준 MCP 실패·후보 결과에서 검증된 사용자 표시 문구를 고른다."""

    if isinstance(result, LectureResolutionResult):
        return _lecture_resolution_display_text(result)
    return result.error.message if result.error is not None else fallback


def _verified_playback_specialist_result(
    result: VerifiedPlaybackResult,
    *,
    tool_name: str,
    evidence_refs: list[str],
) -> SpecialistResult:
    """safe playback의 typed status와 data만으로 최종 결과를 확정한다."""

    status, error_code = _semantic_outcome_contract(result.status)
    preview = tool_name == "preview_resolved_lecture"
    if result.status is McpOutcomeStatus.FOUND:
        if not result.ok or result.data is None or result.target is None:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="검증 참조 영상 Tool이 완전한 재생 결과를 반환하지 않았습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        display = _verified_playback_display_text(result, preview=preview)
        refs = [
            *evidence_refs,
            f"lecture:{result.target.lecture_id}",
            f"playback:{result.data.playback_id}",
        ]
        return SpecialistResult(
            status=SpecialistStatus.COMPLETED,
            summary=display,
            evidence_refs=list(dict.fromkeys(refs)),
            verified_display_text=display,
            verified_followup_context=display,
        )

    display = _semantic_failure_text(result, "영상 재생 요청을 완료하지 못했습니다.")
    return SpecialistResult(
        status=status,
        summary=display,
        error_code=error_code,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        # NOT_FOUND/AMBIGUOUS도 오류 예외가 아니라 검증된 업무 결과이므로 그대로 표시한다.
        verified_display_text=(display if status is SpecialistStatus.COMPLETED else None),
    )


def _raw_playback_specialist_result(
    result: PlaybackResult,
    *,
    evidence_refs: list[str],
) -> SpecialistResult:
    """stop_lecture 결과를 모델의 마지막 문장과 무관하게 확정한다."""

    if not result.ok or result.data is None:
        error = result.error
        auth_required = error is not None and error.code is McpErrorCode.AUTH_REQUIRED
        return SpecialistResult(
            status=(
                SpecialistStatus.AUTH_REQUIRED
                if auth_required
                else SpecialistStatus.FAILED
            ),
            summary=error.message if error is not None else "영상 제어에 실패했습니다.",
            error_code=(
                ErrorCode.AUTH_REQUIRED if auth_required else ErrorCode.TEMPORARY_FAILURE
            ),
        )
    action = {
        "PLAYING": "강의 영상 재생을 시작했습니다.",
        "STOPPED": "강의 영상 재생을 중지했습니다.",
        "TIMED_OUT": "강의 영상 재생 시간이 끝나 자동으로 중지했습니다.",
    }.get(result.data.status, "강의 영상 상태를 확인했습니다.")
    window_text = (
        f"{result.data.window_width}x{result.data.window_height}"
        if result.data.window_width is not None and result.data.window_height is not None
        else "E-Class 기본 크기"
    )
    display = (
        f"{action}\n"
        f"설정: 볼륨 {result.data.volume_percent}% · "
        f"{result.data.playback_rate:g}배속 · {window_text}\n"
        f"재생 ID: {result.data.playback_id}"
    )
    return SpecialistResult(
        status=SpecialistStatus.COMPLETED,
        summary=display,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        verified_display_text=display,
        verified_followup_context=display,
    )


def _assignment_details_display_text(
    result: AssignmentDetailsResult,
    courses: CourseListResult | None = None,
) -> str:
    """선택된 과제 한 건만 모델의 확대·의역 없이 표시한다."""

    assert result.data is not None
    assignment = result.data
    course_names = {
        course.id: _split_eclass_course_name(course.name)[0]
        for course in courses.data
    } if courses is not None else {}
    course_name = course_names.get(
        assignment.course_id,
        assignment.course_name or f"강좌 ID {assignment.course_id}",
    )
    week = f"{assignment.week}주차" if assignment.week is not None else "주차 확인 불가"
    due = assignment.due_at.strftime("%Y-%m-%d %H:%M") if assignment.due_at else "마감 없음"
    submission = (
        "제출 완료"
        if assignment.submitted is True
        else "미제출"
        if assignment.submitted is False
        else "제출 상태 확인 불가"
    )
    description = (
        f"\n\n과제 설명\n{assignment.description}"
        if assignment.description
        else "\n\n과제 설명: 등록된 본문 없음"
    )
    return (
        f"[{course_name} · {week}] {assignment.title}\n"
        f"마감: {due} · {submission}\n"
        f"출처: {assignment.url}"
        f"{description}"
    )


def _attachment_list_display_text(result: AttachmentListResult) -> str:
    """과제 첨부파일 이름과 형식을 MCP 원문 그대로 표시한다."""

    if not result.data:
        return "조회된 과제 첨부파일이 없습니다."
    rows = [f"과제 첨부파일 {len(result.data)}개"]
    for index, attachment in enumerate(result.data, start=1):
        file_type = attachment.mime_type or "파일 형식 확인 불가"
        rows.append(f"{index}. {attachment.name}\n   형식: {file_type}")
    return "\n".join(rows)


def _attachment_followup_context(result: AttachmentListResult) -> str:
    """다음 요청의 `PDF`, 파일명, 번호를 실제 첨부 URL과 연결할 검증 JSON을 만든다."""

    payload = {
        "kind": "verified_attachment_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": attachment.id,
                "parent_id": attachment.parent_id,
                "name": attachment.name,
                "url": attachment.url,
                "mime_type": attachment.mime_type,
            }
            for index, attachment in enumerate(result.data, start=1)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encoded) > 12_000 and payload["items"]:
        payload["items"].pop()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _select_attachments_for_download(
    task: ManagerTask,
    result: AttachmentListResult,
) -> tuple[list[Attachment], str | None]:
    """현재 Tool이 검증한 목록에서 다운로드 범위를 결정적으로 고른다.

    번호와 실제 파일명은 엄격한 선택자로 취급한다. 선택자가 없는 복수형 요청은 같은 과제의
    첨부를 최대 5개까지 모두 처리한다. Manager의 일반 설명어가 ``query``에 들어간 경우에는
    파일명으로 오해하지 않지만, ``missing.pdf``처럼 확장자가 있는 명시 파일명은 완화하지 않는다.
    """

    attachments = list(result.data)
    if not attachments:
        return [], "선택한 과제에는 조회 가능한 첨부파일이 없습니다."

    selected: list[Attachment]
    if task.slots.ordinal is not None:
        index = task.slots.ordinal - 1
        if index < 0 or index >= len(attachments):
            return [], "요청한 번호의 첨부파일을 찾을 수 없습니다."
        selected = [attachments[index]]
    elif task.slots.query:
        normalized_query = re.sub(r"[^0-9a-z가-힣]+", "", task.slots.query.casefold())
        matched = [
            attachment
            for attachment in attachments
            if normalized_query
            and (
                normalized_query
                in re.sub(r"[^0-9a-z가-힣]+", "", attachment.name.casefold())
                or re.sub(r"[^0-9a-z가-힣]+", "", attachment.name.casefold())
                in normalized_query
            )
        ]
        if matched:
            selected = matched
        elif re.search(r"\.[A-Za-z0-9]{1,10}(?:\s|$)", task.slots.query):
            return [], f"'{task.slots.query}'와 일치하는 첨부파일을 찾을 수 없습니다."
        else:
            # 과제 제목이나 "첨부파일" 같은 일반어가 query에 들어온 경우 실제 파일 선택자가
            # 아니므로 현재 과제의 검증된 목록 전체를 사용한다.
            selected = attachments
    else:
        selected = attachments

    if len(selected) > 5:
        return [], "첨부파일이 5개를 초과합니다. 번호나 파일명을 지정해 주세요."
    if len({attachment.parent_id for attachment in selected}) != 1:
        return [], "서로 다른 과제의 첨부파일이 섞여 있어 다운로드하지 않았습니다."
    if len({attachment.id for attachment in selected}) != len(selected):
        return [], "중복된 첨부파일 식별자가 있어 다운로드하지 않았습니다."
    return selected, None


def _split_eclass_course_name(raw_name: str) -> tuple[str, list[str]]:
    """E-Class가 강좌명 뒤에 붙인 ``[A,B,N]`` 그룹 코드를 표시용으로 분리한다."""

    matched = re.fullmatch(r"(.+?)\[([^\[\]]+)]\s*", raw_name)
    if matched is None:
        return raw_name, []
    group_codes = [code.strip() for code in matched.group(2).split(",") if code.strip()]
    return matched.group(1).strip(), group_codes


def _course_list_display_text(result: CourseListResult) -> str:
    """강좌명과 교수명을 MCP 원문 그대로 사용하되 그룹 코드는 오해 없게 분리한다."""

    if not result.data:
        return "조회된 수강 강좌가 없습니다."
    term = result.selected_term
    heading = (
        f"{term.year}년 {term.semester_name} 수강 강좌 {len(result.data)}개"
        if term is not None
        else f"수강 강좌 {len(result.data)}개"
    )
    rows = [heading]
    for index, course in enumerate(result.data, start=1):
        name, _group_codes = _split_eclass_course_name(course.name)
        professor = course.professor or "담당자 정보 없음"
        # 대괄호 코드는 수강생 개인 분반으로 검증된 값이 아니라 E-Class 통합 그룹 표식이다.
        # 사용자가 이를 "9분반"으로 오해하지 않도록 목록 화면에서는 숨기고 원문 문맥에만 보존한다.
        rows.append(f"{index}. {name}\n   담당자: {professor} · 강좌 ID: {course.id}")
    return "\n".join(rows)


def _course_resolution_display_text(result: CourseResolutionResult) -> str:
    """강좌명 해석 결과를 모델의 재작성 없이 표시한다."""

    assert result.data is not None
    resolution = result.data
    if resolution.status == "MATCHED" and resolution.course is not None:
        name, _groups = _split_eclass_course_name(resolution.course.name)
        return f"강좌 확인: {name}\n강좌 ID: {resolution.course.id}"
    if resolution.status == "AMBIGUOUS":
        rows = ["일치하는 강좌가 여러 개입니다."]
        for index, course in enumerate(resolution.candidates, start=1):
            name, _groups = _split_eclass_course_name(course.name)
            rows.append(f"{index}. {name} · 강좌 ID: {course.id}")
        return "\n".join(rows)
    return f"선택 학기에서 '{resolution.query}'와 일치하는 강좌를 찾지 못했습니다."


def _course_followup_context(result: CourseListResult) -> str:
    """후속 강좌 요청에서 모델이 이름·담당자를 바꾸지 않도록 검증된 JSON을 만든다."""

    payload = {
        "kind": "verified_course_candidates",
        "selected_term": (
            result.selected_term.model_dump(mode="json") if result.selected_term is not None else None
        ),
        "items": [
            {
                "number": index,
                "id": course.id,
                "name": course.name,
                "professor": course.professor,
                "url": course.url,
            }
            for index, course in enumerate(result.data, start=1)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encoded) > 12_000 and payload["items"]:
        payload["items"].pop()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _prefer_verified_assignment_list(
    parsed: SpecialistResult,
    capture: _VerifiedMcpOutputCapture,
) -> SpecialistResult:
    """과제 Tool 성공 여부를 Agent의 자연어 판단보다 우선한다.

    E-Class Agent가 ``list_assignments``에서 정상 데이터를 받은 뒤에도 최종 구조화 응답에서
    강좌 ID를 확인하지 못했다고 잘못 판단할 수 있다. 마지막 데이터 Tool이 과제 목록이고 MCP
    응답이 실제로 성공한 경우에는 캡처한 원본 목록으로 결과를 확정한다. 반대로 목록 Tool 자체가
    실패했거나 이후 상세 Tool이 실패했다면 여기서 성공으로 바꾸지 않는다.
    """

    if capture.last_data_tool != "list_assignments" or capture.assignment_list is None:
        return parsed
    verified_text = _assignment_list_display_text(
        capture.assignment_list,
        capture.course_list,
    )
    return parsed.model_copy(
        update={
            "status": SpecialistStatus.COMPLETED,
            "summary": verified_text,
            "error_code": None,
            "verified_display_text": verified_text,
            "verified_followup_context": verified_text,
        }
    )


def _prefer_verified_lecture_list(
    parsed: SpecialistResult,
    capture: _VerifiedMcpOutputCapture,
) -> SpecialistResult:
    """강의 목록 Tool 성공을 Agent의 잘못된 최종 판단보다 우선한다."""

    if capture.last_data_tool != "list_lectures" or capture.lecture_list is None:
        return parsed
    verified_text = _lecture_list_display_text(capture.lecture_list, capture.course_list)
    return parsed.model_copy(
        update={
            "status": SpecialistStatus.COMPLETED,
            "summary": verified_text,
            "error_code": None,
            "verified_display_text": verified_text,
            "verified_followup_context": _lecture_followup_context(
                capture.lecture_list,
                capture.course_list,
            ),
        }
    )


def _is_preview_task(task: ManagerTask) -> bool:
    """Pydantic 검증을 통과한 typed action만 미리보기 권한으로 인정한다."""

    return task.action is ManagerAction.PREVIEW


def _playback_options(instruction: str) -> dict[str, int | float]:
    """사용자가 명시한 영상 설정을 MCP 옵션 이름으로 결정적으로 추출한다."""

    volume_match = re.search(r"볼륨\s*(\d{1,3})", instruction)
    rate_match = re.search(r"(\d+(?:\.\d+)?)\s*배속", instruction)
    window_match = re.search(r"(\d{3,4})\s*[xX×*]\s*(\d{3,4})", instruction)
    options: dict[str, int | float] = {
        "volume_percent": int(volume_match.group(1)) if volume_match else 100,
        "playback_rate": float(rate_match.group(1)) if rate_match else 1.0,
    }
    if window_match:
        options["window_width"] = int(window_match.group(1))
        options["window_height"] = int(window_match.group(2))
    return options


def _safe_playback_arguments(
    task: ManagerTask,
    reference_id: str,
) -> tuple[str, dict[str, object]]:
    """검증 참조용 일반 재생·미리보기 Tool 이름과 인자를 만든다."""

    options = _playback_options(task.instruction)
    if _is_preview_task(task):
        seconds_match = re.search(r"(\d{1,2})\s*초", task.instruction)
        return (
            "preview_resolved_lecture",
            {
                "reference_id": reference_id,
                "explicit_user_request": True,
                "seconds": int(seconds_match.group(1)) if seconds_match else 20,
                "options": options,
            },
        )
    return (
        "play_resolved_lecture",
        {
            "reference_id": reference_id,
            "explicit_user_request": True,
            **options,
        },
    )


def _mcp_response_failure_result(result: object, fallback: str) -> SpecialistResult:
    """구형 ``ok/error`` MCP 응답도 모델 문장에 맡기지 않고 공개 상태로 변환한다."""

    error = getattr(result, "error", None)
    if error is None:
        return SpecialistResult(
            status=SpecialistStatus.FAILED,
            summary=fallback,
            error_code=ErrorCode.TEMPORARY_FAILURE,
        )
    if error.code is McpErrorCode.AUTH_REQUIRED:
        return SpecialistResult(
            status=SpecialistStatus.AUTH_REQUIRED,
            summary=error.message,
            error_code=ErrorCode.AUTH_REQUIRED,
        )
    if error.code in {McpErrorCode.NOT_FOUND, McpErrorCode.AMBIGUOUS_MATCH}:
        return SpecialistResult(
            status=SpecialistStatus.COMPLETED,
            summary=error.message,
            verified_display_text=error.message,
        )
    return SpecialistResult(
        status=SpecialistStatus.FAILED,
        summary=error.message,
        error_code=(
            ErrorCode.INVALID_REQUEST
            if error.code is McpErrorCode.INVALID_REQUEST
            else ErrorCode.TEMPORARY_FAILURE
        ),
    )


def _semantic_result_for_unfulfilled_detail(
    result: CourseAnnouncementResult | CourseAssignmentResult,
    *,
    label: str,
) -> SpecialistResult | None:
    """상세 조회 전 강좌 해석이 끝난 이유를 결정적으로 반환한다.

    ``FOUND``는 대상 강좌의 *목록*을 읽었다는 뜻일 뿐 상세 본문을 읽었다는 뜻이
    아니므로 완료 결과가 아니다. 반면 NOT_FOUND/AMBIGUOUS와 인증·파서 오류는 그
    자체로 사용자에게 전달할 수 있는 종료 상태다.
    """

    if result.status is McpOutcomeStatus.FOUND:
        return None
    status, error_code = _semantic_outcome_contract(result.status)
    display = _semantic_course_outcome_display_text(result, label)
    context = None
    if result.data is not None:
        if isinstance(result, CourseAnnouncementResult):
            context = _course_announcement_followup_context(result)
        else:
            context = _course_assignment_followup_context(result)
    return SpecialistResult(
        status=status,
        summary=display,
        error_code=error_code,
        verified_display_text=(
            display if status is SpecialistStatus.COMPLETED else None
        ),
        verified_followup_context=context,
    )


def _missing_expected_result(label: str, action: ManagerAction) -> SpecialistResult:
    """중간 조회만 수행한 Agent 응답을 성공으로 승격하지 않는다."""

    action_label = {
        ManagerAction.DETAIL: "상세 내용",
        ManagerAction.DOWNLOAD: "다운로드",
        ManagerAction.LIST: "목록",
    }.get(action, "요청 결과")
    return SpecialistResult(
        status=SpecialistStatus.FAILED,
        summary=(
            f"{label} {action_label}에 필요한 최종 E-Class Tool 결과를 "
            "확인하지 못했습니다. 중간 목록 조회만으로는 완료 처리하지 않습니다."
        ),
        suggested_actions=["대상을 더 구체적으로 지정하거나 목록에서 번호를 선택해 주세요."],
        error_code=ErrorCode.TEMPORARY_FAILURE,
    )


def _lecture_status_display_text(result: LectureStatusResult) -> str:
    """강의 상태 Tool 원본을 모델 의역 없이 짧게 표시한다."""

    assert result.data is not None
    lecture = result.data
    week = f"{lecture.week}주차" if lecture.week is not None else "주차 확인 불가"
    progress = (
        f"{lecture.progress_percent:g}%"
        if lecture.progress_percent is not None
        else "진도율 확인 불가"
    )
    return (
        f"[{week}] {lecture.title}\n"
        f"진도: {progress} · 출석: {lecture.attendance_status.value}\n"
        f"출처: {lecture.url}"
    )


def _grade_list_display_text(result: GradeListResult) -> str:
    """공개된 성적 항목을 검증된 MCP 값 그대로 표시한다."""

    if not result.data:
        return "현재 공개된 성적이 없습니다."
    rows = [f"공개 성적 {len(result.data)}건"]
    for index, grade in enumerate(result.data, start=1):
        score = grade.score if grade.score is not None else "점수 미공개"
        rows.append(f"{index}. {grade.item}: {score}")
    return "\n".join(rows)


def _expected_operation_guard(
    task: ManagerTask,
    capture: _VerifiedMcpOutputCapture,
) -> SpecialistResult | None:
    """typed entity/action에 해당하는 최종 Tool 결과가 실제로 존재하는지 검사한다.

    반환값이 ``None``이면 기대한 terminal 결과가 캡처된 것이다. ``SpecialistResult``를
    반환하면 그 결과를 즉시 사용해야 하며 Agent가 작성한 COMPLETED 문구는 무시한다.
    """

    if task.capability is CapabilityCode.VIDEO_PLAY:
        return None

    entity, action = task.entity, task.action
    if entity is ManagerEntityKind.COURSE:
        result = capture.course_list if action is ManagerAction.LIST else capture.course_resolution
        if result is None:
            return _missing_expected_result("강좌", action)
        if not result.ok:
            return _mcp_response_failure_result(result, "강좌 조회를 완료하지 못했습니다.")
        return None

    if entity is ManagerEntityKind.ANNOUNCEMENT:
        if action is ManagerAction.DETAIL:
            details = capture.announcement_details
            if details is not None:
                if details.ok and details.data is not None:
                    return None
                return _mcp_response_failure_result(
                    details,
                    "공지 상세 내용을 조회하지 못했습니다.",
                )
            if capture.course_announcement_result is not None:
                semantic = _semantic_result_for_unfulfilled_detail(
                    capture.course_announcement_result,
                    label="공지사항",
                )
                if semantic is not None:
                    return semantic
            return _missing_expected_result("공지", action)
        result = capture.course_announcement_result or capture.announcement_list
        if result is None:
            return _missing_expected_result("공지", action)
        if isinstance(result, CourseAnnouncementResult):
            status, error_code = _semantic_outcome_contract(result.status)
            if status is not SpecialistStatus.COMPLETED:
                display = _semantic_course_outcome_display_text(result, "공지사항")
                return SpecialistResult(
                    status=status,
                    summary=display,
                    error_code=error_code,
                )
        elif not result.ok:
            return _mcp_response_failure_result(result, "공지 목록을 조회하지 못했습니다.")
        return None

    if entity is ManagerEntityKind.ASSIGNMENT:
        if action is ManagerAction.DETAIL:
            details = capture.assignment_details
            if details is not None:
                if details.ok and details.data is not None:
                    return None
                return _mcp_response_failure_result(
                    details,
                    "과제 상세 내용을 조회하지 못했습니다.",
                )
            if capture.course_assignment_result is not None:
                semantic = _semantic_result_for_unfulfilled_detail(
                    capture.course_assignment_result,
                    label="과제",
                )
                if semantic is not None:
                    return semantic
            return _missing_expected_result("과제", action)
        result = capture.course_assignment_result or capture.assignment_list
        if result is None:
            return _missing_expected_result("과제", action)
        if isinstance(result, CourseAssignmentResult):
            status, error_code = _semantic_outcome_contract(result.status)
            if status is not SpecialistStatus.COMPLETED:
                display = _semantic_course_outcome_display_text(result, "과제")
                return SpecialistResult(
                    status=status,
                    summary=display,
                    error_code=error_code,
                )
        elif not result.ok:
            return _mcp_response_failure_result(result, "과제 목록을 조회하지 못했습니다.")
        return None

    if entity is ManagerEntityKind.ATTACHMENT:
        if action is ManagerAction.DOWNLOAD:
            result = capture.download_result
            if result is None:
                return _missing_expected_result("첨부파일", action)
            if not result.ok or result.data is None:
                return _mcp_response_failure_result(
                    result,
                    "첨부파일을 다운로드하지 못했습니다.",
                )
            return None
        result = capture.attachment_list
        if result is None:
            return _missing_expected_result("첨부파일", action)
        if not result.ok:
            return _mcp_response_failure_result(result, "첨부파일 목록을 조회하지 못했습니다.")
        return None

    if entity is ManagerEntityKind.LECTURE:
        if action is ManagerAction.DETAIL:
            result = capture.lecture_status
            if result is None:
                return _missing_expected_result("강의", action)
            if not result.ok or result.data is None:
                return _mcp_response_failure_result(result, "강의 상태를 조회하지 못했습니다.")
            return None
        result = capture.course_lecture_result or capture.lecture_list
        if result is None:
            return _missing_expected_result("강의", action)
        if isinstance(result, CourseLectureResult):
            status, error_code = _semantic_outcome_contract(result.status)
            if status is not SpecialistStatus.COMPLETED:
                display = _semantic_course_outcome_display_text(result, "강의 영상")
                return SpecialistResult(
                    status=status,
                    summary=display,
                    error_code=error_code,
                )
        elif not result.ok:
            return _mcp_response_failure_result(result, "강의 목록을 조회하지 못했습니다.")
        return None

    if entity is ManagerEntityKind.GRADE:
        result = capture.grade_list
        if result is None:
            return _missing_expected_result("성적", action)
        if not result.ok:
            return _mcp_response_failure_result(result, "성적을 조회하지 못했습니다.")
        return None

    return _missing_expected_result("E-Class", action)


class EclassMcpSpecialistHandler:
    """ManagerTask 하나를 E-Class Agent + 직접 작성한 MCP 서버로 실행한다."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._server: MCPServerStdio | None = None
        self._server_lock = asyncio.Lock()
        self._server_lifecycle_task: asyncio.Task[None] | None = None
        self._server_close_event: asyncio.Event | None = None
        self._server_ready: asyncio.Future[None] | None = None
        self._active_tool_allowlist: frozenset[str] = frozenset()
        self._trace_events: list[tuple[str, str]] = []

    def _new_mcp_server(self) -> MCPServerStdio:
        """Agent 경로와 직접 검증 경로가 동일한 로컬 MCP 서버 설정을 공유한다."""

        return MCPServerStdio(
            params={
                "command": sys.executable,
                "args": ["-m", "mcp_server.server"],
                "cwd": PROJECT_ROOT,
                "env": _mcp_gui_environment(),
            },
            name="E-Class MCP",
            cache_tools_list=False,
            tool_filter=self._filter_mcp_tool,
            client_session_timeout_seconds=180,
            use_structured_content=True,
            require_approval="never",
        )

    def _filter_mcp_tool(self, _context, tool) -> bool:
        """현재 typed operation에 등록된 Tool만 E-Class Agent에 노출한다."""

        # 호환용 원시 Tool은 서버 API에는 남아 있지만 Agent에는 어떤 task에서도 노출하지
        # 않는다. Agent가 복사한 lecture_id로 검증 참조 절차를 우회할 수 없게 하기 위함이다.
        if tool.name in _RAW_PLAYBACK_TOOLS:
            return False
        return tool.name in self._active_tool_allowlist

    async def __call__(self, task: ManagerTask) -> SpecialistResult:
        # handler 인스턴스는 TUI 세션 동안 재사용되므로 이전 요청의 Tool trace를 새 요청에
        # 섞지 않는다. 각 직접 실행 경로는 이 빈 목록에 현재 요청만 기록한다.
        self._trace_events = []
        if (
            task.verified_lecture_target is not None
            and task.action in {ManagerAction.PLAY, ManagerAction.PREVIEW}
        ):
            return await self._play_verified_lecture(task)
        if task.verified_announcement_target is not None:
            return await self._get_verified_announcement_details(task)
        # 현재 첨부 Snapshot에서 파일까지 확정됐다면 부모 과제 재조회보다 이 대상을 우선한다.
        # 둘 다 붙은 task에서 부모 분기가 먼저 실행되면 다운로드 대신 목록만 반복하게 된다.
        if task.verified_attachment_targets:
            return await self._download_verified_attachments(task)
        if task.verified_attachment_target is not None:
            return await self._download_verified_attachment(task)
        if task.verified_assignment_target is not None:
            if task.entity is ManagerEntityKind.ATTACHMENT:
                if task.action is ManagerAction.DOWNLOAD:
                    return await self._download_verified_assignment_attachments(task)
                return await self._list_verified_assignment_attachments(task)
            return await self._get_verified_assignment_details(task)
        if not self.settings.openai_api_key or self.settings.openai_api_key == "...":
            raise OpenAiApiKeyRequiredError("실행 명령의 --setup 옵션에서 OpenAI API 키를 설정하세요.")
        set_default_openai_key(self.settings.openai_api_key)
        capture = _VerifiedMcpOutputCapture()
        # Agent가 현재 요청에서 첨부 목록까지는 검증했지만 보안상 숨겨진 원시 다운로드 Tool을
        # 호출할 수 없는 경우, lock을 빠져나온 뒤 handler가 그 검증 목록으로 다운로드를 완수한다.
        captured_attachment_list: AttachmentListResult | None = None
        captured_attachment_targets: list[VerifiedAttachmentTarget] = []
        captured_selection_error: str | None = None
        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                if task.action in {ManagerAction.PLAY, ManagerAction.PREVIEW}:
                    action_contract = (
                        "이 요청은 Runtime이 확인한 명시적 영상 제어 요청이다. "
                        "resolve_lecture의 FOUND reference_id만 사용하고, 일반 재생은 "
                        "play_resolved_lecture, 미리보기는 preview_resolved_lecture에 "
                        "explicit_user_request=true로 전달한다.\n"
                    )
                elif task.action is ManagerAction.STOP:
                    action_contract = (
                        "이 요청은 Runtime이 확인한 명시적 영상 중지 요청이다. "
                        "사용자가 지정한 playback_id만 stop_lecture에 전달한다.\n"
                    )
                else:
                    action_contract = "이 요청에서는 영상 재생·중지 Tool을 사용할 수 없다.\n"
                run_result = await Runner.run(
                    build_eclass_agent(self.settings, mcp_servers=[server]),
                    (
                        "다음 ManagerTask를 E-Class MCP Tool로 실행하세요. "
                        "결과에 사용한 엔터티는 evidence_refs에 "
                        "'<entity_type>:<id>' 형식으로 남기세요.\n"
                        f"capability={task.capability.value}\n"
                        f"entity={task.entity.value}\n"
                        f"action={task.action.value}\n"
                        f"slots={task.slots.model_dump_json(exclude_none=True)}\n"
                        f"allowed_tools={','.join(sorted(self._active_tool_allowlist))}\n"
                        f"{action_contract}"
                        f"instruction={task.instruction}"
                    ),
                    max_turns=8,
                    hooks=capture,
                    run_config=privacy_safe_run_config(),
                )
                # VIDEO_PLAY인데 Agent가 단일 대상을 resolve만 하고 action Tool을 생략한 경우,
                # 검증 참조를 그대로 사용해 Runtime이 결정적으로 재생을 완수한다.
                resolution = capture.lecture_resolution_result
                if (
                    task.capability is CapabilityCode.VIDEO_PLAY
                    and capture.verified_playback_result is None
                    and capture.playback_result is None
                    and resolution is not None
                    and resolution.ok
                    and resolution.data is not None
                ):
                    tool_name, arguments = _safe_playback_arguments(
                        task,
                        resolution.data.reference_id,
                    )
                    tool_result = await server.call_tool(tool_name, arguments)
                    capture.verified_playback_result = VerifiedPlaybackResult.model_validate(
                        tool_result.structuredContent
                    )
                    capture.verified_playback_tool = tool_name
                    capture.last_data_tool = tool_name
                    playback_outcome_completed = (
                        capture.verified_playback_result.status
                        in {
                            McpOutcomeStatus.FOUND,
                            McpOutcomeStatus.NOT_FOUND,
                            McpOutcomeStatus.AMBIGUOUS,
                        }
                    )
                    playback_ok = (
                        capture.verified_playback_result.ok
                        and capture.verified_playback_result.data is not None
                        and capture.verified_playback_result.target is not None
                    )
                    capture.tool_events.append(
                        (
                            tool_name,
                            "COMPLETED" if playback_outcome_completed else "FAILED",
                        )
                    )
                    if playback_outcome_completed:
                        capture.successful_tools.append(tool_name)
                    if playback_ok:
                        target = capture.verified_playback_result.target
                        playback = capture.verified_playback_result.data
                        assert target is not None and playback is not None
                        capture.evidence_refs.extend(
                            [
                                f"lecture:{target.lecture_id}",
                                f"playback:{playback.playback_id}",
                            ]
                        )
                if (
                    task.entity is ManagerEntityKind.ATTACHMENT
                    and task.action is ManagerAction.DOWNLOAD
                    and capture.download_result is None
                    and capture.attachment_list is not None
                    and capture.attachment_list.ok
                ):
                    captured_attachment_list = capture.attachment_list
                    selected, captured_selection_error = _select_attachments_for_download(
                        task,
                        capture.attachment_list,
                    )
                    captured_attachment_targets = [
                        VerifiedAttachmentTarget(
                            id=attachment.id,
                            parent_id=attachment.parent_id,
                            name=attachment.name,
                            url=attachment.url,
                        )
                        for attachment in selected
                    ]
            self._trace_events = list(capture.tool_events)
            if captured_selection_error is not None and captured_attachment_list is not None:
                return SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary=captured_selection_error,
                    verified_display_text=_attachment_list_display_text(captured_attachment_list),
                    verified_followup_context=_attachment_followup_context(
                        captured_attachment_list
                    ),
                    error_code=ErrorCode.INVALID_REQUEST,
                )
            if captured_attachment_targets and captured_attachment_list is not None:
                download_task = task.model_copy(
                    update={
                        "verified_assignment_target": None,
                        "verified_attachment_target": None,
                        "verified_attachment_targets": captured_attachment_targets,
                        "verified_attachment_id": None,
                        "verified_attachment_ids": [],
                    }
                )
                downloaded = await self._download_verified_attachments(download_task)
                if downloaded.status is not SpecialistStatus.COMPLETED:
                    return downloaded
                return downloaded.model_copy(
                    update={
                        "evidence_refs": list(
                            dict.fromkeys(
                                [
                                    *(f"attachment:{target.id}" for target in captured_attachment_targets),
                                    *downloaded.evidence_refs,
                                ]
                            )
                        ),
                        "verified_followup_context": _attachment_followup_context(
                            captured_attachment_list
                        ),
                    }
                )
            if run_result.final_output is None:
                raise RuntimeError("E-Class Agent가 구조화 결과를 반환하지 않았습니다.")
            parsed = SpecialistResult.model_validate(run_result.final_output)
            if (
                task.capability is CapabilityCode.VIDEO_PLAY
                and capture.verified_playback_result is None
                and capture.playback_result is None
                and capture.lecture_resolution_result is None
            ):
                return SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary="E-Class Agent가 재생 대상을 검증하고 영상 제어를 완료하지 못했습니다.",
                    error_code=ErrorCode.TEMPORARY_FAILURE,
                )
            # 안전 재생 Tool 결과는 그 뒤에 Agent가 다른 조회 Tool을 호출했더라도 항상
            # 최종 결과로 사용한다. ``last_data_tool``에 의존하면 성공/실패가 후속 조회로
            # 덮이는 문제가 생기므로 캡처된 typed 결과와 실제 Tool 이름을 별도로 보존한다.
            if capture.verified_playback_result is not None:
                return _verified_playback_specialist_result(
                    capture.verified_playback_result,
                    tool_name=(
                        capture.verified_playback_tool or "play_resolved_lecture"
                    ),
                    evidence_refs=capture.evidence_refs,
                )
            if (
                task.action is ManagerAction.STOP
                and capture.playback_result is not None
            ):
                return _raw_playback_specialist_result(
                    capture.playback_result,
                    evidence_refs=capture.evidence_refs,
                )
            if capture.verified_playback_tool is not None:
                return SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary="검증 참조 영상 Tool의 구조화 결과를 확인하지 못했습니다.",
                    error_code=ErrorCode.TEMPORARY_FAILURE,
                )
            if task.action is ManagerAction.STOP and capture.playback_tool is not None:
                return SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary="영상 중지 Tool의 구조화 결과를 확인하지 못했습니다.",
                    error_code=ErrorCode.TEMPORARY_FAILURE,
                )
            expected_outcome = _expected_operation_guard(task, capture)
            if expected_outcome is not None:
                return expected_outcome
            # MCP가 검증한 과제 목록이 있는데 모델이 강좌 ID를 확인하지 못했다며 FAILED를
            # 반환하는 경우가 있다. 성공한 구조화 Tool 결과를 사실 원본으로 삼아 이를 교정한다.
            if (
                task.entity is ManagerEntityKind.ASSIGNMENT
                and task.action is ManagerAction.LIST
            ):
                parsed = _prefer_verified_assignment_list(parsed, capture)
            if (
                task.entity is ManagerEntityKind.LECTURE
                and task.action is ManagerAction.LIST
            ):
                parsed = _prefer_verified_lecture_list(parsed, capture)
            has_semantic_outcome = any(
                result is not None
                for result in (
                    capture.course_announcement_result,
                    capture.course_assignment_result,
                    capture.course_lecture_result,
                    capture.lecture_resolution_result,
                    capture.verified_playback_result,
                )
            )
            if (
                parsed.status is SpecialistStatus.COMPLETED
                and not capture.successful_tools
                and not has_semantic_outcome
            ):
                return SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary="E-Class Tool로 검증된 결과가 없어 성공으로 처리하지 않았습니다.",
                    error_code=ErrorCode.TEMPORARY_FAILURE,
                )
            # 모델이 만든 참조는 신뢰하지 않고 실제 성공 Tool에서 캡처한 ID만 사용한다.
            verified_evidence = list(capture.evidence_refs)
            for download in capture.downloads:
                assert download.data is not None
                verified_evidence.append(
                    f"download:{download.data.download_id}:{download.data.attachment_id}"
                )
            # 모델이 같은 필드를 임의로 채울 수 없도록 항상 handler가 검증된 캡처값으로 덮어쓴다.
            verified_text = None
            verified_context = None
            semantic_outcome: McpOutcomeStatus | None = None
            # 요청의 typed entity를 먼저 사용한다. 동일 실행 중 다른 조회가 뒤따랐더라도
            # ``last_data_tool`` 하나만 보고 엉뚱한 엔터티를 최종 결과로 선택하지 않는다.
            if (
                task.entity is ManagerEntityKind.ANNOUNCEMENT
                and task.action is ManagerAction.DETAIL
                and capture.announcement_details is not None
                and capture.announcement_details.data is not None
            ):
                verified_text = _announcement_display_text(capture.announcement_details)
            elif (
                task.entity is ManagerEntityKind.ANNOUNCEMENT
                and task.action is ManagerAction.LIST
                and capture.course_announcement_result is not None
            ):
                semantic = capture.course_announcement_result
                semantic_outcome = semantic.status
                if semantic.data is not None:
                    verified_text = _course_announcement_display_text(semantic)
                    verified_context = _course_announcement_followup_context(semantic)
                else:
                    verified_text = _semantic_course_outcome_display_text(semantic, "공지사항")
            elif (
                task.entity is ManagerEntityKind.ANNOUNCEMENT
                and task.action is ManagerAction.LIST
                and capture.announcement_list is not None
            ):
                verified_text = _announcement_list_display_text(capture.announcement_list)
                verified_context = _announcement_followup_context(capture.announcement_list)
            elif (
                task.entity is ManagerEntityKind.ASSIGNMENT
                and task.action is ManagerAction.DETAIL
                and capture.assignment_details is not None
                and capture.assignment_details.data is not None
            ):
                verified_text = _assignment_details_display_text(
                    capture.assignment_details,
                    capture.course_list,
                )
            elif (
                task.entity is ManagerEntityKind.ASSIGNMENT
                and task.action is ManagerAction.LIST
                and capture.course_assignment_result is not None
            ):
                semantic = capture.course_assignment_result
                semantic_outcome = semantic.status
                if semantic.data is not None:
                    verified_text = _course_assignment_display_text(semantic)
                    verified_context = _course_assignment_followup_context(semantic)
                else:
                    verified_text = _semantic_course_outcome_display_text(semantic, "과제")
            elif (
                task.entity is ManagerEntityKind.ASSIGNMENT
                and task.action is ManagerAction.LIST
                and capture.assignment_list is not None
            ):
                verified_text = _assignment_list_display_text(
                    capture.assignment_list,
                    capture.course_list,
                )
                verified_context = _assignment_followup_context(capture.assignment_list)
            elif (
                task.entity is ManagerEntityKind.LECTURE
                and task.action is ManagerAction.LIST
                and task.capability is CapabilityCode.ECLASS_QUERY
                and capture.course_lecture_result is not None
            ):
                semantic = capture.course_lecture_result
                semantic_outcome = semantic.status
                if semantic.data is not None:
                    verified_text = _course_lecture_display_text(semantic)
                    verified_context = _course_lecture_followup_context(semantic)
                else:
                    verified_text = _semantic_course_outcome_display_text(semantic, "강의 영상")
            elif (
                task.entity is ManagerEntityKind.LECTURE
                and task.capability is CapabilityCode.VIDEO_PLAY
                and capture.lecture_resolution_result is not None
            ):
                semantic_outcome = capture.lecture_resolution_result.status
                verified_text = _lecture_resolution_display_text(
                    capture.lecture_resolution_result
                )
                verified_context = _lecture_resolution_followup_context(
                    capture.lecture_resolution_result
                )
            elif (
                task.entity is ManagerEntityKind.LECTURE
                and task.action is ManagerAction.LIST
                and capture.lecture_list is not None
            ):
                verified_text = _lecture_list_display_text(
                    capture.lecture_list,
                    capture.course_list,
                )
                verified_context = _lecture_followup_context(
                    capture.lecture_list,
                    capture.course_list,
                )
            elif (
                task.entity is ManagerEntityKind.COURSE
                and task.action is ManagerAction.LIST
                and capture.course_list is not None
            ):
                verified_text = _course_list_display_text(capture.course_list)
                verified_context = _course_followup_context(capture.course_list)
            elif (
                task.entity is ManagerEntityKind.COURSE
                and task.action is ManagerAction.DETAIL
                and capture.course_resolution is not None
            ):
                verified_text = _course_resolution_display_text(capture.course_resolution)
            elif (
                task.entity is ManagerEntityKind.ATTACHMENT
                and task.action in {ManagerAction.LIST, ManagerAction.DETAIL}
                and capture.attachment_list is not None
            ):
                verified_text = _attachment_list_display_text(capture.attachment_list)
                verified_context = _attachment_followup_context(capture.attachment_list)
            elif (
                task.entity is ManagerEntityKind.ATTACHMENT
                and task.action is ManagerAction.DOWNLOAD
                and capture.download_result is not None
                and capture.download_result.data is not None
            ):
                download = capture.download_result.data
                verified_text = f"첨부파일 다운로드 완료: {download.filename}"
            elif (
                task.entity is ManagerEntityKind.LECTURE
                and task.action is ManagerAction.DETAIL
                and capture.lecture_status is not None
                and capture.lecture_status.data is not None
            ):
                verified_text = _lecture_status_display_text(capture.lecture_status)
            elif (
                task.entity is ManagerEntityKind.GRADE
                and capture.grade_list is not None
            ):
                verified_text = _grade_list_display_text(capture.grade_list)
            elif (
                task.entity is ManagerEntityKind.LECTURE
                and task.capability is CapabilityCode.VIDEO_PLAY
                and capture.lecture_resolution_result is not None
            ):
                semantic_outcome = capture.lecture_resolution_result.status
                verified_text = _lecture_resolution_display_text(
                    capture.lecture_resolution_result
                )
                verified_context = _lecture_resolution_followup_context(
                    capture.lecture_resolution_result
                )
            semantic_status: SpecialistStatus | None = None
            semantic_error: ErrorCode | None = None
            if semantic_outcome is not None:
                semantic_status, semantic_error = _semantic_outcome_contract(semantic_outcome)
            return parsed.model_copy(
                update={
                    "status": semantic_status or (
                        SpecialistStatus.COMPLETED
                        if verified_text is not None
                        else parsed.status
                    ),
                    "summary": verified_text or parsed.summary,
                    "error_code": (
                        semantic_error
                        if semantic_outcome is not None
                        else parsed.error_code
                    ),
                    "verified_display_text": verified_text,
                    "verified_followup_context": verified_context,
                    "evidence_refs": list(dict.fromkeys(verified_evidence)),
                }
            )

        except OpenAiApiKeyRequiredError:
            raise
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="E-Class MCP 조회를 완료하지 못했습니다.",
                suggested_actions=["세션과 E-Class 연결 상태를 확인한 뒤 다시 시도하세요."],
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )

    async def stop_verified_playback(self, playback_id: str) -> SpecialistResult:
        """Runtime이 보관한 재생 ID를 LLM 복사 없이 기존 MCP 프로세스에 전달한다.

        재생 브라우저는 장수명 stdio MCP 프로세스 안에 보관된다. 따라서 새 서비스 객체를
        import해 중지하면 다른 프로세스의 빈 재생 목록을 보게 된다. 이 메서드는 조회·재생에
        이미 쓰던 ``MCPServerStdio`` 연결을 재사용하고, UUID를 그대로 ``stop_lecture``에
        결박한다. 모델은 이 경로에서 Tool 이름이나 ID를 만들거나 수정할 기회를 갖지 않는다.
        """

        try:
            normalized_id = str(UUID(playback_id))
        except (TypeError, ValueError, AttributeError):
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="검증된 영상 재생 ID가 올바르지 않습니다.",
                error_code=ErrorCode.INVALID_REQUEST,
            )

        try:
            async with self._server_lock:
                server = await self._ensure_server()
                tool_result = await server.call_tool(
                    "stop_lecture",
                    {"playback_id": normalized_id},
                )
            result = PlaybackResult.model_validate(tool_result.structuredContent)
            self._trace_events = [
                ("stop_lecture", "COMPLETED" if result.ok else "FAILED")
            ]
        except Exception:
            self._trace_events = [("stop_lecture", "FAILED")]
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="E-Class 영상 중지 요청을 완료하지 못했습니다.",
                suggested_actions=["현재 재생 창을 확인한 뒤 다시 시도하세요."],
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )

        # 다른 재생 결과가 섞이는 MCP 계약 오류도 성공으로 표시하지 않는다.
        if result.ok and result.data is not None and result.data.playback_id != normalized_id:
            self._trace_events = [("stop_lecture", "FAILED")]
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="영상 중지 결과의 재생 ID가 요청과 일치하지 않습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        return _raw_playback_specialist_result(
            result,
            evidence_refs=[f"playback:{normalized_id}"],
        )

    async def _play_verified_lecture(self, task: ManagerTask) -> SpecialistResult:
        """직전 목록 대상을 다시 resolve한 뒤 불투명 참조로만 재생한다."""

        target = task.verified_lecture_target
        assert target is not None
        if not target.course_name:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="선택한 강의의 검증된 강좌명이 없어 대상을 다시 확인할 수 없습니다.",
                error_code=ErrorCode.INVALID_REQUEST,
            )

        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                resolution_tool_result = await server.call_tool(
                    "resolve_lecture",
                    {
                        "course_query": target.course_name,
                        "week": target.week,
                        "title_query": target.title,
                        "only_unwatched": False,
                        "year": target.year,
                        "semester": target.semester,
                    },
                )
                resolution = LectureResolutionResult.model_validate(
                    resolution_tool_result.structuredContent
                )
                resolution_event = (
                    "COMPLETED"
                    if resolution.status
                    in {
                        McpOutcomeStatus.FOUND,
                        McpOutcomeStatus.NOT_FOUND,
                        McpOutcomeStatus.AMBIGUOUS,
                    }
                    else "FAILED"
                )
                self._trace_events = [("resolve_lecture", resolution_event)]
                if (
                    resolution.status is not McpOutcomeStatus.FOUND
                    or not resolution.ok
                    or resolution.data is None
                ):
                    status, error_code = _semantic_outcome_contract(resolution.status)
                    display = _lecture_resolution_display_text(resolution)
                    return SpecialistResult(
                        status=status,
                        summary=display,
                        error_code=error_code,
                        verified_display_text=(
                            display if status is SpecialistStatus.COMPLETED else None
                        ),
                        verified_followup_context=_lecture_resolution_followup_context(
                            resolution
                        ),
                    )
                # 목록 snapshot이 가리킨 실제 lecture_id와 현재 재해석 결과가 다르면
                # 오래됐거나 잘못 연결된 문맥이므로 어떤 player Tool도 호출하지 않는다.
                if resolution.data.lecture_id != target.id:
                    return SpecialistResult(
                        status=SpecialistStatus.FAILED,
                        summary=(
                            "선택한 강의와 현재 E-Class에서 다시 확인한 강의가 "
                            "일치하지 않습니다. 강의 목록을 새로 조회해 주세요."
                        ),
                        error_code=ErrorCode.INVALID_REQUEST,
                    )
                tool_name, arguments = _safe_playback_arguments(
                    task,
                    resolution.data.reference_id,
                )
                playback_tool_result = await server.call_tool(tool_name, arguments)
            result = VerifiedPlaybackResult.model_validate(
                playback_tool_result.structuredContent
            )
            playback_event = (
                "COMPLETED"
                if result.status
                in {
                    McpOutcomeStatus.FOUND,
                    McpOutcomeStatus.NOT_FOUND,
                    McpOutcomeStatus.AMBIGUOUS,
                }
                else "FAILED"
            )
            self._trace_events.append((tool_name, playback_event))
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="선택한 강의 영상 재생을 시작하지 못했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        return _verified_playback_specialist_result(
            result,
            tool_name=tool_name,
            evidence_refs=[f"lecture:{target.id}"],
        )

    async def _download_verified_attachment(self, task: ManagerTask) -> SpecialistResult:
        """직전 구조화 목록에서 검증된 첨부만 Agent 재작성 없이 다운로드한다."""

        target = task.verified_attachment_target
        assert target is not None
        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                tool_result = await server.call_tool(
                    "download_attachment",
                    {
                        "attachment_url": target.url,
                        "attachment_id": target.id,
                        "filename": target.name,
                    },
                )
            result = DownloadResult.model_validate(tool_result.structuredContent)
            self._trace_events = [
                ("download_attachment", "COMPLETED" if result.ok else "FAILED")
            ]
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="검증된 E-Class 첨부파일을 다운로드하지 못했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        if result.ok and result.data is not None:
            if result.data.attachment_id != target.id:
                return SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary="다운로드 결과가 선택한 첨부파일과 일치하지 않습니다.",
                    error_code=ErrorCode.INVALID_REQUEST,
                )
            reference = f"download:{result.data.download_id}:{result.data.attachment_id}"
            return SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary=f"첨부파일 다운로드 완료: {result.data.filename}",
                evidence_refs=[reference],
            )
        error = result.error
        return SpecialistResult(
            status=(
                SpecialistStatus.AUTH_REQUIRED
                if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                else SpecialistStatus.FAILED
            ),
            summary=error.message if error is not None else "첨부파일 다운로드에 실패했습니다.",
            error_code=(
                ErrorCode.AUTH_REQUIRED
                if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                else ErrorCode.TEMPORARY_FAILURE
            ),
        )

    async def _download_verified_attachments(self, task: ManagerTask) -> SpecialistResult:
        """같은 과제에서 검증된 복수 첨부를 원래 목록 순서대로 다운로드한다."""

        targets = task.verified_attachment_targets
        if not targets or len(targets) > 5 or len({item.parent_id for item in targets}) != 1:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="복수 첨부파일의 검증 범위가 올바르지 않습니다.",
                error_code=ErrorCode.INVALID_REQUEST,
            )

        downloads: list[DownloadResult] = []
        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                for target in targets:
                    tool_result = await server.call_tool(
                        "download_attachment",
                        {
                            "attachment_url": target.url,
                            "attachment_id": target.id,
                            "filename": target.name,
                        },
                    )
                    result = DownloadResult.model_validate(tool_result.structuredContent)
                    downloads.append(result)
                    self._trace_events.append(
                        (
                            f"download_attachment:{target.id}",
                            "COMPLETED" if result.ok else "FAILED",
                        )
                    )
                    if not result.ok or result.data is None:
                        error = result.error
                        return SpecialistResult(
                            status=(
                                SpecialistStatus.AUTH_REQUIRED
                                if error is not None
                                and error.code is McpErrorCode.AUTH_REQUIRED
                                else SpecialistStatus.FAILED
                            ),
                            summary=(
                                error.message
                                if error is not None
                                else f"첨부파일 '{target.name}' 다운로드에 실패했습니다."
                            ),
                            error_code=(
                                ErrorCode.AUTH_REQUIRED
                                if error is not None
                                and error.code is McpErrorCode.AUTH_REQUIRED
                                else ErrorCode.TEMPORARY_FAILURE
                            ),
                        )
                    if result.data.attachment_id != target.id:
                        return SpecialistResult(
                            status=SpecialistStatus.FAILED,
                            summary=(
                                f"첨부파일 '{target.name}'의 다운로드 결과가 선택 대상과 "
                                "일치하지 않습니다."
                            ),
                            error_code=ErrorCode.INVALID_REQUEST,
                        )
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="검증된 E-Class 첨부파일들을 다운로드하지 못했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )

        references = [
            f"download:{result.data.download_id}:{result.data.attachment_id}"
            for result in downloads
            if result.data is not None
        ]
        names = [result.data.filename for result in downloads if result.data is not None]
        display = "첨부파일 다운로드 완료\n" + "\n".join(
            f"{index}. {name}" for index, name in enumerate(names, start=1)
        )
        return SpecialistResult(
            status=SpecialistStatus.COMPLETED,
            summary=display[:2_000],
            evidence_refs=references,
            verified_display_text=display,
        )

    async def _list_verified_assignment_attachments(
        self,
        task: ManagerTask,
    ) -> SpecialistResult:
        """검증된 부모 과제 ID로 첨부 메타데이터를 모델 재작성 없이 조회한다."""

        target = task.verified_assignment_target
        assert target is not None
        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                tool_result = await server.call_tool(
                    "list_assignment_attachments",
                    {
                        "assignment_id": target.id,
                        "year": target.year,
                        "semester": target.semester,
                    },
                )
            result = AttachmentListResult.model_validate(tool_result.structuredContent)
            self._trace_events = [
                ("list_assignment_attachments", "COMPLETED" if result.ok else "FAILED")
            ]
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="선택한 과제의 첨부파일 목록을 조회하지 못했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        if result.ok:
            display = _attachment_list_display_text(result)
            return SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary=display[:2_000],
                evidence_refs=[
                    f"assignment:{target.id}",
                    *(f"attachment:{attachment.id}" for attachment in result.data),
                ],
                verified_display_text=display,
                verified_followup_context=_attachment_followup_context(result),
            )
        error = result.error
        return SpecialistResult(
            status=(
                SpecialistStatus.AUTH_REQUIRED
                if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                else SpecialistStatus.FAILED
            ),
            summary=(
                error.message if error is not None else "과제 첨부파일 목록을 가져오지 못했습니다."
            ),
            error_code=(
                ErrorCode.AUTH_REQUIRED
                if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                else ErrorCode.TEMPORARY_FAILURE
            ),
        )

    async def _download_verified_assignment_attachments(
        self,
        task: ManagerTask,
    ) -> SpecialistResult:
        """선택된 과제의 목록 조회와 검증 다운로드를 한 요청 안에서 이어서 실행한다."""

        assignment = task.verified_assignment_target
        assert assignment is not None
        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                tool_result = await server.call_tool(
                    "list_assignment_attachments",
                    {
                        "assignment_id": assignment.id,
                        "year": assignment.year,
                        "semester": assignment.semester,
                    },
                )
            attachments = AttachmentListResult.model_validate(tool_result.structuredContent)
            self._trace_events = [
                ("list_assignment_attachments", "COMPLETED" if attachments.ok else "FAILED")
            ]
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="선택한 과제의 첨부파일 목록을 조회하지 못했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        if not attachments.ok:
            error = attachments.error
            return SpecialistResult(
                status=(
                    SpecialistStatus.AUTH_REQUIRED
                    if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                    else SpecialistStatus.FAILED
                ),
                summary=(
                    error.message
                    if error is not None
                    else "과제 첨부파일 목록을 가져오지 못했습니다."
                ),
                error_code=(
                    ErrorCode.AUTH_REQUIRED
                    if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                    else ErrorCode.TEMPORARY_FAILURE
                ),
            )

        # 이 경로의 ordinal은 직전 ASSIGNMENT Snapshot에서 부모 과제를 고르는 데 이미
        # 소비된 값이다. 예를 들어 "첫 번째 과제의 파일들"의 1을 첨부 1번으로 다시
        # 해석하면 여러 파일 중 하나만 내려받게 된다. 첨부파일을 직접 고르는 후속 요청은
        # VerifiedAttachmentTarget 경로로 들어오므로 여기서는 ordinal을 비운다.
        attachment_selection_task = task.model_copy(
            update={"slots": task.slots.model_copy(update={"ordinal": None})}
        )
        selected, selection_error = _select_attachments_for_download(
            attachment_selection_task,
            attachments,
        )
        if selection_error is not None:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary=selection_error,
                verified_display_text=_attachment_list_display_text(attachments),
                verified_followup_context=_attachment_followup_context(attachments),
                error_code=ErrorCode.INVALID_REQUEST,
            )
        targets = [
            VerifiedAttachmentTarget(
                id=attachment.id,
                parent_id=attachment.parent_id,
                name=attachment.name,
                url=attachment.url,
            )
            for attachment in selected
        ]
        download_task = task.model_copy(
            update={
                "verified_assignment_target": None,
                "verified_attachment_target": None,
                "verified_attachment_targets": targets,
                "verified_attachment_id": None,
                "verified_attachment_ids": [],
            }
        )
        downloaded = await self._download_verified_attachments(download_task)
        if downloaded.status is not SpecialistStatus.COMPLETED:
            return downloaded
        return downloaded.model_copy(
            update={
                "evidence_refs": list(
                    dict.fromkeys(
                        [
                            f"assignment:{assignment.id}",
                            *(f"attachment:{attachment.id}" for attachment in selected),
                            *downloaded.evidence_refs,
                        ]
                    )
                ),
                "verified_followup_context": _attachment_followup_context(attachments),
            }
        )

    async def _get_verified_assignment_details(self, task: ManagerTask) -> SpecialistResult:
        """직전 목록에서 선택된 과제 ID를 Agent 재검색 없이 상세 Tool에 전달한다."""

        target = task.verified_assignment_target
        assert target is not None
        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                tool_result = await server.call_tool(
                    "get_assignment_details",
                    {
                        "assignment_id": target.id,
                        "year": target.year,
                        "semester": target.semester,
                    },
                )
                attachment_tool_result = await server.call_tool(
                    "list_assignment_attachments",
                    {
                        "assignment_id": target.id,
                        "year": target.year,
                        "semester": target.semester,
                    },
                )
            result = AssignmentDetailsResult.model_validate(tool_result.structuredContent)
            attachments = AttachmentListResult.model_validate(
                attachment_tool_result.structuredContent
            )
            self._trace_events = [
                ("get_assignment_details", "COMPLETED" if result.ok else "FAILED"),
                (
                    "list_assignment_attachments",
                    "COMPLETED" if attachments.ok else "FAILED",
                ),
            ]
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="선택한 과제의 상세 내용을 조회하지 못했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        if result.ok and result.data is not None:
            result = result.model_copy(
                update={
                    "data": result.data.model_copy(update={"course_name": target.course_name})
                }
            )
            display = _assignment_details_display_text(result)
            verified_context = None
            evidence_refs = [f"assignment:{result.data.id}"]
            if attachments.ok:
                display = f"{display}\n\n{_attachment_list_display_text(attachments)}"
                verified_context = _attachment_followup_context(attachments)
                evidence_refs.extend(
                    f"attachment:{attachment.id}" for attachment in attachments.data
                )
            return SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary=display[:2_000],
                evidence_refs=evidence_refs,
                verified_display_text=display,
                verified_followup_context=verified_context,
            )
        error = result.error
        return SpecialistResult(
            status=(
                SpecialistStatus.AUTH_REQUIRED
                if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                else SpecialistStatus.FAILED
            ),
            summary=error.message if error is not None else "과제 상세 내용을 가져오지 못했습니다.",
            error_code=(
                ErrorCode.AUTH_REQUIRED
                if error is not None and error.code is McpErrorCode.AUTH_REQUIRED
                else ErrorCode.TEMPORARY_FAILURE
            ),
        )

    async def _get_verified_announcement_details(self, task: ManagerTask) -> SpecialistResult:
        """직전 목록에서 검증된 URL은 Agent 재검색 없이 MCP 서비스로 직접 연다."""

        target = task.verified_announcement_target
        assert target is not None
        try:
            async with self._server_lock:
                self._active_tool_allowlist = _tool_allowlist_for_task(task)
                server = await self._ensure_server()
                tool_result = await server.call_tool(
                    "get_announcement_details",
                    {
                        "announcement_url": target.url,
                        "course_id": target.course_id,
                        "year": target.year,
                        "semester": target.semester,
                    },
                )
            result = AnnouncementDetailsResult.model_validate(tool_result.structuredContent)
            self._trace_events = [
                ("get_announcement_details", "COMPLETED" if result.ok else "FAILED")
            ]
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="E-Class MCP 공지 상세 조회를 완료하지 못했습니다.",
                suggested_actions=["공지 목록을 새로 조회한 뒤 다시 선택하세요."],
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        if result.ok and result.data is not None:
            display = _announcement_display_text(result)
            return SpecialistResult(
                status=SpecialistStatus.COMPLETED,
                summary=display,
                evidence_refs=[f"announcement:{result.data.id}"],
                verified_display_text=display,
            )
        error = result.error
        if error is not None and error.code is McpErrorCode.AUTH_REQUIRED:
            return SpecialistResult(
                status=SpecialistStatus.AUTH_REQUIRED,
                summary=error.message,
                suggested_actions=["E-Class 로그인 세션을 갱신한 뒤 다시 시도하세요."],
                error_code=ErrorCode.AUTH_REQUIRED,
            )
        return SpecialistResult(
            status=SpecialistStatus.FAILED,
            summary=error.message if error is not None else "공지 상세 내용을 가져오지 못했습니다.",
            suggested_actions=["공지 목록을 새로 조회한 뒤 다시 선택하세요."],
            error_code=ErrorCode.TEMPORARY_FAILURE,
        )

    async def _ensure_server(self) -> MCPServerStdio:
        """재생 브라우저가 후속 stop 요청까지 유지되도록 MCP 프로세스를 재사용한다."""

        lifecycle = self._server_lifecycle_task
        ready = self._server_ready

        # connect 실패 뒤 필드만 남았거나, 연결 이후 lifecycle Task가 이미 종료됐다면
        # 해당 MCP 객체는 재사용할 수 없다. 반드시 기존 Task의 cleanup이 끝난 뒤 새
        # subprocess를 만든다. cleanup을 여기서 직접 호출하지 않는 이유는 AnyIO의
        # cancel scope 규칙상 connect와 cleanup이 같은 Task에서 실행되어야 하기 때문이다.
        stale = (
            self._server is not None
            and (
                lifecycle is None
                or lifecycle.done()
                or ready is None
                or ready.cancelled()
                or (ready.done() and ready.exception() is not None)
            )
        )
        inconsistent = self._server is None and any(
            value is not None
            for value in (
                lifecycle,
                self._server_close_event,
                ready,
            )
        )
        if stale or inconsistent:
            await self._clear_server_state(stop_running=True)

        if self._server is None:
            server = self._new_mcp_server()
            loop = asyncio.get_running_loop()
            close_event = asyncio.Event()
            ready = loop.create_future()
            lifecycle = asyncio.create_task(
                self._run_server_lifecycle(server, ready, close_event),
                name="eclass-mcp-lifecycle",
            )
            self._server = server
            self._server_close_event = close_event
            self._server_ready = ready
            self._server_lifecycle_task = lifecycle
            try:
                await ready
            except BaseException:
                # 첫 connect 실패도 다음 요청까지 깨진 객체를 남기지 않는다. lifecycle
                # Task가 자기 Task 안에서 cleanup을 마치도록 기다린 뒤 참조만 초기화한다.
                await self._clear_server_state(stop_running=True)
                raise
        else:
            # 다른 호출이 생성한 서버가 아직 연결 중인 경우 동일한 ready를 기다린다.
            assert self._server_ready is not None
            try:
                await self._server_ready
            except BaseException:
                await self._clear_server_state(stop_running=True)
                raise

        assert self._server is not None
        return self._server

    async def _run_server_lifecycle(
        self,
        server: MCPServerStdio,
        ready: asyncio.Future[None],
        close_event: asyncio.Event,
    ) -> None:
        """AnyIO cancel scope 규칙에 맞게 MCP connect와 cleanup을 같은 Task에서 수행한다."""

        try:
            await server.connect()
            if not ready.done():
                ready.set_result(None)
            await close_event.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            try:
                await server.cleanup()
            except Exception:
                pass

    async def _clear_server_state(self, *, stop_running: bool) -> None:
        """현재 lifecycle의 종료를 같은 Task 안의 cleanup까지 기다린 뒤 참조를 비운다.

        이 메서드는 ``_server_lock``으로 직렬화된 호출 경로에서 사용한다. 테스트처럼
        ``_ensure_server``를 직접 호출하더라도 한 번에 한 호출만 실행해야 한다.
        """

        lifecycle = self._server_lifecycle_task
        close_event = self._server_close_event
        if stop_running and close_event is not None and lifecycle is not None:
            if not lifecycle.done():
                close_event.set()
        if lifecycle is not None:
            try:
                await lifecycle
            except BaseException:
                # 연결 후 lifecycle이 비정상 종료했더라도 새 연결을 만들 수 있도록
                # 종료 예외는 여기서 회수한다. 실제 요청 실패는 ready await에서 전달된다.
                pass

        # 위에서 기다린 lifecycle과 현재 필드가 같을 때만 비운다. 향후 호출부가 바뀌어
        # 새 lifecycle이 먼저 설치되더라도 오래된 Task가 새 참조를 지우지 않게 한다.
        if self._server_lifecycle_task is lifecycle:
            self._server = None
            self._server_lifecycle_task = None
            self._server_close_event = None
            self._server_ready = None

    async def close(self) -> None:
        """TUI 종료 시 MCP 프로세스와 그 안의 Playwright 자원을 닫는다."""

        async with self._server_lock:
            await self._clear_server_state(stop_running=True)

    def consume_trace_events(self) -> list[tuple[str, str]]:
        events, self._trace_events = self._trace_events, []
        return events
