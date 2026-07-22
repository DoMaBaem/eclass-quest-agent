"""저장된 자격증명으로 만료된 E-Class 세션을 자동 갱신한다.

이 모듈만 아이디와 비밀번호 평문을 잠깐 사용한다. 값은 브라우저 입력란에 전달한 뒤 DB, 로그,
예외 메시지 또는 MCP 결과에 넣지 않는다. 로그인 성공 전에는 기존 암호화 세션도 덮어쓰지 않는다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from app.config import Settings
from mcp_server.browser.language import playwright_locale, with_eclass_language
from mcp_server.browser.selectors import HansungSelectors
from mcp_server.browser.session import AuthRequiredError, save_encrypted_storage_state


# 동시에 여러 동기화가 세션 만료를 발견해 중복 로그인하는 일을 막는다.
_SESSION_REFRESH_LOCK = asyncio.Lock()


def automatic_login_available(settings: Settings) -> bool:
    """아이디·비밀번호가 모두 있는지 값 노출 없이 확인한다."""

    username = settings.eclass_username.get_secret_value() if settings.eclass_username else ""
    password = settings.eclass_password.get_secret_value() if settings.eclass_password else ""
    return bool(username.strip() and password)


async def refresh_encrypted_session(settings: Settings) -> Path:
    """새 headless 브라우저로 로그인하고 성공한 storage state만 암호화 저장한다."""

    if not automatic_login_available(settings):
        raise AuthRequiredError(
            "자동 로그인 정보가 없습니다. ./run.sh --setup 또는 scripts/login.sh를 실행하세요."
        )

    # SecretStr 평문은 이 함수의 지역 변수로만 짧게 유지한다.
    username = settings.eclass_username.get_secret_value()  # type: ignore[union-attr]
    password = settings.eclass_password.get_secret_value()  # type: ignore[union-attr]

    async with _SESSION_REFRESH_LOCK:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                locale=playwright_locale(settings.eclass_default_language)
            )
            try:
                page = await context.new_page()
                await page.goto(
                    with_eclass_language(
                        str(settings.eclass_base_url), settings.eclass_default_language
                    ),
                    wait_until="domcontentloaded",
                )
                username_input = await _first_visible(page, HansungSelectors.LOGIN_USERNAME)
                password_input = await _first_visible(page, HansungSelectors.LOGIN_PASSWORD)
                submit = await _first_visible(page, HansungSelectors.LOGIN_SUBMIT)
                if username_input is None or password_input is None or submit is None:
                    raise AuthRequiredError(
                        "E-Class 로그인 화면 구조가 변경되어 자동 로그인할 수 없습니다."
                    )

                await username_input.fill(username)
                await password_input.fill(password)
                await submit.click()
                await page.wait_for_load_state("domcontentloaded")
                if _looks_like_login_url(page.url):
                    raise AuthRequiredError(
                        "E-Class 자동 로그인에 실패했습니다. 계정 정보를 확인하거나 직접 로그인하세요."
                    )

                # 로그인 POST가 끝나도 대시보드의 사용자 메뉴가 비동기로 늦게 그려질 수 있다.
                # headless 기본 화면 폭에서는 반응형 메뉴가 숨겨지므로 visible이 아닌 DOM 부착을 본다.
                try:
                    await page.locator("a[href*='/login/logout.php']").first.wait_for(
                        state="attached", timeout=10_000
                    )
                except PlaywrightTimeoutError as exc:
                    raise AuthRequiredError(
                        "E-Class 로그인 완료 상태를 확인하지 못했습니다. 직접 로그인하세요."
                    ) from exc
                if not await page.locator("a[href*='/login/logout.php']").count():
                    raise AuthRequiredError("E-Class 로그인 완료 표식을 확인하지 못했습니다.")

                # 성공이 확정된 뒤에만 기존 암호화 세션을 원자적으로 교체한다.
                return save_encrypted_storage_state(settings, await context.storage_state())
            finally:
                await context.close()
                await browser.close()


async def _first_visible(page: Page, selectors: tuple[str, ...]):
    """후보 중 실제 보이는 첫 Playwright Locator를 반환한다."""

    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() and await locator.is_visible():
            return locator
    return None


def _looks_like_login_url(url: str) -> bool:
    """일반 로그인 또는 SSO 주소인지 판정한다."""

    return "login" in url.lower() or "sso" in url.lower()
