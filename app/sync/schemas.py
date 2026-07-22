"""E-Class 동기화·마감 탐지·TUI 사이의 구조화 계약."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.runtime import RuntimeEvent
from app.schemas.workflow import ErrorCode
from mcp_server.schemas import SelectedTerm


class LectureChecklistItem(BaseModel):
    """TUI 왼쪽 패널에 표시할 검증된 현재 강의 상태."""

    model_config = ConfigDict(extra="forbid")

    lecture_id: str = Field(min_length=1, max_length=160)
    course_id: str = Field(min_length=1, max_length=160)
    course_name: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    week: int | None = Field(default=None, ge=1, le=99)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    completed: bool
    available_from: datetime | None = None
    available_until: datetime | None = None


class CourseChecklistItem(BaseModel):
    """주차 강의가 없는 과목도 체크리스트에서 누락하지 않기 위한 최소 강좌 정보."""

    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(min_length=1, max_length=160)
    course_name: str = Field(min_length=1, max_length=300)


class AssignmentChecklistItem(BaseModel):
    """TUI 왼쪽 패널에 표시할 기본 학기의 이번 주 과제."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1, max_length=160)
    course_name: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    due_at: datetime
    completed: bool


class SyncTrigger(str, Enum):
    STARTUP = "STARTUP"
    HEARTBEAT = "HEARTBEAT"
    MANUAL = "MANUAL"


class SyncStatus(str, Enum):
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FAILED = "FAILED"


class SyncResult(BaseModel):
    """한 번의 동기화 결과. 실제 LMS 본문 대신 상태와 이벤트만 TUI에 전달한다."""

    model_config = ConfigDict(extra="forbid")

    status: SyncStatus
    trigger: SyncTrigger
    selected_term: SelectedTerm | None = None
    change_count: int = Field(default=0, ge=0)
    deadline_count: int = Field(default=0, ge=0)
    observed_count: int = Field(default=0, ge=0)
    course_count: int = Field(default=0, ge=0)
    events: list[RuntimeEvent] = Field(default_factory=list, max_length=10)
    change_event_ids: list[str] = Field(default_factory=list, max_length=500)
    course_checklist: list[CourseChecklistItem] = Field(default_factory=list, max_length=200)
    lecture_checklist: list[LectureChecklistItem] = Field(default_factory=list, max_length=500)
    assignment_checklist: list[AssignmentChecklistItem] = Field(default_factory=list, max_length=500)
    started_at: datetime
    finished_at: datetime
    error_code: ErrorCode | None = None


class DeadlineCandidate(BaseModel):
    """중복 검사 전 DB에 저장할 수 있는 마감 알림 후보."""

    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=160)
    notification_type: str = Field(min_length=1, max_length=64)
    dedupe_key: str = Field(min_length=64, max_length=128)
    payload: dict[str, object]
