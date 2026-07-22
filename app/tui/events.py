"""Workflow/MCP에서 TUI로 공개해도 되는 이벤트 계약.

Agent의 내부 추론, Tool 인자, 쿠키는 이벤트에 넣지 않는다. UI는 아래 타입과 사용자용 message만
받아 화면을 바꾸며, 실제 asyncio Queue/Textual Message 연결은 후속 단계에서 추가한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class UiEventType(str, Enum):
    """화면이 처리할 수 있는 고수준 진행 상태."""

    RUNTIME_STARTED = "RUNTIME_STARTED"
    MANAGER_STARTED = "MANAGER_STARTED"
    AGENT_DELEGATED = "AGENT_DELEGATED"
    PROACTIVE_ALERT = "PROACTIVE_ALERT"
    NO_ACTION = "NO_ACTION"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPABILITY_NOT_READY = "CAPABILITY_NOT_READY"
    ERROR = "ERROR"


class UiOperationState(str, Enum):
    """TUI 상단 작업 표시줄에 공개하는 사용자 관점의 실행 상태.

    내부 Agent의 추론 단계나 Tool 인자는 화면에 노출하지 않는다. 대신 사용자가 현재 앱이
    동기화 중인지, 자신의 요청을 처리 중인지, 영상이 재생 중인지처럼 행동을 판단하는 데
    필요한 상태만 고정된 값으로 표시한다.
    """

    READY = "READY"
    SYNCING = "SYNCING"
    PROACTIVE_ALERT = "PROACTIVE_ALERT"
    USER_TASK = "USER_TASK"
    PLAYBACK = "PLAYBACK"
    AUTH_REQUIRED = "AUTH_REQUIRED"


class UiEvent(BaseModel):
    """요청 ID와 발생 시각을 포함하는 단일 UI 이벤트."""

    event_type: UiEventType
    request_id: str
    message: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
