"""
scripts/check_backtest.py
=========================
백테스트 리포트 (ROADMAP §2.1) — Phase 5 신호 / Phase 6 예측의 과거 성과 검증.

3개 섹션:
  1) SPY 200일선 추세추종 (long/flat) vs 매수후보유 — signals 모멘텀 성분의 매매 번역
  2) 워치리스트 모멘텀 top-N 워크포워드 vs 동일가중 벤치마크
  3) 선행지표 예측(PREDICTORS 일부)의 아웃오브샘플 방향 적중률

실행:
    python scripts/check_backtest.py
    python scripts/check_backtest.py --tickers AAPL MSFT NVDA JPM XOM --top-n 2
    python scripts/check_backtest.py --period 10y

⚠️ 생존 편향(현재 워치리스트로 과거를 봄)·슬리피지 미모델링 — 절대 성과가
   아니라 '신호가 벤치마크 대비 유효했는가' 판단용입니다.
"""

from __future__ import annotations

import argparse

from src.backtest import (
    DEFAULT_COST_BPS,
    buy_and_hold_positions,
    evaluate_lead_lag_oos,
    ma_crossover_positions,
    run_backtest,
    walk_forward_topn,
)
from src.exceptions import QuantBotError
from src.logger import get_logger
from src.predictors import analyze_lead_lag, to_monthly, yoy_growth

logger = get_logger(__name__)

# 워크포워드 기본 유니버스 — screener.US_WATCHLIST 의 축소판 (yfinance 호출 절약,
# 섹터 분산 유지). --tickers 로 교체 가능.
DEFAULT_WF_TICKERS = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "JPM", "UNH", "COST", "XOM", "CAT",
)

# 섹션 3 검증 대상 — predictors.PREDICTORS 중 FRED 기반 3개 (predict_* 내부와
# 동일한 시리즈 구성: FRED YoY → 목표 자산 YoY. 오케스트레이터라 fetch 여기서 수행).
LEAD_LAG_CASES = (
    ("M2SL", "2014-01-01", "BTC-USD", "M2 증가율", "BTC 수익률"),
    ("PERMIT", "2013-01-01", "XHB", "건축허가 증가율", "XHB 수익률"),
    ("UMCSENT", "2013-01-01", "XLY", "소비자심리 증가율", "XLY 수익률"),
)


def _section_trend(period: str) -> None:
    """[1] SPY 200일선 long/flat vs 매수후보유."""
    from src.data_fetcher import fetch_prices
    from src.utils import close_series

    closes = close_series(fetch_prices("SPY", period=period))
    strat = run_backtest(
        closes, ma_crossover_positions(closes, window=200),
        cost_bps=DEFAULT_COST_BPS, name="SPY 200일선 long/flat",
    )
    bench = run_backtest(
        closes, buy_and_hold_positions(closes), cost_bps=0.0, name="SPY 매수후보유",
    )
    print(strat)
    print(bench)
    excess = strat.total_return_pct - bench.total_return_pct
    print(f"  초과수익 {excess:+.1f}%p — 추세추종은 수익보다 MDD 방어"
          f" ({strat.max_drawdown_pct:.1f}% vs {bench.max_drawdown_pct:.1f}%) 관점으로 볼 것")


def _section_walk_forward(tickers: tuple[str, ...], period: str, top_n: int) -> None:
    """[2] 모멘텀 top-N 워크포워드 vs 동일가중 매수후보유."""
    from src.data_fetcher import fetch_prices
    from src.exceptions import DataFetchError
    from src.utils import close_series

    prices = {}
    for t in tickers:
        try:
            prices[t] = close_series(fetch_prices(t, period=period))
        except DataFetchError as e:
            logger.warning("워크포워드 유니버스에서 %s 스킵 — %s", t, e)
    if len(prices) < top_n:
        raise QuantBotError(f"가격 확보 종목 {len(prices)}개 < top_n {top_n} — 섹션 생략")

    result = walk_forward_topn(prices, top_n=top_n)
    print(result)
    print("\n  최근 리밸런스 선택:")
    for d, chosen in result.picks[-6:]:
        print(f"    {str(d)[:10]}  {', '.join(chosen)}")


def _section_lead_lag() -> int:
    """[3] 선행지표 예측 아웃오브샘플 검증. 실패 케이스 수 반환."""
    from src.data_fetcher import fetch_macro, fetch_prices
    from src.utils import close_series

    failures = 0
    for series_id, start, etf, lead_name, tgt_name in LEAD_LAG_CASES:
        try:
            lead_yoy = yoy_growth(to_monthly(fetch_macro(series_id, start=start)))
            tgt_monthly = to_monthly(close_series(fetch_prices(etf, period="max")))
            tgt_yoy = yoy_growth(tgt_monthly)
            tgt_ret = tgt_monthly.pct_change().dropna()

            # 전체 표본으로 best lag 탐색 (predictors 와 동일) → 그 lag 를 OOS 검증
            insample = analyze_lead_lag(lead_yoy, tgt_yoy, lead_name, tgt_name)
            oos = evaluate_lead_lag_oos(
                lead_yoy, tgt_yoy,
                lag=insample.best_lag_months,
                leading_name=lead_name, target_name=tgt_name,
                target_returns=tgt_ret,
            )
        except QuantBotError as e:
            logger.warning("lead-lag 검증 실패: %s→%s — %s", lead_name, tgt_name, e)
            print(f"  ❌ [{lead_name} → {tgt_name}] {type(e).__name__}: {e}\n")
            failures += 1
            continue
        print(oos)
        print(f"  (인샘플 R² {insample.r_squared:.2f}, 상관 {insample.correlation:+.2f}"
              f" — OOS 적중률과 함께 보고 신뢰도 판단)\n")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="백테스트 리포트 (ROADMAP §2.1)")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help=f"워크포워드 유니버스 (기본 {' '.join(DEFAULT_WF_TICKERS)})")
    parser.add_argument("--period", default="5y",
                        help="가격 조회 기간 (기본 5y — 섹션 1·2 공통)")
    parser.add_argument("--top-n", type=int, default=3, help="워크포워드 보유 종목 수 (기본 3)")
    args = parser.parse_args()
    tickers = tuple(t.upper() for t in args.tickers) if args.tickers else DEFAULT_WF_TICKERS

    print("=" * 78)
    print(" 백테스트 리포트 (ROADMAP §2.1 — 신호·예측 과거 성과 검증)")
    print("=" * 78)
    print("  ⚠️ 생존 편향·슬리피지 미모델링 — 벤치마크 대비 상대 유효성 판단용.\n")

    failures = 0

    print("[1] 추세추종 신호 — SPY 200일선 long/flat")
    print("-" * 78)
    try:
        _section_trend(args.period)
    except QuantBotError as e:
        logger.warning("섹션 1 실패 — %s", e)
        print(f"  ❌ {type(e).__name__}: {e}")
        failures += 1
    print()

    print(f"[2] 팩터 신호 — 모멘텀 top-{args.top_n} 워크포워드 ({len(tickers)}종목 유니버스)")
    print("-" * 78)
    try:
        _section_walk_forward(tickers, args.period, args.top_n)
    except QuantBotError as e:
        logger.warning("섹션 2 실패 — %s", e)
        print(f"  ❌ {type(e).__name__}: {e}")
        failures += 1
    print()

    print("[3] 선행지표 예측 — 아웃오브샘플 방향 적중률 (확장 윈도우)")
    print("-" * 78)
    failures += _section_lead_lag()

    print("=" * 78)
    print("해석:")
    print("  · 적중률이 '항상 상승' 베이스라인을 못 넘으면 그 예측은 실전 가치 없음")
    print("  · 워크포워드 초과수익은 거래비용 차감 후 기준 (벤치마크는 무비용)")
    print("  · MDD 축소는 수익률 희생과 트레이드오프 — 샤프로 종합 비교")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
