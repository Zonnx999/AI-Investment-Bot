"""
scripts/screen_value.py
=======================
가치주 스크리너 일괄 실행 → dashboard/screener_data.json 으로 저장.

대시보드는 `dashboard/index.html` 을 브라우저로 열면 같은 폴더의
`screener_data.json` 을 자동으로 fetch 합니다.

실행:
    source .venv/bin/activate
    python scripts/screen_value.py                # 전체 (미국+한국+크립토)
    python scripts/screen_value.py --skip-kr      # 한국 제외
    python scripts/screen_value.py --crypto-top 30
    python scripts/screen_value.py --us-only      # 미국만

소요 시간 (예상)
    미국 40종목  ≈ 1~2분
    한국 15종목  ≈ 30초 (FMP 가 막으면 더 빠름)
    크립토 50개  ≈ 5초 (단일 호출)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


from src.logger import get_logger
from src.screener import (
    KR_WATCHLIST,
    US_WATCHLIST,
    screen_crypto,
    screen_watchlist,
)

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
OUTPUT_PATH = DASHBOARD_DIR / "screener_data.json"

KST = ZoneInfo("Asia/Seoul")


def main(skip_kr: bool, us_only: bool, crypto_top: int) -> None:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(tz=KST).isoformat(timespec="seconds")
    logger.info("=" * 60)
    logger.info("가치주 스크리너 시작 (%s)", started_at)
    logger.info("=" * 60)

    all_data: dict = {
        "_meta": {
            "generated_at": started_at,
            "us_count": 0,
            "kr_count": 0,
            "crypto_count": 0,
        }
    }

    # ---- 미국 ----
    us_results = screen_watchlist(US_WATCHLIST, country_label="미국")
    all_data["미국"] = us_results
    all_data["_meta"]["us_count"] = len(us_results)

    if not us_only:
        # ---- 한국 ----
        if not skip_kr:
            kr_results = screen_watchlist(KR_WATCHLIST, country_label="한국")
            all_data["한국"] = kr_results
            all_data["_meta"]["kr_count"] = len(kr_results)
        else:
            logger.info("[한국] 스킵 (--skip-kr)")
            all_data["한국"] = []

        # ---- 크립토 ----
        crypto_results = screen_crypto(top_n=crypto_top)
        all_data["크립토"] = crypto_results
        all_data["_meta"]["crypto_count"] = len(crypto_results)

    # 일본/중국/인도는 이번 MVP 에서 미구현 — 빈 배열로 명시
    for placeholder in ("일본", "중국", "인도"):
        all_data.setdefault(placeholder, [])

    # ---- 저장 ----
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)

    finished_at = datetime.now(tz=KST).isoformat(timespec="seconds")
    meta = all_data["_meta"]
    logger.info("=" * 60)
    logger.info("완료 (%s)", finished_at)
    logger.info(
        "  미국=%d 종목 / 한국=%d 종목 / 크립토=%d 코인",
        meta["us_count"], meta["kr_count"], meta["crypto_count"],
    )
    logger.info("  → %s", OUTPUT_PATH)
    logger.info("=" * 60)

    # 사용자 안내
    print("\n다음 단계:")
    print(f"  open {DASHBOARD_DIR / 'index.html'}")
    print("  (또는 파일 익스플로러에서 dashboard/index.html 더블클릭)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="가치주 스크리너")
    parser.add_argument(
        "--skip-kr", action="store_true", help="한국 종목 스크리닝 건너뛰기"
    )
    parser.add_argument(
        "--us-only", action="store_true", help="미국만 (한국+크립토 건너뛰기)"
    )
    parser.add_argument(
        "--crypto-top", type=int, default=50, help="크립토 시총 상위 N개 (기본 50)"
    )
    args = parser.parse_args()
    main(skip_kr=args.skip_kr, us_only=args.us_only, crypto_top=args.crypto_top)
