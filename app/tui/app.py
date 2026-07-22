"""사용자 입력, Agent 스트림, 상태별 시스템 창을 연결하는 Textual TUI.

화면은 LMS를 직접 조작하지 않는다. 입력을 ProactiveAssistantRuntime에 전달하고, callback으로 받은
Manager 답변 delta와 전문 Agent 연결 이벤트를 시각화한다.
"""

from __future__ import annotations

import asyncio
import random
import re
from contextlib import suppress
from datetime import datetime, timedelta

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input, RichLog, Static
from rich.text import Text

from app.config import Settings
from app.runtime.assistant_runtime import ProactiveAssistantRuntime
from app.runtime.events import RuntimeProgressEvent
from app.schemas.manager import ManagerResult, ManagerStatus
from app.sync.schemas import (
    AssignmentChecklistItem,
    LectureChecklistItem,
    SyncResult,
    SyncStatus,
    SyncTrigger,
)
from app.sync.service import SyncService
from app.tui.events import UiOperationState


class EclassQuestApp(App[None]):
    """E-Class Quest 터미널 애플리케이션의 루트 위젯."""

    TITLE = "E-CLASS 관리 도우미"
    # 한 번의 TUI 실행 안에서는 충분한 기록을 유지하되 무한 메모리 증가만 방지한다.
    MAX_TRANSCRIPT_ENTRIES = 500
    CSS = """
    Screen {
        /* 창 바깥은 사용자가 쓰는 터미널의 기본 배경색을 그대로 사용한다. */
        background: transparent;
        color: #F8FAFC;
        align: center middle;
    }

    #system-window {
        width: 96%;
        max-width: 150;
        height: 90%;
        border: round #3F72AF;
        background: #CBDCEB;
        padding: 1;
    }

    #inner-frame {
        height: 1fr;
        border: none;
        background: #DFE9F3;
        padding: 0 1;
    }

    #alert-mark {
        display: none;
    }

    #top-bar {
        height: 3;
        background: #3F72AF;
    }

    #system-title {
        width: 1fr;
        height: 3;
        content-align: left middle;
        padding-left: 2;
        color: #FFFFFF;
        text-style: bold;
        background: #3F72AF;
        border: none;
    }

    #clock {
        width: 14;
        height: 3;
        content-align: center middle;
        color: #FFFFFF;
        text-style: bold;
        background: #3F72AF;
    }

    #workspace {
        height: 1fr;
        margin-top: 1;
        background: #DFE9F3;
    }

    #sidebar {
        width: 34;
        min-width: 24;
        height: 1fr;
        padding: 0 1;
        border-right: tall #6F98C3;
        background: #D5E4F1;
    }

    .sidebar-title {
        height: 2;
        margin-top: 1;
        content-align: left middle;
        color: #274D73;
        text-style: bold;
    }

    #lecture-summary {
        height: 2;
        color: #3F72AF;
    }

    #lecture-checklist {
        height: 1fr;
        padding: 0 1;
        background: #EEF3F8;
        border: tall #8EACCA;
        color: #1C3854;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        scrollbar-color: #6F98C3;
        scrollbar-background: #DFE9F3;
    }

    #assignment-checklist {
        height: 8;
        padding: 0 1;
        color: #1C3854;
        background: #E5EEF6;
        border: tall #8EACCA;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        scrollbar-color: #6F98C3;
        scrollbar-background: #DFE9F3;
    }

    #main-panel {
        width: 1fr;
        height: 1fr;
        padding-left: 1;
    }

    #status {
        height: 2;
        padding: 0 1;
        color: #274D73;
        background: #E5EEF6;
        content-align: left middle;
        text-style: bold;
    }

    #status.state-syncing {
        color: #112D4E;
        background: #C8E4F4;
    }

    #status.state-proactive-alert {
        color: #78334F;
        background: #F4D9E4;
    }

    #status.state-user-task {
        color: #112D4E;
        background: #DBEAF5;
    }

    #status.state-playback {
        color: #FFFFFF;
        background: #3F72AF;
    }

    #status.state-auth-required {
        color: #6E2945;
        background: #F2CEDC;
    }

    #sync-status {
        height: 1;
        color: #59748E;
        content-align: right middle;
        background: transparent;
    }

    #result {
        height: 1fr;
        margin-top: 0;
        padding: 1 2;
        background: #F8FAFC;
        border: tall #6F98C3;
        color: #1C3854;
        overflow-y: auto;
        overflow-x: auto;
        scrollbar-gutter: stable;
        scrollbar-size-vertical: 2;
        scrollbar-size-horizontal: 1;
        scrollbar-background: #DFE9F3;
        scrollbar-color: #6F98C3;
        scrollbar-color-hover: #3F72AF;
        scrollbar-color-active: #274D73;
    }

    Input {
        margin-top: 1;
        padding: 0 1;
        border: tall #6F98C3;
        background: #EEF3F8;
        color: #1C3854;
    }

    Input:focus {
        border: double #274D73;
        background: #FFFFFF;
    }

    #frame-accent {
        display: none;
        height: 1;
        color: #3F72AF;
        text-style: bold;
        content-align: center middle;
    }

    #command-bar {
        height: 2;
        margin-top: 1;
        content-align: center middle;
        color: #274D73;
        background: #D5E4F1;
    }

    """

    BINDINGS = [
        ("f1", "focus_lectures", "강의"),
        ("f2", "stop_playback", "영상 중지"),
        ("escape", "quit", "종료"),
        ("ctrl+c", "quit", "종료"),
    ]

    _AGENT_TOOL_LABELS = {
        "E-Class Agent": "E-Class MCP",
        "Document Analysis Agent": "MarkItDown / Qwen",
        "Mission Service": "MySQL Mission Service",
    }

    STARTUP_MESSAGES = (
        "E-Class 연결을 마쳤습니다. 무엇부터 확인할까요?",
        "공지·과제·강의를 확인할 준비가 됐습니다.",
        "필요한 E-Class 작업을 입력해 주세요.",
    )
    def __init__(
        self,
        settings: Settings,
        *,
        sync_service: SyncService | None = None,
        enable_sync: bool = True,
    ) -> None:
        super().__init__()
        self.settings = settings
        # runtime은 앱 수명 동안 한 번만 만들어 안전한 요약과 이벤트 상태를 유지한다.
        self.runtime = ProactiveAssistantRuntime(settings)
        # RichLog 자체를 상태 저장소로 쓰지 않고 이번 실행의 대화 문자열을 별도 보관한다.
        self.transcript: list[str] = []
        # 전문 Agent 실행 중 움직이는 SYSTEM 행의 위치다. 별도 팝업으로 전환하지 않고 이 행만 갱신한다.
        self._processing_message_index: int | None = None
        self.sync_service = (
            sync_service
            if sync_service is not None
            else SyncService(settings)
            if enable_sync and settings.mysql_url
            else None
        )
        self._sync_timer: Timer | None = None
        self._clock_timer: Timer | None = None
        self._state_reset_timer: Timer | None = None
        self._last_sync_at: datetime | None = None
        self._next_sync_at: datetime | None = None
        # 이 값들은 화면 장식용 문자열이 아니라 현재 실행 흐름을 나타내는 단일 상태 원본이다.
        self.operation_state = UiOperationState.READY
        self.current_tool = "대기"
        self.operation_progress = 100
        self._user_task_running = False
        self._sync_in_progress = False
        # F2 중지는 이 TUI 실행에서 검증된 재생 결과가 반환한 ID에만 허용한다.
        self._active_playback_id: str | None = None

    def on_mount(self) -> None:
        """위젯이 화면에 붙은 직후 고정 대화창을 준비한다."""

        # 첫 화면의 큰 결과 카드가 비어 보이지 않도록 안내 문장을 대화 영역에 바로 표시한다.
        self._set_operation_state(
            UiOperationState.READY,
            tool="대기",
            progress=100,
            detail="요청 대기",
        )
        self._update_clock()
        self._clock_timer = self.set_interval(1, self._update_clock, name="system-clock")
        self._render_lecture_checklist([])
        self._render_assignment_checklist([])
        self.transcript = [f"SYSTEM > {random.choice(self.STARTUP_MESSAGES)}"]
        self._render_transcript()
        if self.sync_service is not None:
            interval_seconds = self.settings.eclass_sync_interval_minutes * 60
            self._next_sync_at = datetime.now().astimezone() + timedelta(seconds=interval_seconds)
            self._sync_timer = self.set_interval(
                interval_seconds,
                self._on_sync_heartbeat,
                name="eclass-heartbeat",
            )
            self._update_sync_status()
            if self.settings.eclass_sync_on_startup:
                self._start_sync_worker(SyncTrigger.STARTUP)

    def compose(self) -> ComposeResult:
        """화면의 고정 위젯 구조를 정의한다.

        시작·대화·조회·오류 모두 같은 위젯과 같은 디자인을 유지한다.
        """

        with Container(id="system-window"):
            with Vertical(id="inner-frame"):
                with Horizontal(id="top-bar"):
                    yield Static("E-CLASS QUEST SYSTEM", id="system-title")
                    yield Static("--:--:--", id="clock")
                with Horizontal(id="workspace"):
                    with Vertical(id="sidebar"):
                        yield Static("ACTIVE LECTURES", classes="sidebar-title")
                        yield Static("동기화 대기 중", id="lecture-summary")
                        yield RichLog(
                            id="lecture-checklist",
                            wrap=True,
                            markup=False,
                            highlight=False,
                        )
                        yield Static("THIS WEEK ASSIGNMENTS", classes="sidebar-title")
                        yield RichLog(
                            id="assignment-checklist",
                            wrap=True,
                            markup=False,
                            highlight=False,
                        )
                    with Vertical(id="main-panel"):
                        yield Static("READY | TOOL: 대기 | 100% | 요청 대기", id="status")
                        # 긴 URL·과제명이 오른쪽에서 잘리지 않도록 줄바꿈하지 않고
                        # RichLog의 가로 스크롤로 원문 전체를 확인하게 한다.
                        yield RichLog(id="result", wrap=False, markup=False, highlight=False)
                        yield Input(placeholder="요청을 입력하세요", id="request")
                        yield Static("", id="sync-status")
                yield Static(
                    "[F1] LECTURES    [F2] STOP VIDEO    [ENTER] SEND    [ESC] EXIT",
                    id="command-bar",
                    markup=False,
                )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter로 입력한 메시지 한 건을 끝까지 처리한다.

        1. 입력을 transcript에 추가하고 중복 입력을 잠근다.
        2. Manager 분류와 전문 Agent 실행 동안 SYSTEM 행에 움직이는 점을 표시한다.
        3. 중간 계획 문장은 노출하지 않고 검증된 최종 결과만 같은 행에 표시한다.
        4. 창 모양은 바꾸지 않고 작업 상태 행을 최종 결과로 교체한다.
        """

        message = event.value.strip()
        if not message:
            return

        if self.sync_service is not None and self._is_manual_sync_request(message):
            event.input.value = ""
            # 수동 동기화도 일반 대화처럼 사용자의 명령과 진행 상태를 기록한다. 실제 LMS
            # 수집은 아래 Background Worker에서 실행하므로 TUI 입력 루프는 막지 않는다.
            self._append_transcript("USER", message)
            self._append_transcript("SYSTEM", "E-Class 정보를 지금 다시 확인합니다.")
            self._set_operation_state(
                UiOperationState.SYNCING,
                tool="E-Class MCP",
                progress=10,
                detail="수동 동기화 시작",
            )
            self.sync_service.resume_authentication()
            self._start_sync_worker(SyncTrigger.MANUAL)
            return

        event.input.value = ""
        event.input.disabled = True
        self._user_task_running = True

        self._append_transcript("USER", message)
        playback_request = self._is_playback_request(message)
        requested_state = (
            UiOperationState.PLAYBACK if playback_request else UiOperationState.USER_TASK
        )
        self._set_operation_state(
            requested_state,
            tool="LMS Manager Agent",
            progress=10,
            detail="요청 분석 중",
        )

        self._begin_processing_message()
        processing_animation: asyncio.Task[None] | None = asyncio.create_task(
            self._animate_processing(),
            name="request-processing-animation",
        )
        delegated_steps = 0

        async def show_runtime_progress(
            event_name: RuntimeProgressEvent,
            agent_name: str | None,
        ) -> None:
            """전문 Agent 작업이 시작되면 현재 대화창에서 작업 표시를 움직인다."""

            nonlocal processing_animation, delegated_steps

            if event_name is RuntimeProgressEvent.RUNTIME_STARTED:
                self._set_operation_state(
                    requested_state,
                    tool="LMS Runtime",
                    progress=15,
                    detail="요청 확인 중",
                )
                return
            if event_name is RuntimeProgressEvent.MANAGER_STARTED:
                self._set_operation_state(
                    requested_state,
                    tool="LMS Manager Agent",
                    progress=30,
                    detail="작업 계획 중",
                )
                return
            if event_name is RuntimeProgressEvent.AUTH_REQUIRED:
                self._set_operation_state(
                    UiOperationState.AUTH_REQUIRED,
                    tool=self._tool_label(agent_name),
                    progress=100,
                    detail="로그인 필요",
                )
                return
            if event_name is not RuntimeProgressEvent.AGENT_DELEGATED:
                return
            delegated_steps += 1
            self._set_operation_state(
                requested_state,
                tool=self._tool_label(agent_name),
                progress=min(90, 45 + delegated_steps * 20),
                detail="전문 작업 실행 중",
            )
            # 요청 제출 직후부터 이미 같은 대화 행에서 애니메이션이 실행 중이다. 전문 Agent가
            # 여러 단계여도 새 행이나 새 패널을 만들지 않고 기존 작업 표시를 그대로 유지한다.
            if processing_animation is None or processing_animation.done():
                processing_animation = asyncio.create_task(
                    self._animate_processing(),
                    name="agent-processing-animation",
                )

        try:
            result = await self.runtime.handle_user_request(
                message,
                on_progress=show_runtime_progress,
                # "확인하겠습니다" 같은 Manager 계획은 숨기고 검증된 최종 결과만 보여준다.
                on_manager_delta=None,
            )
        finally:
            # 실제 Runtime이 끝나는 순간 애니메이션을 멈춘다. 고정 연출 시간을 기다리지 않는다.
            if processing_animation is not None:
                processing_animation.cancel()
                with suppress(asyncio.CancelledError):
                    await processing_animation
            # API 오류가 발생해도 사용자가 다시 입력할 수 있도록 반드시 잠금을 푼다.
            event.input.disabled = False
            event.input.focus()
            self._user_task_running = False
        if result.status in {ManagerStatus.FAILED, ManagerStatus.AUTH_REQUIRED}:
            # 사용자에게는 대화창의 이해 가능한 실패 메시지만 보여 주고 내부 오류 코드는 숨긴다.
            if result.status is ManagerStatus.AUTH_REQUIRED:
                state = UiOperationState.AUTH_REQUIRED
            elif self._active_playback_id is not None:
                state = UiOperationState.PLAYBACK
            else:
                state = requested_state
            self._set_operation_state(
                state,
                tool=("E-Class Player" if state is UiOperationState.PLAYBACK else self.current_tool),
                progress=100,
                detail=(
                    "로그인 필요"
                    if state is UiOperationState.AUTH_REQUIRED
                    else "영상 재생 중 · 최근 요청 실패"
                    if state is UiOperationState.PLAYBACK
                    else "작업 실패"
                ),
            )
            self._finish_processing_message(result.message)
            if state not in {UiOperationState.AUTH_REQUIRED, UiOperationState.PLAYBACK}:
                self._schedule_ready_reset()
            return
        playback_started = self._update_active_playback(result)
        if result.status is ManagerStatus.COMPLETED and not result.delegated_agents:
            final_chat_state = (
                UiOperationState.PLAYBACK
                if self._active_playback_id is not None
                else UiOperationState.USER_TASK
            )
            self._set_operation_state(
                final_chat_state,
                tool=(
                    "E-Class Player"
                    if final_chat_state is UiOperationState.PLAYBACK
                    else "LMS Manager Agent"
                ),
                progress=100,
                detail=(
                    "영상 재생 중 · F2로 중지"
                    if final_chat_state is UiOperationState.PLAYBACK
                    else "응답 완료"
                ),
            )
            self._finish_processing_message(result.message)
            if final_chat_state is not UiOperationState.PLAYBACK:
                self._schedule_ready_reset()
            return
        final_state = (
            UiOperationState.PLAYBACK
            if playback_started or self._active_playback_id is not None
            else UiOperationState.USER_TASK
        )
        self._set_operation_state(
            final_state,
            tool=("E-Class Player" if final_state is UiOperationState.PLAYBACK else self.current_tool),
            progress=100,
            detail=("영상 재생 중 · F2로 중지" if final_state is UiOperationState.PLAYBACK else "작업 완료"),
        )
        self._show_task_result(result.message)
        if final_state is not UiOperationState.PLAYBACK:
            self._schedule_ready_reset()

    async def show_proactive_result(self, result: ManagerResult) -> None:
        """사용자 입력 없이 발생한 중요한 시스템 이벤트 결과를 화면에 표시한다."""

        if not result.should_notify or result.status is ManagerStatus.NO_ACTION:
            return
        if not self._user_task_running:
            self._set_operation_state(
                UiOperationState.PROACTIVE_ALERT,
                tool="LMS Manager Agent",
                progress=100,
                detail="새 알림",
            )
        # 일반 응답과 같은 SYSTEM 접두사를 사용하면 사용자가 요청해서 받은 답인지 선제
        # 알림인지 구별할 수 없다. ALERT 행은 색과 접두사를 함께 달리한다.
        self._append_transcript("ALERT", result.message)
        if not self._sync_in_progress and not self._user_task_running:
            self._schedule_ready_reset()

    async def on_unmount(self) -> None:
        """TUI가 닫힐 때 Runtime 큐와 이후 작업 수신을 함께 종료한다."""

        if self._sync_timer is not None:
            self._sync_timer.pause()
        if self._clock_timer is not None:
            self._clock_timer.pause()
        if self._state_reset_timer is not None:
            self._state_reset_timer.stop()
        workers = self.workers.cancel_group(self, "eclass-sync")
        if workers:
            await self.workers.wait_for_complete(workers)
        if self.sync_service is not None:
            await self.sync_service.close()
        await self.runtime.shutdown()

    def _on_sync_heartbeat(self) -> None:
        """주기 Timer는 직접 네트워크 작업을 하지 않고 Background Worker만 시작한다."""

        self._start_sync_worker(SyncTrigger.HEARTBEAT)

    def _start_sync_worker(self, trigger: SyncTrigger) -> None:
        if self.sync_service is None:
            return
        self.run_worker(
            self._perform_sync(trigger),
            name=f"eclass-sync-{trigger.value.lower()}",
            group="eclass-sync",
            exit_on_error=False,
        )

    async def _perform_sync(self, trigger: SyncTrigger) -> None:
        """UI 이벤트 루프를 막지 않고 동기화하고 필요한 Manager 이벤트만 전달한다."""

        if self.sync_service is None:
            return
        self._sync_in_progress = True
        if not self._user_task_running:
            self._set_operation_state(
                UiOperationState.SYNCING,
                tool="E-Class MCP",
                progress=20,
                detail="강좌·강의·과제 확인 중",
            )
        self.query_one("#sync-status", Static).update("E-Class 확인 중...")
        result = await self.sync_service.sync(trigger)
        interval = timedelta(minutes=self.settings.eclass_sync_interval_minutes)
        if result.status is SyncStatus.COMPLETED:
            if not self._user_task_running:
                self._set_operation_state(
                    UiOperationState.SYNCING,
                    tool="MySQL Snapshot",
                    progress=80,
                    detail="변경 사항 반영 중",
                )
            self._render_lecture_checklist(result.lecture_checklist, result=result)
            self._render_assignment_checklist(result.assignment_checklist, result=result)
            self._last_sync_at = result.finished_at.astimezone()
            self._next_sync_at = self._last_sync_at + interval
            # 수동 확인 뒤 화면에 표시하는 다음 확인 시각과 실제 Textual heartbeat가
            # 어긋나지 않도록 반복 Timer도 완료 시점부터 다시 센다.
            if trigger is SyncTrigger.MANUAL and self._sync_timer is not None:
                self._sync_timer.reset()
            manager_succeeded = True
            proactive_notice_shown = False
            for runtime_event in result.events:
                manager_result = await self.runtime.handle_system_event(runtime_event)
                if manager_result.status is ManagerStatus.FAILED:
                    manager_succeeded = False
                    continue
                await self.show_proactive_result(manager_result)
                proactive_notice_shown = proactive_notice_shown or (
                    manager_result.should_notify
                    and manager_result.status is not ManagerStatus.NO_ACTION
                )
            if result.change_event_ids and manager_succeeded:
                await self.sync_service.mark_change_events_processed(
                    result.change_event_ids,
                    request_id=result.events[0].event_id if result.events else "sync-no-event",
                )
            if trigger is SyncTrigger.MANUAL:
                self._append_transcript("SYSTEM", "E-Class 정보 업데이트를 완료했습니다.")
            if not proactive_notice_shown and not self._user_task_running:
                self._set_operation_state(
                    UiOperationState.SYNCING,
                    tool="E-Class MCP / MySQL",
                    progress=100,
                    detail="동기화 완료",
                )
        elif result.status is SyncStatus.AUTH_REQUIRED:
            self._set_operation_state(
                UiOperationState.AUTH_REQUIRED,
                tool="E-Class MCP",
                progress=100,
                detail="로그인 필요 · scripts/login.sh",
            )
            if trigger is SyncTrigger.MANUAL:
                self._append_transcript(
                    "SYSTEM",
                    "E-Class 로그인이 필요합니다. 로그인 후 다시 업데이트해 주세요.",
                )
        elif result.status is SyncStatus.FAILED:
            if not self._user_task_running:
                self._set_operation_state(
                    UiOperationState.SYNCING,
                    tool="E-Class MCP / MySQL",
                    progress=100,
                    detail="동기화 실패",
                )
            lecture_summary = self.query_one("#lecture-summary", Static)
            lecture_log = self.query_one("#lecture-checklist", RichLog)
            assignment_log = self.query_one("#assignment-checklist", RichLog)
            lecture_summary.update("동기화 실패")
            lecture_log.clear()
            lecture_log.write("E-Class 또는 MySQL 연결 확인 필요")
            assignment_log.clear()
            assignment_log.write("동기화 실패")
            if trigger is SyncTrigger.MANUAL:
                self._append_transcript(
                    "SYSTEM",
                    "E-Class 정보 업데이트에 실패했습니다. E-Class 또는 MySQL 연결을 확인해 주세요.",
                )
        elif result.status is SyncStatus.SKIPPED and trigger is SyncTrigger.MANUAL:
            # Startup/Heartbeat가 이미 같은 전체 Snapshot을 읽는 중이면 중복 브라우저를
            # 띄우지 않는다. 진행 중인 작업이 끝나면 그 결과가 왼쪽 패널에 반영된다.
            self._append_transcript(
                "SYSTEM",
                "E-Class 정보를 이미 확인 중입니다. 완료되면 왼쪽 패널이 갱신됩니다.",
            )
        self._sync_in_progress = False
        if (
            not self._user_task_running
            and self.operation_state
            in {
                UiOperationState.SYNCING,
                UiOperationState.PROACTIVE_ALERT,
                UiOperationState.USER_TASK,
            }
        ):
            self._schedule_ready_reset()
        self._update_sync_status(result)

    def _update_sync_status(self, result: SyncResult | None = None) -> None:
        if not self.is_mounted:
            return
        if result is not None and result.status is SyncStatus.FAILED:
            text = "마지막 E-Class 확인 실패 | 다음 주기에 재시도"
        elif result is not None and result.status is SyncStatus.AUTH_REQUIRED:
            text = "E-Class 동기화 일시 중지"
        else:
            last = self._last_sync_at.strftime("%H:%M:%S") if self._last_sync_at else "-"
            next_time = self._next_sync_at.strftime("%H:%M:%S") if self._next_sync_at else "-"
            text = f"마지막 확인 {last}  |  다음 확인 {next_time}"
        self.query_one("#sync-status", Static).update(text)

    @staticmethod
    def _is_manual_sync_request(message: str) -> bool:
        # 띄어쓰기와 E-Class 표기 차이는 의도 판정에 영향을 주지 않게 한다.
        normalized = re.sub(r"[^0-9a-z가-힣]+", "", message.casefold())
        direct_commands = (
            "지금다시확인",
            "다시확인해",
            "새로고침",
            "동기화해",
        )
        if any(phrase in normalized for phrase in direct_commands):
            return True

        # `업데이트 주기가 뭐야?` 같은 설명 질문은 일반 대화로 남기고, E-Class 대상을
        # 밝힌 명령형 동사가 있을 때만 즉시 동기화로 보낸다.
        has_eclass_scope = any(
            scope in normalized for scope in ("이클래스", "eclass", "학교lms")
        )
        command_verbs = (
            "업데이트해",
            "업데이트좀",
            "갱신해",
            "최신화해",
            "다시읽어",
            "다시불러",
            "다시가져",
            "다시확인해",
            "새로확인해",
            "확인해줘",
        )
        return has_eclass_scope and any(
            verb in normalized for verb in command_verbs
        )

    async def _animate_processing(self) -> None:
        """전문 Agent 작업이 끝날 때까지 현재 SYSTEM 행의 점을 움직인다."""

        while True:
            for text in ("작업 중", "작업 중.", "작업 중..", "작업 중..."):
                self._update_processing_message(text)
                await asyncio.sleep(0.28)

    def _append_transcript(self, speaker: str, message: str) -> None:
        """가운데 창에 사용자 입력과 시스템 응답을 번갈아 쌓는다."""

        self.transcript.append(f"{speaker} > {message}")
        self._trim_transcript()
        self._render_transcript()

    def _trim_transcript(self) -> None:
        """화면 기록 상한을 적용하고 실행 표시 행의 위치도 함께 보정한다."""

        removed = max(0, len(self.transcript) - self.MAX_TRANSCRIPT_ENTRIES)
        if removed == 0:
            return
        self.transcript = self.transcript[removed:]
        if self._processing_message_index is not None:
            self._processing_message_index -= removed
            if self._processing_message_index < 0:
                self._processing_message_index = None

    def _replace_last_system_message(self, message: str) -> None:
        """마지막 시스템 응답을 갱신하거나 새 대화 응답을 덧붙인다."""

        if self.transcript and self.transcript[-1].startswith("SYSTEM >"):
            self.transcript[-1] = f"SYSTEM > {message}"
        else:
            self.transcript.append(f"SYSTEM > {message}")
        self._trim_transcript()
        self._render_transcript()

    def _append_system_delta(self, delta: str) -> None:
        """스트리밍된 답변 조각을 마지막 SYSTEM 메시지에 덧붙인다."""

        if not delta:
            return
        if not self.transcript or not self.transcript[-1].startswith("SYSTEM >"):
            self.transcript.append("SYSTEM > ")
        self.transcript[-1] += delta
        self._trim_transcript()
        self._render_transcript()

    def _begin_processing_message(self) -> None:
        """기존 패널을 유지한 채 작업 상태를 표시할 SYSTEM 행을 준비한다."""

        if self.transcript and self.transcript[-1].startswith("SYSTEM >"):
            self._processing_message_index = len(self.transcript) - 1
        else:
            self.transcript.append("SYSTEM > 작업 중")
            self._trim_transcript()
            self._processing_message_index = len(self.transcript) - 1
        self._update_processing_message("작업 중")

    def _update_processing_message(self, message: str) -> None:
        """동시 능동 알림이 추가돼도 지정된 작업 상태 행만 변경한다."""

        index = self._processing_message_index
        if index is None or not 0 <= index < len(self.transcript):
            return
        self.transcript[index] = f"SYSTEM > {message}"
        self._render_transcript()

    def _finish_processing_message(self, message: str) -> None:
        """움직이던 작업 상태 행을 최종 응답으로 교체한다."""

        index = self._processing_message_index
        self._processing_message_index = None
        if index is not None and 0 <= index < len(self.transcript):
            self.transcript[index] = f"SYSTEM > {message}"
            self._render_transcript()
            return
        self._replace_last_system_message(message)

    def _show_task_result(self, result: str) -> None:
        """전문 작업 결과를 고정 대화창의 작업 상태 행에 표시한다."""

        # USER 입력은 on_input_submitted() 시작에서 이미 추가됐다. 스트리밍 중 만들어진 임시
        # SYSTEM 행이 있으면 최종 결과로 교체하고, 없으면 새 SYSTEM 행을 추가한다.
        self._finish_processing_message(result)

    def _render_transcript(self) -> None:
        """중앙 RichLog를 다시 그려 메시지가 잘리지 않고 스크롤되게 한다."""

        log = self.query_one("#result", RichLog)
        # RichLog에 직접 일부만 수정하는 대신 세션 transcript를 다시 그려 표시 순서를 안정화한다.
        log.clear()
        for entry in self.transcript:
            if entry.startswith("ALERT >"):
                log.write(Text(entry, style="bold #78334F on #F4D9E4"))
            elif entry.startswith("USER >"):
                log.write(Text(entry, style="#274D73 on #EAF2F8"))
            else:
                log.write(Text(entry, style="#1C3854"))

    def _update_clock(self) -> None:
        """상단 바의 시계를 TUI가 실행되는 동안 갱신한다."""

        if self.is_mounted:
            self.query_one("#clock", Static).update(datetime.now().strftime("%H:%M:%S"))

    def _render_lecture_checklist(
        self,
        items: list[LectureChecklistItem],
        *,
        result: SyncResult | None = None,
    ) -> None:
        """MCP가 검증한 영상을 주차·과목별로 묶어 왼쪽 패널에 표시한다.

        영상 제목의 ``1/2``같은 문구를 수강 개수로 오해하지 않도록,
        수강 개수와 완료율은 항상 같은 ``completed`` 판정값으로 새로 계산한다.
        """

        if not self.is_mounted:
            return
        log = self.query_one("#lecture-checklist", RichLog)
        summary = self.query_one("#lecture-summary", Static)
        log.clear()
        enrolled_courses = {
            course.course_id: course.course_name
            for course in (result.course_checklist if result is not None else [])
        }
        if not items and not enrolled_courses:
            summary.update("주차 미확인 - 0 / 0 완료")
            if result is None:
                empty_message = "동기화 대기 중"
            elif result.course_count == 0:
                empty_message = "수강 강의 없음"
            else:
                empty_message = "현재 열린 강의 없음"
            log.write(empty_message)
            return
        # 같은 과목에 영상이 여러 개여도 왼쪽 요약의 분모는 '과목 수'다.
        grouped: dict[tuple[int | None, str], list[LectureChecklistItem]] = {}
        for item in items:
            grouped.setdefault((item.week, item.course_id), []).append(item)

        known_weeks = [week for week, _course_id in grouped if week is not None]
        primary_week = max(known_weeks) if known_weeks else None
        primary_groups = {
            key: lectures
            for key, lectures in grouped.items()
            if key[0] == primary_week
        }
        # 해당 주차 영상이 아예 없는 과목은 해야 할 수강이 없으므로 완료 수에는 포함한다.
        # 단, 개수나 0%를 만들지 않고 행에는 명확히 '강의 없음'만 표시한다.
        primary_course_ids = {course_id for _week, course_id in primary_groups}
        courses_without_lectures = {
            course_id: course_name
            for course_id, course_name in enrolled_courses.items()
            if course_id not in primary_course_ids
        }
        completed_courses = sum(
            1 for lectures in primary_groups.values() if all(item.completed for item in lectures)
        ) + len(courses_without_lectures)
        total_courses = len(enrolled_courses) or len(primary_groups)
        week_label = f"{primary_week}주차" if primary_week is not None else "주차 미확인"
        summary.update(f"{week_label} - {completed_courses} / {total_courses} 완료")

        ordered_groups = sorted(
            grouped.items(),
            key=lambda pair: (
                -(pair[0][0] or 0),
                all(item.completed for item in pair[1]),
                pair[1][0].course_name,
            ),
        )
        rendered_week: int | None | object = object()
        show_week_headings = len({week for week, _course_id in grouped}) > 1
        for (week, _course_id), lectures in ordered_groups:
            if show_week_headings and week != rendered_week:
                if log.lines:
                    log.write("")
                log.write(f"-- {week}주차 --" if week is not None else "-- 주차 미확인 --")
                rendered_week = week
            watched = sum(1 for item in lectures if item.completed)
            total = len(lectures)
            course_completed = watched == total
            marker = "[X]" if course_completed else "[ ]"
            # 영상별 완료 판정과 퍼센트가 엇갈리지 않게 과목 완료율을 같은 개수로 계산한다.
            completion_percent = watched / total * 100
            log.write(f"{marker} {lectures[0].course_name}")
            log.write(f"    {watched} / {total}개 수강 · {completion_percent:.0f}%")

        for _course_id, course_name in sorted(
            courses_without_lectures.items(), key=lambda item: item[1]
        ):
            log.write(f"[-] {course_name}")
            log.write("    강의 없음")

    def _render_assignment_checklist(
        self,
        items: list[AssignmentChecklistItem],
        *,
        result: SyncResult | None = None,
    ) -> None:
        """기본 학기에서 앞으로 7일 안에 마감되는 과제를 강좌별로 표시한다."""

        if not self.is_mounted:
            return
        log = self.query_one("#assignment-checklist", RichLog)
        log.clear()
        if not items:
            if result is None:
                message = "동기화 대기 중"
            elif result.course_count == 0:
                message = "수강 강의 없음"
            else:
                message = "이번 주 과제 없음"
            log.write(message)
            return

        for item in items:
            marker = "[X]" if item.completed else "[ ]"
            due_at = item.due_at.astimezone().strftime("%m/%d %H:%M")
            log.write(f"{marker} {item.course_name}")
            log.write(f"    {item.title} · {due_at}")

    def action_focus_lectures(self) -> None:
        """F1로 왼쪽 강의 체크리스트 스크롤 영역에 초점을 옮긴다."""

        self.query_one("#lecture-checklist", RichLog).focus()

    async def action_stop_playback(self) -> None:
        """F2로 이 TUI가 시작한 현재 영상 하나만 안전하게 중지한다."""

        if self._active_playback_id is None:
            self._append_transcript("SYSTEM", "현재 TUI에서 재생 중인 강의 영상이 없습니다.")
            return
        if self._user_task_running:
            self._append_transcript("SYSTEM", "현재 작업이 끝난 뒤 영상 중지를 다시 눌러 주세요.")
            return

        playback_id = self._active_playback_id
        field = self.query_one("#request", Input)
        field.disabled = True
        self._user_task_running = True
        self._append_transcript("USER", "[F2] 재생 중인 강의 영상 중지")
        self._begin_processing_message()
        self._set_operation_state(
            UiOperationState.PLAYBACK,
            tool="E-Class MCP / Player",
            progress=30,
            detail="영상 중지 요청 중",
        )
        animation = asyncio.create_task(
            self._animate_processing(),
            name="playback-stop-animation",
        )

        async def show_progress(
            event_name: RuntimeProgressEvent,
            agent_name: str | None,
        ) -> None:
            if event_name is RuntimeProgressEvent.AGENT_DELEGATED:
                self._set_operation_state(
                    UiOperationState.PLAYBACK,
                    tool=self._tool_label(agent_name),
                    progress=70,
                    detail="영상 중지 실행 중",
                )
            elif event_name is RuntimeProgressEvent.AUTH_REQUIRED:
                self._set_operation_state(
                    UiOperationState.AUTH_REQUIRED,
                    tool=self._tool_label(agent_name),
                    progress=100,
                    detail="로그인 필요",
                )

        try:
            # ID는 사용자가 입력한 문자열에서 추측하지 않는다. Runtime이 실제 재생 Tool에서
            # 발급받아 보관한 ID인지 다시 확인한 뒤 같은 MCP 프로세스에 직접 결박한다.
            result = await self.runtime.stop_verified_playback(
                playback_id,
                on_progress=show_progress,
            )
        finally:
            animation.cancel()
            with suppress(asyncio.CancelledError):
                await animation
            self._user_task_running = False
            field.disabled = False
            field.focus()

        if result.status is ManagerStatus.AUTH_REQUIRED:
            self._set_operation_state(
                UiOperationState.AUTH_REQUIRED,
                tool="E-Class MCP",
                progress=100,
                detail="로그인 필요",
            )
        elif result.status is ManagerStatus.COMPLETED:
            self._active_playback_id = None
            self._set_operation_state(
                UiOperationState.PLAYBACK,
                tool="E-Class Player",
                progress=100,
                detail="영상 중지 완료",
            )
            self._schedule_ready_reset()
        else:
            self._set_operation_state(
                UiOperationState.PLAYBACK,
                tool="E-Class Player",
                progress=100,
                detail="영상 중지 실패",
            )
        self._finish_processing_message(result.message)

    @staticmethod
    def _is_playback_request(message: str) -> bool:
        """사용자 요청 중 화면 상태를 PLAYBACK으로 보여 줄 명시적 제어 요청을 찾는다."""

        compact = re.sub(r"\s+", "", message.casefold())
        has_media_scope = any(word in compact for word in ("강의", "영상", "재생"))
        has_control = any(
            word in compact
            for word in ("재생", "틀어", "시청", "중지", "정지", "멈춰", "꺼", "닫아")
        )
        return has_media_scope and has_control

    @classmethod
    def _tool_label(cls, agent_name: str | None) -> str:
        """Runtime이 공개한 Agent 이름을 사용자가 이해할 수 있는 Tool 경로로 바꾼다."""

        if agent_name is None:
            return "LMS Runtime"
        return cls._AGENT_TOOL_LABELS.get(agent_name, agent_name)

    def _set_operation_state(
        self,
        state: UiOperationState,
        *,
        tool: str,
        progress: int,
        detail: str,
    ) -> None:
        """고정 레이아웃을 바꾸지 않고 상태·현재 Tool·단계 진행률만 갱신한다."""

        bounded_progress = max(0, min(100, progress))
        if self._state_reset_timer is not None:
            self._state_reset_timer.stop()
            self._state_reset_timer = None
        self.operation_state = state
        self.current_tool = tool
        self.operation_progress = bounded_progress
        if not self.is_mounted:
            return
        status = self.query_one("#status", Static)
        status.set_classes(f"state-{state.value.casefold().replace('_', '-')}")
        completed_units = min(10, bounded_progress // 10)
        progress_bar = "#" * completed_units + "-" * (10 - completed_units)
        status.update(
            f"{state.value} | TOOL: {tool} | [{progress_bar}] {bounded_progress}% | {detail}"
        )

    def _schedule_ready_reset(self, delay: float = 3.0) -> None:
        """완료·알림 상태를 잠시 보여 준 뒤 고정 레이아웃을 READY로 복귀시킨다."""

        if not self.is_mounted:
            return
        if (
            self._user_task_running
            or self._sync_in_progress
            or self.operation_state is UiOperationState.AUTH_REQUIRED
        ):
            return
        if self._state_reset_timer is not None:
            self._state_reset_timer.stop()
        self._state_reset_timer = self.set_timer(
            delay,
            self._return_to_ready,
            name="operation-state-reset",
        )

    def _return_to_ready(self) -> None:
        """새 작업이 시작되지 않았을 때만 완료 표시를 READY 상태로 되돌린다."""

        self._state_reset_timer = None
        if self._user_task_running or self._sync_in_progress:
            return
        if self._active_playback_id is not None:
            self._set_operation_state(
                UiOperationState.PLAYBACK,
                tool="E-Class Player",
                progress=100,
                detail="영상 재생 중 · F2로 중지",
            )
            return
        if self.operation_state is UiOperationState.AUTH_REQUIRED:
            return
        self._set_operation_state(
            UiOperationState.READY,
            tool="대기",
            progress=100,
            detail="요청 대기",
        )

    def _update_active_playback(self, result: ManagerResult) -> bool:
        """검증 결과에 실제 PLAYING 상태가 있을 때만 F2 중지 대상을 저장한다."""

        playback_ids = [
            ref.removeprefix("playback:")
            for ref in result.evidence_refs
            if ref.startswith("playback:") and len(ref) > len("playback:")
        ]
        if "강의 영상 재생을 시작했습니다." in result.message and playback_ids:
            self._active_playback_id = playback_ids[-1]
            return True
        if any(
            phrase in result.message
            for phrase in (
                "강의 영상 재생을 중지했습니다.",
                "강의 영상 재생 시간이 끝나 자동으로 중지했습니다.",
            )
        ):
            self._active_playback_id = None
        return False
