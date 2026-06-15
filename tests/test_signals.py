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


def _uptrend(n=300) -> pd.Series:
    idx = pd.bdate_range("2023-06-01", periods=n)
    return pd.Series(np.linspace(100, 200, n), index=idx)


def _downtrend(n=300) -> pd.Series:
    idx = pd.bdate_range("2023-06-01", periods=n)
    return pd.Series(np.linspace(200, 100, n), index=idx)


def test_momentum_high_on_uptrend():
    # 강한 상승 → 높은 점수 (상한 100 도달 가능). 연속성은 별도 테스트가 검증.
    score, notes = momentum_score(_uptrend())
    assert score >= 85
    assert len(notes) == 2  # skip-month 모멘텀 + 200일선


def test_momentum_low_on_downtrend():
    score, _ = momentum_score(_downtrend())
    assert score <= 15


def test_momentum_continuous_not_tiered():
    # 완만한 상승과 급한 상승이 다른 점수여야 함 (연속성)
    idx = pd.bdate_range("2023-06-01", periods=300)
    mild = pd.Series(np.linspace(100, 110, 300), index=idx)
    steep = pd.Series(np.linspace(100, 180, 300), index=idx)
    assert momentum_score(steep)[0] > momentum_score(mild)[0]


def test_momentum_partial_data_uses_short_fallback():
    # 100일이면 12mo skip 불가 → 3개월 대체 (1) + 200일선 불가 → notes 1개
    score, notes = momentum_score(_uptrend(100))
    assert len(notes) == 1
    assert 0 <= score <= 100


def test_momentum_too_short_raises():
    with pytest.raises(InsufficientDataError):
        momentum_score(_uptrend(30))


def test_low_vol_score_inverse():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2023-06-01", periods=300)
    low = pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.008, 300)), index=idx)
    high = pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.035, 300)), index=idx)
    from src.signals import low_vol_score
    assert low_vol_score(low)[0] > low_vol_score(high)[0]


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


# ---------------- select_screened_tickers ----------------


def test_select_screened_tickers_takes_top_n(monkeypatch):
    import src.signals as sig

    fake_rows = [
        {"symbol": "AAA", "total_score": 90},
        {"symbol": "BBB", "total_score": 80},
        {"symbol": "CCC", "total_score": 70},
    ]
    monkeypatch.setattr(
        "src.screener.screen_watchlist", lambda wl, country_label="": fake_rows
    )
    assert sig.select_screened_tickers(n=2) == ["AAA", "BBB"]
    assert sig.select_screened_tickers(n=10) == ["AAA", "BBB", "CCC"]
