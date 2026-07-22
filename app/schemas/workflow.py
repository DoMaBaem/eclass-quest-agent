"""Agent Host, 각 Agent, TUI 사이에서 사용하는 실행 상태 계약.

Agent가 임의의 dict를 주고받게 두지 않고 Pydantic으로 필드와 길이를 제한한다. 이 계약 덕분에
누락·오타를 일찍 발견하고, 전체 대화 원문이나 인증 정보가 실행 대상에 섞이는 범위를 줄인다.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_FORBIDDEN_MANAGER_PAYLOAD_KEYS = {
    "password", "passwd", "pwd", "login_id", "student_id", "cookie", "cookies",
    "token", "access_token", "refresh_token", "storage_state", "session_state",
}


def _payload_contains_secret(value: object) -> bool:
    """Manager에 전달할 중첩 payload에서 인증정보 키를 찾는다."""

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_MANAGER_PAYLOAD_KEYS or _payload_contains_secret(nested):
                return True
    elif isinstance(value, list):
        return any(_payload_contains_secret(item) for item in value)
    return False


class CapabilityCode(str, Enum):
    """Manager가 전문 Agent 또는 결정론적 Service에 맡길 기능의 고정 목록."""

    ECLASS_QUERY = "ECLASS_QUERY"
    VIDEO_PLAY = "VIDEO_PLAY"
    DOCUMENT_ANALYSIS = "DOCUMENT_ANALYSIS"
    MISSION_MANAGEMENT = "MISSION_MANAGEMENT"


class InteractionMode(str, Enum):
    """Manager가 직접 답할지 전문 기능으로 연결할지 나타낸다."""

    CHAT = "CHAT"
    TASK = "TASK"


class ErrorCode(str, Enum):
    """UI와 로그가 문장 대신 안정적으로 구분할 수 있는 오류 코드."""

    AUTH_REQUIRED = "AUTH_REQUIRED"
    OPENAI_API_KEY_REQUIRED = "OPENAI_API_KEY_REQUIRED"
    INVALID_REQUEST = "INVALID_REQUEST"
    MANAGER_FAILED = "MANAGER_FAILED"
    WORKFLOW_LIMIT_REACHED = "WORKFLOW_LIMIT_REACHED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    DOCUMENT_CONVERSION_FAILED = "DOCUMENT_CONVERSION_FAILED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


class ManagerInputEvent(BaseModel):
    """ChangeEvent를 Manager에 전달할 때 사용하는 검증된 구조화 입력."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=36)
    event_type: Literal["LMS_CHANGED"] = "LMS_CHANGED"
    change_type: str = Field(min_length=1, max_length=32)
    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any]
    created_at: datetime

    @field_validator("payload")
    @classmethod
    def reject_secret_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """인증정보가 ChangeEvent를 통해 모델 입력으로 넘어가는 것을 차단한다."""

        if _payload_contains_secret(value):
            raise ValueError("Manager 이벤트 payload에는 인증정보를 포함할 수 없습니다.")
        return value
