"""Runtime 내부 진행 상황을 TUI에 전달하는 공개 이벤트."""

from __future__ import annotations

from enum import Enum


class RuntimeProgressEvent(str, Enum):
    RUNTIME_STARTED = "RUNTIME_STARTED"
    MANAGER_STARTED = "MANAGER_STARTED"
    AGENT_DELEGATED = "AGENT_DELEGATED"
    PROACTIVE_ALERT = "PROACTIVE_ALERT"
    NO_ACTION = "NO_ACTION"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPABILITY_NOT_READY = "CAPABILITY_NOT_READY"
    ERROR = "ERROR"
