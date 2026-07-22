"""OpenAI Agents SDK E-Class Agent가 로컬 MCP stdio Tool을 실제 호출하는지 검증한다."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.eclass_mcp_handler import EclassMcpSpecialistHandler
from app.config import get_settings
from app.schemas.manager import ManagerTask, SpecialistAgentName, SpecialistStatus
from app.schemas.workflow import CapabilityCode


async def verify() -> None:
    task = ManagerTask(
        agent=SpecialistAgentName.ECLASS,
        capability=CapabilityCode.ECLASS_QUERY,
        instruction=(
            "2026년 1학기 수강 강좌 중 빅데이터프로그래밍 강좌를 찾고, "
            "해당 강좌에 최근 공지사항이 있는지 E-Class MCP로 확인한다."
        ),
    )
    result = await EclassMcpSpecialistHandler(get_settings())(task)
    if result.status is not SpecialistStatus.COMPLETED:
        raise RuntimeError(f"E-Class Agent 검증 실패: {result.status.value}/{result.error_code}")
    print("E-Class Agent: MCP Tool 연결 성공", flush=True)
    print(f"구조화 근거: {len(result.evidence_refs)}개", flush=True)


if __name__ == "__main__":
    asyncio.run(verify())
