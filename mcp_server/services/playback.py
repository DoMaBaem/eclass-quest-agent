"""사용자가 명시적으로 요청한 강의 영상의 headed Playwright 수명을 관리한다."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from sqlalchemy import select

from app.config import Settings
from app.storage.database import Database
from app.storage.models import PlaybackRunModel, UserModel
from mcp_server.browser.credential_login import automatic_login_available, refresh_encrypted_session
from mcp_server.browser.language import with_eclass_language
from mcp_server.browser.session import AuthRequiredError, load_encrypted_storage_state
from mcp_server.schemas import McpErrorCode, McpToolError, PlaybackInfo, PlaybackResult


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _LivePlayback:
    info: PlaybackInfo
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    timeout_task: asyncio.Task[None] | None = None


class LecturePlaybackService:
    """MCP 서버 프로세스가 살아 있는 동안 영상 브라우저와 취소 핸들을 보관한다."""

    VIEWER_LINK_SELECTOR = (
        "a[onclick*='/mod/vod/viewer.php'], "
        "a[href*='/mod/vod/viewer.php'], "
        "a:has-text('동영상 보기')"
    )
    # Playwright가 사용자의 명시적 재생 요청을 대신 실행하는 headed 전용 브라우저다.
    # 음소거를 해제한 video.play()가 Chromium 자동재생 정책에 막히지 않게 한다.
    CHROMIUM_LAUNCH_ARGS = ("--autoplay-policy=no-user-gesture-required",)

    def __init__(self, settings: Settings, *, user_id: str = "local-user") -> None:
        self.settings = settings
        self.user_id = user_id
        self._runs: dict[str, _LivePlayback] = {}
        self._lock = asyncio.Lock()

    async def play(
        self,
        lecture_id: str,
        *,
        explicit_user_request: bool,
        max_minutes: int = 180,
        auto_stop_seconds: int | None = None,
        volume_percent: int = 100,
        playback_rate: float = 1.0,
        window_width: int | None = None,
        window_height: int | None = None,
    ) -> PlaybackResult:
        """새 headed 브라우저를 열고 실제 player가 시작됐을 때만 PLAYING을 반환한다."""

        if not explicit_user_request:
            return self._failure(
                "영상 재생은 사용자의 명시적 요청이 있어야 합니다.",
                McpErrorCode.INVALID_REQUEST,
            )
        if not re.fullmatch(r"\d{1,20}", lecture_id):
            return self._failure("올바른 강의 ID가 아닙니다.", McpErrorCode.NOT_FOUND)
        if not 1 <= max_minutes <= 360:
            return self._failure(
                "최대 재생 시간은 1~360분이어야 합니다.",
                McpErrorCode.INVALID_REQUEST,
            )
        if auto_stop_seconds is not None and not 5 <= auto_stop_seconds <= 30:
            return self._failure(
                "시연 미리보기 시간은 5~30초여야 합니다.",
                McpErrorCode.INVALID_REQUEST,
            )
        if not 0 <= volume_percent <= 100:
            return self._failure(
                "볼륨은 0~100 사이여야 합니다.",
                McpErrorCode.INVALID_REQUEST,
            )
        if not 0.5 <= playback_rate <= 2.0:
            return self._failure(
                "재생 배속은 0.5~2.0 사이여야 합니다.",
                McpErrorCode.INVALID_REQUEST,
            )
        if (window_width is None) != (window_height is None):
            return self._failure(
                "재생 창의 가로와 세로 크기를 함께 지정해야 합니다.",
                McpErrorCode.INVALID_REQUEST,
            )
        if window_width is not None and window_height is not None and (
            not 640 <= window_width <= 3840 or not 480 <= window_height <= 2160
        ):
            return self._failure(
                "재생 창 크기는 가로 640~3840, 세로 480~2160 사이여야 합니다.",
                McpErrorCode.INVALID_REQUEST,
            )

        async with self._lock:
            playback_id = str(uuid4())
            started_at = datetime.now(timezone.utc)
            playwright = await async_playwright().start()
            browser: Browser | None = None
            context: BrowserContext | None = None
            failure_stage = "브라우저 실행"
            try:
                browser = await playwright.chromium.launch(
                    headless=False,
                    args=list(self.CHROMIUM_LAUNCH_ARGS),
                )
                failure_stage = "로그인 세션 적용"
                context = await browser.new_context(storage_state=load_encrypted_storage_state(self.settings))
                page = await context.new_page()
                failure_stage = "강의 페이지 열기"
                await self._open_lecture(page, lecture_id)
                failure_stage = "플레이어 시작"
                page = await self._start_player(
                    context,
                    page,
                    volume_percent=volume_percent,
                    playback_rate=playback_rate,
                    window_width=window_width,
                    window_height=window_height,
                )
                info = PlaybackInfo(
                    playback_id=playback_id,
                    lecture_id=lecture_id,
                    status="PLAYING",
                    volume_percent=volume_percent,
                    playback_rate=playback_rate,
                    window_width=window_width,
                    window_height=window_height,
                    started_at=started_at,
                )
                run = _LivePlayback(info, playwright, browser, context, page)
                self._runs[playback_id] = run
                run.timeout_task = asyncio.create_task(
                    self._stop_after(
                        playback_id,
                        auto_stop_seconds if auto_stop_seconds is not None else max_minutes * 60,
                    ),
                    name=f"lecture-playback-timeout-{playback_id}",
                )
                # 재생 자체가 핵심 결과다. 부가 감사 DB 기록이 실패해도 이미 시작된 영상을
                # 닫거나 Tool 전체를 실패로 바꾸지 않는다.
                await self._record_safely(info, request_id=playback_id)
                return PlaybackResult(ok=True, data=info)
            except AuthRequiredError:
                self._runs.pop(playback_id, None)
                if context is not None:
                    await context.close()
                if browser is not None:
                    await browser.close()
                await playwright.stop()
                return PlaybackResult(
                    ok=False,
                    error=McpToolError(
                        code=McpErrorCode.AUTH_REQUIRED,
                        message="E-Class 로그인이 필요합니다.",
                    ),
                )
            except Exception:
                # 사용자 응답에는 쿠키나 내부 DOM을 노출하지 않지만 개발 로그에는 traceback을 남긴다.
                logger.exception("E-Class lecture player start failed")
                failed_run = self._runs.pop(playback_id, None)
                if failed_run is not None and failed_run.timeout_task is not None:
                    failed_run.timeout_task.cancel()
                if context is not None:
                    await context.close()
                if browser is not None:
                    await browser.close()
                await playwright.stop()
                return PlaybackResult(
                    ok=False,
                    error=McpToolError(
                        code=McpErrorCode.PARSER_CHANGED,
                        message=f"강의 재생의 '{failure_stage}' 단계에서 실패했습니다.",
                    ),
                )

    async def stop(self, playback_id: str) -> PlaybackResult:
        """playback_id에 해당하는 브라우저를 닫고 종료 상태를 기록한다."""

        async with self._lock:
            run = self._runs.pop(playback_id, None)
            if run is None:
                return self._failure("실행 중인 영상 재생을 찾을 수 없습니다.", McpErrorCode.NOT_FOUND)
            current = asyncio.current_task()
            if run.timeout_task is not None and run.timeout_task is not current:
                run.timeout_task.cancel()
            return await self._finish(run, "STOPPED")

    async def preview(
        self,
        lecture_id: str,
        *,
        explicit_user_request: bool,
        seconds: int = 20,
        volume_percent: int = 100,
        playback_rate: float = 1.0,
        window_width: int | None = None,
        window_height: int | None = None,
    ) -> PlaybackResult:
        """멘토 시연용으로 실제 player를 시작하되 30초 안에 자동 종료한다."""

        return await self.play(
            lecture_id,
            explicit_user_request=explicit_user_request,
            max_minutes=1,
            auto_stop_seconds=seconds,
            volume_percent=volume_percent,
            playback_rate=playback_rate,
            window_width=window_width,
            window_height=window_height,
        )

    async def close(self) -> None:
        """MCP 서버 종료 시 남은 모든 브라우저를 정상 정리한다."""

        for playback_id in list(self._runs):
            await self.stop(playback_id)

    async def _open_lecture(self, page: Page, lecture_id: str) -> None:
        base = urlparse(str(self.settings.eclass_base_url))
        target = urljoin(str(self.settings.eclass_base_url), f"/mod/vod/view.php?id={lecture_id}")
        target = with_eclass_language(target, self.settings.eclass_default_language)
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc:
            raise ValueError("E-Class 외부 URL")
        await page.goto(target, wait_until="domcontentloaded")
        if "/login" in page.url or await page.locator("input[type='password']").count():
            if automatic_login_available(self.settings):
                await refresh_encrypted_session(self.settings)
                refreshed = load_encrypted_storage_state(self.settings)
                await page.context.add_cookies(refreshed.get("cookies", []))
                await page.goto(target, wait_until="domcontentloaded")
                if "/login" not in page.url and not await page.locator("input[type='password']").count():
                    return
            raise AuthRequiredError("세션 만료")

    async def _start_player(
        self,
        context: BrowserContext,
        page: Page,
        *,
        volume_percent: int,
        playback_rate: float,
        window_width: int | None,
        window_height: int | None,
    ) -> Page:
        """현재 탭·popup·iframe의 실제 video 재생 상태를 순서대로 확인한다."""

        active = page
        viewer_link = page.locator(self.VIEWER_LINK_SELECTOR).first
        if await viewer_link.count() and await viewer_link.is_visible():
            # 한성 E-Class의 '동영상 보기'는 window.open()으로 viewer.php 팝업을 연다.
            # 고정 sleep 뒤 context.pages를 확인하면 느린 환경에서 팝업을 놓칠 수 있으므로
            # 클릭과 popup 이벤트를 하나의 expect_popup 구간으로 묶는다.
            try:
                async with page.expect_popup(timeout=10_000) as popup_info:
                    await viewer_link.click()
                active = await popup_info.value
                await active.wait_for_load_state("domcontentloaded")
                await active.bring_to_front()
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("동영상 보기 팝업을 열지 못함") from exc

        # 크기를 명시하지 않은 일반 요청은 E-Class viewer가 정한 원래 창 크기를 보존한다.
        if window_width is not None and window_height is not None:
            await self._resize_window(active, window_width, window_height)

        candidates = (
            "button:has-text('학습하기')", "a:has-text('학습하기')",
            "button:has-text('재생')", "a:has-text('재생')",
            "button:has-text('Play')", "a:has-text('Play')",
        )
        if active is page:
            for selector in candidates:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click()
                    break

        # viewer의 JavaScript가 video를 늦게 붙이는 경우까지 최대 10초간 기다린다.
        for _attempt in range(20):
            for frame in active.frames:
                videos = frame.locator("video")
                if await videos.count():
                    video = videos.first
                    before = float(await video.evaluate("element => element.currentTime || 0"))
                    await video.evaluate(
                        """async (element, options) => {
                            element.muted = options.volumePercent === 0;
                            element.volume = options.volumePercent / 100;
                            element.playbackRate = options.playbackRate;
                            if (element.ended) element.currentTime = 0;
                            await element.play();
                        }""",
                        {"volumePercent": volume_percent, "playbackRate": playback_rate},
                    )
                    # paused 플래그만 잠깐 바뀌었다가 다시 멈추는 초기화 경쟁을 피하려고
                    # 실제 재생 시간이 전진하는지까지 확인한다.
                    await frame.wait_for_timeout(1_200)
                    state = await video.evaluate(
                        "element => ({paused: element.paused, ended: element.ended, time: element.currentTime})"
                    )
                    if not state["paused"] and not state["ended"] and float(state["time"]) > before:
                        return active
                play_button = frame.locator(
                    "button[aria-label*='play' i], button[title*='play' i], .vjs-big-play-button"
                ).first
                if await play_button.count() and await play_button.is_visible():
                    await play_button.click()
                    await frame.wait_for_timeout(300)
                    return active
            await active.wait_for_timeout(500)
        raise RuntimeError("재생 가능한 player를 찾지 못함")

    @staticmethod
    async def _resize_window(page: Page, width: int, height: int) -> None:
        """Chromium 팝업의 실제 창 크기를 바꾸고, 실패하면 viewport만 조정한다."""

        session = None
        try:
            session = await page.context.new_cdp_session(page)
            window = await session.send("Browser.getWindowForTarget")
            await session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {"width": width, "height": height},
                },
            )
        except Exception:
            logger.debug("Could not resize browser window via CDP", exc_info=True)
            await page.set_viewport_size({"width": width, "height": height})
        finally:
            if session is not None:
                with suppress(Exception):
                    await session.detach()

    async def _stop_after(self, playback_id: str, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
            async with self._lock:
                run = self._runs.pop(playback_id, None)
                if run is not None:
                    await self._finish(run, "TIMED_OUT")
        except asyncio.CancelledError:
            return

    async def _finish(self, run: _LivePlayback, status: str) -> PlaybackResult:
        try:
            await run.context.close()
            await run.browser.close()
            await run.playwright.stop()
        finally:
            info = run.info.model_copy(
                update={"status": status, "finished_at": datetime.now(timezone.utc)}
            )
            await self._record_safely(info, request_id=info.playback_id)
        return PlaybackResult(ok=True, data=info)

    async def _record_safely(self, info: PlaybackInfo, *, request_id: str) -> bool:
        """부가 DB 기록 실패가 실제 영상 재생·중지 결과를 뒤집지 않게 격리한다."""

        try:
            await self._record(info, request_id=request_id)
        except Exception:
            logger.exception("Could not persist E-Class playback audit record")
            return False
        return True

    async def _record(self, info: PlaybackInfo, *, request_id: str) -> None:
        """DB가 설정된 실행에서만 playback_runs를 upsert한다."""

        if not self.settings.mysql_url:
            return
        database = Database(self.settings.mysql_url)
        try:
            async with database.session() as session:
                if await session.get(UserModel, self.user_id) is None:
                    session.add(UserModel(id=self.user_id))
                    await session.flush()
                row = await session.scalar(
                    select(PlaybackRunModel).where(PlaybackRunModel.request_id == request_id)
                )
                if row is None:
                    row = PlaybackRunModel(
                        request_id=request_id,
                        user_id=self.user_id,
                        lecture_id=info.lecture_id,
                        status=info.status,
                        started_at=info.started_at,
                    )
                    session.add(row)
                row.status = info.status
                row.finished_at = info.finished_at
        finally:
            await database.dispose()

    @staticmethod
    def _failure(message: str, code: McpErrorCode = McpErrorCode.TEMPORARY_FAILURE) -> PlaybackResult:
        return PlaybackResult(ok=False, error=McpToolError(code=code, message=message))
