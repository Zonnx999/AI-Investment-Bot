"""src/predictors.py — 순수 변환·분석 함수 오프라인 검증 (합성 시리즈)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.exceptions import InsufficientDataError
from src.predictors import (
    analyze_lead_lag,
    lagged_correlations,
    to_monthly,
    yoy_growth,
)


def _monthly(values, start="2018-01-31") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="ME")
    return pd.Series(values, index=idx, dtype=float)


# ---------------- to_monthly ----------------


def test_to_monthly_resamples_daily_to_month_end():
    daily = pd.Series(
        range(90), index=pd.date_range("2024-01-01", periods=90, freq="D"), dtype=float
    )
    m = to_monthly(daily)
    assert len(m) == 3                       # Jan, Feb, Mar
    assert (m.index.day >= 28).all()         # month-end
    assert m.iloc[0] == 30.0                 # 1월 마지막(=index 30, 2024-01-31)


def test_to_monthly_accepts_string_index():
    s = pd.Series([1.0, 2.0], index=["2024-01-15", "2024-02-15"])
    m = to_monthly(s)
    assert isinstance(m.index, pd.DatetimeIndex)


# ---------------- yoy_growth ----------------


def test_yoy_growth_basic():
    # 13개월, 매월 1%씩 복리 상승 → 12개월차 YoY ≈ (1.01^12-1)*100
    vals = [100 * 1.01 ** i for i in range(13)]
    g = yoy_growth(_monthly(vals))
    assert len(g) == 1
    assert g.iloc[0] == pytest.approx((1.01 ** 12 - 1) * 100, abs=0.01)


# ---------------- lagged_correlations ----------------


def test_lagged_correlation_detects_known_lag():
    rng = np.random.default_rng(0)
    base = pd.Series(rng.normal(0, 1, 60), index=pd.date_range("2018-01-31", periods=60, freq="ME"))
    # target 은 leading 을 정확히 3개월 뒤따라감
    target = base.shift(3).dropna()
    corrs = lagged_correlations(base, target, max_lag=6)
    best = max({k: v for k, v in corrs.items() if k >= 1}, key=lambda k: abs(corrs[k]))
    assert best == 3
    assert corrs[3] == pytest.approx(1.0, abs=1e-6)


# ---------------- analyze_lead_lag ----------------


def test_analyze_lead_lag_recovers_linear_relationship():
    # target(t) = 2 * leading(t-2) + 5  → slope≈2, intercept≈5, lag=2, r²≈1
    rng = np.random.default_rng(1)
    idx = pd.date_range("2016-01-31", periods=80, freq="ME")
    leading = pd.Series(rng.normal(0, 3, 80), index=idx)
    target = (2 * leading.shift(2) + 5).dropna()
    res = analyze_lead_lag(leading, target, "X", "Y", max_lag=6)
    assert res.best_lag_months == 2
    assert res.slope == pytest.approx(2.0, abs=1e-6)
    assert res.intercept == pytest.approx(5.0, abs=1e-6)
    assert res.r_squared == pytest.approx(1.0, abs=1e-6)
    assert res.reliable is True


def test_analyze_lead_lag_prediction_direction():
    idx = pd.date_range("2016-01-31", periods=80, freq="ME")
    leading = pd.Series(np.linspace(-5, 5, 80), index=idx)
    target = (3 * leading.shift(1)).dropna()       # 양의 기울기
    res = analyze_lead_lag(leading, target, "X", "Y", max_lag=4)
    # 최신 leading=+5 → 예측 양수 → 상승
    assert res.predicted_change_pct > 0
    assert "상승" in res.direction


def test_analyze_lead_lag_insufficient_overlap_raises():
    idx = pd.date_range("2020-01-31", periods=10, freq="ME")
    leading = pd.Series(range(10), index=idx, dtype=float)
    target = pd.Series(range(10), index=idx, dtype=float)
    with pytest.raises(InsufficientDataError):
        analyze_lead_lag(leading, target, "X", "Y", max_lag=3, min_obs=24)


def test_analyze_lead_lag_flags_weak_relationship():
    rng = np.random.default_rng(7)
    idx = pd.date_range("2014-01-31", periods=100, freq="ME")
    leading = pd.Series(rng.normal(0, 1, 100), index=idx)
    target = pd.Series(rng.normal(0, 1, 100), index=idx)   # 무관한 노이즈
    res = analyze_lead_lag(leading, target, "X", "Y", max_lag=6, min_obs=24)
    assert res.reliable is False
    assert any("R²" in n for n in res.notes)


# ---------------- 위키피디아 파서 (data_fetcher 순수 헬퍼) ----------------


def test_parse_wikipedia_items_builds_sorted_series():
    from src.data_fetcher import _parse_wikipedia_items

    items = [
        {"timestamp": "2024030100", "views": 200},
        {"timestamp": "2024010100", "views": 100},  # 일부러 역순
        {"timestamp": "2024020100", "views": 150},
    ]
    s = _parse_wikipedia_items(items, "Bitcoin")
    assert s.name == "Bitcoin"
    assert s.index.is_monotonic_increasing      # 정렬됨
    assert s.iloc[0] == 100.0
    assert isinstance(s.index, pd.DatetimeIndex)
