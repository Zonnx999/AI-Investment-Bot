"""src/risk_engine.py — 합성 데이터 기반 결정론적 검증."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.risk_engine import (
    expected_shortfall,
    historical_var,
    max_drawdown,
    monte_carlo_simulation,
    parametric_var,
    scenario_impact,
)


def test_historical_var_matches_quantile(price_series):
    returns = price_series.pct_change().dropna()
    var95 = historical_var(returns, 0.95)
    assert var95 == pytest.approx(float(np.quantile(returns, 0.05)))
    assert var95 < 0  # 손실이어야 함


def test_expected_shortfall_is_worse_than_var(price_series):
    returns = price_series.pct_change().dropna()
    var95 = historical_var(returns, 0.95)
    es95 = expected_shortfall(returns, 0.95)
    assert es95 <= var95  # 꼬리 평균은 컷오프보다 항상 나쁘거나 같음


def test_parametric_var_reasonably_close_to_historical(price_series):
    # 정규분포 합성 데이터에서는 두 VaR 가 크게 다르지 않아야 함
    returns = price_series.pct_change().dropna()
    hist = historical_var(returns, 0.95)
    param = parametric_var(returns, 0.95)
    assert abs(hist - param) < 0.01


def test_historical_var_empty_raises():
    with pytest.raises(ValueError):
        historical_var(pd.Series(dtype=float))


def test_max_drawdown_known_path():
    # 100 → 110 (peak) → 88 (trough, -20%) → 120 (recovery)
    dates = pd.date_range("2024-01-01", periods=5)
    prices = pd.Series([100.0, 110.0, 88.0, 95.0, 120.0], index=dates)
    info = max_drawdown(prices)
    assert info.max_dd_pct == pytest.approx(-20.0)
    assert info.peak_date == dates[1]
    assert info.trough_date == dates[2]
    assert info.recovery_date == dates[4]


def test_max_drawdown_no_recovery():
    dates = pd.date_range("2024-01-01", periods=3)
    prices = pd.Series([100.0, 80.0, 70.0], index=dates)
    info = max_drawdown(prices)
    assert info.recovery_date is None
    assert info.recovery_days is None


def test_monte_carlo_seed_reproducible(price_series):
    a = monte_carlo_simulation(price_series, days_forward=30, n_paths=500, seed=42)
    b = monte_carlo_simulation(price_series, days_forward=30, n_paths=500, seed=42)
    assert np.array_equal(a.final_prices, b.final_prices)
    assert a.quantiles == b.quantiles


def test_monte_carlo_different_seeds_differ(price_series):
    a = monte_carlo_simulation(price_series, days_forward=30, n_paths=500, seed=1)
    b = monte_carlo_simulation(price_series, days_forward=30, n_paths=500, seed=2)
    assert not np.array_equal(a.final_prices, b.final_prices)


def test_monte_carlo_shapes(price_series):
    r = monte_carlo_simulation(price_series, days_forward=30, n_paths=500, seed=0)
    assert r.paths.shape == (500, 30)
    assert r.final_prices.shape == (500,)
    assert r.quantiles["p05"] <= r.quantiles["p50"] <= r.quantiles["p95"]


def test_scenario_zero_shock_keeps_price():
    r = scenario_impact(100.0)
    assert r["new_price"] == pytest.approx(100.0)
    assert r["price_change_pct"] == pytest.approx(0.0)


def test_scenario_revenue_shock_scales_price():
    # 마진/멀티플 충격 없이 매출 -10% → 가격도 -10%
    r = scenario_impact(100.0, revenue_shock_pct=-10)
    assert r["new_price"] == pytest.approx(90.0)
