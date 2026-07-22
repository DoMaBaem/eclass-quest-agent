"""Playwright 프로세스, Browser, Context, Page의 수명을 관리한다.

Adapter는 브라우저 생성·종료 방법을 몰라도 된다. ``async with`` 블록을 벗어나면 성공·실패와
무관하게 context와 browser를 닫아 탭과 프로세스가 남지 않게 한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import BrowserContext, Page, async_playwright

from app.config import Settings
from mcp_server.browser.language import playwright_locale
from mcp_server.browser.session import load_encrypted_storage_state


class EclassBrowserWorker:
    """작업마다 독립 Context를 만들고 종료해 사용자 쿠키가 섞이지 않게 한다."""

    def __init__(self, settings: Settings, *, headless: bool = True) -> None:
        self.settings = settings
        self.headless = headless

    @asynccontextmanager
    async def authenticated_page(self) -> AsyncIterator[Page]:
        """암호화 세션을 불러온 독립 BrowserContext의 새 Page를 빌려준다."""

        # 브라우저를 시작하기 전에 세션을 확인하므로 세션이 없으면 불필요한 Chromium을 띄우지 않는다.
        state = load_encrypted_storage_state(self.settings)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.headless)
            # Context가 사용자별 쿠키 격리 단위다. 다른 사용자의 state와 절대 공유하지 않는다.
            context: BrowserContext = await browser.new_context(
                storage_state=state,
                locale=playwright_locale(self.settings.eclass_default_language),
            )
            try:
                yield await context.new_page()
            finally:
                await context.close()
                await browser.close()

    @asynccontextmanager
    async def headed_login_page(self) -> AsyncIterator[tuple[BrowserContext, Page]]:
        """사용자가 직접 로그인할 일회성 headed Context를 제공한다."""

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=False)
            context = await browser.new_context(
                locale=playwright_locale(self.settings.eclass_default_language)
            )
            try:
                yield context, await context.new_page()
            finally:
                await context.close()
                await browser.close()
