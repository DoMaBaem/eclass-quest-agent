"""`.env` 자격증명으로 E-Class 암호화 세션을 즉시 갱신하는 점검용 CLI."""

from __future__ import annotations

import asyncio

from app.config import get_settings
from mcp_server.browser.credential_login import refresh_encrypted_session
from mcp_server.browser.session import AuthRequiredError


async def refresh() -> int:
    """비밀값을 출력하지 않고 자동 로그인 성공 여부와 저장 경로만 알린다."""

    try:
        path = await refresh_encrypted_session(get_settings())
    except AuthRequiredError as exc:
        print(f"AUTH_REQUIRED: {exc}")
        return 2
    print(f"E-Class 자동 로그인과 암호화 세션 갱신에 성공했습니다: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(refresh()))
