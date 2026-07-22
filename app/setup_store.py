"""최초 실행에서 받은 일반 설정과 비밀값을 로컬에 안전하게 보관한다.

모델 이름은 일반 JSON에, API 키와 E-Class 계정은 Fernet 암호문에 나눠 저장한다. 두 파일과
암호화 키는 모두 Git에서 제외되는 ``data/config`` 아래에 있으며 비밀값은 로그로 출력하지 않는다.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_SECRET_NAMES = ("openai_api_key", "eclass_username", "eclass_password")
_LEGACY_SETUP_ENV_NAMES = {
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ECLASS_USERNAME",
    "ECLASS_PASSWORD",
    "ECLASS_AUTO_LOGIN",
    "DOWNLOAD_RETENTION_HOURS",
}


class SetupStoreError(RuntimeError):
    """로컬 설정 파일이 손상됐거나 안전하게 읽고 쓸 수 없는 상태."""


class LocalSetupStore:
    """Git에 포함되지 않는 사용자별 최초 실행 설정 저장소."""

    def __init__(self, root: Path = Path("data/config")) -> None:
        self.root = root
        self.settings_path = root / "settings.json"
        self.credentials_path = root / "credentials.enc"
        self.key_path = root / ".credentials.key"

    def load_overrides(self) -> dict[str, Any]:
        """Settings 생성자에 전달할 검증된 일반 설정과 복호화한 비밀값을 반환한다."""

        result: dict[str, Any] = {}
        if self.settings_path.exists():
            payload = self._read_json(self.settings_path)
            model = payload.get("openai_model")
            if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
                raise SetupStoreError("저장된 OpenAI 모델 설정이 올바르지 않습니다.")
            result["openai_model"] = model

        if self.credentials_path.exists():
            try:
                decrypted = Fernet(self._load_key()).decrypt(self.credentials_path.read_bytes())
                payload = json.loads(decrypted.decode("utf-8"))
            except (InvalidToken, OSError, ValueError, json.JSONDecodeError) as exc:
                raise SetupStoreError("저장된 자격증명을 읽을 수 없습니다. 설정을 다시 진행하세요.") from exc
            if not isinstance(payload, dict):
                raise SetupStoreError("저장된 자격증명 형식이 올바르지 않습니다.")
            for name in _SECRET_NAMES:
                value = payload.get(name)
                if not isinstance(value, str) or not value.strip():
                    raise SetupStoreError("저장된 자격증명에 필수 항목이 누락되었습니다.")
                result[name] = value
        return result

    def is_complete(self) -> bool:
        """모델·API 키·E-Class 계정이 모두 저장됐는지 비밀값을 노출하지 않고 확인한다."""

        values = self.load_overrides()
        return bool(values.get("openai_model") and all(values.get(name) for name in _SECRET_NAMES))

    def save(
        self,
        *,
        openai_model: str,
        openai_api_key: str,
        eclass_username: str,
        eclass_password: str,
    ) -> None:
        """일반 설정과 비밀값을 각각 원자적으로 저장한다."""

        model = openai_model.strip()
        if not _MODEL_PATTERN.fullmatch(model):
            raise SetupStoreError("모델 이름은 영문자·숫자와 . _ : - 만 사용할 수 있습니다.")
        secrets = {
            "openai_api_key": openai_api_key.strip(),
            "eclass_username": eclass_username.strip(),
            "eclass_password": eclass_password,
        }
        if not all(secrets.values()):
            raise SetupStoreError("API 키와 E-Class 계정은 비워 둘 수 없습니다.")

        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self.settings_path,
            json.dumps({"openai_model": model}, ensure_ascii=False, separators=(",", ":")).encode(),
        )
        encrypted = Fernet(self._load_or_create_key()).encrypt(
            json.dumps(secrets, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        self._atomic_write(self.credentials_path, encrypted)

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SetupStoreError("저장된 사용자 설정을 읽을 수 없습니다.") from exc
        if not isinstance(payload, dict):
            raise SetupStoreError("저장된 사용자 설정 형식이 올바르지 않습니다.")
        return payload

    def _load_key(self) -> bytes:
        try:
            key = self.key_path.read_bytes().strip()
            Fernet(key)
            return key
        except (OSError, ValueError) as exc:
            raise SetupStoreError("자격증명 암호화 키를 읽을 수 없습니다.") from exc

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self._load_key()
        self.root.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._load_key()
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
        return key

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)


def remove_legacy_setup_env(path: Path = Path(".env")) -> bool:
    """마법사로 이동한 키만 기존 .env에서 제거하고 나머지 연결 설정은 보존한다."""

    if not path.exists():
        return False
    try:
        original = path.read_text(encoding="utf-8")
        kept: list[str] = []
        removed = False
        for line in original.splitlines(keepends=True):
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match and match.group(1) in _LEGACY_SETUP_ENV_NAMES:
                removed = True
                continue
            kept.append(line)
        if not removed:
            return False
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("".join(kept), encoding="utf-8")
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
        return True
    except OSError as exc:
        raise SetupStoreError("기존 .env의 자격증명 항목을 정리하지 못했습니다.") from exc


__all__ = [
    "DEFAULT_OPENAI_MODEL",
    "LocalSetupStore",
    "SetupStoreError",
    "remove_legacy_setup_env",
]
