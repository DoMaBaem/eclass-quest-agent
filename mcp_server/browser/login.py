"""사용자가 직접 headed Chromium에서 로그인한 뒤 암호화 세션을 생성하는 CLI."""

from __future__ import annotations

import asyncio

from app.config import get_settings
from mcp_server.adapters.hansung_playwright import HansungPlaywrightAdapter
from mcp_server.browser.language import with_eclass_language
from mcp_server.browser.session import save_encrypted_storage_state
from mcp_server.browser.worker import EclassBrowserWorker


async def login() -> int:
    """headed 브라우저에서 최대 5분간 로그인을 기다리고 세션만 암호화 저장한다."""

    settings = get_settings()
    worker = EclassBrowserWorker(settings, headless=False)
    print("브라우저에서 한성 e-Class 로그인을 직접 완료하세요. 최대 5분간 기다립니다.")
    async with worker.headed_login_page() as (context, page):
        await page.goto(
            with_eclass_language(
                str(settings.eclass_base_url), settings.eclass_default_language
            ),
            wait_until="domcontentloaded",
        )
        adapter = HansungPlaywrightAdapter(settings)
        # 1초 간격 300회 = 최대 5분. 비밀번호나 입력값 자체는 읽지 않는다.
        for _ in range(300):
            await page.wait_for_timeout(1_000)
            if adapter._looks_like_login_url(page.url):
                continue
            # 한성 e-Class 화면은 환경에 따라 "로그아웃" 표식을 숨길 수 있다.
            # 로그인 URL을 벗어났고 비밀번호 입력칸도 사라졌다면 대시보드 진입으로 판단한다.
            password_inputs = page.locator("input[type='password'], input[name*='password' i]")
            has_logout_marker = await adapter._has_any_visible(page, adapter_selector_login_success())
            if has_logout_marker or await password_inputs.count() == 0:
                # context.storage_state()는 쿠키/스토리지를 dict로 반환하고 session.py가 즉시 암호화한다.
                path = save_encrypted_storage_state(settings, await context.storage_state())
                print(f"로그인 세션을 암호화해 저장했습니다: {path}")
                return 0
    print("로그인 완료를 확인하지 못했습니다. 다시 실행한 뒤 로그아웃 표식이 보이는지 확인하세요.")
    return 1


def adapter_selector_login_success() -> tuple[str, ...]:
    """순환 import 없이 로그인 성공 선택자를 명확히 전달한다."""

    from mcp_server.browser.selectors import HansungSelectors

    return HansungSelectors.LOGIN_SUCCESS


if __name__ == "__main__":
    raise SystemExit(asyncio.run(login()))
