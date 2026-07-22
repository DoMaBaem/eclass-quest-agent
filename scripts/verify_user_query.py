"""TUI와 동일한 Manager → E-Class Agent → MCP 전체 경로를 검증한다."""

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
        result = await runtime.handle_user_request(
            "E-Class 공지사항 새로 올라온 거 없나? "
            "2026년 1학기에 수강한 빅데이터프로그래밍 말이야."
        )
        if result.status is not ManagerStatus.COMPLETED:
            raise RuntimeError(f"전체 경로 검증 실패: {result.status.value}/{result.error_code}")
        if SpecialistAgentName.ECLASS not in result.delegated_agents:
            raise RuntimeError("Manager가 E-Class Agent를 호출하지 않았습니다.")
        print("Manager → E-Class Agent → MCP: 성공", flush=True)
        print(f"구조화 근거: {len(result.evidence_refs)}개", flush=True)
    finally:
        await runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(verify())
