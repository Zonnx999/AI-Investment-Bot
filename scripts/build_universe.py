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
import time
from datetime import timedelta

from src import universe
from src.logger import get_logger

logger = get_logger(__name__)


def _make_progress_bar():
    """보강 진행바 콜백 (\\r 갱신, 새 의존성 없음). rate/ETA 포함."""
    start = time.monotonic()
    bar_len = 28

    def cb(i: int, total: int, stats: dict) -> None:
        elapsed = time.monotonic() - start
        rate = i / elapsed if elapsed > 0 else 0
        eta_min = (total - i) / rate / 60 if rate > 0 else 0
        pct = i / total if total else 1.0
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(
            f"\r    [{bar}] {i}/{total} {pct*100:3.0f}%  "
            f"보강{stats['enriched']} 빈값{stats['no_data']} 실패{stats['failed']}  "
            f"{rate:.1f}/s  ETA {eta_min:.1f}분 ",
            end="", flush=True,
        )

    return cb


def main() -> int:
    parser = argparse.ArgumentParser(description="유니버스 DB 구축/갱신")
    parser.add_argument("--discover", action="store_true", help="유니버스 발굴만")
    parser.add_argument("--enrich", action="store_true", help="펀더멘털 보강만")
    parser.add_argument("--limit", type=int, default=None, help="이번에 보강할 종목 수 상한")
    parser.add_argument("--max-age", type=int, default=7, help="재보강 주기(일, 기본 7)")
    parser.add_argument("--force", action="store_true",
                        help="신선도 무시하고 전 종목 재점수 (점수 공식 변경 후). "
                             "원본 데이터는 캐시라 API 호출 거의 없이 빠름")
    args = parser.parse_args()

    do_discover = args.discover or not args.enrich
    do_enrich = args.enrich or not args.discover

    # --force: max_age=0 → updated_at 이 항상 cutoff(=now) 이전이라 전 종목 재보강
    max_age = timedelta(0) if args.force else timedelta(days=args.max_age)

    print("=" * 70)
    print(" 유니버스 DB 빌드")
    print("=" * 70)

    if do_discover:
        print("\n[1] 발굴 (company-screener + CoinGecko)...")
        counts = universe.discover()
        for mkt, n in counts.items():
            print(f"    {mkt}: {n}종목 발굴")

    if do_enrich:
        if args.force:
            print("\n(--force) 신선도 무시 — 전 종목 재점수 (캐시 데이터로 빠름)")
        print("\n[2] 미국 보강 (FMP key-metrics → 점수)...")
        pending = len(universe.symbols_needing_enrichment(max_age))
        print(f"    보강 대상: {pending}종목" + (f" (이번 {args.limit}개)" if args.limit else ""))
        stats = universe.enrich(
            max_age=max_age, limit=args.limit,
            on_progress=_make_progress_bar(),
        )
        print()
        print(f"    완료: 보강 {stats['enriched']} / 데이터없음 {stats['no_data']} / 실패 {stats['failed']}")

        print("\n[3] 한국 보강 (DART 펀더멘털 → ROE/PER/PBR)...")
        kr_pending = len(universe._kr_symbols_needing_enrichment(max_age))
        print(f"    보강 대상: {kr_pending}종목" + (f" (이번 {args.limit}개)" if args.limit else ""))
        kr_stats = universe.enrich_kr(
            max_age=max_age, limit=args.limit,
            on_progress=_make_progress_bar(),
        )
        print()
        print(f"    완료: 보강 {kr_stats['enriched']} / 데이터없음 {kr_stats['no_data']} / 실패 {kr_stats['failed']}")

    # 호스팅 DB(Turso)면 클라우드로 push (로컬 sqlite3 면 no-op)
    from src.storage import get_storage
    get_storage().sync()

    print("\n[현황]")
    for k, v in universe.stats().items():
        print(f"    {k}: {v}")

    remaining = len(universe.symbols_needing_enrichment(max_age))
    if remaining:
        print(f"\n  ⏳ 보강 남음 {remaining}종목 — 이어하려면: python scripts/build_universe.py --enrich")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
