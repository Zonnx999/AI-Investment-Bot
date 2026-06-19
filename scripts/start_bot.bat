@echo off
REM Windows: 봇 수동 실행 스크립트
REM 사용: scripts\start_bot.bat (또는 더블클릭)
REM 절전 모드를 막으려면 Windows 설정 > 전원 > 절전 모드 > "사용 안 함" 으로 설정

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo 가상환경이 없습니다. 먼저 실행하세요:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -e ".[hosting]"
    pause
    exit /b 1
)

.venv\Scripts\python.exe scripts\bot.py
