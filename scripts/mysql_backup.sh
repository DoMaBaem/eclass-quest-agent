#!/usr/bin/env bash
set -euo pipefail
umask 077

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENVIRONMENT="${1:-}"
REQUESTED_OUTPUT="${2:-}"

if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production" ]]; then
  echo "사용법: $0 <staging|production> [backup.sql.gz]" >&2
  exit 64
fi

cd "$PROJECT_ROOT"
BACKUP_DIR="$PROJECT_ROOT/data/backups/$ENVIRONMENT"
mkdir -p "$BACKUP_DIR"
if [[ -n "$REQUESTED_OUTPUT" ]]; then
  OUTPUT="$REQUESTED_OUTPUT"
else
  OUTPUT="$BACKUP_DIR/eclass_quest_${ENVIRONMENT}_$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
fi
if [[ "$OUTPUT" != /* ]]; then
  OUTPUT="$PROJECT_ROOT/$OUTPUT"
fi
mkdir -p "$(dirname -- "$OUTPUT")"
TEMPORARY="${OUTPUT}.tmp"
trap 'rm -f -- "$TEMPORARY"' EXIT

docker compose \
  -f docker-compose.yml \
  -f "compose.${ENVIRONMENT}.yml" \
  exec -T mysql sh -eu -c '
    export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
    exec mysqldump --host=127.0.0.1 --user=root \
      --single-transaction --quick --routines --triggers --events \
      --set-gtid-purged=OFF "$MYSQL_DATABASE"
  ' | gzip -9 > "$TEMPORARY"

test -s "$TEMPORARY"
mv -- "$TEMPORARY" "$OUTPUT"
sha256sum "$OUTPUT" > "${OUTPUT}.sha256"
trap - EXIT
echo "백업 완료: $OUTPUT"
