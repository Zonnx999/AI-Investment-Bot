"""src/portfolio.py — 합성 데이터 기반 결정론적 검증 (네트워크 없음)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.exceptions import InsufficientDataError
from src.portfolio import (
    MIN_KELLY_POINTS,
    PortfolioProposal,
    correlation_penalty,
    equal_weights,
    inverse_vol_weights,
    kelly_cap,
    propose,
    weighted_backtest,
)


def _prices_from_returns(returns, start="2022-01-03", base=100.0) -> pd.Series:
    """수익률 리스트 → 가격 시리즈 (영업일 인덱스). 첫 값은 base."""
    r = np.asarray(returns, dtype=float)
    prices = base * np.cumprod(np.concatenate([[1.0], 1.0 + r]))
    idx = pd.bdate_range(start, periods=len(prices))
    return pd.Series(prices, index=idx)


def _alternating(mag: float, n: int, start="2022-01-03") -> pd.Series:
    """±mag 교대 수익률 가격 시리즈 — 평균 ~0, 표준편차가 mag 에 정비례."""
    r = [mag if i % 2 == 0 else -mag for i in range(n)]
    return _prices_from_returns(r, start=start)


def _noise_prices(n: int, scale: float, seed: int, loc: float = 0.0,
                  start="2022-01-03") -> pd.Series:
    rng = np.random.default_rng(seed)
    return _prices_from_returns(rng.normal(loc, scale, n), start=start)


# ---------------- inverse_vol_weights ----------------


def test_inverse_vol_two_assets_exact_ratio():
    # B 수익률 = A 수익률 × 2 (표본별 동일 배율) → 변동성 정확히 1:2 → 비중 2:1
    a = _alternating(0.01, 130)
    b = _prices_from_returns(a.pct_change().dropna() * 2.0)
    w = inverse_vol_weights({"A": a, "B": b}, max_weight=1.0)
    assert w["A"] == pytest.approx(2.0 / 3.0)
    assert w["B"] == pytest.approx(1.0 / 3.0)
    assert sum(w.values()) == pytest.approx(1.0)


def test_inverse_vol_cap_and_redistribution():
    # 변동성 1 : 10 : 10 → 무상한 비중 [10/12, 1/12, 1/12]. 상한 0.5 → [0.5, 0.25, 0.25]
    a = _alternating(0.002, 130)
    ra = a.pct_change().dropna()
    b = _prices_from_returns(ra * 10.0)
    c = _prices_from_returns(ra * 10.0)
    w = inverse_vol_weights({"A": a, "B": b, "C": c}, max_weight=0.5)
    assert w["A"] == pytest.approx(0.5)
    assert w["B"] == pytest.approx(0.25)
    assert w["C"] == pytest.approx(0.25)
    assert max(w.values()) <= 0.5 + 1e-9
    assert sum(w.values()) == pytest.approx(1.0)


def test_inverse_vol_zero_vol_excluded():
    # 상수 가격(변동성 0)은 무한 가중을 받으면 안 됨 — 제외 후 나머지로 정규화
    flat = pd.Series(100.0, index=pd.bdate_range("2022-01-03", periods=130))
    a = _alternating(0.01, 130)
    b = _noise_prices(130, 0.02, seed=1)
    w = inverse_vol_weights({"FLAT": flat, "A": a, "B": b}, max_weight=1.0)
    assert "FLAT" not in w
    assert set(w) == {"A", "B"}
    assert sum(w.values()) == pytest.approx(1.0)


def test_inverse_vol_short_history_excluded_and_insufficient_raises():
    short = _alternating(0.01, 30)              # 수익률 30개 < 룩백 63 → 제외
    a = _alternating(0.01, 130)
    b = _noise_prices(130, 0.02, seed=2)
    w = inverse_vol_weights({"SHORT": short, "A": a, "B": b}, max_weight=1.0)
    assert "SHORT" not in w

    with pytest.raises(InsufficientDataError):  # 제외 후 1개 남음 → 포트폴리오 불가
        inverse_vol_weights({"SHORT": short, "A": a}, max_weight=1.0)


def test_inverse_vol_infeasible_cap_raises():
    # 자산 2개 × 상한 0.25 = 합 0.5 < 1 — 어떤 재배분도 합 1 불가 → 명시적 에러
    a = _alternating(0.01, 130)
    b = _noise_prices(130, 0.02, seed=3)
    with pytest.raises(InsufficientDataError):
        inverse_vol_weights({"A": a, "B": b}, max_weight=0.25)


def test_inverse_vol_invalid_params():
    a = _alternating(0.01, 130)
    b = _noise_prices(130, 0.02, seed=4)
    with pytest.raises(ValueError):
        inverse_vol_weights({"A": a, "B": b}, lookback=1)
    with pytest.raises(ValueError):
        inverse_vol_weights({"A": a, "B": b}, max_weight=0.0)
    with pytest.raises(ValueError):
        inverse_vol_weights({"A": a, "B": b}, max_weight=1.5)


# ---------------- correlation_penalty ----------------


def _corr_universe():
    """X 는 보유 H 와 상관 1.0, Y 는 독립 노이즈."""
    rng = np.random.default_rng(10)
    base = rng.normal(0.0, 0.01, 200)
    x = _prices_from_returns(base)
    h = _prices_from_returns(base * 1.5)        # 배율만 달라도 상관은 1.0
    y = _prices_from_returns(rng.normal(0.0, 0.01, 200))
    return x, y, h


def test_correlation_penalty_triggers_and_renormalizes():
    x, y, h = _corr_universe()
    out = correlation_penalty(
        {"X": 0.5, "Y": 0.5}, {"X": x, "Y": y}, {"H": h}, threshold=0.7, penalty=0.5,
    )
    # X ×0.5 → 0.25, 재정규화: X=0.25/0.75=1/3, Y=0.5/0.75=2/3
    assert out["X"] == pytest.approx(1.0 / 3.0)
    assert out["Y"] == pytest.approx(2.0 / 3.0)
    assert sum(out.values()) == pytest.approx(1.0)


def test_correlation_penalty_no_held_is_noop():
    x, y, _ = _corr_universe()
    w = {"X": 0.4, "Y": 0.6}
    out = correlation_penalty(w, {"X": x, "Y": y}, {})
    assert out == w
    assert out is not w                          # 입력 dict 원본 보존 (복사본)


def test_correlation_penalty_min_overlap_guard():
    # 보유 시계열이 후보와 전혀 안 겹침 (수년 전 데이터) → 판정 보류 = 페널티 없음
    x, y, _ = _corr_universe()
    old_h = _prices_from_returns(x.pct_change().dropna().to_numpy(), start="2010-01-04")
    out = correlation_penalty({"X": 0.5, "Y": 0.5}, {"X": x, "Y": y}, {"H": old_h})
    assert out["X"] == pytest.approx(0.5)
    assert out["Y"] == pytest.approx(0.5)


def test_correlation_penalty_zero_penalty_all_redundant():
    # penalty=0 + 모든 후보가 보유와 중복 → 신규 배분 없음 (전부 0, 재정규화 안 함)
    x, _, h = _corr_universe()
    out = correlation_penalty({"X": 1.0}, {"X": x}, {"H": h}, penalty=0.0)
    assert out["X"] == 0.0


def test_correlation_penalty_invalid_params():
    x, y, h = _corr_universe()
    with pytest.raises(ValueError):
        correlation_penalty({"X": 1.0}, {"X": x}, {"H": h}, threshold=1.0)
    with pytest.raises(ValueError):
        correlation_penalty({"X": 1.0}, {"X": x}, {"H": h}, penalty=-0.1)


# ---------------- kelly_cap ----------------


def test_kelly_cap_zeroes_negative_edge_and_leaves_cash():
    # NEG: 평균 음수 (교대 −2%/+1%) → f* < 0 → 비중 0.  POS: 강한 양의 엣지 → 유지.
    neg = _prices_from_returns([-0.02, 0.01] * 150)
    pos = _prices_from_returns([0.02, -0.005] * 150)
    out = kelly_cap({"NEG": 0.5, "POS": 0.5}, {"NEG": neg, "POS": pos})
    assert out["NEG"] == 0.0
    assert out["POS"] == pytest.approx(0.5)      # f* 큼 → 상한 미발동
    assert sum(out.values()) == pytest.approx(0.5)   # 잔여는 현금 (재정규화 금지)


def test_kelly_cap_caps_weak_edge_at_half_kelly():
    # 약한 엣지: 교대 +2.1%/−2% → 손계산으로 기대 상한 = 0.5 × mean/var
    weak = _prices_from_returns([0.021, -0.02] * 150)
    r = weak.pct_change().dropna().iloc[-252:]
    f_star = float(r.mean()) / float(r.var())
    expected = 0.5 * f_star
    assert 0.0 < expected < 0.9                  # 테스트 구성 자체 검증 (상한 발동 조건)
    out = kelly_cap({"WEAK": 0.9}, {"WEAK": weak})
    assert out["WEAK"] == pytest.approx(expected)


def test_kelly_cap_zero_variance_and_missing_data_zeroed():
    flat = pd.Series(100.0, index=pd.bdate_range("2022-01-03", periods=300))
    short = _prices_from_returns([0.01] * (MIN_KELLY_POINTS - 5))
    out = kelly_cap({"FLAT": 0.5, "SHORT": 0.3, "GHOST": 0.2},
                    {"FLAT": flat, "SHORT": short})
    assert out == {"FLAT": 0.0, "SHORT": 0.0, "GHOST": 0.0}


def test_kelly_cap_renormalizes_only_if_total_above_one():
    pos = _prices_from_returns([0.02, -0.005] * 150)
    pos2 = _prices_from_returns([0.018, -0.004] * 150)
    out = kelly_cap({"A": 0.8, "B": 0.8}, {"A": pos, "B": pos2})
    assert sum(out.values()) == pytest.approx(1.0)   # 1.6 → 정규화


def test_kelly_cap_invalid_params():
    pos = _prices_from_returns([0.01, -0.005] * 150)
    with pytest.raises(ValueError):
        kelly_cap({"A": 1.0}, {"A": pos}, cap_fraction=0.0)
    with pytest.raises(ValueError):
        kelly_cap({"A": 1.0}, {"A": pos}, lookback=1)


# ---------------- propose (파이프라인) ----------------


def test_propose_end_to_end_notes_and_cash():
    rng = np.random.default_rng(42)
    a = _prices_from_returns(rng.normal(0.0005, 0.01, 300))
    b = _prices_from_returns(rng.normal(0.0005, 0.02, 300))
    c_base = rng.normal(0.0004, 0.015, 300)
    c = _prices_from_returns(c_base)
    held = {"H": _prices_from_returns(c_base * 1.2)}       # C 와 상관 1.0
    neg = _prices_from_returns([-0.02, 0.01] * 150)        # 음의 엣지 → Kelly 0
    flat = pd.Series(100.0, index=a.index)                 # 변동성 0 → 제외

    prices = {"A": a, "B": b, "C": c, "NEG": neg, "FLAT": flat}
    proposal = propose(
        ["A", "B", "C", "NEG", "FLAT", "MISSING", "A"],    # 중복 + 미존재 포함
        prices, held=held, max_weight=0.6,
    )

    assert isinstance(proposal, PortfolioProposal)
    assert "FLAT" not in proposal.weights                  # 변동성 0 제외
    assert "MISSING" not in proposal.weights               # 가격 없음 제외
    assert proposal.weights["NEG"] == 0.0                  # 음의 엣지 → 0
    assert proposal.weights["C"] < proposal.weights["A"] or proposal.weights["C"] == 0.0

    total = sum(proposal.weights.values())
    assert total <= 1.0 + 1e-9
    assert proposal.cash_weight == pytest.approx(1.0 - total)

    joined = "\n".join(proposal.notes)
    assert "제외" in joined                                # 제외 사유 노트
    assert "상관" in joined                                # 상관 페널티 노트
    assert "엣지" in joined                                # Kelly 노트

    d = proposal.to_dict()
    assert set(d) == {"weights", "cash_weight", "notes"}
    assert "현금" in str(proposal) or proposal.cash_weight <= 1e-6
    assert "제안 비중" in str(proposal)


def test_propose_insufficient_candidates_raises():
    a = _alternating(0.01, 130)
    with pytest.raises(InsufficientDataError):
        propose(["A"], {"A": a}, max_weight=1.0)


# ---------------- weighted_backtest ----------------


def _iv_fn(hist):
    return inverse_vol_weights(hist, lookback=30, max_weight=1.0)


def _two_regime_universe(change_at: int, factor: float, n: int = 300, seed: int = 7):
    """change_at 이후 B 의 수익률 크기가 factor 배로 바뀌는 2자산 유니버스."""
    rng = np.random.default_rng(seed)
    ra = rng.normal(0.0, 0.01, n)
    rb = rng.normal(0.0, 0.01, n)
    rb2 = rb.copy()
    rb2[change_at:] *= factor
    a = _prices_from_returns(ra)
    return {"A": a, "B": _prices_from_returns(rb2)}, a.index


def test_weighted_backtest_no_lookahead():
    # t 이후의 국면 전환(변동성 5배)이 t 이전의 비중·수익에 영향을 주면 안 됨:
    # 전환 전 구간의 에쿼티 커브가 두 시나리오에서 완전히 일치해야 한다.
    change_at = 200
    u_same, idx = _two_regime_universe(change_at, factor=1.0)
    u_regime, _ = _two_regime_universe(change_at, factor=5.0)
    res_same = weighted_backtest(u_same, _iv_fn, cost_bps=0.0)
    res_regime = weighted_backtest(u_regime, _iv_fn, cost_bps=0.0)

    cutoff = idx[change_at]                     # 첫 번째로 달라지는 가격 시점
    e1 = res_same.equity_curve[res_same.equity_curve.index < cutoff]
    e2 = res_regime.equity_curve.reindex(e1.index)
    assert len(e1) > 50                          # 비교 구간이 실제로 존재
    assert np.allclose(e1.to_numpy(), e2.to_numpy())
    # 전환 후에는 달라져야 함 (변동성 국면이 비중에 반영되긴 하는지)
    assert res_same.equity_curve.iloc[-1] != pytest.approx(res_regime.equity_curve.iloc[-1])


def test_weighted_backtest_costs_reduce_return():
    universe, _ = _two_regime_universe(200, factor=1.0)
    free = weighted_backtest(universe, _iv_fn, cost_bps=0.0)
    costly = weighted_backtest(universe, _iv_fn, cost_bps=100.0)
    assert costly.total_return_pct < free.total_return_pct
    assert costly.cost_bps == 100.0


def test_weighted_backtest_inverse_vol_beats_equal_weight_on_vol():
    # 저변동 + 고변동 유니버스: 역변동성은 저변동 자산 쏠림 → 포트폴리오 변동성이
    # 동일가중보다 낮아야 함 (수익률 우위는 요구하지 않음 — risk parity 의 목적함수)
    rng = np.random.default_rng(11)
    low = _prices_from_returns(rng.normal(0.0, 0.002, 400))
    high = _prices_from_returns(rng.normal(0.0, 0.03, 400))
    universe = {"LOW": low, "HIGH": high}
    strat = weighted_backtest(universe, _iv_fn, cost_bps=10.0)
    bench = weighted_backtest(universe, equal_weights, cost_bps=10.0)
    assert strat.annualized_vol_pct < bench.annualized_vol_pct


def test_weighted_backtest_early_insufficiency_skipped_then_starts():
    # 첫 리밸런스(월말)엔 룩백 30 미달 → 스킵, 이후 성공 — 에러 없이 결과 산출
    universe, _ = _two_regime_universe(100, factor=1.0, n=150)
    res = weighted_backtest(universe, _iv_fn, cost_bps=0.0)
    assert res.n_periods >= 20
    assert res.n_trades >= 1


def test_weighted_backtest_contract_violations_raise():
    universe, _ = _two_regime_universe(100, factor=1.0, n=200)
    with pytest.raises(ValueError):
        weighted_backtest(universe, _iv_fn, cost_bps=-1.0)
    with pytest.raises(ValueError):
        weighted_backtest(universe, lambda h: {"A": -0.2, "B": 0.5})
    with pytest.raises(ValueError):
        weighted_backtest(universe, lambda h: {"A": 0.8, "B": 0.8})
    with pytest.raises(ValueError):
        weighted_backtest(universe, lambda h: {"ZZZ": 1.0})


def test_weighted_backtest_never_enough_data_raises():
    universe, _ = _two_regime_universe(10, factor=1.0, n=40)   # 룩백 30 > 월 표본
    def always_raises(hist):
        raise InsufficientDataError("no", n_points=0, required=1)
    with pytest.raises(InsufficientDataError):
        weighted_backtest(universe, always_raises)
    with pytest.raises(InsufficientDataError):
        weighted_backtest({}, _iv_fn)


def test_equal_weights_and_guard():
    a = _alternating(0.01, 50)
    b = _noise_prices(50, 0.01, seed=5)
    assert equal_weights({"A": a, "B": b}) == {"A": 0.5, "B": 0.5}
    with pytest.raises(InsufficientDataError):
        equal_weights({"A": a})


# ---------------- 오케스트레이터 스크립트 — 오프라인 import 보장 ----------------


def test_check_portfolio_script_imports_offline():
    import scripts.check_portfolio as mod

    assert callable(mod.main)


# ---------------- 리뷰 회귀: 상관 재정규화 후 상한 불변식 ----------------


def test_propose_max_weight_invariant_survives_correlation_stage():
    """상관 페널티의 재정규화가 비페널티 종목을 상한 위로 못 밀어올림 (초과분→현금).

    회귀: held 와 고상관 후보가 감액→재정규화되며 나머지 종목이 max_weight 를
    넘겼음 (노트는 '상한 적용' 이라 쓰면서 실제 비중은 위반).
    """
    rng = np.random.default_rng(21)
    base = rng.normal(0.0, 0.01, 300)
    prices = {
        # 저변동 3종목 (상한에 걸릴 후보) + held 와 상관 1.0 인 2종목
        "A": _prices_from_returns(rng.normal(0.0, 0.004, 300)),
        "B": _prices_from_returns(rng.normal(0.0, 0.004, 300)),
        "C": _prices_from_returns(rng.normal(0.0, 0.004, 300)),
        "D": _prices_from_returns(base),
        "E": _prices_from_returns(base * 1.2),
    }
    held = {"H": _prices_from_returns(base * 1.5)}
    prop = propose(list(prices), prices, held=held,
                   max_weight=0.25, kelly_fraction=999.0)   # Kelly 상한 무력화(불변식만 검증)
    assert max(prop.weights.values()) <= 0.25 + 1e-9
    assert prop.cash_weight >= 0.0
    assert prop.cash_weight + sum(prop.weights.values()) == pytest.approx(1.0)


def test_fetch_close_map_skips_all_nan_close(monkeypatch):
    """전체 NaN Close frame(yfinance 부분응답) → 해당 종목 스킵, TypeError 로
    리포트 전체가 죽지 않음 (회귀: first_valid_index()=None → max() TypeError)."""
    import scripts.check_portfolio as mod

    idx = pd.bdate_range("2024-01-01", periods=50)
    good = pd.DataFrame({"Close": np.linspace(100, 110, 50)}, index=idx)
    bad = pd.DataFrame({"Close": [np.nan] * 50}, index=idx)
    monkeypatch.setattr("src.data_fetcher.fetch_prices",
                        lambda t, period="1y", **k: bad if t == "BAD" else good)
    out = mod._fetch_close_map(["GOOD", "BAD"], period="max")
    assert list(out) == ["GOOD"]
