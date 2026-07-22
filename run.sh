#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
LOCAL_MYSQL_URL="mysql+asyncmy://eclass_app:local_password@localhost:3306/eclass_quest?charset=utf8mb4"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python 가상환경을 찾을 수 없습니다: .venv" >&2
  echo "먼저 프로젝트 설치 안내에 따라 .venv를 생성해 주세요." >&2
  exit 1
fi

# 도움말은 Docker를 시작하지 않고 바로 보여준다.
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  exec "$PYTHON_BIN" -m app.main "$@"
fi

# 셸 환경변수가 없으면 선택적인 .env 값을 읽고, 그것도 없으면 로컬 Docker 주소를 쓴다.
if [[ -z "${MYSQL_URL:-}" && -f .env ]]; then
  MYSQL_URL="$("$PYTHON_BIN" -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("MYSQL_URL") or "")')"
fi
export MYSQL_URL="${MYSQL_URL:-$LOCAL_MYSQL_URL}"

if [[ "$MYSQL_URL" == "$LOCAL_MYSQL_URL" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker 명령을 찾을 수 없습니다." >&2
    echo "Docker Desktop을 설치하고 WSL 연동을 켠 뒤 다시 실행해 주세요." >&2
    exit 1
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "Docker가 실행되지 않았습니다." >&2
    echo "Docker Desktop을 실행한 뒤 다시 시도해 주세요." >&2
    exit 1
  fi

  echo "[1/3] MySQL을 준비하고 있습니다."
  docker compose up -d mysql

  for ((attempt = 1; attempt <= 60; attempt++)); do
    health="$(docker inspect --format='{{.State.Health.Status}}' eclass-quest-mysql 2>/dev/null || true)"
    if [[ "$health" == "healthy" ]]; then
      break
    fi
    if [[ "$health" == "unhealthy" ]]; then
      echo "MySQL 상태 확인에 실패했습니다. Docker Desktop에서 컨테이너 로그를 확인해 주세요." >&2
      exit 1
    fi
    if [[ "$attempt" -eq 60 ]]; then
      echo "MySQL 준비 시간이 초과되었습니다." >&2
      exit 1
    fi
    sleep 1
  done
else
  echo "[1/3] 사용자가 지정한 MySQL 연결을 사용합니다."
fi

echo "[2/3] 데이터베이스를 최신 상태로 맞추고 있습니다."
"$PYTHON_BIN" -m alembic upgrade head

echo "[3/3] E-Class Quest를 시작합니다."
exec "$PYTHON_BIN" -m app.main "$@"
