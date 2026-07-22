"""다운로드 ID만 받아 로컬 문서를 Markdown으로 변환하는 MCP stdio 서버."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from app.guardrails import contained_path
from app.schemas.document import MarkdownConversionResult


settings = get_settings()
mcp = FastMCP(
    "eclass-quest-markitdown",
    instructions="격리된 E-Class 임시 첨부파일만 Markdown으로 변환합니다.",
    log_level="WARNING",
)


@mcp.tool(structured_output=True)
async def convert_download(download_id: str) -> MarkdownConversionResult:
    """임의 경로가 아닌 서버 발급 download_id의 로컬 파일만 변환합니다."""

    if not re.fullmatch(r"[0-9a-f-]{36}", download_id):
        return _failure(download_id, "PATH_OUT_OF_SCOPE", "올바른 다운로드 참조가 아닙니다.")
    try:
        root = settings.download_root.resolve()
        directory = contained_path(root, root / download_id)
        manifest_path = contained_path(root, directory / "manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("download_id") != download_id:
            raise ValueError("manifest 불일치")
        expires_at = datetime.fromisoformat(str(manifest["expires_at"]))
        if expires_at <= datetime.now(timezone.utc):
            return _failure(download_id, "DOWNLOAD_EXPIRED", "첨부파일 보존 기한이 지났습니다.")
        source = contained_path(root, directory / str(manifest["relative_path"]))
        if hashlib.sha256(source.read_bytes()).hexdigest() != manifest.get("sha256"):
            return _failure(download_id, "HASH_MISMATCH", "첨부파일 무결성 검증에 실패했습니다.")
        converted = MarkItDown(enable_plugins=False).convert_local(source)
        markdown = (converted.text_content or "").strip()
        if not markdown:
            return _failure(download_id, "EMPTY_CONVERSION", "문서에서 분석 가능한 텍스트를 찾지 못했습니다.")
        if len(markdown) > 1_000_000:
            return _failure(download_id, "DOCUMENT_TOO_LARGE", "변환 문서가 분석 허용 크기를 초과했습니다.")
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        # 변환본도 같은 보존 기한·격리 디렉터리에 두어 원본과 함께 정리한다.
        contained_path(root, directory / "converted.md").write_text(markdown, encoding="utf-8")
        return MarkdownConversionResult(
            ok=True,
            download_id=download_id,
            markdown=markdown,
            markdown_sha256=digest,
        )
    except Exception:
        return _failure(download_id, "CONVERSION_FAILED", "MarkItDown 문서 변환에 실패했습니다.")


def _failure(download_id: str, code: str, message: str) -> MarkdownConversionResult:
    safe_id = download_id if len(download_id) == 36 else "00000000-0000-0000-0000-000000000000"
    return MarkdownConversionResult(
        ok=False,
        download_id=safe_id,
        error_code=code,
        message=message,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
