"""
scripts/build_universe.py
=========================
전 종목 유니버스 DB 구축/갱신 (Phase 8) — 주 1회 배치.

  --discover  : company-screener(US/KR) + CoinGecko(crypto) 로 유니버스 발굴
  --enrich    : 보강 안 됐거나 오래된 주식만 key-metrics 로 점수화 (재개 가능)
  (옵션 없으면 둘 다 실행)

  --limit N   : 이번 실행에서 보강할 종목 수 상한 (쿼터/시간 조절)
  --max-age D : 며칠 지난 보강을 다시 할지 (기본 7)

예:
    python scripts/build_universe.py                 # 발굴 + 전체 보강 (첫 빌드 ~10분)
    python scripts/build_universe.py --enrich --limit 500   # 보강 500개만 (이어하기)
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from src import universe
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="유니버스 DB 구축/갱신")
    parser.add_argument("--discover", action="store_true", help="유니버스 발굴만")
    parser.add_argument("--enrich", action="store_true", help="펀더멘털 보강만")
    parser.add_argument("--limit", type=int, default=None, help="이번에 보강할 종목 수 상한")
    parser.add_argument("--max-age", type=int, default=7, help="재보강 주기(일, 기본 7)")
    args = parser.parse_args()

    do_discover = args.discover or not args.enrich
    do_enrich = args.enrich or not args.discover

    print("=" * 70)
    print(" 유니버스 DB 빌드")
    print("=" * 70)

    if do_discover:
        print("\n[1] 발굴 (company-screener + CoinGecko)...")
        counts = universe.discover()
        for mkt, n in counts.items():
            print(f"    {mkt}: {n}종목 발굴")

    if do_enrich:
        print("\n[2] 보강 (key-metrics → 점수)...")
        pending = len(universe.symbols_needing_enrichment(timedelta(days=args.max_age)))
        print(f"    보강 대상: {pending}종목" + (f" (이번 {args.limit}개)" if args.limit else ""))
        stats = universe.enrich(max_age=timedelta(days=args.max_age), limit=args.limit)
        print(f"    완료: 보강 {stats['enriched']} / 데이터없음 {stats['no_data']} / 실패 {stats['failed']}")

    print("\n[현황]")
    for k, v in universe.stats().items():
        print(f"    {k}: {v}")

    remaining = len(universe.symbols_needing_enrichment(timedelta(days=args.max_age)))
    if remaining:
        print(f"\n  ⏳ 보강 남음 {remaining}종목 — 이어하려면: python scripts/build_universe.py --enrich")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
