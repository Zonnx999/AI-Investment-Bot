"""
scripts/send_digest.py
======================
일일 투자 신호 다이제스트를 텔레그램으로 전송 (Phase 7).

매일 아침 cron/launchd 가 호출할 진입점. 신호 + 예측 + 국면을 한 통으로.

매일 팩터 표에는 그날 스크리너가 발굴한 저평가 상위 종목이 자동으로 올라옵니다.

실행:
    python scripts/send_digest.py              # 조립 후 텔레그램 전송
    python scripts/send_digest.py --dry-run    # 전송 없이 터미널에만 출력 (미리보기)
    python scripts/send_digest.py --top 8      # 발굴 종목 상위 N개 (기본 6)
"""

from __future__ import annotations

import argparse

from src.digest import build_daily_digest, send_daily_digest
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 다이제스트 전송")
    parser.add_argument("--dry-run", action="store_true", help="전송 없이 미리보기만")
    parser.add_argument("--top", type=int, default=6, help="발굴 종목 상위 N개 (기본 6)")
    args = parser.parse_args()

    if args.dry_run:
        digest = build_daily_digest(top_n=args.top)
        print(digest)  # 미리보기 — stdout deliverable
        return 0

    ok = send_daily_digest(top_n=args.top)
    if ok:
        print("✅ 텔레그램 전송 완료")
        return 0
    print("❌ 전송 실패 — logs/quant_bot.log 확인 (TELEGRAM_* 설정?)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
