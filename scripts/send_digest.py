"""
scripts/send_digest.py
======================
일일 투자 신호 다이제스트를 텔레그램으로 전송 (Phase 7).

매일 아침 cron/launchd 가 호출할 진입점. 신호 + 예측 + 국면을 한 통으로.

매일 팩터 표에는 그날 스크리너가 발굴한 저평가 상위 종목이 자동으로 올라옵니다.

실행:
    python scripts/send_digest.py              # 구독 동기화 → 전 구독자에게 브로드캐스트
    python scripts/send_digest.py --dry-run    # 전송 없이 터미널에만 출력 (미리보기)
    python scripts/send_digest.py --top 8      # 발굴 종목 상위 N개 (기본 6)
    python scripts/send_digest.py --no-sync    # 구독 동기화(getUpdates) 건너뛰고 발송만

가입(Phase 11a, 소유자 승인제): 친구가 봇에게 /start → 소유자에게 승인 요청 알림 →
소유자가 /approve <chat_id> 로 승인하면 다음 실행부터 수신. /stop 으로 해지.
"""

from __future__ import annotations

import argparse

from src.digest import build_daily_digest, send_daily_digest
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 다이제스트 전송 (멀티유저 브로드캐스트)")
    parser.add_argument("--dry-run", action="store_true", help="전송 없이 미리보기만")
    parser.add_argument("--top", type=int, default=6, help="발굴 종목 상위 N개 (기본 6)")
    parser.add_argument("--no-sync", action="store_true",
                        help="구독 동기화(getUpdates) 건너뛰고 발송만")
    args = parser.parse_args()

    if args.dry_run:
        digest = build_daily_digest(top_n=args.top)
        print(digest)  # 미리보기 — stdout deliverable
        return 0

    # 1) 명령 수거 → 요청/승인/거절/해지 처리 (best-effort)
    if not args.no_sync:
        from src.subscribers import sync_subscribers
        sub = sync_subscribers()
        if any(sub.values()):
            print(f"구독 동기화: 요청 {sub['requests']} / 승인 {sub['approved']} "
                  f"/ 거절 {sub['denied']} / 해지 {sub['unsubscribed']}")

    # 2) 전 구독자에게 브로드캐스트
    result = send_daily_digest(top_n=args.top)
    if result["recipients"] == 0:
        print("⚠️ 구독자 없음 — TELEGRAM_CHAT_ID(소유자) 또는 /start 가입 확인")
        return 1
    if result["failed"]:
        print(f"⚠️ 일부 실패: 전송 {result['sent']} / 실패 {result['failed']} "
              f"(대상 {result['recipients']}) — logs/quant_bot.log 확인")
        return 1
    print(f"✅ 브로드캐스트 완료: {result['sent']}명 전송")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
