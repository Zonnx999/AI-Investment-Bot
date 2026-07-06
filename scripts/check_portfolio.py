"""
scripts/check_portfolio.py
==========================
포트폴리오 사이징 리포트 (ROADMAP §1.1 13b + 13d).

2개 섹션:
  1) 미국 워치리스트 제안 비중 — 역변동성 → (보유 있으면) 상관 페널티 → Kelly 상한
     파이프라인 결과를 동일가중(1/n)과 비교
  2) All Weather 자산군 검증 (13d) — macro_analyzer 7개 자산군 패널을
     변동성 역가중 vs 동일가중으로 월 리밸런스 리플레이 (max 기간)

실행:
    python scripts/check_portfolio.py
    python scripts/check_portfolio.py --tickers AAPL MSFT NVDA JPM XOM
    python scripts/check_portfolio.py --held SPY QQQ --period 2y

⚠️ 정보 제공용 — 주문 집행 없음. 생존 편향(현재 워치리스트로 과거를 봄)·
   슬리피지 미모델링. 절대 성과가 아니라 '사이징 룰이 동일가중 대비 유효한가'
   판단용입니다.
"""

from __future__ import annotations

import argparse

from src.exceptions import QuantBotError
from src.logger import get_logger
from src.macro_analyzer import DEFAULT_PANEL
from src.portfolio import (
    DEFAULT_MAX_WEIGHT,
    DEFAULT_VOL_LOOKBACK_D,
    equal_weights,
    inverse_vol_weights,
    propose,
    weighted_backtest,
)
from src.screener import US_WATCHLIST

logger = get_logger(__name__)


def _fetch_close_map(tickers, period: str) -> dict:
    """티커별 종가 시리즈 수집 — 실패 종목은 경고 로깅 후 스킵."""
    from src.data_fetcher import fetch_prices
    from src.exceptions import DataFetchError
    from src.utils import close_series

    prices = {}
    for t in tickers:
        try:
            prices[t] = close_series(fetch_prices(t, period=period))
        except DataFetchError as e:
            logger.warning("가격 수집 실패 — %s 스킵: %s", t, e)
    return prices


def _section_watchlist(tickers: tuple[str, ...], held: tuple[str, ...], period: str) -> None:
    """[1] 워치리스트 제안 비중 vs 동일가중."""
    prices = _fetch_close_map(tickers, period)
    if not prices:
        raise QuantBotError("가격 확보 종목 0개 — 섹션 생략 (네트워크/티커 확인)")

    held_prices = _fetch_close_map(held, period) if held else None
    if held and not held_prices:
        logger.warning("보유 종목 가격 확보 실패 — 상관 페널티 없이 진행")
        held_prices = None

    proposal = propose(list(prices), prices, held=held_prices)
    print(proposal)

    eq = 1.0 / len(prices)
    print(f"\n  동일가중 기준선: 1/{len(prices)} = {eq:.1%}")
    tilts = sorted(
        ((sym, w - eq) for sym, w in proposal.weights.items()),
        key=lambda kv: kv[1],
    )
    if tilts:
        top_under = ", ".join(f"{s} {d:+.1%}" for s, d in tilts[:3])
        top_over = ", ".join(f"{s} {d:+.1%}" for s, d in tilts[-3:][::-1])
        print(f"  최대 오버웨이트: {top_over}")
        print(f"  최대 언더웨이트: {top_under}")


def _section_all_weather(cost_bps: float) -> None:
    """[2] 13d — All Weather 자산군 risk parity 리플레이 vs 동일가중."""
    prices = _fetch_close_map(DEFAULT_PANEL.values(), period="max")
    # 티커 → 한국어 라벨로 치환 (macro_analyzer 패널 라벨 재사용)
    label_of = {tkr: label for label, tkr in DEFAULT_PANEL.items()}
    prices = {label_of[t]: s for t, s in prices.items()}
    if len(prices) < 2:
        raise QuantBotError(f"자산군 가격 확보 {len(prices)}개 — 섹션 생략 (네트워크 확인)")

    # 공통 구간 정렬 — 전략(역변동성)과 벤치마크(동일가중)가 같은 기간을 봐야 공정.
    # (BTC 등 늦게 태어난 자산이 시작일을 정함 — 그만큼 짧아지는 트레이드오프를 명시)
    common_start = max(s.first_valid_index() for s in prices.values())
    prices = {k: s.loc[common_start:] for k, s in prices.items()}
    print(f"  공통 데이터 구간 시작: {str(common_start)[:10]} (막내 자산 기준 정렬)\n")

    def _inverse_vol_fn(hist: dict) -> dict:
        return inverse_vol_weights(
            hist, lookback=DEFAULT_VOL_LOOKBACK_D, max_weight=DEFAULT_MAX_WEIGHT
        )

    def _equal_after_warmup(hist: dict) -> dict:
        # 벤치마크에도 같은 워밍업 게이트 — 역변동성이 룩백 미달로 스킵하는 초반
        # 회차를 동일가중도 스킵해야 두 결과의 평가 기간이 정확히 일치 (공정 비교).
        inverse_vol_weights(hist, lookback=DEFAULT_VOL_LOOKBACK_D, max_weight=DEFAULT_MAX_WEIGHT)
        return equal_weights(hist)

    strat = weighted_backtest(
        prices, _inverse_vol_fn, cost_bps=cost_bps,
        name=f"변동성 역가중 (상한 {DEFAULT_MAX_WEIGHT:.0%}, 월 리밸런스)",
    )
    bench = weighted_backtest(
        prices, _equal_after_warmup, cost_bps=cost_bps,
        name="동일가중 (월 리밸런스)",
    )
    print(strat)
    print(bench)

    vol_better = strat.annualized_vol_pct < bench.annualized_vol_pct
    mdd_better = strat.max_drawdown_pct > bench.max_drawdown_pct   # 덜 음수 = 방어
    sharpe_better = strat.sharpe > bench.sharpe
    flag = "✅" if (vol_better and mdd_better) else ("🟡" if (vol_better or mdd_better) else "⚠️")
    print(
        f"  {flag} 판정: 변동성 {'개선' if vol_better else '악화'}"
        f" ({strat.annualized_vol_pct:.1f}% vs {bench.annualized_vol_pct:.1f}%)"
        f" | MDD {'개선' if mdd_better else '악화'}"
        f" ({strat.max_drawdown_pct:.1f}% vs {bench.max_drawdown_pct:.1f}%)"
        f" | 샤프 {'우위' if sharpe_better else '열위'}"
        f" ({strat.sharpe:.2f} vs {bench.sharpe:.2f})"
    )
    print("  → risk parity 의 목적은 수익 극대화가 아니라 리스크 균형 — 변동성·MDD 관점으로 볼 것")


def main() -> int:
    parser = argparse.ArgumentParser(description="포트폴리오 사이징 리포트 (ROADMAP §1.1 13b+13d)")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help=f"후보 유니버스 (기본 US_WATCHLIST {len(US_WATCHLIST)}종목)")
    parser.add_argument("--held", nargs="*", default=None,
                        help="현재 보유 티커 — 상관 페널티 적용 (기본 없음)")
    parser.add_argument("--period", default="2y",
                        help="섹션 1 가격 조회 기간 (기본 2y — Kelly 룩백 252일 충족)")
    parser.add_argument("--cost-bps", type=float, default=10.0,
                        help="섹션 2 편도 거래비용 bps (기본 10)")
    args = parser.parse_args()
    tickers = tuple(t.upper() for t in args.tickers) if args.tickers else US_WATCHLIST
    held = tuple(t.upper() for t in args.held) if args.held else ()

    print("=" * 78)
    print(" 포트폴리오 사이징 리포트 (13b 포지션 사이징 + 13d All Weather 검증)")
    print("=" * 78)
    print("  ⚠️ 정보 제공용 — 주문 집행 없음. 생존 편향·슬리피지 미모델링.\n")

    failures = 0

    print(f"[1] 제안 비중 — 워치리스트 {len(tickers)}종목"
          + (f" (보유 {', '.join(held)} 상관 페널티)" if held else ""))
    print("-" * 78)
    try:
        _section_watchlist(tickers, held, args.period)
    except QuantBotError as e:
        logger.warning("섹션 1 실패 — %s", e)
        print(f"  ❌ {type(e).__name__}: {e}")
        failures += 1
    print()

    print("[2] All Weather 자산군 검증 — 변동성 역가중 vs 동일가중 (월 리밸런스, max 기간)")
    print("-" * 78)
    try:
        _section_all_weather(args.cost_bps)
    except QuantBotError as e:
        logger.warning("섹션 2 실패 — %s", e)
        print(f"  ❌ {type(e).__name__}: {e}")
        failures += 1

    print("=" * 78)
    print("해석:")
    print("  · 제안 비중은 '후보 간 상대 배분' — 종목 선별·점수는 signals/screener 몫")
    print("  · Kelly 상한이 남긴 몫은 현금 — 채우는 게 아니라 비우는 장치")
    print("  · 역변동성의 성공 기준은 수익률이 아니라 변동성·MDD 축소 (샤프로 종합)")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
