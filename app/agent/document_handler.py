"""Document Agent 단계에서 MarkItDown MCP와 Qwen Tool을 순서대로 실행한다."""

from __future__ import annotations

import sys
from pathlib import Path

from agents import Runner, function_tool, set_default_openai_key
from agents.mcp import MCPServerStdio

from app.agent.document_agent import build_document_agent
from app.agent.errors import OpenAiApiKeyRequiredError
from app.agent.run_config import privacy_safe_run_config
from app.config import Settings
from app.document_analysis import QwenDocumentAnalyzer
from app.schemas.document import MarkdownConversionResult
from app.schemas.manager import (
    ManagerTask,
    SpecialistResult,
    SpecialistStatus,
    parse_verified_download_ref,
)
from app.schemas.workflow import ErrorCode


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DocumentSpecialistHandler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.analyzer = QwenDocumentAnalyzer(settings)
        self._trace_events: list[tuple[str, str]] = []

    async def __call__(self, task: ManagerTask) -> SpecialistResult:
        self._trace_events = []
        # instruction은 Manager가 만든 자연어라 실행 권한으로 사용하지 않는다. Runtime이 직전
        # E-Class 다운로드 결과에서 채운 typed 참조만 Document 파이프라인에 전달한다.
        if not 1 <= len(task.verified_input_refs) <= 5:
            return SpecialistResult(
                status=SpecialistStatus.CAPABILITY_NOT_READY,
                summary="분석할 첨부파일의 검증된 다운로드 참조가 없습니다.",
                suggested_actions=["과제 첨부파일을 먼저 E-Class에서 선택해 다운로드하세요."],
            )
        parsed_refs = [parse_verified_download_ref(ref) for ref in task.verified_input_refs]
        if any(parsed is None for parsed in parsed_refs):
            return SpecialistResult(
                status=SpecialistStatus.CAPABILITY_NOT_READY,
                summary="분석할 첨부파일의 검증된 다운로드 참조가 올바르지 않습니다.",
                suggested_actions=["과제 첨부파일을 다시 E-Class에서 선택해 다운로드하세요."],
            )
        verified_refs = [parsed for parsed in parsed_refs if parsed is not None]
        attachment_ids = [attachment_id for _, attachment_id in verified_refs]
        if len(attachment_ids) != len(set(attachment_ids)):
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="같은 첨부파일의 다운로드 참조가 중복되었습니다.",
                error_code=ErrorCode.INVALID_REQUEST,
            )
        expected_ids = (
            task.verified_attachment_ids
            if task.verified_attachment_ids
            else (
                [task.verified_attachment_id]
                if task.verified_attachment_id is not None
                else []
            )
        )
        if expected_ids and attachment_ids != expected_ids:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="검증된 첨부파일 순서와 다운로드 결과가 일치하지 않습니다.",
                error_code=ErrorCode.INVALID_REQUEST,
            )

        if not self.settings.openai_api_key or self.settings.openai_api_key == "...":
            raise OpenAiApiKeyRequiredError("./run.sh --setup에서 OpenAI API 키를 설정하세요.")
        set_default_openai_key(self.settings.openai_api_key)

        # Agent에게 식별자를 다시 전달시키지 않는다. 인자가 없는 고수준 Tool이 클로저에 들어 있는
        # 검증된 download_id만 사용하므로 파일 ID 변형·환각이 실행 경계를 넘지 못한다.
        verified_result: SpecialistResult | None = None

        names_by_id = {
            target.id: target.name for target in task.verified_attachment_targets
        }
        if task.verified_attachment_target is not None:
            names_by_id[task.verified_attachment_target.id] = task.verified_attachment_target.name

        @function_tool(
            name_override="analyze_verified_document",
            description_override=(
                "Runtime이 검증한 문서 하나 또는 같은 과제의 문서 묶음을 "
                "MarkItDown과 Qwen으로 분석한다."
            ),
        )
        async def analyze_verified_document() -> str:
            nonlocal verified_result
            if verified_result is None:
                verified_result = await self._run_verified_batch(
                    verified_refs,
                    names_by_id=names_by_id,
                )
            return verified_result.model_dump_json()

        try:
            run_result = await Runner.run(
                build_document_agent(self.settings, tools=[analyze_verified_document]),
                "검증된 문서를 analyze_verified_document Tool로 정확히 한 번 분석하세요.",
                max_turns=3,
                run_config=privacy_safe_run_config(),
            )
        except OpenAiApiKeyRequiredError:
            raise
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="Document Analysis Agent 실행에 실패했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        if verified_result is None:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="Document Analysis Agent가 검증된 문서 Tool을 실행하지 않았습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        # 자연어 재작성 결과가 아니라 고수준 Tool이 확정한 결과를 최종 사실로 사용한다.
        if run_result.final_output is None:
            return verified_result
        return verified_result

    async def _run_verified_batch(
        self,
        verified_refs: list[tuple[str, str]],
        *,
        names_by_id: dict[str, str],
    ) -> SpecialistResult:
        """검증된 파일들을 순서대로 변환·분석하고 파일명별 결과를 합친다."""

        results: list[tuple[str, SpecialistResult]] = []
        for download_id, attachment_id in verified_refs:
            result = await self._run_verified_pipeline(download_id, attachment_id)
            results.append((names_by_id.get(attachment_id, attachment_id), result))

        completed = [result for _, result in results if result.status is SpecialistStatus.COMPLETED]
        if not completed:
            first_name, first_result = results[0]
            return first_result.model_copy(
                update={
                    "summary": f"[{first_name}]\n{first_result.summary}"[:2_000],
                    "verified_display_text": (
                        f"[{first_name}]\n"
                        f"{first_result.verified_display_text or first_result.summary}"
                    ),
                }
            )

        blocks: list[str] = []
        evidence_refs: list[str] = []
        suggested_actions: list[str] = []
        for name, result in results:
            body = result.verified_display_text or result.summary
            if result.status is SpecialistStatus.COMPLETED:
                blocks.append(f"[{name}]\n{body}")
                evidence_refs.extend(result.evidence_refs)
            else:
                blocks.append(f"[{name}]\n분석 실패: {body}")
            suggested_actions.extend(result.suggested_actions)
        display = "\n\n".join(blocks)
        return SpecialistResult(
            status=SpecialistStatus.COMPLETED,
            summary=display[:2_000],
            evidence_refs=evidence_refs,
            suggested_actions=list(dict.fromkeys(suggested_actions)),
            verified_display_text=display,
        )

    async def _run_verified_pipeline(
        self,
        download_id: str,
        attachment_id: str,
    ) -> SpecialistResult:
        """검증된 파일을 MarkItDown→Qwen 순서로 한 번만 처리한다."""

        server = MCPServerStdio(
            params={
                "command": sys.executable,
                "args": ["-m", "document_mcp_server.server"],
                "cwd": PROJECT_ROOT,
            },
            name="MarkItDown MCP",
            use_structured_content=True,
            require_approval="never",
            client_session_timeout_seconds=180,
        )
        try:
            async with server:
                tool_result = await server.call_tool("convert_download", {"download_id": download_id})
            converted = MarkdownConversionResult.model_validate(tool_result.structuredContent)
            self._trace_events.append(("MarkItDown MCP.convert_download", "COMPLETED" if converted.ok else "FAILED"))
            if not converted.ok or not converted.markdown_sha256:
                return SpecialistResult(
                    status=SpecialistStatus.FAILED,
                    summary=converted.message or "문서 변환에 실패했습니다.",
                    error_code=ErrorCode.DOCUMENT_CONVERSION_FAILED,
                )
            analysis = await self.analyzer.analyze(
                attachment_id=attachment_id,
                markdown=converted.markdown,
                markdown_sha256=converted.markdown_sha256,
            )
            self._trace_events.append(("Ollama qwen3:0.6b", "COMPLETED"))
        except Exception:
            return SpecialistResult(
                status=SpecialistStatus.FAILED,
                summary="MarkItDown 또는 Qwen 문서 분석에 실패했습니다.",
                error_code=ErrorCode.TEMPORARY_FAILURE,
            )
        warning = "\n분석 신뢰도가 낮아 원문 확인이 필요합니다." if analysis.error else ""
        requirements = "\n".join(f"- {item}" for item in analysis.submission_requirements)
        checklist = "\n".join(f"- {item}" for item in analysis.checklist)
        display = f"{analysis.summary}\n\n제출 요구사항\n{requirements}\n\n체크리스트\n{checklist}{warning}"
        return SpecialistResult(
            status=SpecialistStatus.COMPLETED,
            summary=display[:2_000],
            evidence_refs=[f"document:{analysis.attachment_id}:{analysis.source_markdown_sha256}"],
            verified_display_text=display,
            error_code=analysis.error,
        )

    def consume_trace_events(self) -> list[tuple[str, str]]:
        events, self._trace_events = self._trace_events, []
        return events
