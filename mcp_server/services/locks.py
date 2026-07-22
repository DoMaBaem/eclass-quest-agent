"""사용자별 Playwright 작업을 직렬화하는 프로세스 내부 잠금."""

from __future__ import annotations

import asyncio
from collections import defaultdict


class UserBrowserLockRegistry:
    """한 사용자의 세션 파일과 브라우저 작업이 동시에 갱신되지 않게 한다."""

    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def for_user(self, user_id: str) -> asyncio.Lock:
        return self._locks[user_id]
