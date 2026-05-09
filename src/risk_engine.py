"""
risk_engine.py
==============
에드워드 소프 식 리스크 엔진 (Phase 3 에서 본격 구현).

핵심 책임:
- VaR (Value at Risk) — 통계적 최악 손실
- MDD (Maximum Drawdown) — 최대 낙폭
- 시나리오 다운사이드 — "이 악재 터지면 얼마나 빠질까"
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def historical_var(returns: pd.Series, confidence: float = 0.99) -> float:
    """과거 수익률 분포 기반 VaR. confidence=0.99 면 하위 1% 분위수.

    Returns
    -------
    float
        해당 신뢰구간에서의 1일 최대 예상 손실률 (음수).
    """
    if returns.empty:
        raise ValueError("수익률 시리즈가 비어있습니다.")
    return float(np.quantile(returns, 1 - confidence))


def max_drawdown(prices: pd.Series) -> float:
    """최대 낙폭 (Maximum Drawdown). 가장 고점에서 가장 저점까지의 비율."""
    cumulative = prices / prices.iloc[0]
    running_peak = cumulative.cummax()
    drawdown = (cumulative - running_peak) / running_peak
    return float(drawdown.min())


# TODO(Phase 3):
# - monte_carlo_simulation(returns, days, n_paths)
# - historical_analog_match(event_category)  # 과거 유사 사건 매칭
