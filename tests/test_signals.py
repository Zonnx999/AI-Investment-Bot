"""src/signals.py — 순수 함수 (점수/룰/알림) 오프라인 검증."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.exceptions import InsufficientDataError
from src.signals import (
    apply_screen_rules,
    drawdown_alerts,
    momentum_score,
    regime_change_alert,
    vol_spike_alerts,
)


# ---------------- momentum_score ----------------


def _uptrend(n=260) -> pd.Series:
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series(np.linspace(100, 200, n), index=idx)


def _downtrend(n=260) -> pd.Series:
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.Series(np.linspace(200, 100, n), index=idx)


def test_momentum_full_score_on_uptrend():
    score, notes = momentum_score(_uptrend())
    assert score == 100
    assert len(notes) == 3  # 6개월/3개월/200일선 모두 평가


def test_momentum_zero_on_downtrend():
    score, _ = momentum_score(_downtrend())
    assert score == 0


def test_momentum_partial_data_uses_available_factors():
    # 100일이면 6개월(126) 평가 불가, 3개월(63)만 평가 가능, 200일선 불가
    score, notes = momentum_score(_uptrend(100))
    assert len(notes) == 1
    assert score == 100


def test_momentum_too_short_raises():
    with pytest.raises(InsufficientDataError):
        momentum_score(_uptrend(30))


# ---------------- apply_screen_rules ----------------


def test_screen_passes_quality_value_stock():
    rows = [
        {"ticker": "GOOD", "pe": 10.0, "roe": 0.20, "fcf_yield": 0.05},
        {"ticker": "MEH", "pe": 50.0, "roe": 0.20, "fcf_yield": 0.05},  # P/E > 중간값
    ]
    out = apply_screen_rules(rows)
    tickers = [r["ticker"] for r in out]
    assert "GOOD" in tickers
    assert "MEH" not in tickers


def test_screen_rejects_low_roe():
    rows = [{"ticker": "LOW", "pe": 8.0, "roe": 0.05, "fcf_yield": 0.05}]
    assert apply_screen_rules(rows) == []


def test_screen_rejects_negative_fcf():
    rows = [{"ticker": "BURN", "pe": 8.0, "roe": 0.20, "fcf_yield": -0.02}]
    assert apply_screen_rules(rows) == []


def test_screen_exempts_pe_rule_for_loss_makers_but_keeps_others():
    # P/E 없음(적자) → P/E 룰 면제, ROE/FCF 는 통과해야 함
    rows = [{"ticker": "NOPE", "pe": None, "roe": 0.20, "fcf_yield": 0.05}]
    out = apply_screen_rules(rows)
    assert len(out) == 1
    assert out[0]["ticker"] == "NOPE"


def test_screen_includes_reasons():
    rows = [{"ticker": "GOOD", "pe": 10.0, "roe": 0.20, "fcf_yield": 0.05}]
    out = apply_screen_rules(rows)
    assert out[0]["reasons"]
    assert any("ROE" in r for r in out[0]["reasons"])


# ---------------- regime_change_alert ----------------


def test_regime_alert_on_change():
    a = regime_change_alert("🔴 위험회피", "🟢 위험선호")
    assert a is not None
    assert a.category == "regime"


def test_regime_no_alert_when_same():
    assert regime_change_alert("🟢 위험선호", "🟢 위험선호") is None


def test_regime_no_alert_on_first_run():
    assert regime_change_alert("🟢 위험선호", None) is None


# ---------------- drawdown_alerts ----------------


def test_drawdown_alert_on_new_breach():
    alerts, state = drawdown_alerts({"비트코인": -15.0}, {})
    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert state == {"비트코인": True}


def test_drawdown_no_duplicate_alert_when_already_breached():
    alerts, state = drawdown_alerts({"비트코인": -15.0}, {"비트코인": True})
    assert alerts == []
    assert state == {"비트코인": True}


def test_drawdown_recovery_alert():
    alerts, state = drawdown_alerts({"비트코인": -5.0}, {"비트코인": True})
    assert len(alerts) == 1
    assert alerts[0].severity == "info"
    assert state == {"비트코인": False}


# ---------------- vol_spike_alerts ----------------


def test_vol_spike_alert():
    alerts = vol_spike_alerts({"NVDA": 80.0}, {"NVDA": 50.0})  # ×1.6
    assert len(alerts) == 1
    assert alerts[0].category == "volatility"


def test_vol_no_alert_below_ratio():
    assert vol_spike_alerts({"NVDA": 55.0}, {"NVDA": 50.0}) == []  # ×1.1


def test_vol_no_alert_without_prev():
    assert vol_spike_alerts({"NVDA": 80.0}, {}) == []
