"""배포 구성요소를 실제 경계에서 점검하고 비밀정보 없는 JSON 진단 결과를 출력한다.

기본 검사는 MySQL, Playwright Chromium, 두 stdio MCP, Ollama에 실제로 연결한다. OpenAI와
E-Class는 외부 요청·비용·개인정보 경계를 가지므로 각각 명시적 옵션을 준 경우에만 연결한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI
from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings, get_settings


ECLASS_TOOLS = {
    "check_session",
    "list_courses",
    "get_dashboard_snapshot",
    "resolve_course",
    "list_announcements",
    "list_course_announcements",
    "get_announcement_details",
    "list_assignments",
    "list_course_assignments",
    "get_assignment_details",
    "list_assignment_attachments",
    "list_lectures",
    "list_course_lectures",
    "resolve_lecture",
    "get_lecture_status",
    "get_grades",
    "play_lecture",
    "play_resolved_lecture",
    "preview_resolved_lecture",
    "stop_lecture",
    "preview_lecture",
    "download_attachment",
}
DOCUMENT_TOOLS = {"convert_download"}


@dataclass(frozen=True)
class CheckResult:
    component: str
    status: str
    error_code: str | None
    duration_ms: int
    detail: str


def emit(result: CheckResult) -> None:
    """Secret이나 개인 데이터가 없는 고정 스키마 JSON 한 줄을 출력한다."""

    print(json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":")), flush=True)


async def check(
    component: str,
    error_code: str,
    operation: Callable[[], Awaitable[str]],
) -> CheckResult:
    started = time.monotonic()
    try:
        detail = await asyncio.wait_for(operation(), timeout=45)
        return CheckResult(component, "PASS", None, int((time.monotonic() - started) * 1000), detail)
    except asyncio.CancelledError:
        return CheckResult(
            component,
            "FAIL",
            error_code,
            int((time.monotonic() - started) * 1000),
            "CancelledError",
        )
    except Exception as exc:
        # 예외 메시지에는 URL이나 응답 본문이 섞일 수 있으므로 타입만 남긴다.
        return CheckResult(
            component,
            "FAIL",
            error_code,
            int((time.monotonic() - started) * 1000),
            type(exc).__name__,
        )


def skipped(component: str, reason: str) -> CheckResult:
    return CheckResult(component, "SKIP", reason, 0, "명시적 실행 옵션이 필요합니다.")


async def check_runtime(settings: Settings) -> str:
    if not settings.mysql_url:
        raise RuntimeError("MYSQL_URL missing")
    if str(settings.eclass_base_url).rstrip("/") != "https://learn.hansung.ac.kr":
        raise RuntimeError("unexpected E-Class origin")
    __import__("app.main")
    return "model_configured=true"


async def check_database(settings: Settings) -> str:
    # asyncmy의 연결 timeout/cancellation이 같은 event loop의 후속 검사를 취소하지 않도록
    # DB probe만 짧은 별도 프로세스에서 실행한다.
    if not settings.mysql_url:
        raise RuntimeError("MYSQL_URL missing")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_ROOT / "scripts/db_health_probe.py"),
        cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("DB probe timeout")
    if process.returncode != 0:
        raise RuntimeError("DB probe failed")
    detail = stdout.decode("utf-8", errors="replace").strip()
    if detail != "connection=ok; migration=head":
        raise RuntimeError("DB probe output mismatch")
    return detail


async def check_playwright() -> str:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<title>eclass-smoke</title><main>ok</main>")
            if await page.title() != "eclass-smoke":
                raise RuntimeError("Chromium page mismatch")
        finally:
            await browser.close()
    return "chromium=headless-ok"


async def check_mcp_registry(module: str, expected: set[str]) -> str:
    # MCP SDK나 자식 stdio 종료가 지연돼도 나머지 구성요소 검사는 계속되도록 격리한다.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_ROOT / "scripts/mcp_registry_probe.py"),
        module,
        cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("MCP registry probe timeout")
    if process.returncode != 0:
        raise RuntimeError("MCP registry probe failed")
    try:
        actual = set(json.loads(stdout.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise RuntimeError("MCP registry probe output mismatch")
    if actual != expected:
        raise RuntimeError("MCP registry mismatch")
    return f"transport=stdio; tools={len(actual)}"


def _ollama_tags_url(settings: Settings) -> str:
    url = httpx.URL(str(settings.ollama_url))
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{url.host}{port}/api/tags"


async def check_ollama(settings: Settings) -> str:
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(_ollama_tags_url(settings))
        response.raise_for_status()
        payload = response.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("unexpected Ollama response")
    qwen_present = any("qwen3:0.6b" in str(item.get("name", "")) for item in models if isinstance(item, dict))
    if not qwen_present:
        raise RuntimeError("required Qwen model missing")
    return "api=ok; qwen3:0.6b=present"


async def check_openai(settings: Settings) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI key missing")
    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=15.0, max_retries=0)
    try:
        # 모델 조회는 생성 응답을 만들지 않으면서 키·네트워크·모델 접근권한을 함께 검증한다.
        await client.models.retrieve(settings.openai_model)
    finally:
        await client.close()
    return "api=ok; configured_model=accessible"


def structured(result: Any) -> dict[str, Any]:
    value = result.structuredContent
    if not isinstance(value, dict):
        raise RuntimeError("MCP result is not structured")
    return value


async def check_live_eclass() -> str:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=PROJECT_ROOT,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            session_result = structured(await session.call_tool("check_session"))
            session_data = session_result.get("data") or {}
            if session_result.get("ok") is not True or not session_data.get("authenticated"):
                raise RuntimeError("E-Class authentication failed")
            dashboard = structured(await session.call_tool("get_dashboard_snapshot"))
            if dashboard.get("ok") is not True:
                raise RuntimeError("E-Class dashboard failed")
            data = dashboard.get("data") or {}
            counts = {
                key: len(data.get(key) or [])
                for key in ("courses", "announcements", "assignments", "lectures")
            }
    # 명칭·본문·URL은 출력하지 않고 건수만 관측한다.
    return "authenticated=true; " + "; ".join(f"{key}={value}" for key, value in counts.items())


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    operations: list[tuple[str, str, Callable[[], Awaitable[str]]]] = [
        ("runtime", "RUNTIME_CONFIG_FAILED", lambda: check_runtime(settings)),
        ("mysql", "MYSQL_HEALTH_FAILED", lambda: check_database(settings)),
        ("playwright", "PLAYWRIGHT_HEALTH_FAILED", check_playwright),
        (
            "eclass_mcp",
            "ECLASS_MCP_HEALTH_FAILED",
            lambda: check_mcp_registry("mcp_server.server", ECLASS_TOOLS),
        ),
        (
            "document_mcp",
            "DOCUMENT_MCP_HEALTH_FAILED",
            lambda: check_mcp_registry("document_mcp_server.server", DOCUMENT_TOOLS),
        ),
    ]
    if not args.skip_ollama:
        operations.append(("ollama", "OLLAMA_HEALTH_FAILED", lambda: check_ollama(settings)))

    failed = False
    for component, code, operation in operations:
        result = await check(component, code, operation)
        emit(result)
        failed = failed or result.status == "FAIL"

    if args.live_openai:
        result = await check("openai", "OPENAI_HEALTH_FAILED", lambda: check_openai(settings))
    else:
        result = skipped("openai", "LIVE_OPENAI_NOT_REQUESTED")
    emit(result)
    failed = failed or result.status == "FAIL"

    if args.live_eclass:
        result = await check("eclass_live", "ECLASS_LIVE_SMOKE_FAILED", check_live_eclass)
    else:
        result = skipped("eclass_live", "LIVE_ECLASS_NOT_REQUESTED")
    emit(result)
    failed = failed or result.status == "FAIL"
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="배포 구성요소별 smoke test를 실행합니다.")
    parser.add_argument("--live-openai", action="store_true", help="OpenAI API와 모델 접근을 실제 확인합니다.")
    parser.add_argument("--live-eclass", action="store_true", help="E-Class 세션과 dashboard를 실제 확인합니다.")
    parser.add_argument("--skip-ollama", action="store_true", help="외부 Ollama를 사용하지 않을 때만 지정합니다.")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
