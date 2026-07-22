"""Agent Runtime의 입력·출력 경계에서 민감정보와 금지 작업을 차단한다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings


MAX_USER_INPUT_LENGTH = 4_000


class GuardrailViolation(ValueError):
    """사용자에게 안전하게 설명할 수 있는 정책 위반."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GuardedInput:
    """비밀값이 제거되고 길이 검증까지 끝난 사용자 입력."""

    text: str
    explicit_playback_request: bool


_LABELED_SECRET = re.compile(
    r"(?i)(password|passwd|pwd|token|api[_ -]?key|cookie|비밀번호|암호|토큰)\s*[:=]\s*\S+"
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_SESSION_COOKIE = re.compile(r"(?i)\b(MoodleSession|sessionid)\s*=\s*[^\s;]+")
_DISALLOWED_ACTIONS = (
    "과제 제출", "대신 제출", "제출해", "제출해줘", "삭제해", "공지 삭제",
    "자동 출석", "출석 채워", "백그라운드 재생", "몰래 재생",
)
_PLAYBACK_ACTIONS = (
    "재생", "틀어", "시청", "영상 켜", "강의 켜", "중지", "영상 꺼", "강의 꺼",
    "영상 봐", "강의 봐", "그거 봐", "멈춰", "미리보기", "시연", "데모",
)


def guard_user_input(message: str, settings: Settings) -> GuardedInput:
    """사용자 입력을 Agent 문맥에 넣기 전에 길이·비밀값·금지 동작을 검사한다."""

    text = message.strip()
    if not text:
        raise GuardrailViolation("INVALID_REQUEST", "요청 내용을 입력해 주세요.")
    if len(text) > MAX_USER_INPUT_LENGTH:
        raise GuardrailViolation(
            "INPUT_TOO_LONG",
            f"한 번의 요청은 {MAX_USER_INPUT_LENGTH:,}자 이하로 입력해 주세요.",
        )
    configured_secrets = [
        secret.get_secret_value()
        for secret in (settings.eclass_username, settings.eclass_password)
        if secret is not None and secret.get_secret_value()
    ]
    if any(secret in text for secret in configured_secrets) or any(
        pattern.search(text) for pattern in (_LABELED_SECRET, _OPENAI_KEY, _SESSION_COOKIE)
    ):
        raise GuardrailViolation(
            "SECRET_DETECTED",
            "학번·비밀번호·토큰·쿠키는 대화에 입력하지 마세요. 해당 값은 실행 문맥에 전달하지 않았습니다.",
        )
    compact = "".join(text.casefold().split())
    if any("".join(phrase.casefold().split()) in compact for phrase in _DISALLOWED_ACTIONS):
        raise GuardrailViolation(
            "ACTION_NOT_ALLOWED",
            "과제 제출·삭제·자동 출석 취득은 이 서비스에서 수행하지 않습니다.",
        )
    explicit_playback = any("".join(word.split()) in compact for word in _PLAYBACK_ACTIONS)
    return GuardedInput(text=text, explicit_playback_request=explicit_playback)


def require_eclass_url(url: str, settings: Settings) -> str:
    """scheme·host가 설정된 E-Class와 정확히 같은 URL만 허용한다."""

    candidate = urlparse(url)
    base = urlparse(str(settings.eclass_base_url))
    if candidate.scheme not in {"http", "https"} or candidate.netloc != base.netloc:
        raise GuardrailViolation("URL_OUT_OF_SCOPE", "E-Class 외부 주소에는 접근할 수 없습니다.")
    return url


def contained_path(root: Path, candidate: Path) -> Path:
    """심볼릭 링크와 ``..``를 해석한 뒤 지정 루트 안의 경로만 반환한다."""

    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise GuardrailViolation("PATH_OUT_OF_SCOPE", "허용된 임시 저장 경로 밖에는 접근할 수 없습니다.")
    return resolved


def sanitize_output(text: str, settings: Settings, *, download_root: Path | None = None) -> str:
    """TUI 출력 직전에 비밀값·HTML·내부 다운로드 경로를 제거한다."""

    safe = text
    for secret in (settings.eclass_username, settings.eclass_password):
        if secret is not None and secret.get_secret_value():
            safe = safe.replace(secret.get_secret_value(), "[REDACTED]")
    safe = _LABELED_SECRET.sub("[REDACTED]", safe)
    safe = _OPENAI_KEY.sub("[REDACTED]", safe)
    safe = _SESSION_COOKIE.sub("[REDACTED]", safe)
    safe = re.sub(r"(?is)<script\b.*?</script>|<[^>]+>", "", safe)
    if download_root is not None:
        safe = safe.replace(str(download_root.resolve()), "[TEMP_STORAGE]")
    return safe.strip()
