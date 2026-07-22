@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python 가상환경을 찾을 수 없습니다: .venv 1>&2
  echo README의 설치 안내에 따라 가상환경을 먼저 생성해 주세요. 1>&2
  exit /b 1
)

".venv\Scripts\python.exe" -m scripts.local_launcher %*
exit /b %ERRORLEVEL%
