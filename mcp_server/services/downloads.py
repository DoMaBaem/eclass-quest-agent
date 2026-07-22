"""E-Class 첨부파일을 허용된 임시 루트에만 저장하는 다운로드 서비스."""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from playwright.async_api import async_playwright

from app.config import Settings
from app.guardrails import GuardrailViolation, contained_path, require_eclass_url
from mcp_server.browser.session import load_encrypted_storage_state
from mcp_server.browser.credential_login import automatic_login_available, refresh_encrypted_session
from mcp_server.schemas import DownloadInfo, DownloadResult, McpErrorCode, McpToolError


def _looks_like_html(content: bytes, content_type: str | None) -> bool:
    """로그인 페이지나 viewer wrapper를 파일 본문으로 저장하지 않기 위한 최소 판정."""

    media_type = (content_type or "").split(";", 1)[0].strip().casefold()
    # ``bytes``에는 ``str.casefold()``가 없다. HTML 시그니처는 ASCII이므로
    # 바이트 상태에서 ``lower()``만 적용해도 대소문자 판별에 충분하다.
    prefix = content[:1_024].lstrip().lower()
    return media_type in {"text/html", "application/xhtml+xml"} or prefix.startswith(
        (b"<!doctype html", b"<html")
    )


def _content_matches_filename(filename: str, content: bytes) -> bool:
    """대표 바이너리 확장자가 로그인 HTML 등으로 바뀌지 않았는지 확인한다."""

    suffix = Path(filename).suffix.casefold()
    if not content:
        return False
    if suffix == ".pdf":
        return b"%PDF-" in content[:1_024]
    if suffix in {".docx", ".xlsx", ".pptx", ".zip"}:
        return content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    return True


def _viewer_file_url(
    content: bytes,
    *,
    base_url: str,
    settings: Settings,
) -> str | None:
    """동일 E-Class HTML wrapper 안의 실제 pluginfile URL 하나만 안전하게 추출한다."""

    text = html.unescape(content.decode("utf-8", errors="ignore"))
    candidates = re.findall(
        r"(?:src|href|data)\s*=\s*['\"]([^'\"]+)['\"]",
        text,
        flags=re.IGNORECASE,
    )
    candidates.extend(
        re.findall(
            r"['\"]([^'\"]*/pluginfile\.php[^'\"]*)['\"]",
            text,
            flags=re.IGNORECASE,
        )
    )
    for raw_candidate in candidates:
        candidate = urljoin(base_url, raw_candidate.replace("&amp;", "&"))
        if candidate == base_url:
            continue
        try:
            require_eclass_url(candidate, settings)
        except GuardrailViolation:
            continue
        if urlparse(candidate).path.startswith("/pluginfile.php"):
            return candidate
    return None


class AttachmentDownloadService:
    """서버가 생성한 불투명 ID로만 임시 파일을 참조하게 한다."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def download(
        self,
        attachment_url: str,
        attachment_id: str,
        filename: str,
    ) -> DownloadResult:
        try:
            self.cleanup_expired()
            require_eclass_url(attachment_url, self.settings)
            parsed = urlparse(attachment_url)
            if not parsed.path.startswith(("/pluginfile.php", "/mod/")):
                raise ValueError("허용되지 않은 첨부 URL")
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", attachment_id):
                raise ValueError("첨부 ID 형식 오류")
            safe_name = self._safe_filename(filename)
            download_id = str(uuid4())
            root = self.settings.download_root.resolve()

            state = load_encrypted_storage_state(self.settings)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(storage_state=state)
                try:
                    session_refreshed = False

                    async def fetch(url: str):
                        nonlocal session_refreshed
                        response = await context.request.get(url)
                        if (
                            response.status in {401, 403}
                            and not session_refreshed
                            and automatic_login_available(self.settings)
                        ):
                            await refresh_encrypted_session(self.settings)
                            refreshed_state = load_encrypted_storage_state(self.settings)
                            await context.add_cookies(refreshed_state.get("cookies", []))
                            session_refreshed = True
                            response = await context.request.get(url)
                        return response

                    response = await fetch(attachment_url)
                    if response.status in {401, 403}:
                        return self._failure(
                            McpErrorCode.AUTH_REQUIRED,
                            "E-Class 로그인이 필요합니다.",
                        )
                    if not response.ok:
                        return self._failure(
                            McpErrorCode.NOT_FOUND,
                            "첨부파일을 다운로드할 수 없습니다.",
                        )
                    require_eclass_url(response.url, self.settings)
                    content = await response.body()
                    headers = {key.casefold(): value for key, value in response.headers.items()}
                    content_type = headers.get("content-type")

                    # PDF 등이 브라우저 새 탭에서 열리는 `inline` 응답은 그대로 원본 파일이다.
                    # 반면 HTML viewer/login wrapper는 파일로 저장하지 않고 실제 pluginfile URL을
                    # 한 번만 찾아 동일 호스트·경로 검증 뒤 다시 요청한다.
                    expects_html_file = Path(safe_name).suffix.casefold() in {".html", ".htm"}
                    if _looks_like_html(content, content_type) and not expects_html_file:
                        viewer_url = _viewer_file_url(
                            content,
                            base_url=response.url,
                            settings=self.settings,
                        )
                        if viewer_url is None and not session_refreshed and automatic_login_available(
                            self.settings
                        ):
                            await refresh_encrypted_session(self.settings)
                            refreshed_state = load_encrypted_storage_state(self.settings)
                            await context.add_cookies(refreshed_state.get("cookies", []))
                            session_refreshed = True
                            response = await context.request.get(attachment_url)
                            if response.status in {401, 403}:
                                return self._failure(
                                    McpErrorCode.AUTH_REQUIRED,
                                    "E-Class 로그인이 필요합니다.",
                                )
                            if not response.ok:
                                return self._failure(
                                    McpErrorCode.NOT_FOUND,
                                    "첨부파일을 다운로드할 수 없습니다.",
                                )
                            require_eclass_url(response.url, self.settings)
                            content = await response.body()
                            headers = {
                                key.casefold(): value for key, value in response.headers.items()
                            }
                            content_type = headers.get("content-type")
                            viewer_url = (
                                _viewer_file_url(
                                    content,
                                    base_url=response.url,
                                    settings=self.settings,
                                )
                                if _looks_like_html(content, content_type)
                                else None
                            )
                        if viewer_url is not None:
                            response = await fetch(viewer_url)
                            if response.status in {401, 403}:
                                return self._failure(
                                    McpErrorCode.AUTH_REQUIRED,
                                    "E-Class 로그인이 필요합니다.",
                                )
                            if not response.ok:
                                return self._failure(
                                    McpErrorCode.NOT_FOUND,
                                    "첨부파일 원본을 열 수 없습니다.",
                                )
                            require_eclass_url(response.url, self.settings)
                            content = await response.body()
                            headers = {
                                key.casefold(): value for key, value in response.headers.items()
                            }
                            content_type = headers.get("content-type")
                        if _looks_like_html(content, content_type):
                            return self._failure(
                                McpErrorCode.AUTH_REQUIRED
                                if "login" in response.url.casefold()
                                else McpErrorCode.PARSER_CHANGED,
                                "첨부파일 대신 로그인 또는 미리보기 페이지가 반환되었습니다.",
                            )
                finally:
                    await context.close()
                    await browser.close()
            if len(content) > self.settings.download_max_bytes:
                return self._failure(McpErrorCode.NOT_FOUND, "첨부파일이 허용된 최대 크기를 초과합니다.")
            if not _content_matches_filename(safe_name, content):
                return self._failure(
                    McpErrorCode.PARSER_CHANGED,
                    "첨부파일의 실제 형식이 표시된 파일명과 일치하지 않습니다.",
                )
            # 네트워크 응답과 파일 형식을 모두 검증한 뒤에만 임시 디렉터리를 만든다.
            directory = contained_path(root, root / download_id)
            directory.mkdir(parents=True, exist_ok=False)
            destination = contained_path(root, directory / safe_name)
            destination.write_bytes(content)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=self.settings.download_retention_hours)
            digest = hashlib.sha256(content).hexdigest()
            manifest = {
                "download_id": download_id,
                "attachment_id": attachment_id,
                "filename": safe_name,
                "relative_path": safe_name,
                "sha256": digest,
                "expires_at": expires_at.isoformat(),
            }
            contained_path(root, directory / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            return DownloadResult(
                ok=True,
                data=DownloadInfo(
                    download_id=download_id,
                    attachment_id=attachment_id,
                    filename=safe_name,
                    mime_type=content_type,
                    size_bytes=len(content),
                    sha256=digest,
                    expires_at=expires_at,
                ),
            )
        except Exception:
            return self._failure(McpErrorCode.TEMPORARY_FAILURE, "첨부파일 다운로드에 실패했습니다.")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(filename).name.strip()
        name = re.sub(r"[^0-9A-Za-z가-힣._() -]", "_", name)[:500]
        if not name or name in {".", ".."}:
            raise ValueError("파일 이름 오류")
        return name

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """만료 manifest의 원본·Markdown·디렉터리를 containment 확인 후 함께 삭제한다."""

        root = self.settings.download_root.resolve()
        if not root.exists():
            return 0
        current = now or datetime.now(timezone.utc)
        deleted = 0
        for manifest_path in root.glob("*/manifest.json"):
            try:
                safe_manifest = contained_path(root, manifest_path)
                manifest = json.loads(safe_manifest.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(str(manifest["expires_at"]))
                if expires_at >= current:
                    continue
                directory = contained_path(root, safe_manifest.parent)
                shutil.rmtree(directory)
                deleted += 1
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return deleted

    @staticmethod
    def _failure(code: McpErrorCode, message: str) -> DownloadResult:
        return DownloadResult(ok=False, error=McpToolError(code=code, message=message))
