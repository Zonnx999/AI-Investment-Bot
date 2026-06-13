"""
scripts/check_signals.py
========================
Phase 5 신호 엔진 리포트 — 매일 아침 받아볼 핵심 화면.

3개 섹션:
  1) 팩터 점수표 — momentum / value / quality / 종합
  2) 발굴 종목 — 스크리닝 룰 통과 (--screen 시)
  3) 알림 — 지난 실행 대비 변화 (국면 전환 / 낙폭 돌파 / 변동성 급등)

실행:
    python scripts/check_signals.py
    python scripts/check_signals.py CPNG NVDA AAPL
    python scripts/check_signals.py --screen          # 미국 워치리스트 발굴 포함
"""

from __future__ import annotations

import argparse

from src.logger import get_logger
from src.signals import DEFAULT_SIGNAL_TICKERS, generate_signal_report

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 신호 리포트")
    parser.add_argument("tickers", nargs="*", help="팩터 점수 대상 (기본 CPNG NVDA)")
    parser.add_argument("--screen", action="store_true",
                        help="미국 워치리스트 발굴 스크리닝 포함 (FMP 호출 다수)")
    args = parser.parse_args()

    tickers = tuple(t.upper() for t in args.tickers) or DEFAULT_SIGNAL_TICKERS

    screen_tickers = None
    if args.screen:
        from src.screener import US_WATCHLIST
        screen_tickers = list(US_WATCHLIST)

    report = generate_signal_report(tickers=tickers, screen_tickers=screen_tickers)

    print("=" * 78)
    print(" 일일 신호 리포트 (Phase 5 — Signal Engine)")
    print("=" * 78)
    print(f"\n시장 국면: {report.regime_label}")
    if report.first_run:
        print("(첫 실행 — 비교 기준이 없어 변화 알림은 다음 실행부터)")

    # ---------- 1. 팩터 점수 ----------
    print("\n[1] 팩터 점수  (각 0~100, 높을수록 매력적)")
    print("-" * 78)
    print(f"  {'종목':<8} {'모멘텀':>8} {'밸류':>8} {'퀄리티':>8} {'종합':>8}")
    for f in sorted(report.factors, key=lambda x: x.composite, reverse=True):
        print(f"  {f.ticker:<8} {f.momentum:>8} {f.value:>8} {f.quality:>8} {f.composite:>8}")
    print()
    for f in report.factors:
        if f.notes:
            print(f"  · {f.ticker}: {'; '.join(f.notes)}")

    # ---------- 2. 발굴 종목 ----------
    if report.candidates:
        print("\n[2] 발굴 종목 — 스크리닝 룰 통과")
        print("-" * 78)
        for c in report.candidates:
            pe = f"{c['pe']:.1f}" if c.get("pe") else "—"
            print(f"  {c['ticker']:<8} P/E {pe:>6}   {' / '.join(c['reasons'])}")
    elif report.candidates == [] and not report.first_run:
        pass  # --screen 안 했거나 통과 종목 없음 — 조용히 생략

    # ---------- 3. 알림 ----------
    print("\n[3] 알림 — 지난 실행 대비 변화")
    print("-" * 78)
    if report.alerts:
        for a in report.alerts:
            print(f"  {a}")
    else:
        print("  변화 없음 (신규 알림 0건)")

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
