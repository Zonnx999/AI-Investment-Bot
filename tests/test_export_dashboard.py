"""
tests/test_export_dashboard.py
==============================
Phase 12 — export_dashboard.py 순수 헬퍼 오프라인 테스트.

네트워크 / DB / API 없음. 순수 변환 함수만 검증.
"""

from __future__ import annotations

import math

import pytest

from scripts.export_dashboard import (
    clean_float,
    prediction_to_dict,
    regime_to_dict,
    row_to_crypto,
    row_to_kr,
    row_to_us,
)


# ----------------------------------------------------------------------
# clean_float
# ----------------------------------------------------------------------


def test_clean_float_none():
    assert clean_float(None) is None


def test_clean_float_nan():
    assert clean_float(float("nan")) is None


def test_clean_float_inf():
    assert clean_float(float("inf")) is None
    assert clean_float(float("-inf")) is None


def test_clean_float_normal():
    assert clean_float(1.5) == 1.5


def test_clean_float_int():
    assert clean_float(42) == 42.0


def test_clean_float_string_invalid():
    assert clean_float("abc") is None


def test_clean_float_rounds_to_6_places():
    result = clean_float(1.1234567890)
    assert result == pytest.approx(1.123457, rel=1e-5)


# ----------------------------------------------------------------------
# row_to_us
# ----------------------------------------------------------------------

_US_ROW = ("AAPL", "Apple Inc.", "Technology", 189.3, 2_900_000_000_000.0, 0.6, 147.0, 85, 45, 65)


def test_row_to_us_basic():
    d = row_to_us(_US_ROW)
    assert d["symbol"] == "AAPL"
    assert d["name"] == "Apple Inc."
    assert d["sector"] == "Technology"
    assert d["price"] == pytest.approx(189.3)
    assert d["health_score"] == 85
    assert d["value_score"] == 45
    assert d["total_score"] == 65


def test_row_to_us_scores_are_int():
    d = row_to_us(_US_ROW)
    assert isinstance(d["health_score"], int)
    assert isinstance(d["value_score"], int)
    assert isinstance(d["total_score"], int)


def test_row_to_us_none_name_becomes_empty():
    row = (None, None, None, 10.0, 1e9, None, None, 50, 50, 50)
    d = row_to_us(row)
    assert d["symbol"] == ""
    assert d["name"] == ""
    assert d["dividend_yield"] is None
    assert d["roe"] is None


def test_row_to_us_nan_price_becomes_none():
    row = ("T", "AT&T", "Comm.", float("nan"), 1e9, 0.0, 5.0, 60, 40, 50)
    d = row_to_us(row)
    assert d["price"] is None


# ----------------------------------------------------------------------
# row_to_kr
# ----------------------------------------------------------------------

_KR_ROW = ("005930", "삼성전자", "전기전자", 72000.0, 4.3e14, 2.1, 8.5, 18.2, 1.4, 72, 58, 65)


def test_row_to_kr_basic():
    d = row_to_kr(_KR_ROW)
    assert d["symbol"] == "005930"
    assert d["name"] == "삼성전자"
    assert d["per"] == pytest.approx(18.2)
    assert d["pbr"] == pytest.approx(1.4)
    assert d["total_score"] == 65


def test_row_to_kr_none_per_pbr():
    row = ("000001", "테스트", "금융", 5000.0, 1e12, 0.0, None, None, None, 40, 30, 35)
    d = row_to_kr(row)
    assert d["per"] is None
    assert d["pbr"] is None
    assert d["roe"] is None


# ----------------------------------------------------------------------
# row_to_crypto
# ----------------------------------------------------------------------

_CRYPTO_ROW = ("BTC", "Bitcoin", 67000.0, 1.3e12, 90, 85, 87)


def test_row_to_crypto_basic():
    d = row_to_crypto(_CRYPTO_ROW)
    assert d["symbol"] == "BTC"
    assert d["name"] == "Bitcoin"
    assert d["rank_score"] == 90     # health_score = rank_score
    assert d["volatility_score"] == 85
    assert d["total_score"] == 87


def test_row_to_crypto_scores_are_int():
    d = row_to_crypto(_CRYPTO_ROW)
    assert isinstance(d["rank_score"], int)
    assert isinstance(d["volatility_score"], int)
    assert isinstance(d["total_score"], int)


# ----------------------------------------------------------------------
# regime_to_dict (pandas import 필요 — 오프라인에서 사용 가능)
# ----------------------------------------------------------------------


def _make_fake_summary():
    import pandas as pd

    from src.macro_analyzer import RegimeReport

    idx = ["S&P 500", "금"]
    regime = RegimeReport(
        regime="🟢 위험선호 (Risk-on)",
        score=2,
        signals=["장단기 금리차 +0.5%p 정상", "HY 스프레드 안정"],
        raw={"T10Y2Y": 0.5},
        failures=[],
    )
    return {
        "period": "6mo",
        "prices": pd.DataFrame(),
        "cumulative_returns_pct": pd.Series([12.3, 5.1], index=idx),
        "annualized_vol_pct": pd.Series([18.2, 12.0], index=idx),
        "current_drawdown_pct": pd.Series([-3.1, -1.5], index=idx),
        "sharpe_ratio": pd.Series([1.2, 0.8], index=idx),
        "correlation": pd.DataFrame([[1.0, -0.1], [-0.1, 1.0]], index=idx, columns=idx),
        "regime": regime,
    }


def test_regime_to_dict_label():
    d = regime_to_dict(_make_fake_summary())
    assert d["label"] == "🟢 위험선호 (Risk-on)"
    assert d["score"] == 2


def test_regime_to_dict_panel_structure():
    d = regime_to_dict(_make_fake_summary())
    assert len(d["panel"]) == 2
    sp = next(p for p in d["panel"] if p["name"] == "S&P 500")
    assert sp["return_6m"] == pytest.approx(12.3)
    assert sp["vol"] == pytest.approx(18.2)
    assert sp["drawdown"] == pytest.approx(-3.1)


def test_regime_to_dict_correlations_exclude_self():
    d = regime_to_dict(_make_fake_summary())
    # 자기 자신(대각선)은 제외
    assert "S&P 500" not in d["correlations"]["S&P 500"]
    assert "금" in d["correlations"]["S&P 500"]


def test_regime_to_dict_signals():
    d = regime_to_dict(_make_fake_summary())
    assert len(d["signals"]) == 2
    assert d["failures"] == []


# ----------------------------------------------------------------------
# prediction_to_dict
# ----------------------------------------------------------------------


def _make_fake_result():
    from src.predictors import LeadLagResult

    return LeadLagResult(
        leading_name="M2 증가율",
        target_name="BTC 수익률",
        best_lag_months=3,
        correlation=0.62,
        r_squared=0.42,
        slope=1.5,
        intercept=0.2,
        n_obs=48,
        latest_leading_value=5.3,
        predicted_change_pct=15.3,
        direction="상승 ↑",
        reliable=True,
        notes=["최적 선행 3개월", "예측 +15.3%"],
    )


def test_prediction_to_dict_basic():
    d = prediction_to_dict("M2 → 비트코인", _make_fake_result())
    assert d["name"] == "M2 → 비트코인"
    assert d["leading"] == "M2 증가율"
    assert d["target"] == "BTC 수익률"
    assert d["best_lag_months"] == 3
    assert d["r_squared"] == pytest.approx(0.42)
    assert d["direction"] == "상승 ↑"
    assert d["reliable"] is True
    assert d["n_obs"] == 48
    assert len(d["notes"]) == 2


def test_prediction_to_dict_unreliable():
    result = _make_fake_result()
    result.reliable = False
    result.r_squared = 0.08
    d = prediction_to_dict("테스트", result)
    assert d["reliable"] is False
    assert d["r_squared"] == pytest.approx(0.08)
