"""src/signals.py — 순수 함수 (점수/룰/알림) 오프라인 검증."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.exceptions import InsufficientDataError
from src.macro_analyzer import RegimeReport
from src.signals import (
    AssetObs,
    apply_screen_rules,
    asset_observations,
    credit_transition,
    drawdown_transitions,
    fx_transition,
    momentum_score,
    regime_view,
    vol_ratio_transitions,
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


# ---------------- 알림 규칙 공통 헬퍼 ----------------

_MD_SPECIAL = ("_", "*", "`", "[", "]")


def _obs(label, dd=None, asof="2026-10-07", *, n_52w=250, vol_ratio=None,
         chg_20d_pct=None, last=100.0) -> AssetObs:
    """순수 함수 입력용 관측 (필드 직접 지정)."""
    return AssetObs(
        label=label, asof=asof, last=last, chg_1d_pct=None, chg_1d_abs=None,
        chg_20d_pct=chg_20d_pct, dd_52w_pct=dd, vs_ma200_pct=None,
        vol_ratio=vol_ratio, n_52w=n_52w,
    )


def _no_md(text: str) -> bool:
    return not any(c in text for c in _MD_SPECIAL)


# ---------------- drawdown_transitions ----------------


def test_drawdown_enter_once_then_quiet_then_escalate_once():
    # 비트코인 임계 -25 → 진입 warning 1건
    a1, n1, s1 = drawdown_transitions({"비트코인": _obs("비트코인", -26.0, "2026-10-01")}, {})
    assert [a.severity for a in a1] == ["warning"]
    assert a1[0].category == "drawdown" and a1[0].key == "dd:비트코인:enter"
    assert "경보 임계 -25% 진입" in a1[0].message
    assert n1 == [] and s1["비트코인"]["on"] is True and s1["비트코인"]["T"] == -25.0

    # 같은 낙폭 다음 런 → 0건 (상태 유지)
    a2, _, s2 = drawdown_transitions({"비트코인": _obs("비트코인", -26.0, "2026-10-02")}, s1)
    assert a2 == [] and s2["비트코인"]["on"] is True

    # 1.75T(-43.75) 도달 → critical 1건, 다시 도달해도 0건 (에피소드당 1회)
    a3, _, s3 = drawdown_transitions({"비트코인": _obs("비트코인", -44.0, "2026-10-03")}, s2)
    assert [(a.severity, a.key) for a in a3] == [("critical", "dd:비트코인:escalate")]
    a4, _, s4 = drawdown_transitions({"비트코인": _obs("비트코인", -45.0, "2026-10-04")}, s3)
    assert a4 == [] and s4["비트코인"]["worst"] == pytest.approx(-45.0)


def test_drawdown_enter_beyond_escalation_is_single_critical():
    # 진입 시점에 이미 확대 구간이면 critical 1건으로 합침 (같은 날 2줄 방지)
    alerts, _, state = drawdown_transitions({"S&P 500": _obs("S&P 500", -19.0)}, {})
    assert len(alerts) == 1 and alerts[0].severity == "critical"
    assert state["S&P 500"]["esc"] is True


def test_drawdown_clear_needs_three_distinct_asofs_and_is_not_an_alert():
    prev = {"비트코인": {"on": True, "T": -25.0, "entered": "2026-03-11", "worst": -30.0,
                       "esc": False, "ok": 0, "ok_asof": None}}
    # 해제 레벨 = 0.5T = -12.5 → -8 은 해제 조건 충족
    a1, n1, s1 = drawdown_transitions({"비트코인": _obs("비트코인", -8.0, "2026-04-23")}, prev)
    # 같은 asof 재관측(US 런 = 같은 데이터)은 세지 않음
    a1b, n1b, s1b = drawdown_transitions({"비트코인": _obs("비트코인", -8.0, "2026-04-23")}, s1)
    assert s1b["비트코인"]["ok"] == 1
    a2, n2, s2 = drawdown_transitions({"비트코인": _obs("비트코인", -8.0, "2026-04-24")}, s1b)
    assert s2["비트코인"]["on"] is True and n2 == []      # 2개 asof — 아직 해제 아님
    a3, n3, s3 = drawdown_transitions({"비트코인": _obs("비트코인", -8.1, "2026-04-25")}, s2)
    assert a1 == a1b == a2 == a3 == []                     # 해제는 🔔 아님
    assert s3["비트코인"]["on"] is False
    assert n3 == ["해제: 비트코인 -8.1% (03-11~04-25)"]


def test_drawdown_clear_counter_resets_when_condition_breaks():
    prev = {"KOSPI": {"on": True, "T": -12.0, "entered": "2026-07-03", "worst": -25.0,
                      "esc": True, "ok": 2, "ok_asof": "2026-09-01"}}
    _, _, s = drawdown_transitions({"KOSPI": _obs("KOSPI", -9.0, "2026-09-02")}, prev)
    assert s["KOSPI"]["ok"] == 0 and s["KOSPI"]["on"] is True


def test_drawdown_alr08_missing_asset_keeps_previous_state():
    prev = {"비트코인": {"on": True, "T": -25.0, "entered": "2026-09-01", "worst": -30.0,
                       "esc": False, "ok": 0, "ok_asof": None}}
    # 이번 런에 BTC 조회 실패 (obs 에 없음) → 상태 그대로 병합
    _, _, s1 = drawdown_transitions({"금": _obs("금", -3.0)}, prev)
    assert s1["비트코인"] == prev["비트코인"]
    # 다음 런 BTC -26% → 이미 on 이므로 재진입 알림 없음
    a2, _, _ = drawdown_transitions({"비트코인": _obs("비트코인", -26.0, "2026-10-05")}, s1)
    assert a2 == []


def test_drawdown_boundaries_threshold_fixed_and_min_obs(monkeypatch):
    import src.signals as sig

    # dd == T 정확히 → 진입
    a, _, s = drawdown_transitions({"S&P 500": _obs("S&P 500", -10.0)}, {})
    assert len(a) == 1 and s["S&P 500"]["T"] == -10.0
    # 진입 후 임계표가 바뀌어도 해제 레벨은 진입 시점 T 기준 (-5 이상)
    monkeypatch.setitem(sig.DD_THRESHOLDS_PCT, "S&P 500", -20)
    _, _, s2 = drawdown_transitions({"S&P 500": _obs("S&P 500", -6.0, "2026-10-08")}, s)
    assert s2["S&P 500"]["ok"] == 0                       # -6 < 0.5×(-10) → 미충족
    _, _, s3 = drawdown_transitions({"S&P 500": _obs("S&P 500", -4.0, "2026-10-09")}, s2)
    assert s3["S&P 500"]["ok"] == 1
    # 52주 관측 200개 미만 → 평가 생략
    a4, _, s4 = drawdown_transitions({"금": _obs("금", -30.0, n_52w=150)}, {})
    assert a4 == [] and "금" not in s4


def test_drawdown_skips_fx_label():
    a, _, s = drawdown_transitions({"원/달러": _obs("원/달러", -30.0)}, {})
    assert a == [] and s == {}


def test_drawdown_messages_are_markdown_safe():
    obs = {lbl: _obs(lbl, -50.0) for lbl in ("S&P 500", "장기국채 (20Y+)", "KOSPI")}
    alerts, _, _ = drawdown_transitions(obs, {})
    assert alerts and all(_no_md(a.message) for a in alerts)


# ---------------- asset_observations ----------------


def test_asset_observations_mixed_calendar_and_52w_window():
    days = pd.date_range("2024-01-01", "2026-01-30", freq="D")
    bdays = days[days.dayofweek < 5]
    # BTC: 매일 거래, 2024-03-01 에 300 고점(365일 창 밖) → 이후 100~200 선형 상승 후 하락
    btc = pd.Series(np.linspace(100, 200, len(days)), index=days)
    btc.loc["2024-03-01"] = 300.0
    btc.iloc[-10:] = 150.0
    # ETF: 평일만
    spy = pd.Series(np.linspace(100, 120, len(bdays)), index=bdays)
    panel = pd.concat({"비트코인": btc, "S&P 500": spy}, axis=1)   # 주말 행에 SPY NaN

    obs = asset_observations(panel)
    b, s = obs["비트코인"], obs["S&P 500"]
    assert b.asof == "2026-01-30"
    assert s.asof == "2026-01-30"                         # 금요일 (자기 캘린더)
    # 52주 창은 365일 — 2024-03 의 300 고점은 창 밖 → 창 안 최고가 기준
    window_max = float(btc.loc[pd.Timestamp("2026-01-30") - pd.Timedelta(days=365):].max())
    assert b.dd_52w_pct == pytest.approx((150.0 / window_max - 1) * 100)
    assert b.dd_52w_pct > -40                              # 300 기준(-50%)이 아님
    assert s.dd_52w_pct == pytest.approx(0.0)              # 단조 상승 → 고점
    # ETF 의 20일 변화는 자기 관측 20개 기준 (주말 NaN 이 끼지 않음)
    spy_obs = spy.dropna()
    assert s.chg_20d_pct == pytest.approx((spy_obs.iloc[-1] / spy_obs.iloc[-21] - 1) * 100)
    assert s.n_52w == len(spy_obs.loc[pd.Timestamp("2026-01-30") - pd.Timedelta(days=365):])


def test_asset_observations_vol_ratio_20_over_252():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2024-01-01", periods=400)
    r = rng.normal(0, 0.01, 400)
    r[-20:] = rng.normal(0, 0.04, 20)                      # 최근 20일 변동성 급등
    px = pd.Series(100 * np.cumprod(1 + r), index=idx)
    o = asset_observations(pd.DataFrame({"KOSPI": px}))["KOSPI"]
    rets = px.pct_change().dropna()
    expected = rets.iloc[-20:].std() / rets.iloc[-252:].std()
    assert o.vol_ratio == pytest.approx(expected)
    assert o.vol_ratio > 2.0


def test_asset_observations_short_history_has_no_vol_ratio():
    idx = pd.bdate_range("2026-01-01", periods=50)
    o = asset_observations(pd.DataFrame({"금": np.linspace(100, 110, 50)}, index=idx))["금"]
    assert o.vol_ratio is None and o.vs_ma200_pct is None and o.n_52w == 50


# ---------------- vol_ratio_transitions ----------------


def test_vol_ratio_enter_hold_and_clear():
    a1, _, s1 = vol_ratio_transitions({"S&P 500": _obs("S&P 500", vol_ratio=2.3, asof="d1")}, {})
    assert len(a1) == 1 and a1[0].category == "volatility"
    assert "2.3배" in a1[0].message and _no_md(a1[0].message)
    # 1.3 은 해제 기준(<1.25) 미달 → 유지, 카운터 0
    a2, n2, s2 = vol_ratio_transitions({"S&P 500": _obs("S&P 500", vol_ratio=1.3, asof="d2")}, s1)
    assert a2 == [] and n2 == [] and s2["S&P 500"]["on"] is True and s2["S&P 500"]["ok"] == 0
    s = s2
    notes = []
    for d in ("d3", "d4", "d5"):
        a, notes, s = vol_ratio_transitions({"S&P 500": _obs("S&P 500", vol_ratio=1.24, asof=d)}, s)
        assert a == []
    assert s["S&P 500"]["on"] is False and len(notes) == 1 and "해제" in notes[0]


def test_vol_ratio_below_enter_no_alert_and_fx_excluded():
    a, _, s = vol_ratio_transitions({"금": _obs("금", vol_ratio=1.9),
                                     "원/달러": _obs("원/달러", vol_ratio=5.0)}, {})
    assert a == [] and s == {}


# ---------------- credit_transition ----------------


def _hy(d20: float, end="2026-10-06", base=3.0) -> pd.Series:
    idx = pd.bdate_range(end=end, periods=30)
    vals = np.full(30, base)
    vals[-1] = base + d20
    return pd.Series(vals, index=idx)


def test_credit_enter_escalate_once_and_none_keeps_state():
    a1, _, s1 = credit_transition(_hy(0.59), {})
    assert [(a.severity, a.key) for a in a1] == [("warning", "credit:enter")]
    assert "+0.59%p" in a1[0].message and "3.59%" in a1[0].message and _no_md(a1[0].message)
    a2, _, s2 = credit_transition(_hy(1.05, end="2026-10-07"), s1)
    assert [(a.severity, a.key) for a in a2] == [("critical", "credit:escalate")]
    a3, _, s3 = credit_transition(_hy(1.2, end="2026-10-08"), s2)
    assert a3 == []                                        # 확대는 1회
    # 조회 실패(hy=None) → 상태 그대로, 알림 없음
    a4, n4, s4 = credit_transition(None, s3)
    assert a4 == [] and n4 == [] and s4 == s3


def test_credit_below_threshold_no_alert_and_clear_after_three_dates():
    a, _, s = credit_transition(_hy(0.49), {})
    assert a == [] and not s.get("on")
    _, _, s = credit_transition(_hy(0.6), {})
    notes = []
    for end in ("2026-10-07", "2026-10-08", "2026-10-09"):
        _, notes, s = credit_transition(_hy(0.1, end=end), s)
    assert s == {"on": False, "cleared": "2026-10-09"} and len(notes) == 1


# ---------------- fx_transition ----------------


def test_fx_direction_wording():
    a_up, _, s_up = fx_transition(_obs("원/달러", chg_20d_pct=5.9), {})
    assert len(a_up) == 1 and "원화 약세" in a_up[0].message and "+5.9%" in a_up[0].message
    a_dn, _, _ = fx_transition(_obs("원/달러", chg_20d_pct=-5.0), {})
    assert len(a_dn) == 1 and "원화 강세" in a_dn[0].message
    a_no, _, s_no = fx_transition(_obs("원/달러", chg_20d_pct=4.9), {})
    assert a_no == [] and not s_no.get("on")


def test_fx_clear_after_three_distinct_dates_and_none_keeps_state():
    _, _, s = fx_transition(_obs("원/달러", chg_20d_pct=6.0, asof="2026-10-01"), {})
    a, n, s_same = fx_transition(None, s)
    assert a == [] and n == [] and s_same == s
    notes = []
    for d in ("2026-10-02", "2026-10-02", "2026-10-05", "2026-10-06"):  # 중복 asof 1회
        _, notes, s = fx_transition(_obs("원/달러", chg_20d_pct=1.0, asof=d), s)
    assert s["on"] is False and len(notes) == 1


# ---------------- regime_view ----------------


def _regime(label, score, available=3, total=3, failures=None):
    return RegimeReport(regime=label, score=score, available=available, total=total,
                        failures=failures or [])


_RISK_ON = "🟢 위험선호 (Risk-on)"
_WEAK_ON = "🟡 약한 위험선호"
_NEUTRAL = "⚪ 중립 / 혼조"


def test_regime_view_incomplete_holds_previous_and_state_unchanged():
    prev = {"confirmed": _RISK_ON, "confirmed_score": 2, "pending": None, "pending_dates": []}
    rep = _regime("⚪ 판정 보류 (지표 2/3)", 1, available=2, failures=["yield_curve"])
    view, alert, state = regime_view(rep, prev, "2026-10-08")
    assert view.shown_label == f"{_RISK_ON} (직전 판정 유지 · 지표 2/3)"
    assert view.held is True and alert is None
    assert state == prev


def test_regime_view_incomplete_without_history_is_hold_label():
    view, alert, state = regime_view(_regime("x", 0, available=0), {}, "2026-10-08")
    assert view.shown_label == "⚪ 판정 보류 (지표 0/3)" and alert is None and state == {}


def test_regime_view_first_run_confirms_immediately_without_alert():
    view, alert, state = regime_view(_regime(_RISK_ON, 2), None, "2026-10-08")
    assert view.shown_label == _RISK_ON and alert is None
    assert state["confirmed"] == _RISK_ON and state["confirmed_score"] == 2


def test_regime_view_needs_two_distinct_kst_dates():
    prev = {"confirmed": _RISK_ON, "confirmed_score": 2, "pending": None, "pending_dates": []}
    # KR 런 (10-08) → 후보 1/2
    v1, a1, s1 = regime_view(_regime(_NEUTRAL, 0), prev, "2026-10-08")
    assert a1 is None and v1.shown_label == _RISK_ON
    assert v1.pending == _NEUTRAL and v1.pending_count == 1
    # 같은 날 US 런 → 여전히 1/2 (같은 날은 1회)
    v2, a2, s2 = regime_view(_regime(_NEUTRAL, 0), s1, "2026-10-08")
    assert a2 is None and v2.pending_count == 1 and s2["confirmed"] == _RISK_ON
    # 다음 날 → 확정 + '중립 이하' 경계 통과 → 🔔
    v3, a3, s3 = regime_view(_regime(_NEUTRAL, 0), s2, "2026-10-09")
    assert a3 is not None and a3.severity == "warning" and a3.category == "regime"
    assert v3.shown_label == _NEUTRAL and v3.prev_label == _RISK_ON
    assert s3["confirmed"] == _NEUTRAL and s3["pending"] is None


def test_regime_view_same_side_transition_has_no_alert():
    prev = {"confirmed": _RISK_ON, "confirmed_score": 2, "pending": _WEAK_ON,
            "pending_dates": ["2026-10-07"]}
    view, alert, state = regime_view(_regime(_WEAK_ON, 1), prev, "2026-10-08")
    assert alert is None
    assert view.shown_label == _WEAK_ON and view.prev_label == _RISK_ON
    assert state["confirmed"] == _WEAK_ON


def test_regime_view_back_to_confirmed_clears_pending():
    prev = {"confirmed": _RISK_ON, "confirmed_score": 2, "pending": _NEUTRAL,
            "pending_dates": ["2026-10-07"]}
    view, alert, state = regime_view(_regime(_RISK_ON, 2), prev, "2026-10-08")
    assert alert is None and view.pending is None
    assert state["pending"] is None and state["pending_dates"] == []


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


# ---------------- generate_signal_report (무쓰기) / commit_signal_state ----------------


@pytest.fixture
def fresh_state_db(tmp_path, monkeypatch):
    """signals 가 쓰는 storage 싱글톤을 tmp DB 로 격리 (state 테이블)."""
    import src.storage as storage_mod

    db = tmp_path / "sig.db"
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(db))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield db
    if storage_mod._storage is not None:
        storage_mod._storage.close()
    monkeypatch.setattr(storage_mod, "_storage", None)


def _state_rows(db) -> list[tuple]:
    import sqlite3

    if not db.exists():
        return []
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT namespace, key, payload, updated_at FROM state ORDER BY namespace, key"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _panel(dd_by_label: dict[str, float], end="2026-10-07", n=300) -> pd.DataFrame:
    """각 자산이 고점 대비 약 dd% 로 끝나는 평일 패널.

    ±0.5% 지그재그(일정한 변동성 → σ20/σ252 ≈ 1, 변동성 알림이 끼지 않음) 위에
    마지막 60일 선형 하락을 얹음. 측정 낙폭은 dd 의 ±1%p 이내.
    """
    idx = pd.bdate_range(end=end, periods=n)
    zig = 100.0 * np.cumprod(1 + 0.005 * (-1.0) ** np.arange(n))
    cols = {}
    for label, dd in dd_by_label.items():
        trend = np.ones(n)
        trend[-60:] = np.linspace(1.0, 1 + dd / 100.0, 60)
        cols[label] = pd.Series(zig * trend, index=idx)
    return pd.DataFrame(cols)


def _wire_market(monkeypatch, dd_by_label, *, regime=None, failed_assets=(),
                 hy=None, hy_error=None, end="2026-10-07"):
    """generate_signal_report 의 fetch 경계를 합성 데이터로 대체 (네트워크 0)."""
    import src.macro_analyzer as ma

    regime = regime or RegimeReport(regime="🟢 위험선호 (Risk-on)", score=2,
                                    available=3, total=3)
    monkeypatch.setattr(ma, "classify_regime", lambda: regime)
    monkeypatch.setattr(ma, "fetch_panel_with_failures",
                        lambda tickers, period="2y": (_panel(dd_by_label, end=end),
                                                      list(failed_assets)))

    def fake_resilient(series_id):
        if hy_error is not None:
            raise hy_error
        s = hy if hy is not None else pd.Series(
            np.full(30, 3.0), index=pd.bdate_range(end=end, periods=30))
        return s, None

    monkeypatch.setattr(ma, "_fetch_resilient", fake_resilient)


def test_generate_signal_report_writes_no_state_and_commit_creates_global_v2(
        fresh_state_db, monkeypatch):
    import src.signals as sig
    from src.storage import get_storage

    get_storage().put_state("signals", "last_run", {"regime": "old"})   # 구 키 — 불변이어야 함
    before = _state_rows(fresh_state_db)

    _wire_market(monkeypatch, {"비트코인": -30.0, "금": -1.0})
    r1 = sig.generate_signal_report(tickers=(), today_kst="2026-10-08")
    assert _state_rows(fresh_state_db) == before                # 무쓰기
    assert r1.first_run is True and r1.alerts == []             # 첫 실행 = 시딩, 알림 생략
    assert r1.pending_state["v"] == 2 and r1.pending_state["dd"]["비트코인"]["on"] is True
    assert any(a.startswith("비트코인") for a in r1.active)      # '경보 중' 으로 현재 상황 노출

    # 두 번 돌려도 상태 불변 (dry-run·check_signals 반복 안전 — MSG-09)
    sig.generate_signal_report(tickers=(), today_kst="2026-10-08")
    assert _state_rows(fresh_state_db) == before

    assert sig.commit_signal_state(r1) is True
    saved = get_storage().get_state("signals", "global:v2")
    assert saved == r1.pending_state
    assert get_storage().get_state("signals", "last_run") == {"regime": "old"}


def test_generate_signal_report_us_after_kr_commit_reports_only_new(fresh_state_db, monkeypatch):
    """KR 런 커밋 후 같은 데이터의 US 런은 알림 0건 — 새 변화만 알림."""
    import src.signals as sig

    _wire_market(monkeypatch, {"비트코인": -10.0, "KOSPI": -3.0})
    seed = sig.generate_signal_report(tickers=(), market="KR", today_kst="2026-10-07")
    assert sig.commit_signal_state(seed)

    _wire_market(monkeypatch, {"비트코인": -10.0, "KOSPI": -14.0})
    kr = sig.generate_signal_report(tickers=(), market="KR", today_kst="2026-10-08")
    assert [a.key for a in kr.alerts] == ["dd:KOSPI:enter"]
    assert kr.kr is not None and kr.kr.kospi is not None
    assert kr.pending_state["committed_by"] == "KR"
    assert sig.commit_signal_state(kr)

    us = sig.generate_signal_report(tickers=(), market="US", today_kst="2026-10-08")
    assert us.alerts == [] and us.kr is None                    # 스냅샷은 KR 만
    assert any(a.startswith("KOSPI") for a in us.active)


def test_generate_signal_report_not_committed_means_alert_repeats(fresh_state_db, monkeypatch):
    """발송 실패(커밋 안 함)면 다음 런에서 같은 전환이 다시 나온다."""
    import src.signals as sig

    _wire_market(monkeypatch, {"금": -1.0})
    assert sig.commit_signal_state(sig.generate_signal_report(tickers=(), today_kst="2026-10-07"))
    _wire_market(monkeypatch, {"금": -12.0})
    r1 = sig.generate_signal_report(tickers=(), today_kst="2026-10-08")
    r2 = sig.generate_signal_report(tickers=(), today_kst="2026-10-08")
    assert [a.key for a in r1.alerts] == [a.key for a in r2.alerts] == ["dd:금:enter"]


def test_generate_signal_report_baseline_read_failure_blocks_commit(fresh_state_db, monkeypatch):
    import src.signals as sig
    from src.storage import Storage, StorageError

    _wire_market(monkeypatch, {"비트코인": -30.0})

    def boom(self, namespace, key, strict=False):
        raise StorageError("db down")

    monkeypatch.setattr(Storage, "get_state", boom)
    r = sig.generate_signal_report(tickers=(), today_kst="2026-10-08")
    assert r.baseline_missing is True and r.alerts == []
    assert r.pending_state is None
    assert "비교 기준 읽기 실패 — 변화 알림 생략" in r.data_issues
    assert sig.commit_signal_state(r) is False


def test_generate_signal_report_data_issues_and_alr08(fresh_state_db, monkeypatch):
    """자산·국면·신용 조회 실패가 data_issues 로 노출되고, 실패 자산 상태는 유지."""
    import src.signals as sig
    from src.exceptions import ApiHttpError

    _wire_market(monkeypatch, {"비트코인": -30.0, "금": -1.0})
    assert sig.commit_signal_state(sig.generate_signal_report(tickers=(), today_kst="2026-10-07"))

    partial = RegimeReport(regime="⚪ 판정 보류 (지표 2/3)", score=1, available=2, total=3,
                           failures=["yield_curve"])
    _wire_market(monkeypatch, {"금": -1.0}, regime=partial, failed_assets=["비트코인"],
                 hy_error=ApiHttpError("FRED BAMLH0A0HYM2: HTTP 502", status_code=502))
    r = sig.generate_signal_report(tickers=(), today_kst="2026-10-08")
    assert "가격 조회 실패: 비트코인" in r.data_issues
    assert "국면 지표 2/3 — 장단기 금리차 조회 실패" in r.data_issues
    assert "신용 스프레드 조회 실패" in r.data_issues
    assert r.pending_state["dd"]["비트코인"]["on"] is True       # ALR-08 병합
    assert r.regime.held is True
    assert r.regime_label == "🟢 위험선호 (Risk-on) (직전 판정 유지 · 지표 2/3)"
    # 사용자에게 보이는 동적 문자열은 Markdown 특수문자 없음 (§4.10 #10)
    for text in [*r.data_issues, *r.active, r.regime.reason_line]:
        assert _no_md(text), text


def test_build_daily_digest_dry_run_does_not_write_state(fresh_state_db, monkeypatch):
    """send_digest --dry-run 경로(build_daily_digest)는 상태를 쓰지 않는다."""
    import src.digest as digest
    from src.storage import get_storage

    _wire_market(monkeypatch, {"KOSPI": -20.0})
    monkeypatch.setattr(digest, "_collect_predictions", lambda: [])
    monkeypatch.setattr("src.universe.scan", lambda market="KR", limit=6, **k: [])
    get_storage()
    before = _state_rows(fresh_state_db)
    text = digest.build_daily_digest(market="kr")
    assert isinstance(text, str) and text
    assert _state_rows(fresh_state_db) == before


def test_alerts_sorted_critical_first_then_kr_assets():
    import src.signals as sig

    alerts = [
        sig.Alert("warning", "fx", "fx", key="fx:enter"),
        sig.Alert("warning", "drawdown", "spy", key="dd:S&P 500:enter"),
        sig.Alert("warning", "drawdown", "kospi", key="dd:KOSPI:enter"),
        sig.Alert("critical", "drawdown", "btc", key="dd:비트코인:escalate"),
    ]
    alerts.sort(key=sig._alert_sort_key)
    assert [a.message for a in alerts] == ["btc", "kospi", "spy", "fx"]


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
