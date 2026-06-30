#!/usr/bin/env bash
#
# scripts/server_autopull.sh
# ==========================
# 서버측 자동 업데이트 (Phase 11b 배포 보조). systemd timer 가 주기적으로 호출.
# origin/main 이 로컬보다 앞설 때만 reset --hard + (필요 시)의존성 재설치 + 봇 재시작.
# 변경이 없으면 아무 것도 하지 않음 (불필요한 재시작·로그 방지).
#
# 전제 (설정은 docs/DEPLOYMENT.md §6.1):
#  - **ubuntu 유저**로 실행 (git deploy key `~/.ssh/github_deploy` 접근 위해 — root 아님).
#  - `systemctl restart` 는 passwordless sudoers drop-in 으로 허용 (해당 명령만).
#  - `git reset --hard` 안전: `.env`·`data/` 는 미추적이라 보존됨.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BRANCH="main"
SERVICE="quant-bot"

git fetch origin "$BRANCH" --quiet

local_sha="$(git rev-parse @)"
remote_sha="$(git rev-parse "origin/$BRANCH")"

if [ "$local_sha" = "$remote_sha" ]; then
    exit 0   # 이미 최신 — 조용히 종료 (timer 가 자주 돌아도 무해)
fi

echo "[autopull] $(date -u +%FT%TZ) update ${local_sha:0:7} -> ${remote_sha:0:7}"

changed="$(git diff --name-only "$local_sha" "$remote_sha")"
git reset --hard "origin/$BRANCH" --quiet

# 의존성(pyproject) 이 바뀐 경우에만 재설치 — 매 업데이트마다 pip 호출 회피
if echo "$changed" | grep -q "pyproject.toml"; then
    echo "[autopull] pyproject.toml changed -> pip install -e .[hosting]"
    "$REPO/.venv/bin/pip" install -e ".[hosting]" --quiet
fi

# 봇 프로세스가 실제로 쓰는 파일이 바뀐 경우에만 재시작.
# dashboard/*.json(매일 자동 커밋)·docs·tests·CI 만 바뀌면 reset 만 하고 재시작 스킵
# → 봇이 안 쓰는 데이터 커밋으로 매일 불필요하게 재시작되는 것 방지.
# (grep -q 의 종료코드는 grep 구현마다 달라질 수 있어, '제외 후 남는 줄이 있나'를 출력으로 판정)
code_changed="$(printf '%s\n' "$changed" | grep -vE '^(dashboard/|docs/|tests/|README\.md|\.github/)' || true)"
if [ -n "$code_changed" ]; then
    sudo systemctl restart "$SERVICE"
    echo "[autopull] restarted $SERVICE"
else
    echo "[autopull] data/docs only — skip restart"
fi
