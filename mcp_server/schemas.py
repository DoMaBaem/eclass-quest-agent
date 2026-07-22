"""E-Class MCP Tool이 외부로 반환하는 구체적인 Pydantic 응답 계약."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.domain import (
    Announcement,
    AnnouncementDetails,
    Assignment,
    Attachment,
    Course,
    Grade,
    Lecture,
    utc_now,
)


class McpErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    PARSER_CHANGED = "PARSER_CHANGED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"


class McpOutcomeStatus(str, Enum):
    """고수준 MCP 작업이 끝난 이유를 Agent가 분기하기 위한 상태.

    기존 ``ok``/``error.code`` 계약은 하위 호환성을 위해 그대로 둔다. 새 업무 단위 Tool은
    이 값을 함께 반환하므로 Agent가 오류 문장을 해석하거나 ID를 추측할 필요가 없다.
    """

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PARSER_CHANGED = "PARSER_CHANGED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    INVALID_REQUEST = "INVALID_REQUEST"


class McpToolError(BaseModel):
    """내부 예외나 선택자를 노출하지 않는 MCP 공통 오류."""

    model_config = ConfigDict(extra="forbid")

    code: McpErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False


class SelectedTerm(BaseModel):
    """E-Class 화면에 실제로 적용된 연도·학기."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=2000, le=2100)
    semester: int = Field(ge=1, le=4)
    selection_source: Literal["eclass_default", "user_request"]
    semester_name: Literal["1학기", "2학기", "여름학기", "겨울학기"] | None = None

    @model_validator(mode="after")
    def fill_semester_name(self) -> "SelectedTerm":
        """학기 코드와 표시명이 항상 일치하도록 모델 생성 시 확정한다."""

        self.semester_name = {
            1: "1학기",
            2: "2학기",
            3: "여름학기",
            4: "겨울학기",
        }[self.semester]
        return self


class McpResponse(BaseModel):
    """모든 E-Class 읽기 응답이 공유하는 메타데이터."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    source: Literal["live"] = "live"
    fetched_at: datetime = Field(default_factory=utc_now)
    selected_term: SelectedTerm | None = None
    error: McpToolError | None = None


class SemanticMcpResponse(McpResponse):
    """과목명·주차 같은 사용자 표현을 처리하는 고수준 Tool의 공통 응답."""

    status: McpOutcomeStatus


class SessionInfo(BaseModel):
    authenticated: bool
    auto_login_enabled: bool


class SessionCheckResult(McpResponse):
    data: SessionInfo | None = None


class CourseListResult(McpResponse):
    data: list[Course] = Field(default_factory=list)


class DashboardSnapshotData(BaseModel):
    """E-Class 기본 학기의 동기화 원본을 한 번에 전달하는 구조화 묶음.

    새 항목 판정, 마감 계산과 체크리스트 생성은 이전 MySQL Snapshot을 가진 앱의
    책임이다. MCP는 화면에서 확인한 현재 사실만 반환하며 첨부파일 본문을 내려받지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    courses: list[Course] = Field(default_factory=list)
    announcements: list[Announcement] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)
    lectures: list[Lecture] = Field(default_factory=list)
    grades: list[Grade] = Field(default_factory=list)


class DashboardSnapshotResult(SemanticMcpResponse):
    """동일한 E-Class 기본 학기에서 모두 성공했을 때만 완성되는 Snapshot."""

    data: DashboardSnapshotData | None = None

    @model_validator(mode="after")
    def validate_snapshot_envelope(self) -> "DashboardSnapshotResult":
        """성공·상태·학기·데이터·오류가 모순된 Snapshot을 경계에서 거부한다."""

        if self.ok:
            if (
                self.status is not McpOutcomeStatus.FOUND
                or self.selected_term is None
                or self.data is None
                or self.error is not None
            ):
                raise ValueError("성공한 Dashboard Snapshot 계약이 일치하지 않습니다.")
            return self

        if self.data is not None or self.error is None:
            raise ValueError("실패한 Dashboard Snapshot에는 오류만 포함해야 합니다.")
        expected_status = {
            McpErrorCode.INVALID_REQUEST: McpOutcomeStatus.INVALID_REQUEST,
            McpErrorCode.AUTH_REQUIRED: McpOutcomeStatus.AUTH_REQUIRED,
            McpErrorCode.NOT_FOUND: McpOutcomeStatus.NOT_FOUND,
            McpErrorCode.AMBIGUOUS_MATCH: McpOutcomeStatus.AMBIGUOUS,
            McpErrorCode.PARSER_CHANGED: McpOutcomeStatus.PARSER_CHANGED,
            McpErrorCode.TEMPORARY_FAILURE: McpOutcomeStatus.TEMPORARY_FAILURE,
        }[self.error.code]
        if self.status is not expected_status:
            raise ValueError("Dashboard Snapshot 오류 상태가 오류 코드와 일치하지 않습니다.")
        return self


class CourseResolution(BaseModel):
    """사용자 강좌 표현을 실제 E-Class 강좌 후보와 연결한 결과."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300)
    status: Literal["MATCHED", "AMBIGUOUS", "NOT_FOUND"]
    course: Course | None = None
    candidates: list[Course] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_resolution_contract(self) -> "CourseResolution":
        if self.status == "MATCHED" and self.course is None:
            raise ValueError("MATCHED 강좌 해석에는 course가 필요합니다.")
        if self.status != "MATCHED" and self.course is not None:
            raise ValueError("MATCHED가 아닌 강좌 해석에는 course를 넣을 수 없습니다.")
        if self.status == "AMBIGUOUS" and len(self.candidates) < 2:
            raise ValueError("AMBIGUOUS 강좌 해석에는 둘 이상의 후보가 필요합니다.")
        if self.status == "NOT_FOUND" and self.candidates:
            raise ValueError("NOT_FOUND 강좌 해석에는 후보를 넣을 수 없습니다.")
        return self


class CourseResolutionResult(McpResponse):
    data: CourseResolution | None = None


class AnnouncementListResult(McpResponse):
    data: list[Announcement] = Field(default_factory=list)


class VerifiedCourseReference(BaseModel):
    """실제 E-Class 강좌 목록에서 확인한 뒤 만든 읽기 전용 강좌 참조."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["verified_course"] = "verified_course"
    course_id: str = Field(min_length=1, max_length=160)
    course_name: str = Field(min_length=1, max_length=300)
    professor: str | None = Field(default=None, max_length=160)
    year: int = Field(ge=2000, le=2100)
    semester: int = Field(ge=1, le=4)


class CourseAnnouncementData(BaseModel):
    """검증된 강좌와 그 강좌에서 직접 읽은 공지 목록."""

    model_config = ConfigDict(extra="forbid")

    course: VerifiedCourseReference
    announcements: list[Announcement] = Field(default_factory=list)


class CourseAnnouncementResult(SemanticMcpResponse):
    data: CourseAnnouncementData | None = None
    candidates: list[Course] = Field(default_factory=list, max_length=20)


class AnnouncementDetailsResult(McpResponse):
    data: AnnouncementDetails | None = None


class AssignmentListResult(McpResponse):
    data: list[Assignment] = Field(default_factory=list)


class CourseAssignmentData(BaseModel):
    """검증된 강좌와 그 강좌에서 직접 읽은 과제 목록."""

    model_config = ConfigDict(extra="forbid")

    course: VerifiedCourseReference
    assignments: list[Assignment] = Field(default_factory=list)


class CourseAssignmentResult(SemanticMcpResponse):
    data: CourseAssignmentData | None = None
    candidates: list[Course] = Field(default_factory=list, max_length=20)


class AssignmentDetailsResult(McpResponse):
    data: Assignment | None = None


class AttachmentListResult(McpResponse):
    data: list[Attachment] = Field(default_factory=list)


class LectureListResult(McpResponse):
    data: list[Lecture] = Field(default_factory=list)


class CourseLectureData(BaseModel):
    """검증된 강좌와 선택 조건을 적용한 강의 목록."""

    model_config = ConfigDict(extra="forbid")

    course: VerifiedCourseReference
    requested_week: int | None = Field(default=None, ge=1, le=99)
    lectures: list[Lecture] = Field(default_factory=list)


class CourseLectureResult(SemanticMcpResponse):
    data: CourseLectureData | None = None
    candidates: list[Course] = Field(default_factory=list, max_length=20)


class VerifiedLectureTarget(BaseModel):
    """서버가 강좌·주차·제목을 대조해 단 하나로 확정한 재생 대상.

    ``reference_id``는 MCP 서버 메모리의 검증 레코드를 가리킨다. 재생 Tool은 모델이 복사한
    ``lecture_id`` 대신 이 불투명 참조를 검증한 뒤 실제 ID를 내부에서 사용한다.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["verified_lecture"] = "verified_lecture"
    reference_id: str = Field(min_length=36, max_length=36)
    lecture_id: str = Field(min_length=1, max_length=160)
    course_id: str = Field(min_length=1, max_length=160)
    course_name: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    week: int | None = Field(default=None, ge=1, le=99)
    year: int = Field(ge=2000, le=2100)
    semester: int = Field(ge=1, le=4)
    verified_at: datetime
    expires_at: datetime


class LectureResolutionResult(SemanticMcpResponse):
    data: VerifiedLectureTarget | None = None
    course_candidates: list[Course] = Field(default_factory=list, max_length=20)
    candidates: list[Lecture] = Field(default_factory=list, max_length=50)


class LectureStatusResult(McpResponse):
    data: Lecture | None = None


class PlaybackInfo(BaseModel):
    """실행 중인 headed Chromium 영상 세션의 공개 상태."""

    model_config = ConfigDict(extra="forbid")

    playback_id: str = Field(min_length=36, max_length=36)
    lecture_id: str = Field(min_length=1, max_length=160)
    status: Literal["PLAYING", "STOPPED", "TIMED_OUT", "FAILED"]
    volume_percent: int = Field(default=100, ge=0, le=100)
    playback_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    window_width: int | None = Field(default=None, ge=640, le=3840)
    window_height: int | None = Field(default=None, ge=480, le=2160)
    started_at: datetime
    finished_at: datetime | None = None


class PlaybackResult(McpResponse):
    data: PlaybackInfo | None = None


class VerifiedPlaybackResult(SemanticMcpResponse):
    """검증 참조를 통해 시작한 재생 결과."""

    target: VerifiedLectureTarget | None = None
    data: PlaybackInfo | None = None


class DownloadInfo(BaseModel):
    """실제 경로 대신 Document Agent가 사용할 일회성 다운로드 참조."""

    model_config = ConfigDict(extra="forbid")

    download_id: str = Field(min_length=36, max_length=36)
    attachment_id: str = Field(min_length=1, max_length=160)
    filename: str = Field(min_length=1, max_length=500)
    mime_type: str | None = Field(default=None, max_length=160)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    expires_at: datetime


class DownloadResult(McpResponse):
    data: DownloadInfo | None = None


class GradeListResult(McpResponse):
    data: list[Grade] = Field(default_factory=list)


__all__ = [
    "AnnouncementDetailsResult",
    "AnnouncementListResult",
    "CourseAnnouncementData",
    "CourseAnnouncementResult",
    "AssignmentDetailsResult",
    "AssignmentListResult",
    "CourseAssignmentData",
    "CourseAssignmentResult",
    "AttachmentListResult",
    "CourseListResult",
    "DashboardSnapshotData",
    "DashboardSnapshotResult",
    "CourseResolution",
    "CourseResolutionResult",
    "CourseLectureData",
    "CourseLectureResult",
    "GradeListResult",
    "LectureListResult",
    "LectureResolutionResult",
    "LectureStatusResult",
    "McpOutcomeStatus",
    "PlaybackInfo",
    "PlaybackResult",
    "DownloadInfo",
    "DownloadResult",
    "McpErrorCode",
    "McpResponse",
    "McpToolError",
    "SemanticMcpResponse",
    "SelectedTerm",
    "SessionCheckResult",
    "SessionInfo",
    "VerifiedCourseReference",
    "VerifiedLectureTarget",
    "VerifiedPlaybackResult",
]
