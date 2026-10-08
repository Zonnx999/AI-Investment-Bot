"""
scripts/check_signals.py
========================
Phase 5 신호 엔진 리포트 — 매일 아침 받아볼 핵심 화면.

3개 섹션:
  1) 팩터 점수표 — momentum / value / quality / 종합
  2) 발굴 종목 — 스크리닝 룰 통과 (--screen 시)
  3) 알림 — 마지막으로 **발송·커밋된** 상태 대비 변화 (낙폭/변동성/신용/환율/국면)

이 스크립트는 알림 상태를 **쓰지 않습니다** (읽기 전용 미리보기). 상태는 다이제스트가
발송에 성공한 뒤에만 커밋되므로, 몇 번을 돌려도 다음 실제 발송의 알림이 사라지지 않습니다.

실행:
    python scripts/check_signals.py
    python scripts/check_signals.py CPNG NVDA AAPL
    python scripts/check_signals.py --screen          # 미국 워치리스트 발굴 포함
"""

from __future__ import annotations

import argparse

from src.logger import get_logger
from src.signals import DEFAULT_SIGNAL_TICKERS, REGIME_CONFIRM_DATES, generate_signal_report

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
    if report.regime is not None:
        print(f"  {report.regime.reason_line}")
        if report.regime.pending:
            print(f"  · 변화 관찰 중: {report.regime.pending} "
                  f"{report.regime.pending_count}/{REGIME_CONFIRM_DATES}")
    if report.baseline_missing:
        print("(비교 기준 읽기 실패 — 변화 알림 생략)")
    elif report.first_run:
        print("(비교 기준 없음 — 다이제스트가 처음 발송·커밋된 뒤부터 변화 알림)")

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
    print("\n[3] 알림 — 마지막 발송 상태 대비 변화 (상태는 저장하지 않음)")
    print("-" * 78)
    if report.alerts:
        for a in report.alerts:
            print(f"  {a}")
    else:
        print("  변화 없음 (신규 알림 0건)")
    for note in report.cleared:
        print(f"  · {note}")
    if report.active:
        print(f"  경보 중: {' · '.join(report.active)}")
    for issue in report.data_issues:
        print(f"  ! {issue}")

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
