"""MarkItDown 변환본을 로컬 Ollama Qwen으로 구조화 분석한다."""

from __future__ import annotations

import json
import re

import httpx

from app.config import Settings
from app.schemas.document import QwenDocumentPayload
from app.schemas.domain import DocumentAnalysisResult
from app.schemas.workflow import ErrorCode


class QwenDocumentAnalyzer:
    """Qwen이 반환한 JSON을 신뢰하지 않고 Pydantic으로 다시 검증한다."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def analyze(
        self,
        *,
        attachment_id: str,
        markdown: str,
        markdown_sha256: str,
    ) -> DocumentAnalysisResult:
        prompt = (
            "다음 과제 문서를 한국어로 분석하라. 문서에 없는 사실은 만들지 말고, "
            "설명이나 Markdown 없이 JSON 객체 하나만 반환하라. 키는 summary(문자열), "
            "submission_requirements(문자열 배열), checklist(문자열 배열), "
            "confidence(0~1 숫자)만 사용한다.\n\n"
            + markdown[:120_000]
        )
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                str(self.settings.ollama_url),
                json={
                    "model": "qwen3:0.6b",
                    "stream": False,
                    # 일부 Ollama grammar backend는 Pydantic의 복잡한 JSON Schema 제약을
                    # 해석하지 못하므로 JSON mode로 생성한 뒤 아래에서 엄격하게 검증한다.
                    "format": "json",
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
        content = str(response.json().get("message", {}).get("content", ""))
        content = re.sub(r"(?is)<think>.*?</think>", "", content).strip()
        payload = QwenDocumentPayload.model_validate(json.loads(content))
        low_confidence = payload.confidence < 0.55
        return DocumentAnalysisResult(
            attachment_id=attachment_id,
            summary=payload.summary,
            submission_requirements=payload.submission_requirements,
            checklist=payload.checklist,
            confidence=payload.confidence,
            source_markdown_sha256=markdown_sha256,
            error=ErrorCode.LOW_CONFIDENCE if low_confidence else None,
        )
