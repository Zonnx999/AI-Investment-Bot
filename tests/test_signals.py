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


def test_low_vol_score_returns_vol_pct_single_source():
    """low_vol_score 가 (점수, 근거, vol_pct) 를 반환 — vol 계산 단일 지점."""
    from src.signals import annualized_vol_pct, low_vol_score

    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2023-06-01", periods=300)
    prices = pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.02, 300)), index=idx)

    score, notes, vol = low_vol_score(prices)
    assert vol == pytest.approx(annualized_vol_pct(prices))
    assert vol is not None and vol > 0

    # 데이터 부족 → 중립 점수 + vol None
    short_score, _, short_vol = low_vol_score(prices.iloc[:10])
    assert short_score == 50 and short_vol is None


def test_annualized_vol_pct_empty_returns_none():
    from src.signals import annualized_vol_pct

    assert annualized_vol_pct(pd.Series([], dtype=float)) is None
    assert annualized_vol_pct(pd.Series([100.0])) is None   # 수익률 0개


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


# ---------------- screen_candidates (음수 earningsYield → PER None) ----------------


def test_screen_candidates_negative_earnings_yield_gives_pe_none(monkeypatch):
    """적자(음수 earningsYield)의 역수는 '음수 PER' 저평가 착시 → 명시적 None."""
    from src.signals import screen_candidates

    metrics = {
        "LOSS": {"earningsYield": -0.05, "returnOnEquity": 0.20, "freeCashFlowYield": 0.05},
        "PROF": {"earningsYield": 0.10, "returnOnEquity": 0.20, "freeCashFlowYield": 0.05},
    }
    monkeypatch.setattr(
        "src.data_fetcher.fetch_key_metrics",
        lambda t, limit=1: pd.DataFrame([metrics[t]]),
    )
    out = {r["ticker"]: r for r in screen_candidates(["LOSS", "PROF"])}
    assert out["LOSS"]["pe"] is None                      # 음수 → None (P/E 룰 면제 경로)
    assert out["PROF"]["pe"] == pytest.approx(10.0)       # 양수 → 1/yield
    # 음수 PER 가 '초저PER' 로 최상단 정렬되지 않음 (None 은 후순위)
    tickers_sorted = [r["ticker"] for r in screen_candidates(["LOSS", "PROF"])]
    assert tickers_sorted == ["PROF", "LOSS"]


def test_screen_candidates_zero_earnings_yield_gives_pe_none(monkeypatch):
    from src.signals import screen_candidates

    monkeypatch.setattr(
        "src.data_fetcher.fetch_key_metrics",
        lambda t, limit=1: pd.DataFrame(
            [{"earningsYield": 0.0, "returnOnEquity": 0.15, "freeCashFlowYield": 0.02}]
        ),
    )
    out = screen_candidates(["ZERO"])
    assert out[0]["pe"] is None


# ---------------- generate_signal_report (drawdown_alerts 단일 호출) ----------------


@pytest.fixture
def fresh_state_db(tmp_path, monkeypatch):
    """signals 가 쓰는 storage 싱글톤을 tmp DB 로 격리 (state 테이블)."""
    import src.storage as storage_mod

    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "sig.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


def _wire_macro(monkeypatch, dd_map, regime_label="🟢 위험선호"):
    """generate_signal_report 의 매크로 fetch 를 합성 데이터로 대체."""
    regime = type("R", (), {"regime": regime_label})()
    monkeypatch.setattr("src.macro_analyzer.classify_regime", lambda: regime)
    monkeypatch.setattr("src.macro_analyzer.fetch_cross_asset_panel",
                        lambda period="6mo": pd.DataFrame())
    monkeypatch.setattr("src.macro_analyzer.current_drawdown", lambda panel: dict(dd_map))


def test_generate_signal_report_calls_drawdown_alerts_once(fresh_state_db, monkeypatch):
    """drawdown_alerts 는 실행당 1회 호출 (구현 전 2회) — 알림 의미는 동일."""
    import src.signals as sig

    _wire_macro(monkeypatch, {"비트코인": -15.0})
    calls: list = []
    original = sig.drawdown_alerts

    def counting(dd, prev):
        calls.append(dd)
        return original(dd, prev)

    monkeypatch.setattr(sig, "drawdown_alerts", counting)

    # 1) 첫 실행 — 알림 없음(시딩), 호출 1회
    r1 = sig.generate_signal_report(tickers=())
    assert r1.first_run is True and r1.alerts == []
    assert len(calls) == 1

    # 2) 두 번째 실행 (여전히 breach) — 이미 돌파 상태라 중복 알림 없음, 호출 1회
    calls.clear()
    r2 = sig.generate_signal_report(tickers=())
    assert r2.first_run is False and r2.alerts == []
    assert len(calls) == 1

    # 3) 회복 — info 알림 1건 (의미 보존)
    _wire_macro(monkeypatch, {"비트코인": -3.0})
    r3 = sig.generate_signal_report(tickers=())
    assert len(r3.alerts) == 1 and r3.alerts[0].severity == "info"


def test_generate_signal_report_reuses_factor_vol_pct(fresh_state_db, monkeypatch):
    """변동성 급등 알림 baseline 이 FactorScores.vol_pct 를 재사용 (재계산·재fetch 없음)."""
    import src.signals as sig
    from src.signals import FactorScores

    _wire_macro(monkeypatch, {})
    monkeypatch.setattr(
        sig, "factor_scores",
        lambda t: FactorScores(t, 50, 50, 50, 50, [], low_vol=50, vol_pct=42.5),
    )
    sig.generate_signal_report(tickers=("NVDA",))

    from src.storage import get_storage
    saved = get_storage().get_state("signals", "last_run")
    assert saved["vols"] == {"NVDA": 42.5}


# ---------------- 전수 리뷰 회귀 (2026-07-06) ----------------


def test_momentum_primary_branch_needs_over_273_rows():
    """273행 초과에서만 12-1 스킵월 주 브랜치 — 250행(구 period=1y)은 폴백."""
    idx250 = pd.bdate_range("2023-01-02", periods=250)
    p250 = pd.Series(np.linspace(100, 150, 250), index=idx250)
    _, notes250 = momentum_score(p250)
    assert any("단기 대체" in n for n in notes250)          # 1y 면 영원히 폴백

    idx300 = pd.bdate_range("2023-01-02", periods=300)
    p300 = pd.Series(np.linspace(100, 150, 300), index=idx300)
    _, notes300 = momentum_score(p300)
    assert not any("단기 대체" in n for n in notes300)      # 2y 면 주 브랜치


def test_factor_scores_fetches_two_years(monkeypatch):
    """factor_scores 의 가격 조회가 period=2y — 1y 회귀 방지 (주 브랜치 사수)."""
    captured = {}

    def fake_prices(ticker, period="1y", **k):
        captured["period"] = period
        idx = pd.bdate_range("2022-01-03", periods=520)
        return pd.DataFrame({"Close": np.linspace(100, 200, 520)}, index=idx)

    monkeypatch.setattr("src.data_fetcher.fetch_prices", fake_prices)
    monkeypatch.setattr("src.data_fetcher.fetch_quote",
                        lambda t, **k: {"price": 150.0})
    monkeypatch.setattr("src.signals.latest_fundamentals",
                        lambda t: {"returnOnEquity": 0.15})
    from src.signals import factor_scores
    factor_scores("TEST")
    assert captured["period"] == "2y"
