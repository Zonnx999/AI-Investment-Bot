#!/usr/bin/env bash
# WSL2 / Linux: 봇 수동 실행 스크립트
# 사용: bash scripts/start_bot.sh
set -e
cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
    echo "가상환경(.venv)이 없습니다. 먼저 실행하세요:"
    echo "  python3.12 -m venv .venv && pip install -e '.[hosting]'"
    exit 1
fi

source .venv/bin/activate
exec python scripts/bot.py
