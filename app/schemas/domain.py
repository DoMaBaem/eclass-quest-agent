"""LMS 화면을 서비스 내부의 안정적인 데이터로 바꾸는 공통 계약.

Playwright가 읽은 HTML을 Agent에게 그대로 주지 않는다. Adapter가 아래 모델로 정규화하므로
화면 선택자가 바뀌어도 Agent·DB·TUI는 같은 필드 이름을 계속 사용할 수 있다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import ErrorCode


def utc_now() -> datetime:
    """서버 지역과 무관하게 비교할 수 있는 UTC 현재 시각을 만든다."""

    return datetime.now(timezone.utc)


class EntityStatus(str, Enum):
    """서로 다른 LMS 엔터티가 공유하는 최소 상태 값."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"


class Course(BaseModel):
    """학기별 수강 강좌 한 개."""

    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=300)
    professor: str | None = Field(default=None, max_length=160)
    url: str = Field(min_length=1, max_length=2_000)
    year: int = Field(ge=2000, le=2100)
    semester: int = Field(ge=1, le=4)
    status: EntityStatus = EntityStatus.OPEN


class Assignment(BaseModel):
    """강좌에 속한 과제와 제출 여부·마감 상태."""

    id: str = Field(min_length=1, max_length=160)
    course_id: str = Field(min_length=1, max_length=160)
    course_name: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    # 상세 페이지의 과제 지시문이다. 목록 조회에서는 None이고 상세 조회에서만 채운다.
    description: str | None = Field(default=None, max_length=50_000)
    # 과제 목록 화면의 주차 열이다. DB 최신 상태에는 필수 정보가 아니므로 조회 결과에서만 활용한다.
    week: int | None = Field(default=None, ge=1, le=99)
    due_at: datetime | None = None
    submitted: bool | None = None
    submitted_at: datetime | None = None
    status: EntityStatus = EntityStatus.UNKNOWN


class Lecture(BaseModel):
    """주차별 온라인 강의와 진도율·시청 가능 기간."""

    id: str = Field(min_length=1, max_length=160)
    course_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    week: int | None = Field(default=None, ge=1, le=99)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    available_from: datetime | None = None
    available_until: datetime | None = None
    attendance_status: EntityStatus = EntityStatus.UNKNOWN
    completed_at: datetime | None = None
    status: EntityStatus = EntityStatus.UNKNOWN


class Announcement(BaseModel):
    """강좌 공지 또는 학교 공지 한 건."""

    id: str = Field(min_length=1, max_length=160)
    course_id: str | None = Field(default=None, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    posted_at: datetime | None = None
    status: EntityStatus = EntityStatus.OPEN


class AnnouncementDetails(Announcement):
    """공지 상세 페이지에서만 읽는 작성자와 본문."""

    author: str | None = Field(default=None, max_length=200)
    content: str = Field(min_length=1, max_length=50_000)


class Attachment(BaseModel):
    """첨부파일 메타데이터. 다운로드한 파일 본문은 포함하지 않는다."""

    id: str = Field(min_length=1, max_length=160)
    parent_type: str = Field(min_length=1, max_length=64)
    parent_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    mime_type: str | None = Field(default=None, max_length=160)
    size_bytes: int | None = Field(default=None, ge=0)


class Grade(BaseModel):
    """강좌의 공개된 평가 항목과 점수."""

    id: str = Field(min_length=1, max_length=160)
    course_id: str = Field(min_length=1, max_length=160)
    item: str = Field(min_length=1, max_length=300)
    score: str | None = Field(default=None, max_length=80)
    published_at: datetime | None = None
    status: EntityStatus = EntityStatus.UNKNOWN


# 한 ToolResult에 여러 LMS 엔터티 종류가 들어갈 수 있도록 만든 합집합 타입이다.
EclassEntity = Annotated[
    Course | Assignment | Lecture | Announcement | AnnouncementDetails | Attachment | Grade,
    Field(discriminator=None),
]


class ToolResult(BaseModel):
    """MCP 도구의 공통 결과. HTML·쿠키·비밀번호는 허용하지 않는다."""

    # 예기치 않은 html/cookie 같은 필드가 실수로 섞이면 즉시 검증 오류를 낸다.
    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: list[EclassEntity] = Field(default_factory=list)
    source: Literal["live"] = "live"
    fetched_at: datetime = Field(default_factory=utc_now)
    error: ErrorCode | None = None


class EclassCollectionResult(BaseModel):
    """한 번의 LMS 읽기에서 정규화한 결과 묶음."""

    model_config = ConfigDict(extra="forbid")

    courses: list[Course] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)
    lectures: list[Lecture] = Field(default_factory=list)
    announcements: list[Announcement] = Field(default_factory=list)
    grades: list[Grade] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    source: Literal["live"] = "live"
    fetched_at: datetime = Field(default_factory=utc_now)
    error: ErrorCode | None = None


class DocumentAnalysisResult(BaseModel):
    """MarkItDown 변환본을 Qwen이 분석한 뒤 검증하는 결과 계약."""

    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4_000)
    submission_requirements: list[str] = Field(default_factory=list, max_length=50)
    checklist: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_markdown_sha256: str = Field(min_length=64, max_length=64)
    analyzed_at: datetime = Field(default_factory=utc_now)
    error: ErrorCode | None = None


class Mission(BaseModel):
    """결정론적 Mission Service가 생성·조회·완료 처리하는 사용자 작업."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = Field(default=None, ge=1)
    source_type: Literal["ASSIGNMENT", "LECTURE", "MANUAL"]
    source_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    status: Literal["PENDING", "COMPLETED"] = "PENDING"
    priority: Literal["LOW", "NORMAL", "HIGH", "URGENT"] = "NORMAL"
    due_at: datetime | None = None
    completed_at: datetime | None = None


__all__ = [
    "Announcement",
    "AnnouncementDetails",
    "Assignment",
    "Attachment",
    "Course",
    "DocumentAnalysisResult",
    "EclassCollectionResult",
    "EclassEntity",
    "EntityStatus",
    "Grade",
    "Lecture",
    "Mission",
    "ToolResult",
]
