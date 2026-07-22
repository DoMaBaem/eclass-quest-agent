#!/usr/bin/env bash
set -euo pipefail

# 가상환경을 활성화한 터미널에서 실행한다. ID·비밀번호는 인자로 받거나 저장하지 않는다.
python -m mcp_server.browser.login
