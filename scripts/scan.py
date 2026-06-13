"""
scripts/scan.py
===============
유니버스 전수 스캔 (Phase 8) — 오프라인, API 0콜.

저평가 상위 종목 발굴 + 내가 아는 종목의 순위/점수 확인.
먼저 `python scripts/build_universe.py` 로 DB 를 채워야 합니다.

예:
    python scripts/scan.py                      # 전체 상위 30
    python scripts/scan.py --market US --top 50
    python scripts/scan.py --sector Technology
    python scripts/scan.py --min-score 70
    python scripts/scan.py --check NVDA         # 특정 종목 점수/위치
"""

from __future__ import annotations

import argparse

from src import universe
from src.logger import get_logger

logger = get_logger(__name__)


def _print_rows(rows) -> None:
    print(f"  {'순위':>3} {'종목':<10} {'시장':<6} {'종합':>4} {'밸류':>4} {'건전':>4} "
          f"{'ROE':>7} {'시총($B)':>9}  회사")
    print("  " + "-" * 86)
    for i, r in enumerate(rows, 1):
        roe = f"{r.roe:.1f}%" if r.roe is not None else "—"
        mcap = (r.market_cap or 0) / 1e9
        print(f"  {i:>3} {r.symbol:<10} {r.market:<6} {r.total_score:>4} {r.value_score:>4} "
              f"{r.health_score:>4} {roe:>7} {mcap:>9.1f}  {(r.name or '')[:28]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="유니버스 전수 스캔")
    parser.add_argument("--market", choices=["US", "KR", "CRYPTO"], help="시장 필터")
    parser.add_argument("--sector", help="섹터 필터 (예: Technology)")
    parser.add_argument("--top", type=int, default=30, help="상위 N개 (기본 30)")
    parser.add_argument("--min-score", type=int, default=0, help="종합점수 하한")
    parser.add_argument("--check", help="특정 종목 점수/위치 조회")
    args = parser.parse_args()

    if args.check:
        row = universe.lookup(args.check)
        if not row:
            print(f"'{args.check.upper()}' — DB 에 없거나 아직 보강 안 됨. build_universe 먼저 실행.")
            return 1
        print(f"\n[{row.symbol}] {row.name}  ({row.market} · {row.sector})")
        print(f"  종합 {row.total_score} (밸류 {row.value_score} / 건전 {row.health_score})  "
              f"ROE {row.roe:.1f}%  시총 ${row.market_cap/1e9:.1f}B" if row.roe is not None
              else f"  종합 {row.total_score}")
        # 같은 시장 내 순위
        peers = universe.scan(market=row.market, limit=100000)
        rank = next((i for i, r in enumerate(peers, 1) if r.symbol == row.symbol), None)
        if rank:
            print(f"  {row.market} 내 순위: {rank}위 / {len(peers)}종목")
        return 0

    st = universe.stats()
    if not st:
        print("DB 가 비어있습니다. 먼저: python scripts/build_universe.py")
        return 1

    # 시장 지정 시 단일 섹션, 미지정 시 시장별 분리 (점수 체계가 달라 한 랭킹에 못 섞음)
    markets = [args.market] if args.market else ["US", "KR", "CRYPTO"]
    print("=" * 90)
    print(f" 전수 스캔 — 저평가 상위" + (f" [{args.sector}]" if args.sector else ""))
    print("=" * 90)
    for mkt in markets:
        rows = universe.scan(market=mkt, limit=args.top,
                             min_total=args.min_score, sector=args.sector)
        label = {"US": "🇺🇸 미국", "KR": "🇰🇷 한국(ADR)", "CRYPTO": "🪙 크립토"}.get(mkt, mkt)
        print(f"\n[{label}]  (보강 {st.get(f'{mkt}_enriched',0)}/{st.get(f'{mkt}_total',0)})")
        if mkt == "CRYPTO":
            print("  ※ 크립토 점수는 시총순위+변동성 기반 — 주식 펀더멘털 점수와 비교 불가")
        if not rows:
            print("  (해당 없음)")
        else:
            _print_rows(rows)
    print("\n" + "=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
