"""Windows, macOS, Linux에서 동일한 순서로 로컬 앱을 시작한다.

셸 스크립트에는 가상환경 Python을 찾는 최소 코드만 두고, Docker MySQL 준비·migration·TUI
실행은 이 모듈에서 통합한다. Playwright는 컨테이너가 아닌 호스트 OS에서 실행되므로 사용자가
브라우저 화면과 강의 오디오를 그대로 사용할 수 있다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_MYSQL_URL = (
    "mysql+asyncmy://eclass_app:local_password@localhost:3306/"
    "eclass_quest?charset=utf8mb4"
)
MYSQL_CONTAINER_NAME = "eclass-quest-mysql"


class LauncherError(RuntimeError):
    """사용자가 해결할 수 있는 로컬 실행 환경 오류."""


def _selected_mysql_url() -> str:
    """셸 환경변수, 선택적 .env, 기본 Docker 주소 순으로 MySQL URL을 정한다."""

    configured = os.environ.get("MYSQL_URL", "").strip()
    if configured:
        return configured
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        configured = str(dotenv_values(env_path).get("MYSQL_URL") or "").strip()
    return configured or LOCAL_MYSQL_URL


def _run(command: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """shell 해석 없이 명령을 실행해 Windows와 POSIX에서 동일하게 동작하게 한다."""

    return subprocess.run(
        list(command),
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _require_success(result: subprocess.CompletedProcess[str], message: str) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip()
    raise LauncherError(f"{message}{': ' + detail if detail else ''}")


def _prepare_local_mysql() -> None:
    """Docker Desktop/Engine을 확인하고 Compose MySQL이 healthy가 될 때까지 기다린다."""

    if shutil.which("docker") is None:
        raise LauncherError(
            "Docker 명령을 찾을 수 없습니다. Docker Desktop 또는 Docker Engine을 설치해 주세요."
        )
    if _run(("docker", "info"), capture=True).returncode != 0:
        raise LauncherError("Docker가 실행되지 않았습니다. Docker Desktop 또는 Engine을 시작해 주세요.")

    print("[1/3] MySQL을 준비하고 있습니다.", flush=True)
    _require_success(
        _run(("docker", "compose", "up", "-d", "mysql")),
        "MySQL 컨테이너를 시작하지 못했습니다",
    )

    for _attempt in range(60):
        result = _run(
            (
                "docker",
                "inspect",
                "--format={{.State.Health.Status}}",
                MYSQL_CONTAINER_NAME,
            ),
            capture=True,
        )
        health = result.stdout.strip() if result.returncode == 0 else ""
        if health == "healthy":
            return
        if health == "unhealthy":
            raise LauncherError(
                "MySQL 상태 확인에 실패했습니다. Docker에서 컨테이너 로그를 확인해 주세요."
            )
        time.sleep(1)
    raise LauncherError("MySQL 준비 시간이 초과되었습니다.")


def launch(argv: Sequence[str] | None = None) -> int:
    """필요한 로컬 서비스를 준비한 뒤 현재 가상환경 Python으로 TUI를 실행한다."""

    arguments = list(argv if argv is not None else sys.argv[1:])
    os.chdir(PROJECT_ROOT)

    # 도움말은 Docker 상태와 무관하게 볼 수 있어야 한다.
    if any(argument in {"-h", "--help"} for argument in arguments):
        return _run((sys.executable, "-m", "app.main", *arguments)).returncode

    mysql_url = _selected_mysql_url()
    os.environ["MYSQL_URL"] = mysql_url
    if mysql_url == LOCAL_MYSQL_URL:
        _prepare_local_mysql()
    else:
        print("[1/3] 사용자가 지정한 MySQL 연결을 사용합니다.", flush=True)

    print("[2/3] 데이터베이스를 최신 상태로 맞추고 있습니다.", flush=True)
    _require_success(
        _run((sys.executable, "-m", "alembic", "upgrade", "head")),
        "데이터베이스 migration에 실패했습니다",
    )

    print("[3/3] E-Class Quest를 시작합니다.", flush=True)
    return _run((sys.executable, "-m", "app.main", *arguments)).returncode


def main() -> int:
    try:
        return launch()
    except LauncherError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
