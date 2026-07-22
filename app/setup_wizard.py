"""Textual TUI가 열리기 전에 한 번만 실행되는 터미널 설정 마법사."""

from __future__ import annotations

from collections.abc import Callable
from getpass import getpass

from app.setup_store import (
    DEFAULT_OPENAI_MODEL,
    LocalSetupStore,
    SetupStoreError,
    remove_legacy_setup_env,
)


class SetupCancelledError(RuntimeError):
    """사용자가 설정 입력을 취소했거나 입력 스트림을 사용할 수 없는 상태."""


def run_setup_wizard(
    store: LocalSetupStore,
    *,
    force: bool = False,
    input_fn: Callable[[str], str] = input,
    secret_input_fn: Callable[[str], str] = getpass,
    output_fn: Callable[[str], None] = print,
) -> bool:
    """필요할 때만 모델과 자격증명을 입력받아 저장하고 실행 여부를 반환한다."""

    if not force and store.is_complete():
        return False
    try:
        existing = store.load_overrides()
    except SetupStoreError:
        if not force:
            raise
        # 암호문이나 키가 손상돼도 --setup에서는 새 값으로 복구할 수 있어야 한다.
        existing = {}
    output_fn("E-Class Quest 최초 설정을 시작합니다.")
    output_fn("입력한 API 키와 E-Class 계정은 암호화된 로컬 파일로 저장됩니다.")
    try:
        current_model = str(existing.get("openai_model") or DEFAULT_OPENAI_MODEL)
        model = input_fn(f"OpenAI 모델 [{current_model}]: ").strip() or current_model
        api_key = _secret_value(
            "OpenAI API 키",
            existing.get("openai_api_key"),
            secret_input_fn,
        )
        username = _visible_value(
            "E-Class 아이디",
            existing.get("eclass_username"),
            input_fn,
        )
        password = _secret_value(
            "E-Class 비밀번호",
            existing.get("eclass_password"),
            secret_input_fn,
        )
        store.save(
            openai_model=model,
            openai_api_key=api_key,
            eclass_username=username,
            eclass_password=password,
        )
        migrated = remove_legacy_setup_env()
    except (EOFError, KeyboardInterrupt) as exc:
        raise SetupCancelledError("설정 입력이 취소되었습니다.") from exc
    output_fn("설정이 저장되었습니다. TUI를 시작합니다.")
    if migrated:
        output_fn("기존 .env의 API 키·모델·E-Class 계정 항목도 안전하게 정리했습니다.")
    return True


def _secret_value(
    label: str,
    existing: object,
    input_fn: Callable[[str], str],
) -> str:
    saved = isinstance(existing, str) and bool(existing)
    suffix = " (Enter: 기존 값 유지)" if saved else ""
    value = input_fn(f"{label}{suffix}: ")
    if value:
        return value
    if saved:
        return str(existing)
    raise SetupStoreError(f"{label}는 비워 둘 수 없습니다.")


def _visible_value(
    label: str,
    existing: object,
    input_fn: Callable[[str], str],
) -> str:
    """아이디처럼 사용자가 확인해야 하는 값은 일반 입력으로 받는다."""

    saved = isinstance(existing, str) and bool(existing)
    suffix = " (Enter: 기존 값 유지)" if saved else ""
    value = input_fn(f"{label}{suffix}: ").strip()
    if value:
        return value
    if saved:
        return str(existing)
    raise SetupStoreError(f"{label}는 비워 둘 수 없습니다.")


__all__ = ["SetupCancelledError", "run_setup_wizard"]
