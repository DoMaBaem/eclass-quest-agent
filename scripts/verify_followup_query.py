"""생략형 후속 요청이 직전 E-Class 작업 문맥을 이어받는지 실제 경로로 검증한다."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.runtime.assistant_runtime import ProactiveAssistantRuntime
from app.schemas.manager import ManagerStatus, SpecialistAgentName


async def verify() -> None:
    runtime = ProactiveAssistantRuntime(get_settings())
    try:
        first = await runtime.handle_user_request(
            "2026년 1학기 빅데이터프로그래밍 공지사항 알려줘."
        )
        if first.status is not ManagerStatus.COMPLETED:
            raise RuntimeError(f"첫 조회 실패: {first.status.value}/{first.error_code}")
        if SpecialistAgentName.ECLASS not in first.delegated_agents:
            raise RuntimeError("첫 요청이 E-Class Agent로 전달되지 않았습니다.")

        second = await runtime.handle_user_request("날짜순으로 알려줘.")
        if second.status is not ManagerStatus.COMPLETED:
            raise RuntimeError(f"후속 조회 실패: {second.status.value}/{second.error_code}")
        if SpecialistAgentName.ECLASS not in second.delegated_agents:
            raise RuntimeError("후속 요청이 CHAT으로 빠지고 E-Class Agent를 호출하지 않았습니다.")

        print("공지 조회 → 날짜순 후속 요청: E-Class 문맥 연결 성공", flush=True)
        print(f"후속 구조화 근거: {len(second.evidence_refs)}개", flush=True)
    finally:
        await runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(verify())
