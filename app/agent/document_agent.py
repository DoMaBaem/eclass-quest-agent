"""MarkItDown·Qwen 분석만 담당하는 Document Analysis Agent factory."""

from agents import Agent, Tool

from app.config import Settings
from app.schemas.manager import SpecialistResult

DOCUMENT_AGENT_INSTRUCTIONS = """당신은 Document Analysis Agent다.
- Runtime이 검증한 파일 하나 또는 같은 과제의 검증된 파일 묶음만 분석하며 LMS 탐색·다운로드·제출·수정은 하지 않는다.
- 제공된 analyze_verified_document Tool을 정확히 한 번 호출한다. Tool 입력을 만들거나 바꾸지 않는다.
- Tool 결과에 없는 문서 내용, 요구사항, 체크리스트를 추측하지 않는다.
- Tool이 반환한 상태·요약·evidence_refs를 그대로 SpecialistResult에 옮긴다.
- Tool이 없으면 CAPABILITY_NOT_READY, 변환·분석 실패는 FAILED로 반환한다.
"""


def build_document_agent(settings: Settings, tools: list[Tool] | None = None) -> Agent:
    """MarkItDown·Qwen Tool을 외부에서 주입할 수 있는 Agent를 만든다."""

    return Agent(
        name="Document Analysis Agent",
        instructions=DOCUMENT_AGENT_INSTRUCTIONS,
        tools=list(tools or []),
        output_type=SpecialistResult,
        model=settings.openai_model,
    )
