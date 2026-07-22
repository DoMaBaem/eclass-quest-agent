"""환경변수를 한곳에서 읽고 타입까지 검증한다.

다른 파일은 직접 ``os.getenv()``를 호출하지 않고 이 Settings만 사용한다. 덕분에 개발·운영 설정과
비밀값의 위치가 한 군데로 모이고, 잘못된 URL이나 범위 값은 앱 시작 시점에 발견된다.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.setup_store import LocalSetupStore


class Settings(BaseSettings):
    """개발/배포 환경에 따라 달라지는 설정값.

    비밀값은 ``SecretStr``로 받아 Settings repr·로그에 평문이 나타나지 않게 한다. 실제 세션
    내용은 모델에 넣지 않고 암호화 파일 경로만 받는다.
    """

    # 아직 코드가 사용하지 않는 .env 변수가 있어도 시작이 막히지 않게 extra="ignore"를 둔다.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 비동기 SQLAlchemy가 사용할 MySQL 접속 URL이다.
    mysql_url: str | None = None
    
    # repr=False: Settings 객체가 로그에 찍혀도 API 키 본문은 노출하지 않는다.
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_model: str = "gpt-5.6-terra"
    
    # 문서 분석용 로컬 Qwen(Ollama) 주소. 실제 연결은 후속 단계에서 한다.
    ollama_url: HttpUrl = "http://localhost:11434/api/chat"
    
    eclass_base_url: HttpUrl = "https://learn.hansung.ac.kr"
    # 사용자가 URL에 lang을 직접 지정하지 않으면 MCP가 이 표시 언어를 사용한다.
    eclass_default_language: Literal["ko", "en"] = "ko"

    # 개발 환경의 자동 재로그인에만 사용한다. Agent·MCP 결과·DB로 전달하지 않는다.
    eclass_username: SecretStr | None = Field(default=None, repr=False)
    eclass_password: SecretStr | None = Field(default=None, repr=False)
    # 세션 내용이 아니라 암호화된 세션 파일의 위치다.
    eclass_storage_state_encrypted: Path = Path("data/sessions/eclass_state.enc") # 그 열쇠로 여는 암호화된 로그인 세션 파일
    
    # 운영은 환경변수 키, 개발은 아래 권한 600 로컬 키 파일을 사용한다.
    eclass_session_encryption_key: SecretStr | None = Field(default=None, repr=False) # 열쇠
    eclass_session_key_path: Path = Path("data/sessions/.eclass_state.key") # 열쇠 파일이 있는 위치

    # TUI가 실행 중일 때만 사용하는 시작 동기화 여부와 heartbeat 주기다.
    eclass_sync_on_startup: bool = True
    eclass_sync_interval_minutes: int = Field(default=30, ge=5, le=1_440)

    # 내려받은 분석 파일은 사용자 설정을 받지 않고 항상 24시간 뒤 정리한다.
    download_retention_hours: ClassVar[int] = 24
    download_root: Path = Path("data/downloads")
    download_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1_024, le=200 * 1024 * 1024)
    workflow_audit_path: Path = Path("data/audit/workflow.jsonl")

def get_settings(setup_store: LocalSetupStore | None = None) -> Settings:
    """환경변수에 최초 실행 저장값을 덮어 적용한 Settings를 반환한다."""

    store = setup_store or LocalSetupStore()
    return Settings(**store.load_overrides())
