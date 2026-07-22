#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENVIRONMENT="${1:-}"

if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production" ]]; then
  echo "사용법: $0 <staging|production>" >&2
  exit 64
fi

cd "$PROJECT_ROOT"
docker compose \
  -f docker-compose.yml \
  -f "compose.${ENVIRONMENT}.yml" \
  run --rm migrate
