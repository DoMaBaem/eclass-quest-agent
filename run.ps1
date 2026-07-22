$ErrorActionPreference = "Stop"

# Windows PowerShell용 시작 파일. 공통 Python 런처를 사용해 다른 OS와 실행 순서를 통일한다.
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonBin = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonBin -PathType Leaf)) {
    Write-Error "Python 가상환경을 찾을 수 없습니다: .venv`nREADME의 설치 안내에 따라 가상환경을 먼저 생성해 주세요."
    exit 1
}

Set-Location $ProjectRoot
& $PythonBin -m scripts.local_launcher @args
exit $LASTEXITCODE
