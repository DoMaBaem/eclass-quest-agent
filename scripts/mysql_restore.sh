#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENVIRONMENT="${1:-}"
BACKUP="${2:-}"
CONFIRM="${3:-}"

if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production" ]]; then
  echo "사용법: $0 <staging|production> <backup.sql.gz> --yes" >&2
  exit 64
fi
if [[ -z "$BACKUP" || "$CONFIRM" != "--yes" ]]; then
  echo "복구는 현재 DB를 변경합니다. 실행하려면 백업 경로 뒤에 --yes를 지정하세요." >&2
  exit 64
fi
if [[ "$BACKUP" != /* ]]; then
  BACKUP="$PROJECT_ROOT/$BACKUP"
fi
if [[ ! -f "$BACKUP" || ! -s "$BACKUP" ]]; then
  echo "유효한 백업 파일을 찾을 수 없습니다." >&2
  exit 66
fi
if [[ -f "${BACKUP}.sha256" ]]; then
  (cd "$(dirname -- "$BACKUP")" && sha256sum --check "$(basename -- "${BACKUP}.sha256")")
fi
gzip -t "$BACKUP"

cd "$PROJECT_ROOT"
gzip -dc "$BACKUP" | docker compose \
  -f docker-compose.yml \
  -f "compose.${ENVIRONMENT}.yml" \
  exec -T mysql sh -eu -c '
    export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
    exec mysql --host=127.0.0.1 --user=root "$MYSQL_DATABASE"
  '

echo "복구 완료: $ENVIRONMENT"
echo "스키마 버전 확인을 위해 ./scripts/migrate.sh $ENVIRONMENT 를 실행하세요."
