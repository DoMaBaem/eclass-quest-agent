"""TUI가 켜진 동안 사용자 요청과 시스템 이벤트를 전달하는 Runtime 계약."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_FORBIDDEN_KEYS = {
    "password", "passwd", "pwd", "login_id", "student_id", "cookie", "cookies",
    "token", "access_token", "refresh_token", "storage_state", "session_state",
}


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().lower().replace("-", "_") in _FORBIDDEN_KEYS
            or _contains_secret_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


class RuntimeEventType(str, Enum):
    USER_REQUEST = "USER_REQUEST"
    STARTUP_BRIEFING = "STARTUP_BRIEFING"
    LMS_CHANGED = "LMS_CHANGED"
    DEADLINE_WARNING = "DEADLINE_WARNING"
    ATTENDANCE_WARNING = "ATTENDANCE_WARNING"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    MANUAL_SYNC_REQUESTED = "MANUAL_SYNC_REQUESTED"


class RuntimeEvent(BaseModel):
    """원문 대화나 인증정보 없이 Runtime이 처리하는 이벤트 한 건."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    event_type: RuntimeEventType
    user_id: str = Field(default="local-user", min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("payload")
    @classmethod
    def reject_secret_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _contains_secret_key(value):
            raise ValueError("Runtime 이벤트에는 인증정보를 포함할 수 없습니다.")
        return value

    @model_validator(mode="after")
    def reject_user_message_in_system_event(self) -> "RuntimeEvent":
        if self.event_type is not RuntimeEventType.USER_REQUEST and "user_message" in self.payload:
            raise ValueError("시스템 이벤트에는 사용자 원문 대화를 포함할 수 없습니다.")
        return self


class ConversationTurn(BaseModel):
    """Manager가 후속 표현을 이해할 때 쓰는, 마스킹된 최근 대화 한 건."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4_000)


class VerifiedEntityKind(str, Enum):
    """MCP가 검증한 후속 대화 대상의 종류.

    자연어 요약 문자열을 다시 파싱하지 않고도 ``1번``, ``그 과제``, ``그 영상``을
    실제 LMS 식별자에 연결하기 위한 Runtime 전용 타입이다.
    """

    COURSE = "COURSE"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    ASSIGNMENT = "ASSIGNMENT"
    LECTURE = "LECTURE"
    ATTACHMENT = "ATTACHMENT"


class VerifiedEntityReference(BaseModel):
    """한 번 이상 MCP Tool 결과로 확인된 LMS 엔터티 참조."""

    model_config = ConfigDict(extra="forbid")

    kind: VerifiedEntityKind
    id: str = Field(min_length=1, max_length=160)
    number: int | None = Field(default=None, ge=1, le=1_000)
    title: str | None = Field(default=None, max_length=500)
    name: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2_000)
    course_id: str | None = Field(default=None, max_length=160)
    course_name: str | None = Field(default=None, max_length=300)
    parent_id: str | None = Field(default=None, max_length=160)
    week: int | None = Field(default=None, ge=1, le=99)
    professor: str | None = Field(default=None, max_length=300)
    mime_type: str | None = Field(default=None, max_length=200)
    posted_at: str | None = Field(default=None, max_length=64)


class VerifiedEntitySnapshot(BaseModel):
    """같은 조회에서 얻은 검증 후보와 학기 범위를 함께 보존한다."""

    model_config = ConfigDict(extra="forbid")

    kind: VerifiedEntityKind
    items: list[VerifiedEntityReference] = Field(default_factory=list, max_length=100)
    year: int | None = Field(default=None, ge=2000, le=2100)
    semester: int | None = Field(default=None, ge=1, le=4)


class AssistantContext(BaseModel):
    """TUI 한 번의 실행 동안 Manager가 다음 요청에 사용할 안전한 문맥."""

    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    safe_summary: str = Field(default="새로운 실행 문맥입니다.", max_length=500)
    # 무제한 원문 대신 비밀값을 마스킹한 최근 대화만 보존한다. Runtime이 최대 12건을 강제한다.
    recent_turns: list[ConversationTurn] = Field(default_factory=list, max_length=12)
    turn_count: int = Field(default=0, ge=0)
    last_request_id: str | None = None
    last_event_id: str | None = None
    last_verified_entity_refs: list[str] = Field(default_factory=list, max_length=100)
    # 종류별 최신 후보를 따로 보존한다. 공지 목록 뒤에 강의 목록을 조회해도 공지 참조가
    # 단일 문자열처럼 덮어써지지 않으며, Runtime만 이 값을 생성·선택할 수 있다.
    verified_entity_snapshots: list[VerifiedEntitySnapshot] = Field(
        default_factory=list,
        max_length=8,
    )
    # 직전 전문 작업의 원문 대화 전체가 아니라, Manager가 만든 안전한 작업 범위만 보존한다.
    # "날짜순으로", "그중 미제출만" 같은 생략형 후속 요청을 해석할 때 사용한다.
    last_specialist_scope: str = Field(default="", max_length=2_000)
    # 이전 버전 및 외부 호출자와의 호환을 위한 마지막 JSON 문맥이다. 새 실행 경로는 위의
    # typed snapshot을 우선하며, 비밀번호·쿠키·HTML 원문은 어느 경계에도 넣지 않는다.
    last_verified_result_summary: str = Field(default="", max_length=12_000)
    last_specialist_agents: list[str] = Field(default_factory=list, max_length=4)
