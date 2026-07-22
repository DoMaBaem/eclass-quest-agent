"""Docker가 TUI 프로세스와 필수 런타임 파일을 비밀값 없이 확인하는 경량 healthcheck."""

from __future__ import annotations

import json
import os
from pathlib import Path


def fail(code: str) -> int:
    print(json.dumps({"component": "app", "status": "FAIL", "error_code": code}, separators=(",", ":")))
    return 1


def app_process_running() -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except (OSError, PermissionError):
            continue
        if b"app.main" in command:
            return True
    return False


def main() -> int:
    if not app_process_running():
        return fail("APP_PROCESS_NOT_RUNNING")
    for path in (Path("/app/data/audit"), Path("/app/data/downloads"), Path("/app/data/sessions")):
        if not path.is_dir() or not os.access(path, os.W_OK):
            return fail("APP_DATA_NOT_WRITABLE")
    browser_root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"))
    if not any(browser_root.glob("chromium-*/chrome-linux*/chrome")):
        return fail("PLAYWRIGHT_BROWSER_MISSING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
