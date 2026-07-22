"""Staging/Production용 파일 기반 Secret을 터미널에 노출하지 않고 초기화한다."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_SECRETS = {
    "mysql_app_password.txt": lambda: secrets.token_urlsafe(36),
    "mysql_root_password.txt": lambda: secrets.token_urlsafe(48),
    "eclass_session_key.txt": lambda: Fernet.generate_key().decode("ascii"),
}
INPUT_SECRETS = {
    "openai_api_key.txt": ("OPENAI_API_KEY", "OpenAI API key"),
    "eclass_username.txt": ("ECLASS_USERNAME", "E-Class 아이디"),
    "eclass_password.txt": ("ECLASS_PASSWORD", "E-Class 비밀번호"),
}


def write_secret(path: Path, value: str) -> bool:
    """기존 값을 덮지 않고 신규 Secret을 원자적으로 권한 0600으로 기록한다."""

    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.write("\n")
    return True


def initialize(environment: str, *, from_env: bool = False) -> tuple[int, int]:
    """환경별 누락 파일만 생성하고 생성/기존 개수를 반환한다."""

    directory = PROJECT_ROOT / "secrets" / environment
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass

    created = 0
    existing = 0
    for filename, factory in GENERATED_SECRETS.items():
        if write_secret(directory / filename, factory()):
            created += 1
        else:
            existing += 1

    for filename, (variable, prompt) in INPUT_SECRETS.items():
        path = directory / filename
        if path.exists():
            existing += 1
            continue
        value = os.environ.get(variable, "") if from_env else getpass.getpass(f"{prompt}: ")
        value = value.rstrip("\r\n")
        if not value:
            raise RuntimeError(f"{variable} 값이 비어 있어 초기화를 중단합니다.")
        if write_secret(path, value):
            created += 1
    return created, existing


def main() -> int:
    parser = argparse.ArgumentParser(description="배포 환경별 Secret 파일을 안전하게 초기화합니다.")
    parser.add_argument("environment", choices=("staging", "production"))
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="대화형 입력 대신 현재 프로세스의 OPENAI/ECLASS 환경변수를 사용합니다.",
    )
    args = parser.parse_args()
    try:
        created, existing = initialize(args.environment, from_env=args.from_env)
    except RuntimeError as exc:
        print(f"Secret 초기화 실패: {exc}")
        return 1
    # 값은 절대 출력하지 않고 파일 개수만 알린다.
    print(f"{args.environment}: Secret {created}개 생성, 기존 {existing}개 유지")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
