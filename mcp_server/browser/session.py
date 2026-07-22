"""Playwright storage state를 암호화해 보관하고 메모리에서만 복호화한다.

storage state에는 로그인 쿠키가 있으므로 평문 JSON 파일로 남기지 않는다. 개발 환경은 권한 600의
로컬 Fernet 키를, 운영 환경은 Secret Manager가 주입한 키를 사용한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings


class SessionStateError(RuntimeError):
    """세션 파일이 없거나 복호화할 수 없는 경우의 상위 오류."""


class AuthRequiredError(SessionStateError):
    """사용자가 headed 브라우저에서 다시 로그인해야 하는 상태."""


def _load_or_create_key(settings: Settings) -> bytes:
    """운영 키가 있으면 사용하고, 개발에서는 권한 600의 로컬 키를 한 번 생성한다."""

    configured_key = (
        settings.eclass_session_encryption_key.get_secret_value()
        if settings.eclass_session_encryption_key is not None
        else ""
    )
    if configured_key:
        # 운영 환경: .env 파일보다 배포 플랫폼의 Secret 주입을 전제로 한다.
        return configured_key.encode("utf-8")

    key_path = settings.eclass_session_key_path
    if key_path.exists():
        # 개발 환경: 처음 생성한 키를 재사용해야 기존 .enc 파일을 다시 열 수 있다.
        return key_path.read_bytes().strip()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # O_EXCL은 동시에 두 프로세스가 서로 다른 키 파일을 덮어쓰는 일을 막는다.
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as key_file:
        key_file.write(key)
    return key


def save_encrypted_storage_state(settings: Settings, state: dict[str, Any]) -> Path:
    """평문 JSON 파일을 남기지 않고 storage state를 암호문 파일로 바꾼다."""

    destination = settings.eclass_storage_state_encrypted
    destination.parent.mkdir(parents=True, exist_ok=True)
    encrypted = Fernet(_load_or_create_key(settings)).encrypt(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    # 임시 파일을 완성한 뒤 replace하여 중간에 종료돼도 반쪽짜리 세션 파일을 남기지 않는다.
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def load_encrypted_storage_state(settings: Settings) -> dict[str, Any]:
    """암호문을 메모리에서만 복호화해 BrowserContext에 줄 dict로 반환한다."""

    path = settings.eclass_storage_state_encrypted
    if not path.exists():
        raise AuthRequiredError("저장된 E-Class 로그인 세션이 없습니다. scripts/login.sh를 실행하세요.")
    try:
        # 복호화 결과는 메모리의 bytes/dict로만 존재하고 평문 파일로 기록하지 않는다.
        raw_state = Fernet(_load_or_create_key(settings)).decrypt(path.read_bytes())
        state = json.loads(raw_state.decode("utf-8"))
    except (InvalidToken, OSError, ValueError, json.JSONDecodeError) as exc:
        raise AuthRequiredError("E-Class 로그인 세션을 읽을 수 없습니다. 다시 로그인하세요.") from exc
    if not isinstance(state, dict) or "cookies" not in state:
        raise AuthRequiredError("E-Class 로그인 세션 형식이 올바르지 않습니다. 다시 로그인하세요.")
    return state
