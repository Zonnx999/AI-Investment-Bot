"""
scripts/daily_update.py
=======================
매일 아침 1회 실행하는 데이터 수집 오케스트레이터 (Phase 4).

하는 일: 모든 데이터 소스를 한 번씩 받아 SQLite 캐시를 데움 + 핵심 요약 출력.
이후 같은 날 어떤 스크립트를 돌려도 캐시 적중 → API 호출 없음.
Phase 7 에서 cron/GitHub Actions 이 이 스크립트를 호출할 예정.

실행:
    python scripts/daily_update.py                  # 기본 (캐시 활용)
    python scripts/daily_update.py --refresh        # 캐시 무시하고 전부 새로 수집
    python scripts/daily_update.py --risk CPNG --risk NVDA
    python scripts/daily_update.py --screen         # 가치주 스크리너까지 (FMP 쿼터 주의)

섹션 하나가 실패해도 나머지는 계속 진행하고, 마지막에 실패 목록을 보여줍니다.
하나라도 실패하면 exit code 1 (cron 알림용).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from src.exceptions import QuantBotError
from src.logger import get_logger
from src.storage import get_storage

logger = get_logger(__name__)

DEFAULT_RISK_TICKERS = ["CPNG", "NVDA"]


def _section_market_regime() -> str:
    from src.macro_analyzer import market_summary

    summary = market_summary(period="6mo")
    regime = summary["regime"]
    return f"{regime.regime} (점수 {regime.score:+d}, 실패 {len(regime.failures)}건)"


def _section_macro_dashboard() -> str:
    from src.data_fetcher import fetch_macro_dashboard

    df = fetch_macro_dashboard()
    return f"{df.shape[1]}개 시리즈, 최근 관측 {df.index.max().date()}"


def _section_korea_trade() -> str:
    from src.data_fetcher import fetch_korea_trade

    df = fetch_korea_trade()
    return f"{df.shape[1]}개 시리즈, 최근 관측 {df.index.max().date()}"


def _section_crypto() -> str:
    from src.data_fetcher import fetch_crypto

    parts = []
    for coin in ("bitcoin", "ethereum"):
        df = fetch_crypto(coin, days=180)
        parts.append(f"{coin} ${df['price'].iloc[-1]:,.0f}")
    return ", ".join(parts)


def _make_risk_section(ticker: str):
    def run() -> str:
        from src.risk_engine import risk_report

        rep = risk_report(ticker)
        return (
            f"${rep['current_price']:,.2f}, vol {rep['annualized_vol_pct']:.0f}%, "
            f"VaR95 {rep['var_95_hist_pct']:.1f}%, MDD {rep['max_drawdown'].max_dd_pct:.0f}%"
        )

    return run


def _section_screener() -> str:
    from src.screener import US_WATCHLIST, screen_watchlist

    rows = screen_watchlist(US_WATCHLIST, country_label="미국")
    top = rows[0] if rows else None
    head = f"{top['symbol']} {top['total_score']}점" if top else "없음"
    return f"{len(rows)}종목 스캔, 1위 {head}"


def _section_predictions() -> str:
    from src.predictors import PREDICTORS

    parts = []
    for name, predict in PREDICTORS.items():
        try:
            r = predict()
            flag = "" if r.reliable else "?"
            parts.append(f"{name.split(' → ')[-1].split('(')[0]} {r.direction[:2]}{flag}")
        except QuantBotError:
            parts.append(f"{name} ✗")
    return " | ".join(parts)


def _make_signals_section(tickers: list[str]):
    def run() -> str:
        from src.signals import generate_signal_report

        report = generate_signal_report(tickers=tuple(tickers))
        n_alerts = len(report.alerts)
        seed = " (첫 실행 — 상태 시딩)" if report.first_run else ""
        return f"팩터 {len(report.factors)}종목, 알림 {n_alerts}건{seed}"

    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 데이터 수집 오케스트레이터")
    parser.add_argument("--refresh", action="store_true", help="캐시 무시하고 전부 새로 수집")
    parser.add_argument("--risk", action="append", metavar="TICKER",
                        help="리스크 리포트 대상 (반복 지정 가능, 기본 CPNG NVDA)")
    parser.add_argument("--screen", action="store_true",
                        help="가치주 스크리너 포함 (FMP 호출 다수 — 쿼터 주의)")
    args = parser.parse_args()

    if args.refresh:
        os.environ["QUANT_BOT_CACHE"] = "refresh"  # 읽기만 끄고 캐시는 갱신

    risk_tickers = [t.upper() for t in (args.risk or DEFAULT_RISK_TICKERS)]

    sections: list[tuple[str, callable]] = [
        ("시장 국면 (cross-asset + regime)", _section_market_regime),
        ("FRED 거시 대시보드", _section_macro_dashboard),
        ("한국 수출입", _section_korea_trade),
        ("암호화폐 (BTC/ETH)", _section_crypto),
    ]
    sections += [(f"리스크 리포트 {t}", _make_risk_section(t)) for t in risk_tickers]
    sections.append(("선행지표 예측 (alt-data)", _section_predictions))
    sections.append(("신호 엔진 (팩터 + 알림)", _make_signals_section(risk_tickers)))
    if args.screen:
        sections.append(("가치주 스크리너 (미국)", _section_screener))

    print("=" * 78)
    print(" 일일 데이터 업데이트" + ("  [--refresh: 캐시 강제 갱신]" if args.refresh else ""))
    print("=" * 78)

    failures: list[str] = []
    for name, run in sections:
        t0 = time.monotonic()
        try:
            detail = run()
            status = "✅"
        except QuantBotError as e:
            logger.exception("섹션 실패: %s", name)
            detail = f"{type(e).__name__}: {e}"
            status = "❌"
            failures.append(name)
        except Exception:  # noqa: BLE001 — 오케스트레이터 최후 방어선: 한 섹션이 전체를 못 막게
            logger.exception("섹션 예상치 못한 실패: %s", name)
            detail = "예상치 못한 예외 (로그 참조)"
            status = "❌"
            failures.append(name)
        elapsed = time.monotonic() - t0
        print(f"  {status} {name:<34} {elapsed:>5.1f}s  {detail}")

    print("-" * 78)
    stats = get_storage().stats()
    total_rows = sum(stats.values())
    print(f"  캐시: {total_rows}행 / {len(stats)}개 네임스페이스  ({get_storage().db_path})")

    if failures:
        print(f"\n  ⚠️ 실패 {len(failures)}건: {', '.join(failures)} — logs/quant_bot.log 확인")
    else:
        print("\n  모든 섹션 완료 — 오늘 하루 캐시 적중으로 재사용됩니다.")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
