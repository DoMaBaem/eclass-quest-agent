# E-Class Quest 배포·운영 가이드

## 1. 배포 형태

이 프로젝트는 여러 회원이 웹으로 접속하는 SaaS가 아니라 **한 사용자가 터미널에서 실행하는
Textual TUI**다. Staging과 Production Compose는 같은 이미지를 사용하지만 DB·volume·Secret을
완전히 분리한다.

| 환경 | Compose override | DB | Secret 디렉터리 | 영속 volume |
|---|---|---|---|---|
| Staging | `compose.staging.yml` | `eclass_quest_staging` | `secrets/staging` | `eclass-quest-staging-*` |
| Production | `compose.production.yml` | `eclass_quest_production` | `secrets/production` | `eclass-quest-production-*` |

별도의 Gateway, Cron, 상시 worker는 없다. TUI 프로세스가 실행 중일 때만 Startup·Heartbeat
동기화가 일어난다.

## 2. 최초 Secret 준비

다음 명령은 DB 비밀번호와 Fernet 키를 생성하고, API 키와 E-Class 계정은 화면에 보이지 않는
입력으로 받는다.

```bash
python scripts/init_deployment_secrets.py staging
python scripts/init_deployment_secrets.py production
```

만들어지는 여섯 파일은 권한 `0600`이며 Git과 이미지에서 제외된다. 기존 파일은 덮어쓰지 않는다.
CI/CD에서는 플랫폼 Secret을 `/run/secrets/mysql_app_password` 등
[`secrets/README.md`](../secrets/README.md)의 동일한 대상 이름으로 마운트한다. 평문 값을
Compose `environment`나 이미지 `ENV`에 직접 적지 않는다.

> DB를 만든 뒤 MySQL 비밀번호 파일만 바꾸면 기존 DB 계정 비밀번호는 자동 변경되지 않는다.
> 키 회전은 DB 계정 변경과 세션 재로그인을 함께 계획해야 한다.

## 3. 구성 검증과 이미지 빌드

```bash
docker compose -f docker-compose.yml -f compose.staging.yml --profile app config --quiet
docker compose -f docker-compose.yml -f compose.production.yml --profile app config --quiet

docker compose -f docker-compose.yml -f compose.production.yml build app migrate
```

운영 override는 개발 `.env`를 앱 컨테이너에 주입하지 않는다. `container_entrypoint.py`가 Secret
파일을 메모리에서 읽고 특수문자를 URL 인코딩하여 `MYSQL_URL`을 만든다. 이미지는 비-root
사용자로 실행되며 Chromium은 공용 읽기 전용 경로에 설치된다. 최초 named app-data volume은
`app-data-init` 일회성 컨테이너가 `1000:1000` 소유권과 세션 디렉터리 권한을 설정하며, 이 작업이
성공해야 앱이 시작된다. 다른 UID로 이미지를 빌드했다면 `APP_UID`, `APP_GID`도 같은 값으로 지정한다.

## 4. DB·Ollama 시작과 Migration

```bash
docker compose -f docker-compose.yml -f compose.production.yml up -d mysql ollama
docker compose -f docker-compose.yml -f compose.production.yml run --rm ollama-model
./scripts/migrate.sh production
```

`ollama-model`은 Ollama healthcheck 뒤 `qwen3:0.6b`를 준비하고 종료하는 일회성 서비스다. 모델은
named volume에 남으므로 이후 실행에서는 다시 다운로드하지 않는다. `migrate`는 MySQL healthcheck가
성공한 뒤 `alembic upgrade head`를 실행하는 일회성 서비스다.
앱도 Migration이 성공해야 시작된다. 배포 순서는 항상 다음과 같다.

```text
새 이미지 빌드 → DB 백업 → MySQL·Ollama health 확인 → 모델 준비 → Alembic upgrade head
→ smoke test → TUI 시작
```

## 5. 구성요소 Smoke test

```bash
./scripts/deploy_smoke.sh production
```

기본 실행은 다음 실제 경계를 각각 검사한다.

- MySQL 연결과 현재 Alembic head
- Playwright Chromium headless 기동
- E-Class MCP stdio 연결과 정확한 Tool registry
- Document MCP stdio 연결과 Tool registry
- Docker가 Ollama API와 `qwen3:0.6b`를 자동으로 준비할 수 있는 네트워크 연결

외부 API와 실제 개인 E-Class는 명시적으로 허용한 경우에만 검사한다.

```bash
./scripts/deploy_smoke.sh production --live-openai --live-eclass
```

출력은 다음처럼 구성요소·상태·오류 코드가 구분된 JSON Lines이며 Secret, 강좌명, 본문, URL은
출력하지 않는다.

```json
{"component":"playwright","status":"PASS","error_code":null,"duration_ms":410,"detail":"chromium=headless-ok"}
{"component":"ollama","status":"FAIL","error_code":"OLLAMA_HEALTH_FAILED","duration_ms":8001,"detail":"ConnectError"}
```

## 6. TUI 실행

TUI는 표준 입력이 필요한 프로그램이므로 백그라운드 `up -d app`보다 터미널을 붙인 실행을 쓴다.

```bash
docker compose \
  -f docker-compose.yml \
  -f compose.production.yml \
  --profile app run --rm app
```

### 로컬 Desktop 이미지와 영상 창

`Dockerfile.desktop`은 LinuxServer Webtop의 HTTPS 웹 데스크톱과 오디오 스트리밍을 사용한다.
TUI·Agent·MCP·Playwright·Chromium이 같은 컨테이너에서 실행되며 headed 강의 창은 사용자의
`https://localhost:3001` 화면에 나타난다. MySQL과 Ollama는 별도 Compose 서비스이며
`qwen3:0.6b` 모델은 `ollama_data` named volume에 저장한다. 웹 데스크톱은 로컬 인터페이스에만 바인딩하며
HTTPS를 사용해야 브라우저의 영상·오디오 기능이 동작한다.

```bash
docker compose --profile desktop up -d --build
```

이 Desktop 모드는 Windows·macOS·Linux 개인 PC 배포용이다. 인터넷에 직접 포트를 공개하는 서버
배포는 Desktop 서비스가 아니라 기존 Production Compose와 인증된 reverse proxy를 사용한다.

## 7. 백업과 복구

백업은 일관된 transaction dump를 gzip과 SHA-256 파일로 저장하며 Secret을 명령 인자에 넣지 않는다.

```bash
./scripts/mysql_backup.sh production
./scripts/mysql_backup.sh staging data/backups/staging/before-release.sql.gz
```

복구는 DB를 변경하므로 `--yes`가 반드시 필요하며, SHA-256 파일이 있으면 먼저 검증한다.

```bash
./scripts/mysql_restore.sh production data/backups/production/파일.sql.gz --yes
./scripts/migrate.sh production
```

Production 백업은 Production에, Staging 백업은 Staging에 복구하는 것을 원칙으로 한다. 백업 파일
역시 개인정보를 포함할 수 있으므로 Git에 넣지 않고 암호화된 별도 저장소에 보관한다.

## 8. 관측과 장애 확인

Compose는 앱·Migration·MySQL·Ollama 로그를 `json-file`로 보존하고 파일 크기와 개수를 제한한다.

```bash
docker compose -f docker-compose.yml -f compose.production.yml ps
docker compose -f docker-compose.yml -f compose.production.yml logs --tail=200 mysql ollama ollama-model migrate app
docker inspect --format '{{json .State.Health}}' eclass-quest-production-app-1
```

앱 healthcheck는 TUI 프로세스, 쓰기 가능한 data volume, Chromium 설치를 확인한다. 더 깊은 장애는
`deployment_smoke.py`의 `MYSQL_*`, `PLAYWRIGHT_*`, `ECLASS_MCP_*`, `DOCUMENT_MCP_*`,
`OPENAI_*`, `OLLAMA_*`, `ECLASS_LIVE_*` 오류 코드로 구성요소를 구분한다. Workflow 실행 단계는
`/app/data/audit/workflow.jsonl`에 민감 원문 없이 남는다.

## 9. Rollback

1. 배포 직전 DB 백업을 보존한다.
2. 이전 이미지 태그로 Compose 이미지를 되돌린다.
3. Alembic downgrade는 revision의 데이터 손실 위험을 검토한 경우에만 수행한다.
4. 스키마가 호환되지 않을 때는 서비스를 멈추고 검증된 DB 백업을 복구한다.
5. `deploy_smoke.sh`가 전부 통과한 뒤 TUI를 다시 시작한다.
