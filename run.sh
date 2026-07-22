#!/usr/bin/env bash
set -euo pipefail

# Linux와 macOS용 얇은 시작 파일이다. 실제 준비 절차는 Windows와 공유하는 Python 런처가 맡는다.
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python 가상환경을 찾을 수 없습니다: .venv" >&2
  echo "README의 설치 안내에 따라 가상환경을 먼저 생성해 주세요." >&2
  exit 1
fi

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m scripts.local_launcher "$@"
