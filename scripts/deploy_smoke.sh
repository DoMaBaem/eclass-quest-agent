#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENVIRONMENT="${1:-}"
shift || true

if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production" ]]; then
  echo "사용법: $0 <staging|production> [--live-openai] [--live-eclass] [--skip-ollama]" >&2
  exit 64
fi

cd "$PROJECT_ROOT"
COMPOSE=(docker compose -f docker-compose.yml -f "compose.${ENVIRONMENT}.yml")
"${COMPOSE[@]}" --profile app config --quiet
"${COMPOSE[@]}" up -d mysql ollama
"${COMPOSE[@]}" run --rm ollama-model
"${COMPOSE[@]}" run --rm migrate
"${COMPOSE[@]}" --profile app run --rm --no-deps app \
  python scripts/deployment_smoke.py "$@"
