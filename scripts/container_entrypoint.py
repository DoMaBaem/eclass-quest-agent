"""컨테이너 Secret 파일을 환경변수로 안전하게 연결한 뒤 실제 명령을 실행한다.

Docker Compose와 대부분의 배포 플랫폼은 Secret을 ``/run/secrets`` 아래 파일로 제공한다.
애플리케이션 설정 코드를 플랫폼마다 바꾸지 않도록 이 진입점에서 파일을 읽되 값 자체는
출력하지 않는다. 마지막에는 ``exec``로 교체되므로 종료 신호가 TUI/Alembic에 직접 전달된다.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import quote


SECRET_FILE_ENV_MAP = {
    "MYSQL_PASSWORD_FILE": "MYSQL_PASSWORD",
    "OPENAI_API_KEY_FILE": "OPENAI_API_KEY",
    "ECLASS_USERNAME_FILE": "ECLASS_USERNAME",
    "ECLASS_PASSWORD_FILE": "ECLASS_PASSWORD",
    "ECLASS_SESSION_ENCRYPTION_KEY_FILE": "ECLASS_SESSION_ENCRYPTION_KEY",
}


def read_secret(path: str) -> str:
    """Secret 파일 하나를 읽고 파일 끝의 줄바꿈만 제거한다."""

    secret_path = Path(path)
    if not secret_path.is_file():
        raise RuntimeError(f"Secret 파일을 찾을 수 없습니다: {secret_path}")
    if secret_path.stat().st_size > 64 * 1024:
        raise RuntimeError(f"Secret 파일 크기가 비정상적으로 큽니다: {secret_path}")
    value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise RuntimeError(f"Secret 파일이 비어 있습니다: {secret_path}")
    if "\x00" in value:
        raise RuntimeError(f"Secret 파일 형식이 올바르지 않습니다: {secret_path}")
    return value


def load_file_secrets(environment: dict[str, str]) -> None:
    """설정된 ``*_FILE``만 읽어 대응하는 런타임 환경변수에 넣는다."""

    for file_variable, value_variable in SECRET_FILE_ENV_MAP.items():
        path = environment.get(file_variable, "").strip()
        if path:
            environment[value_variable] = read_secret(path)


def build_mysql_url(environment: dict[str, str]) -> str | None:
    """분리된 DB 설정을 asyncmy URL로 조립한다.

    비밀번호에 ``@``, ``/``, ``#`` 등이 포함돼도 URL 문법과 섞이지 않도록 인코딩한다.
    ``MYSQL_HOST``가 없으면 기존 개발용 ``MYSQL_URL``을 그대로 유지한다.
    """

    host = environment.get("MYSQL_HOST", "").strip()
    if not host:
        return environment.get("MYSQL_URL") or None

    user = environment.get("MYSQL_USER", "").strip()
    password = environment.get("MYSQL_PASSWORD", "")
    database = environment.get("MYSQL_DATABASE", "").strip()
    port = environment.get("MYSQL_PORT", "3306").strip()
    if not all((user, password, database, port)):
        raise RuntimeError("MYSQL_HOST 방식에는 USER, PASSWORD, DATABASE, PORT가 모두 필요합니다.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise RuntimeError("MYSQL_HOST 형식이 올바르지 않습니다.")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise RuntimeError("MYSQL_PORT 형식이 올바르지 않습니다.")
    if not re.fullmatch(r"[A-Za-z0-9_$-]+", database):
        raise RuntimeError("MYSQL_DATABASE 형식이 올바르지 않습니다.")
    return (
        "mysql+asyncmy://"
        f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/"
        f"{quote(database, safe='')}?charset=utf8mb4"
    )


def validate_file_secrets(environment: dict[str, str]) -> None:
    """Docker Secret 파일 모드가 빈 값으로 기동되는 실수를 막는다.

    로컬 실행은 ``.env`` 값을 직접 사용하므로 검사하지 않는다. 반면 하나라도 ``*_FILE``을
    지정한 컨테이너는 배포용 Secret 모드로 보고 필요한 값이 모두 준비됐는지 확인한다.
    """

    if not any(environment.get(name, "").strip() for name in SECRET_FILE_ENV_MAP):
        return
    if environment.get("DEPLOYMENT_ROLE", "app").lower() == "migration":
        # Migration 컨테이너는 DB 외부 서비스에 접근하지 않으므로 최소 권한 DB Secret만 받는다.
        if not environment.get("MYSQL_URL"):
            raise RuntimeError("배포 Migration용 MYSQL_URL이 누락되었습니다.")
        return
    required = (
        "MYSQL_URL",
        "OPENAI_API_KEY",
        "ECLASS_USERNAME",
        "ECLASS_PASSWORD",
        "ECLASS_SESSION_ENCRYPTION_KEY",
    )
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise RuntimeError("배포 필수 Secret이 누락되었습니다: " + ", ".join(missing))


def prepare_environment(environment: dict[str, str]) -> None:
    """Secret과 DB URL을 준비하고 불필요한 평문 보조 변수는 제거한다."""

    load_file_secrets(environment)
    mysql_url = build_mysql_url(environment)
    if mysql_url:
        environment["MYSQL_URL"] = mysql_url
    validate_file_secrets(environment)
    # 애플리케이션은 MYSQL_URL만 필요하므로 별도 평문 비밀번호는 자식 프로세스에 넘기지 않는다.
    environment.pop("MYSQL_PASSWORD", None)


def main(argv: list[str] | None = None) -> int:
    """환경을 준비한 뒤 전달받은 프로그램으로 현재 프로세스를 교체한다."""

    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        print("실행할 컨테이너 명령이 없습니다.", file=sys.stderr)
        return 64
    try:
        prepare_environment(os.environ)
    except RuntimeError as exc:
        # 오류에는 변수명·파일 경로만 있고 Secret 값은 포함하지 않는다.
        print(f"배포 설정 오류: {exc}", file=sys.stderr)
        return 78
    os.execvpe(command[0], command, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
