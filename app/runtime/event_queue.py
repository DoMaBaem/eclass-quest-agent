"""단일 TUI 프로세스에서 중복 없이 RuntimeEvent를 전달하는 메모리 큐."""

from __future__ import annotations

import asyncio

from app.schemas.runtime import RuntimeEvent


class RuntimeEventQueue:
    """같은 event_id를 두 번 넣지 않고 종료 시 새 입력을 거부한다."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue()
        self._seen_ids: set[str] = set()
        self._closed = False

    async def publish(self, event: RuntimeEvent) -> bool:
        if self._closed:
            raise RuntimeError("종료된 RuntimeEventQueue에는 이벤트를 넣을 수 없습니다.")
        if event.event_id in self._seen_ids:
            return False
        self._seen_ids.add(event.event_id)
        await self._queue.put(event)
        return True

    async def get(self) -> RuntimeEvent:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
