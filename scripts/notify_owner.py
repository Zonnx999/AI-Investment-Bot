"""
scripts/notify_owner.py
=======================
소유자(TELEGRAM_CHAT_ID)에게 **평문**(parse_mode=None — CLAUDE.md §4.10 #10) 운영 알림 1통.

용도
  · systemd OnFailure=quant-alert@<alias>.service → `--failed-unit <alias>`
  · scripts/server_autopull.sh 의 CI 실패/대기 알림 → `--text "..."`

설계 제약
  · DB/레플리카를 절대 열지 않는다 (src.storage import 금지) — 실패 알림이 레플리카 충돌을
    만들면 안 되고(§4.10 #9 '한 파일 = 한 프로세스'), Turso 장애 중에도 알림은 가야 하므로.
    notifier+config+http 만 사용 (pandas 도 로딩하지 않음 — 1GB 박스, 테스트로 강제).
  · journal 꼬리는 mask_secrets 로 키를 가린다 (unhandled traceback 은 logging 마스킹을 안 거침, §4.9).
  · 항상 exit 0 (알림 실패가 또 다른 실패 유닛을 만들지 않도록). argparse 사용법 오류만 예외.
  · send_digest 가 exit 20(강등 보류 — 앱이 이미 소유자에게 DM 함)으로 끝난 경우 중복 DM 생략.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from src.http import mask_secrets
from src.logger import get_logger
from src.notifier import send_safe

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

UNITS = {
    "bot": "quant-bot.service",
    "digest-kr": "quant-digest@kr.service",
    "digest-us": "quant-digest@us.service",
    "universe": "quant-universe.service",
    "kr-refresh": "quant-kr-refresh.service",
    "autopull": "quant-autopull.service",
}
APP_NOTIFIED_EXIT = {"20"}       # send_digest: 강등 보류 + 소유자 DM 완료
TAIL_LINES = 12
MAX_LINE = 200
MAX_TEXT = 3800                  # 텔레그램 4096 한도 + 여유


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("%s 실행 실패: %s", cmd[0], e)
        return ""


def unit_status(unit: str) -> dict[str, str]:
    """systemctl show → {"Result": ..., "ExecMainStatus": ...}. systemctl 없으면 {}."""
    out = _run(["systemctl", "show", unit, "-p", "Result", "-p", "ExecMainStatus"])
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def format_failure(alias: str, unit: str, status: dict[str, str], tail: str, now: datetime) -> str:
    """순수 함수 — 테스트 대상. 평문, 키 마스킹, MAX_TEXT 자 이하."""
    lines = [
        f"[quant-bot 실패] {unit} ({alias})",
        f"결과: {status.get('Result', '?')} (exit {status.get('ExecMainStatus', '?')})",
        f"시각: {now:%Y-%m-%d %H:%M} KST",
    ]
    if tail:
        # 마스킹을 '먼저' — 줄 절단(MAX_LINE) 경계에 걸친 키는 앞부분만 남아 전체 문자열 치환에
        # 안 걸리고 접두부가 DM 으로 샌다 (SEC-01). 마지막 mask_secrets 는 이중 안전망.
        tail = mask_secrets(tail)
        lines.append("--- 최근 로그 ---")
        lines += [ln[:MAX_LINE] for ln in tail.splitlines()[-TAIL_LINES:]]
    lines.append(f"조치: journalctl -u {unit} -n 50 --no-pager")
    return mask_secrets("\n".join(lines))[:MAX_TEXT]


def _notify(args: argparse.Namespace) -> None:
    if args.text:
        send_safe(mask_secrets(args.text)[:MAX_TEXT], parse_mode=None)
        return

    unit = UNITS[args.failed_unit]
    status = unit_status(unit)
    if status.get("ExecMainStatus") in APP_NOTIFIED_EXIT:
        logger.info("%s exit %s — 앱이 이미 소유자에게 알림, 중복 DM 생략",
                    unit, status["ExecMainStatus"])
        return
    tail = _run(["journalctl", "-u", unit, "-n", str(TAIL_LINES), "--no-pager", "-o", "cat"])
    text = format_failure(args.failed_unit, unit, status, tail, datetime.now(KST))
    send_safe(text, parse_mode=None)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="소유자 평문 운영 알림")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--failed-unit", choices=sorted(UNITS))
    g.add_argument("--text")
    args = p.parse_args(argv)
    try:
        _notify(args)
    except Exception:  # noqa: BLE001 — 최후 fallback: 알림 실패가 또 하나의 실패 유닛을 만들면 안 됨
        logger.exception("소유자 알림 실패 — 무시 (exit 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
