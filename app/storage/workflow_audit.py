"""비밀 원문 없이 Agent·Guardrail·오류 상태를 로컬 JSONL에 기록한다."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from app.config import Settings


class WorkflowAuditService:
    """감사 기록 실패가 실제 사용자 작업을 깨뜨리지 않도록 호출부와 분리된 저장 서비스."""

    def __init__(self, settings: Settings, *, user_id: str = "local-user") -> None:
        self.settings = settings
        self.user_id = user_id

    async def start(self, request_id: str, *, trigger_type: str, event_id: str | None = None) -> None:
        self._append_local(request_id, "Runtime", "STARTED", trigger_type, event_id=event_id)

    async def step(self, request_id: str, *, component: str, state: str, code: str | None = None) -> None:
        self._append_local(request_id, component, state, code)

    async def finish(self, request_id: str, *, status: str, error_code: str | None = None) -> None:
        self._append_local(request_id, "Runtime", status, error_code)

    def _append_local(
        self,
        request_id: str,
        component: str,
        state: str,
        code: str | None,
        *,
        event_id: str | None = None,
    ) -> None:
        """DB 연결과 무관한 비밀 없는 JSONL 추적을 남긴다."""

        path = self.settings.workflow_audit_path
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "event_id": event_id,
            "component": component[:100],
            "state": state[:48],
            "code": code[:64] if code else None,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
