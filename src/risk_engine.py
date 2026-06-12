"""
risk_engine.py
==============
에드워드 소프 식 리스크 엔진.

핵심 질문: "이 자산이 얼만큼 떨어질 수 있나?"

네 가지 답:
  1. VaR / CVaR        — 일별 통계적 최악 손실 (95%, 99% 신뢰수준)
  2. Max Drawdown      — 과거 기간 중 가장 큰 낙폭과 기간
  3. Monte Carlo       — N일 후 가격 분포 시뮬레이션 (GBM)
  4. Scenario Analysis — "매출 -10%, 마진 -2%p" 같은 충격을 가격에 환산

Phase 5 (Signal Engine) 의 알림 룰이 이 함수들의 출력을 입력으로 사용할 예정.

규약 (7단계 리팩토링): VaR/ES 함수는 **일별 수익률 시리즈**를 받습니다.
가격 시리즈는 ``returns_from_prices()`` 로 명시 변환 후 전달하세요.
(과거의 "값이 다 양수면 가격으로 간주" 자동 판별은 페니스톡/저변동
수익률에서 오작동할 수 있어 제거됨.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.exceptions import AnalysisError, InsufficientDataError
from src.utils import TRADING_DAYS_PER_YEAR

# ======================================================================
# 0. 상수 & 유틸
# ======================================================================

MIN_RETURN_POINTS = 20        # VaR/ES 통계가 의미를 갖는 최소 표본 수
_PRICE_LIKE_RATIO = 0.5       # |값|>1 비율이 이 이상이면 가격 시리즈 오입력으로 판정


def returns_from_prices(prices: pd.Series) -> pd.Series:
    """가격 시리즈 → 일별 단순 수익률. (구 `_clean_returns` 의 암묵 변환을 명시 API 로)"""
    p = prices.dropna().squeeze()
    return p.pct_change().dropna()


def _validate_returns(returns: pd.Series, min_points: int = MIN_RETURN_POINTS) -> pd.Series:
    """수익률 시리즈 검증: 표본 수 + '가격을 잘못 넣은' 실수 감지.

    일별 수익률은 ±1.0 (±100%) 을 넘는 값이 드뭅니다. 절반 이상이 |값|>1 이면
    가격 시리즈를 잘못 넣었을 가능성이 압도적 → 조용히 오답을 내는 대신 raise.
    """
    s = returns.dropna().squeeze()
    if len(s) < min_points:
        raise InsufficientDataError(
            f"수익률 표본 {len(s)}개 — 최소 {min_points}개 필요",
            n_points=len(s), required=min_points,
        )
    if (s.abs() > 1.0).mean() > _PRICE_LIKE_RATIO:
        raise AnalysisError(
            "수익률이 아니라 가격 시리즈로 보입니다 — "
            "returns_from_prices(prices) 로 변환 후 호출하세요."
        )
    return s


# ======================================================================
# 1. VaR / CVaR
# ======================================================================


def historical_var(returns: pd.Series, confidence: float = 0.95) -> float:
    """과거 분포 기반 VaR. 가정 없음, 분포가 기형적이어도 OK.

    confidence=0.95 면 하위 5% 분위수를 반환. 결과는 음수 (손실).
    """
    r = _validate_returns(returns)
    return float(np.quantile(r, 1 - confidence))


def parametric_var(returns: pd.Series, confidence: float = 0.95) -> float:
    """정규분포 가정 기반 VaR. 빠르지만 fat-tail 을 과소평가.

    historical 과 비교해서 차이가 크면 분포가 정규분포에서 멀리 떨어진 것.
    """
    from scipy.stats import norm

    r = _validate_returns(returns)
    mu = r.mean()
    sigma = r.std()
    return float(mu - sigma * norm.ppf(confidence))


def expected_shortfall(returns: pd.Series, confidence: float = 0.95) -> float:
    """CVaR = VaR 를 넘는 손실들의 평균. "최악의 5% 가 평균적으로 얼마나 나쁜가."

    VaR 가 하한선이라면 ES 는 그 너머 평균 깊이. fat-tail 자산일수록 VaR 와 ES 차이가 큽니다.
    """
    r = _validate_returns(returns)
    var = historical_var(r, confidence)
    tail = r[r <= var]
    return float(tail.mean()) if len(tail) > 0 else var


# ======================================================================
# 2. Drawdown
# ======================================================================


@dataclass
class DrawdownInfo:
    max_dd_pct: float
    peak_date: pd.Timestamp
    trough_date: pd.Timestamp
    duration_days: int           # peak → trough
    recovery_date: pd.Timestamp | None = None
    recovery_days: int | None = None  # trough → recovery


def drawdown_series(prices: pd.Series) -> pd.Series:
    """매 시점의 낙폭 시계열 (0 또는 음수, 단위: %)."""
    p = prices.dropna().squeeze()
    cummax = p.cummax()
    return ((p - cummax) / cummax) * 100


def max_drawdown(prices: pd.Series) -> DrawdownInfo:
    """최대 낙폭의 크기 + 기간 + 회복 여부를 한 번에 반환."""
    p = prices.dropna().squeeze()
    if p.empty:
        raise InsufficientDataError("가격 시리즈가 비어있습니다.", n_points=0, required=1)

    dd = drawdown_series(p)
    trough_date = dd.idxmin()
    peak_date = p.loc[:trough_date].idxmax()
    max_dd = float(dd.min())

    # 회복 = 고점 가격을 다시 회복한 시점 (trough 이후)
    after_trough = p.loc[trough_date:]
    peak_price = float(p.loc[peak_date])
    recovered = after_trough[after_trough >= peak_price]
    recovery_date = recovered.index[0] if not recovered.empty else None
    recovery_days = (
        (recovery_date - trough_date).days if recovery_date is not None else None
    )

    return DrawdownInfo(
        max_dd_pct=max_dd,
        peak_date=peak_date,
        trough_date=trough_date,
        duration_days=(trough_date - peak_date).days,
        recovery_date=recovery_date,
        recovery_days=recovery_days,
    )


# ======================================================================
# 3. Monte Carlo (GBM)
# ======================================================================


@dataclass
class MonteCarloResult:
    days_forward: int
    n_paths: int
    start_price: float
    paths: np.ndarray  # shape: (n_paths, days_forward)
    final_prices: np.ndarray
    quantiles: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        q = self.quantiles
        return (
            f"{self.days_forward}일 후 가격 분포 (시작 ${self.start_price:,.2f}):\n"
            f"  P05  {q['p05']:>12,.2f}  ({q['p05_ret']:+.2f}%)\n"
            f"  P25  {q['p25']:>12,.2f}  ({q['p25_ret']:+.2f}%)\n"
            f"  P50  {q['p50']:>12,.2f}  ({q['p50_ret']:+.2f}%)\n"
            f"  P75  {q['p75']:>12,.2f}  ({q['p75_ret']:+.2f}%)\n"
            f"  P95  {q['p95']:>12,.2f}  ({q['p95_ret']:+.2f}%)"
        )


def monte_carlo_simulation(
    prices: pd.Series,
    days_forward: int = 90,
    n_paths: int = 10_000,
    seed: int | None = None,
) -> MonteCarloResult:
    """GBM 기반 가격 경로 시뮬레이션.

    가정: log-수익률이 i.i.d. 정규분포 (실제로는 fat-tail 이라 P5 가 너무 낙관적일 수 있음).

    RNG 는 격리된 Generator 사용 — 글로벌 np.random 상태를 오염시키지 않음
    (7단계: 구버전은 np.random.seed() 로 글로벌 시드를 바꿔서 같은 프로세스의
    다른 random 연산까지 결정론화시키는 부작용이 있었음).
    """
    p = prices.dropna().squeeze()
    log_returns = np.log(p / p.shift(1)).dropna()
    if len(log_returns) < MIN_RETURN_POINTS:
        raise InsufficientDataError(
            f"GBM 파라미터 추정에 수익률 {len(log_returns)}개 — 최소 {MIN_RETURN_POINTS}개 필요",
            n_points=len(log_returns), required=MIN_RETURN_POINTS,
        )
    mu = float(log_returns.mean())
    sigma = float(log_returns.std())
    last_price = float(p.iloc[-1])

    rng = np.random.default_rng(seed)  # seed=None 이면 비결정적

    drift = mu - 0.5 * sigma * sigma
    shocks = sigma * rng.standard_normal((n_paths, days_forward))
    log_increments = drift + shocks
    cumulative = np.cumsum(log_increments, axis=1)
    paths = last_price * np.exp(cumulative)

    final = paths[:, -1]
    quantiles: dict[str, float] = {}
    for q_pct, label in [(5, "p05"), (25, "p25"), (50, "p50"), (75, "p75"), (95, "p95")]:
        v = float(np.quantile(final, q_pct / 100))
        quantiles[label] = v
        quantiles[f"{label}_ret"] = (v / last_price - 1) * 100

    return MonteCarloResult(
        days_forward=days_forward,
        n_paths=n_paths,
        start_price=last_price,
        paths=paths,
        final_prices=final,
        quantiles=quantiles,
    )


# ======================================================================
# 4. Scenario Analysis
# ======================================================================


def scenario_impact(
    current_price: float,
    revenue_shock_pct: float = 0,
    margin_shock_pp: float = 0,
    multiple_shock_pct: float = 0,
    current_operating_margin_pct: float = 5,
) -> dict[str, float]:
    """매출/마진/멀티플 충격을 가격에 환산하는 단순 회계 모델.

    Parameters
    ----------
    current_price : float
        현재 주가.
    revenue_shock_pct : float
        매출 변동률 (예: -10 = 매출 10% 감소).
    margin_shock_pp : float
        영업마진 % 포인트 변동 (예: -2 = 영업마진 2%p 하락).
    multiple_shock_pct : float
        P/E 멀티플 디레이팅/리레이팅 (예: -20 = 멀티플 20% 축소).
    current_operating_margin_pct : float
        현재 영업마진 (마진 충격 영향 계산에 사용).

    모델:
        new_revenue = current_revenue * (1 + rev_shock)
        new_margin  = current_margin + margin_shock
        new_earnings_factor = (new_revenue * new_margin) / (current_revenue * current_margin)
        new_price = current_price * new_earnings_factor * (1 + multiple_shock)

    매우 단순한 모델이지만 "어떤 부분이 하락의 주범인지" 분리해 보기 좋습니다.
    """
    rev = revenue_shock_pct / 100
    new_margin = current_operating_margin_pct + margin_shock_pp
    if current_operating_margin_pct == 0:
        earnings_factor = (1 + rev)
    else:
        earnings_factor = (1 + rev) * (new_margin / current_operating_margin_pct)
    multiple = 1 + multiple_shock_pct / 100
    new_price = current_price * earnings_factor * multiple

    return {
        "current_price": current_price,
        "new_price": new_price,
        "price_change_pct": (new_price / current_price - 1) * 100,
        "revenue_shock_pct": revenue_shock_pct,
        "margin_shock_pp": margin_shock_pp,
        "multiple_shock_pct": multiple_shock_pct,
    }


# ======================================================================
# 5. 통합 리포트
# ======================================================================


def risk_report(
    ticker: str,
    period: str = "2y",
    mc_days: int = 90,
    mc_paths: int = 10_000,
    seed: int | None = 42,
) -> dict[str, Any]:
    """종합 리스크 리포트. 한 종목의 모든 리스크 통계를 한 dict 으로 반환.

    Phase 5 (Signal Engine) 의 알림 룰이 이 dict 을 입력으로 사용할 예정.
    """
    from src.data_fetcher import fetch_prices
    from src.utils import close_series

    df = fetch_prices(ticker, period=period)
    prices = close_series(df)
    returns = returns_from_prices(prices)

    return {
        "ticker": ticker,
        "period": period,
        "current_price": float(prices.iloc[-1]),
        "annualized_vol_pct": float(returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100),
        # VaR / CVaR (일별, %)
        "var_95_hist_pct": historical_var(returns, 0.95) * 100,
        "var_99_hist_pct": historical_var(returns, 0.99) * 100,
        "var_95_param_pct": parametric_var(returns, 0.95) * 100,
        "es_95_pct": expected_shortfall(returns, 0.95) * 100,
        "es_99_pct": expected_shortfall(returns, 0.99) * 100,
        # Drawdown
        "max_drawdown": max_drawdown(prices),
        # Monte Carlo
        "monte_carlo": monte_carlo_simulation(
            prices, days_forward=mc_days, n_paths=mc_paths, seed=seed
        ),
    }
