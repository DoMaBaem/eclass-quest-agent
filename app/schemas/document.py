"""MarkItDown MCP와 Qwen Tool 사이의 구조화 문서 계약."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MarkdownConversionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    download_id: str = Field(min_length=36, max_length=36)
    markdown: str = Field(default="", max_length=1_000_000)
    markdown_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    error_code: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=500)


class QwenDocumentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4_000)
    submission_requirements: list[str] = Field(default_factory=list, max_length=50)
    checklist: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(ge=0, le=1)

