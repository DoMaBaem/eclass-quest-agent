"""향후 LMS 상태 변경 Tool용 human-in-the-loop 승인 저장·재개 경계."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import PendingApprovalModel, WorkflowRunModel


MUTATING_TOOLS_REQUIRING_APPROVAL = {
    "submit_assignment",
    "delete_submission",
    "edit_announcement",
    "delete_announcement",
}


class ApprovalGate:
    """민감하지 않은 재개 payload와 동일 request_id만 저장한다."""

    def __init__(self, session: AsyncSession, *, user_id: str) -> None:
        self.session = session
        self.user_id = user_id

    @staticmethod
    def validate_registration(tool_name: str, *, needs_approval: bool) -> None:
        """상태 변경 Tool이 승인 표시 없이 등록되는 것을 시작 시점에 차단한다."""

        if tool_name in MUTATING_TOOLS_REQUIRING_APPROVAL and not needs_approval:
            raise ValueError(f"{tool_name}은 needs_approval=True가 필요합니다.")

    async def suspend(
        self,
        *,
        request_id: str,
        tool_name: str,
        payload: dict[str, object],
        expires_in_minutes: int = 30,
    ) -> str:
        """workflow를 WAITING_APPROVAL로 바꾸고 재개 토큰을 반환한다."""

        self.validate_registration(tool_name, needs_approval=True)
        run = await self.session.get(WorkflowRunModel, request_id)
        if run is None or run.user_id != self.user_id:
            raise ValueError("승인할 workflow run을 찾을 수 없습니다.")
        row = PendingApprovalModel(
            request_id=request_id,
            user_id=self.user_id,
            tool_name=tool_name,
            payload=dict(payload),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes),
        )
        self.session.add(row)
        run.status = "WAITING_APPROVAL"
        await self.session.flush()
        return row.id

    async def resume(self, approval_id: str, *, approved: bool) -> tuple[str, str, dict[str, object]]:
        """승인 상태를 한 번만 결정하고 원래 request_id와 payload를 반환한다."""

        row = await self.session.scalar(
            select(PendingApprovalModel)
            .where(PendingApprovalModel.id == approval_id)
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if row is None or row.user_id != self.user_id:
            raise ValueError("승인 요청을 찾을 수 없습니다.")
        expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if row.status != "WAITING_APPROVAL" or expires_at <= now:
            raise ValueError("승인 요청이 만료됐거나 이미 처리됐습니다.")
        row.status = "APPROVED" if approved else "REJECTED"
        row.decided_at = now
        run = await self.session.get(WorkflowRunModel, row.request_id)
        if run is not None:
            run.status = "RUNNING" if approved else "CANCELLED"
        await self.session.flush()
        return row.request_id, row.tool_name, dict(row.payload)

