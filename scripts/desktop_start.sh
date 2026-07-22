#!/usr/bin/env bash
set -euo pipefail

cd /app

echo "[1/2] 데이터베이스를 최신 상태로 맞추고 있습니다."
/usr/local/bin/python /app/scripts/container_entrypoint.py \
  /usr/local/bin/python -m alembic upgrade head

echo "[2/2] E-Class Quest를 시작합니다."
exec /usr/local/bin/python /app/scripts/container_entrypoint.py \
  /usr/local/bin/python -m app.main
