#!/usr/bin/env bash
#
# scripts/server_autopull.sh
# ==========================
# 서버측 자동 배포 (Phase 11b → 2026-10 CI 게이트판). systemd timer(quant-autopull) 가 15분마다 호출.
#
# 흐름: fetch → (배포 기준 = data/.deployed_sha) 와 origin/main 비교 → 코드 변경이면 GitHub CI(pytest)
#       통과 확인 → (requirements.lock 변경 시) 의존성 '먼저' 설치 → reset --hard → 봇 재시작 →
#       60초 기동 확인 → 성공 시에만 .deployed_sha 갱신 / 실패 시 직전 sha 로 롤백 + exit 1(OnFailure DM).
#
# 원칙
#  - 배포 완료 표식은 git HEAD 가 아니라 data/.deployed_sha (부분 실패 시 다음 주기에 자동 재시도).
#  - CI 결과를 모르면(API 오류·레이트리밋·진행 중) '배포 안 함' 이 기본 (보수적).
#  - dashboard/·docs/·tests/·deploy/·*.md·.github/ 만 바뀐 커밋은 CI 없이 반영, 재시작도 안 함.
#  - 다이제스트·재점수가 도는 중엔 코드 교체 금지 (지연 import 가 새 코드를 섞어 읽는 것 방지).
#  - 비상 수동 반영: AUTOPULL_SKIP_CI=1 /home/ubuntu/AI-Investment-Bot/scripts/server_autopull.sh
#
# 전제 (docs/DEPLOYMENT.md §6.1): ubuntu 유저로 실행, `sudo systemctl restart quant-bot` 만 NOPASSWD.
# 스크립트 전체를 main() 으로 감싼 이유: 실행 중 git reset 이 이 파일을 바꿔도 bash 가 이미
# 파싱을 끝낸 함수만 실행하도록 (스크립트 자기 교체 안전).
#
set -euo pipefail

main() {
    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    cd "$REPO"

    BRANCH="main"
    SERVICE="quant-bot"
    GH_REPO="${AUTOPULL_GH_REPO:-Zonnx999/AI-Investment-Bot}"
    CHECK_NAME="${AUTOPULL_CHECK_NAME:-pytest}"
    STATE_DIR="$REPO/data"
    DEPLOYED_FILE="$STATE_DIR/.deployed_sha"
    BAD_FILE="$STATE_DIR/.bad_sha"
    NOTIFIED_FILE="$STATE_DIR/.autopull_notified"
    FETCH_FAIL_FILE="$STATE_DIR/.autopull_fetch_fail"
    FETCH_FAIL_NOTIFY="${AUTOPULL_FETCH_FAIL_NOTIFY:-8}"
    PY="$REPO/.venv/bin/python"
    PIP="$REPO/.venv/bin/pip"
    HEALTH_WAIT="${AUTOPULL_HEALTH_WAIT:-60}"
    NOCODE_RE='^(dashboard/|docs/|tests/|deploy/|\.github/|README\.md$|CLAUDE\.md$)'

    mkdir -p "$STATE_DIR"
    exec 9>"$STATE_DIR/.autopull.lock"
    if ! flock -n 9; then
        log "다른 autopull 실행 중 — 스킵"
        exit 0
    fi

    local u st
    for u in quant-digest@kr.service quant-digest@us.service quant-universe.service quant-kr-refresh.service; do
        st="$(systemctl is-active "$u" 2>/dev/null || true)"
        if [ "$st" = "active" ] || [ "$st" = "activating" ]; then
            log "$u 실행 중 — 다음 주기로 미룸"
            exit 0
        fi
    done

    # 서버 .venv ↔ requirements.lock 불일치 감시 (RVOPS-03) — lock 은 '바뀐 커밋' 에서만 설치되므로
    # lock 이 처음 들어온 배포(구 autopull 이 처리)나 수동 설치본은 CI 가 검증한 스택과 다를 수 있다.
    # 설치는 하지 않고 lock 내용별 1회 소유자 DM 만 (일괄 업그레이드 여부는 소유자 결정).
    lock_drift_check

    # fetch 일시 실패(GitHub·네트워크 장애)는 exit 0 — 15분마다 OnFailure DM 폭주 방지.
    # 연속 FETCH_FAIL_NOTIFY 회(기본 8 = 약 2시간) 실패하면 그때 1회만 알린다.
    if ! git fetch origin "$BRANCH" --quiet; then
        local nfail
        nfail=$(( $(cat "$FETCH_FAIL_FILE" 2>/dev/null || echo 0) + 1 ))
        echo "$nfail" > "$FETCH_FAIL_FILE"
        log "git fetch 실패 (연속 ${nfail}회) — 다음 주기 재시도"
        if [ "$nfail" -eq "$FETCH_FAIL_NOTIFY" ]; then
            notify_once "fetch-fail:$(date -u +%FT%TZ)" \
                "[quant-bot 배포 점검] git fetch 가 ${nfail}회 연속 실패 — GitHub/네트워크/deploy key 확인. (서버는 현재 버전 유지)"
        fi
        exit 0
    fi
    rm -f "$FETCH_FAIL_FILE"
    local remote_sha deployed_sha
    remote_sha="$(git rev-parse "origin/$BRANCH")"
    if [ -s "$DEPLOYED_FILE" ]; then
        deployed_sha="$(cat "$DEPLOYED_FILE")"
    else
        deployed_sha="$(git rev-parse HEAD)"          # 첫 실행: 현재 HEAD 를 배포본으로 간주
        echo "$deployed_sha" > "$DEPLOYED_FILE"
    fi

    if [ "$remote_sha" = "$deployed_sha" ]; then
        exit 0                                        # 최신 — 조용히 종료
    fi
    if [ -s "$BAD_FILE" ] && [ "$(cat "$BAD_FILE")" = "$remote_sha" ]; then
        exit 0                                        # 롤백된 sha — 새 커밋이 올 때까지 재시도 안 함
    fi

    local changed code_changed eff=""
    changed="$(git diff --name-only "$deployed_sha" "$remote_sha")"
    code_changed="$(grep -vE "$NOCODE_RE" <<<"$changed" | grep -v '^$' || true)"

    if [ -n "$code_changed" ]; then
        # 롤백 표식은 remote 가 아니라 '실효 코드 커밋' 기준: 롤백 뒤 [skip ci] 대시보드·docs 커밋이
        # 올라와 remote 가 달라져도 같은 불량 코드를 재배포하지 않는다 (그 비코드 커밋은 다음 코드 커밋과 함께 반영).
        eff="$(last_code_commit "$deployed_sha" "$remote_sha")"
        [ -n "$eff" ] || eff="$remote_sha"
        if [ -s "$BAD_FILE" ] && [ "$(cat "$BAD_FILE")" = "$eff" ]; then
            log "롤백된 코드 커밋 ${eff:0:7} — 새 코드 커밋 대기 (수동 재시도: rm $BAD_FILE)"
            exit 0
        fi
        local gate age
        gate="$(ci_gate "$remote_sha" "$deployed_sha")"
        case "$gate" in
            success) ;;
            failure)
                notify_once "$remote_sha:failure" \
                    "[quant-bot 배포 보류] ${remote_sha:0:7} CI(pytest) 실패 — 서버는 ${deployed_sha:0:7} 유지. GitHub Actions 탭 확인."
                exit 0 ;;
            missing)
                age=$(( $(date +%s) - $(git show -s --format=%ct "$remote_sha") ))
                if [ "$age" -gt 7200 ]; then
                    notify_once "$remote_sha:missing" \
                        "[quant-bot 배포 대기] ${remote_sha:0:7} 의 CI(pytest) 결과가 2시간 넘게 없음. Actions 확인, 급하면 AUTOPULL_SKIP_CI=1 로 수동 반영."
                fi
                log "CI 결과 없음 (${remote_sha:0:7}) — 대기"
                exit 0 ;;
            *)
                # pending/unknown 이 계속되면(CI 멈춤·GitHub API 장애·repo 를 private 로 바꿔 무인증 조회 불가)
                # 코드가 조용히 영구 보류된다 — 이 sha 를 '처음 본' 시각부터 2시간 넘으면 1회 DM (§4.10 #15).
                # 커밋 시각이 아니라 처음 본 시각 기준: 검토 후 늦게 push 한 옛 커밋이 CI 진행 중에 바로 DM 되지 않게.
                local seen_file="$STATE_DIR/.ci_wait_seen" first now
                now=$(date +%s)
                if [ -s "$seen_file" ] && [ "$(cut -d' ' -f1 "$seen_file")" = "$remote_sha" ]; then
                    first="$(cut -d' ' -f2 "$seen_file")"
                else
                    first="$now"
                    echo "$remote_sha $now" > "$seen_file"
                fi
                if [ $(( now - first )) -gt 7200 ]; then
                    notify_once "$remote_sha:$gate" \
                        "[quant-bot 배포 대기] ${remote_sha:0:7} CI 상태 '$gate' 가 2시간 넘게 지속 — 서버는 ${deployed_sha:0:7} 유지. Actions·GitHub API 확인(repo 를 private 로 바꿨다면 autopull 게이트에 토큰 필요), 급하면 AUTOPULL_SKIP_CI=1 로 수동 반영."
                fi
                log "CI 상태 '$gate' (${remote_sha:0:7}) — 다음 주기 재확인"
                exit 0 ;;
        esac
    fi

    log "update ${deployed_sha:0:7} -> ${remote_sha:0:7}"

    # 1) 의존성 '먼저' — 실패하면 코드는 그대로(이전 버전) 두고 exit 0 → 다음 주기 재시도 (DM 은 sha 당 1회)
    local lock_changed=""
    if grep -qx 'requirements.lock' <<<"$changed"; then
        lock_changed=1
        git show "$remote_sha:requirements.lock" > "$STATE_DIR/.requirements.lock.new"
        log "requirements.lock 변경 → pip install -r (코드 교체 전)"
        if ! "$PIP" install --quiet -r "$STATE_DIR/.requirements.lock.new"; then
            # exit 1 이면 지속 실패 시 15분마다 OnFailure DM → 같은 sha 는 1회만 알리고 exit 0 (재시도는 계속)
            log "pip 실패 — 코드 미반영, 다음 주기 재시도"
            notify_once "$remote_sha:pip" \
                "[quant-bot 배포 보류] ${remote_sha:0:7} requirements.lock 설치 실패 — 서버는 ${deployed_sha:0:7} 유지, 15분마다 재시도 중. journalctl -u quant-autopull 확인."
            exit 0
        fi
    fi

    # 2) 코드 교체
    git reset --hard "$remote_sha" --quiet
    if grep -qx 'pyproject.toml' <<<"$changed"; then
        "$PIP" install --quiet --no-deps -e . || log "editable 재등록 실패 (src 는 그대로 반영됨 — 무시 가능)"
    fi

    # 3) 봇이 쓰는 코드가 안 바뀌었으면 재시작 없이 완료
    if [ -z "$code_changed" ]; then
        echo "$remote_sha" > "$DEPLOYED_FILE"
        log "data/docs only — skip restart"
        exit 0
    fi

    # 4) 재시작 + 기동 확인 (Type=notify 라 restart 는 READY=1 까지 대기)
    local n1
    if sudo systemctl restart "$SERVICE"; then
        n1="$(systemctl show -p NRestarts --value "$SERVICE")"
        sleep "$HEALTH_WAIT"
        if [ "$(systemctl is-active "$SERVICE" || true)" = "active" ] \
           && [ "$(systemctl show -p NRestarts --value "$SERVICE")" = "$n1" ]; then
            echo "$remote_sha" > "$DEPLOYED_FILE"
            rm -f "$BAD_FILE"
            log "restarted $SERVICE OK (${remote_sha:0:7})"
            exit 0
        fi
    fi

    # 5) 롤백
    log "ROLLBACK: ${remote_sha:0:7} 기동 실패 → ${deployed_sha:0:7} 복귀"
    echo "$eff" > "$BAD_FILE"                         # remote 가 아니라 실효 코드 커밋 (위 주석 참고)
    git reset --hard "$deployed_sha" --quiet
    if [ -n "$lock_changed" ]; then
        git show "$deployed_sha:requirements.lock" > "$STATE_DIR/.requirements.lock.old" 2>/dev/null \
            && "$PIP" install --quiet -r "$STATE_DIR/.requirements.lock.old" \
            || log "롤백 pip 실패 — 수동 확인 필요"
    fi
    sudo systemctl restart "$SERVICE" || log "롤백 후 재시작도 실패 — 수동 조치 필요"
    exit 1                                            # → OnFailure=quant-alert@autopull (journal 꼬리 포함 DM)
}

log() {
    echo "[autopull] $(date -u +%FT%TZ) $*"
}

# $1=sha → success | failure | pending | missing | unknown  (GitHub 공개 API, 무인증 60회/시간)
ci_conclusion() {
    local sha="$1" body
    if ! body="$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' \
            "https://api.github.com/repos/$GH_REPO/commits/$sha/check-runs?check_name=$CHECK_NAME&per_page=20")"; then
        echo unknown
        return 0
    fi
    # 응답 형태가 기대와 다르면(최상위 비-dict·check_runs 비-list·원소 비-dict) 예외 종료 대신 'unknown'
    # (set -e 로 스크립트가 죽어 15분마다 OnFailure DM 이 가는 것 방지). GitHub Actions 앱의 체크만 인정
    # (다른 앱이 같은 이름의 체크를 올려 게이트를 속이는 것 방지).
    "$PY" -c '
import json, sys
try:
    data = json.loads(sys.stdin.read())
except ValueError:
    print("unknown"); sys.exit(0)
if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
    print("unknown"); sys.exit(0)
if not all(isinstance(x, dict) for x in data["check_runs"]):
    print("unknown"); sys.exit(0)
def is_actions(x):
    app = x.get("app")
    return isinstance(app, dict) and app.get("slug") == "github-actions"
def rid(x):
    v = x.get("id")
    return v if isinstance(v, int) and not isinstance(v, bool) else 0
runs = [x for x in data["check_runs"] if is_actions(x)]
if not runs:
    print("missing"); sys.exit(0)
r = max(runs, key=rid)
if r.get("status") != "completed":
    print("pending")
elif r.get("conclusion") == "success":
    print("success")
elif r.get("conclusion") in ("cancelled", "skipped", "neutral", "stale"):
    print("missing")
else:
    print("failure")
' <<<"$body" || echo unknown
}

# $1=deployed $2=remote → 범위 내 '마지막 코드 커밋' (비코드 경로 제외). 없으면 빈 문자열.
last_code_commit() {
    git log -1 --format=%H "$1..$2" -- . \
        ':(exclude)dashboard' ':(exclude)docs' ':(exclude)tests' ':(exclude)deploy' \
        ':(exclude).github' ':(exclude)README.md' ':(exclude)CLAUDE.md'
}

# remote_sha 자체의 CI 결과가 우선. [skip ci] 데이터 커밋·문서 전용 푸시처럼 remote 에 CI 가 없으면
# 범위 내 '마지막 코드 커밋' 의 CI 로 판정 (그 뒤 커밋은 전부 비코드 경로라 트리 동작 동일).
ci_gate() {
    local remote="$1" deployed="$2" r c
    if [ "${AUTOPULL_SKIP_CI:-0}" = "1" ]; then
        echo success
        return 0
    fi
    r="$(ci_conclusion "$remote")"
    if [ "$r" = "missing" ]; then
        c="$(last_code_commit "$deployed" "$remote")"
        if [ -n "$c" ] && [ "$c" != "$remote" ]; then
            r="$(ci_conclusion "$c")"
        fi
    fi
    echo "$r"
}

# 배포 중인 requirements.lock 과 .venv 설치 버전 비교 → 다르면 lock 내용당 1회 DM. 점검 실패는 무시.
lock_drift_check() {
    local lock="$REPO/requirements.lock" out rc=0 h
    [ -f "$lock" ] || return 0
    out="$("$PY" "$REPO/scripts/lock_drift.py" "$lock" 2>/dev/null)" || rc=$?
    if [ "$rc" -eq 3 ]; then
        h="$(sha256sum "$lock" | cut -c1-12)"
        log "의존성 불일치: $out"
        notify_once "lockdrift:$h" \
            "[quant-bot 의존성 점검] 서버 .venv 가 requirements.lock 과 다름 — CI 가 검증한 스택 ≠ 운영 스택. ${out}. 조치: 발송 시각을 피해 flock -w 600 data/.digest.lock .venv/bin/pip install -r requirements.lock 후 봇 재시작·dry-run 스모크, 또는 서버 pip freeze 로 lock 교체 (DEPLOYMENT 참고)."
    fi
    return 0
}

# 같은 사건은 1회만 DM (키 = "<sha>:<종류>")
notify_once() {
    local key="$1" text="$2"
    if [ -f "$NOTIFIED_FILE" ] && grep -qxF "$key" "$NOTIFIED_FILE"; then
        return 0
    fi
    "$PY" "$REPO/scripts/notify_owner.py" --text "$text" || log "소유자 알림 실패 (무시)"
    echo "$key" >> "$NOTIFIED_FILE"
}

main "$@"
exit 0
