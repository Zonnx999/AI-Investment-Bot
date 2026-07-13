"""src/risk_engine.py — 합성 데이터 기반 결정론적 검증."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.exceptions import AnalysisError, InsufficientDataError
from src.risk_engine import (
    expected_shortfall,
    historical_var,
    max_drawdown,
    monte_carlo_simulation,
    parametric_var,
    returns_from_prices,
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


def test_historical_var_too_few_points_raises():
    with pytest.raises(InsufficientDataError):
        historical_var(pd.Series([0.01, -0.02, 0.005]))


def test_var_rejects_price_series(price_series):
    # 7단계 보장: 가격 시리즈를 실수로 넣으면 조용한 오답 대신 raise
    with pytest.raises(AnalysisError):
        historical_var(price_series)


def test_returns_from_prices(price_series):
    r = returns_from_prices(price_series)
    assert len(r) == len(price_series) - 1
    expected = price_series.iloc[1] / price_series.iloc[0] - 1
    assert r.iloc[0] == pytest.approx(expected)


def test_monte_carlo_does_not_pollute_global_rng(price_series):
    # 7단계 보장: MC 가 글로벌 np.random 상태를 건드리지 않음
    np.random.seed(123)
    before = np.random.get_state()[1][:5].copy()
    monte_carlo_simulation(price_series, days_forward=10, n_paths=100, seed=42)
    after = np.random.get_state()[1][:5]
    assert np.array_equal(before, after)


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


# ---------------- 전수 리뷰 회귀 (2026-07-06) ----------------


def test_monte_carlo_drift_not_double_ito_corrected():
    """mu(log-수익률 평균)는 이미 Ito 보정 반영 — 0.5σ² 재차감 금지 회귀.

    합성 GBM(알려진 log-drift)에서 시뮬레이션 경로의 중앙값 log-drift 가
    입력 시계열에서 추정된 mu_hat 과 일치해야 함 (이중 차감 시 0.5σ² 낮게 나옴).
    """
    rng = np.random.default_rng(7)
    m, s, n = 0.001, 0.03, 500                       # 일당 log-drift / 변동성
    log_rets = rng.normal(m, s, n)
    prices = pd.Series(100.0 * np.exp(np.cumsum(log_rets)),
                       index=pd.bdate_range("2023-01-02", periods=n))
    mu_hat = float(np.log(prices / prices.shift(1)).dropna().mean())

    res = monte_carlo_simulation(prices, days_forward=250, n_paths=4000, seed=11)
    med_drift = float(np.log(np.median(res.final_prices) / res.start_price)) / 250
    assert med_drift == pytest.approx(mu_hat, abs=0.5 * (0.03 ** 2) * 0.5)
    # 이중 차감이면 mu_hat - 0.00045 근처로 떨어져 위 허용오차를 벗어남
    assert abs(med_drift - (mu_hat - 0.5 * 0.03 ** 2)) > abs(med_drift - mu_hat)


def test_scenario_impact_floors_at_zero_price():
    """마진 붕괴 시나리오는 $0 바닥 — 음수 주가 금지 (§4.10 #5)."""
    r = scenario_impact(100.0, revenue_shock_pct=-30, margin_shock_pp=-5,
                        multiple_shock_pct=-50, current_operating_margin_pct=2)
    assert r["new_price"] == 0.0
    assert r["price_change_pct"] == pytest.approx(-100.0)
    r2 = scenario_impact(100.0, margin_shock_pp=-8, current_operating_margin_pct=5)
    assert r2["new_price"] >= 0.0
