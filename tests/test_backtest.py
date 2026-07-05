"""src/backtest.py — 합성 데이터 기반 결정론적 검증 (네트워크 없음)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest import (
    buy_and_hold_positions,
    evaluate_lead_lag_oos,
    ma_crossover_positions,
    momentum_score_fn,
    run_backtest,
    walk_forward_topn,
)
from src.exceptions import InsufficientDataError


def _prices(values, start="2022-01-03") -> pd.Series:
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def _geometric(daily_return: float, n: int, start="2022-01-03") -> pd.Series:
    return _prices([100.0 * (1.0 + daily_return) ** i for i in range(n)], start=start)


# ---------------- run_backtest — 지표 산식 (손계산 검증) ----------------


def test_constant_gain_known_metrics():
    # 매일 정확히 +1% × 100일 (수익률 99개) — 모든 지표가 손으로 계산 가능
    p = _geometric(0.01, 100)
    res = run_backtest(p, buy_and_hold_positions(p), cost_bps=0.0)

    assert res.n_periods == 99
    assert res.total_return_pct == pytest.approx((1.01 ** 99 - 1) * 100)
    assert res.annualized_return_pct == pytest.approx((1.01 ** 252 - 1) * 100)
    assert res.annualized_vol_pct == pytest.approx(0.0)
    assert res.sharpe == 0.0                       # 변동성 0 → 가드 (0 나눗셈 방지)
    assert res.max_drawdown_pct == pytest.approx(0.0)
    assert res.hit_rate_pct == pytest.approx(100.0)
    assert res.n_trades == 1                       # 최초 진입 1회뿐


def test_transaction_cost_accounting():
    # 진입 비용(편도 100bp)이 첫 평가 기간에 차감되는지: (1.10-0.01)×1.10
    p = _prices([100.0, 110.0, 121.0])
    pos = pd.Series(1.0, index=p.index)
    res = run_backtest(p, pos, cost_bps=100.0, min_points=2)
    assert res.total_return_pct == pytest.approx((1.09 * 1.10 - 1) * 100)
    assert res.n_trades == 1

    free = run_backtest(p, pos, cost_bps=0.0, min_points=2)
    assert free.total_return_pct == pytest.approx(21.0)


def test_position_applied_next_period_no_lookahead():
    # t 종가에 잡은 포지션은 t→t+1 수익률부터 적용 — 10% 상승은 t1 에 잡은 포지션이 먹음
    p = _prices([100.0, 100.0, 110.0, 110.0])
    pos = pd.Series([0.0, 1.0, 0.0, 0.0], index=p.index)
    res = run_backtest(p, pos, cost_bps=0.0, min_points=3)
    assert res.total_return_pct == pytest.approx(10.0)
    assert res.n_trades == 2                       # 진입 + 청산
    assert res.hit_rate_pct == pytest.approx(100.0)   # 보유 기간(1개) 전부 수익


def test_short_position_profits_in_decline():
    p = _geometric(-0.01, 50)
    pos = pd.Series(-1.0, index=p.index)
    res = run_backtest(p, pos, cost_bps=0.0)
    assert res.total_return_pct == pytest.approx((1.01 ** 49 - 1) * 100)
    assert res.hit_rate_pct == pytest.approx(100.0)


def test_flat_prices_zero_vol_sharpe_guard():
    p = _prices([100.0] * 60)
    res = run_backtest(p, buy_and_hold_positions(p), cost_bps=0.0)
    assert res.sharpe == 0.0
    assert res.annualized_vol_pct == pytest.approx(0.0)
    assert res.total_return_pct == pytest.approx(0.0)


def test_positions_reindexed_and_ffilled():
    # 첫날에만 1.0 을 준 포지션은 ffill 되어 매수후보유와 동일해야 함
    p = _geometric(0.005, 40)
    sparse = pd.Series([1.0], index=[p.index[0]])
    res = run_backtest(p, sparse, cost_bps=0.0)
    full = run_backtest(p, buy_and_hold_positions(p), cost_bps=0.0)
    assert res.total_return_pct == pytest.approx(full.total_return_pct)


def test_run_backtest_insufficient_data_raises():
    p = _geometric(0.01, 10)
    with pytest.raises(InsufficientDataError):
        run_backtest(p, buy_and_hold_positions(p))   # 기본 min_points=20 미달


def test_run_backtest_empty_positions_raises():
    p = _geometric(0.01, 60)
    with pytest.raises(InsufficientDataError):
        run_backtest(p, pd.Series(dtype=float))


def test_run_backtest_negative_cost_rejected():
    p = _geometric(0.01, 60)
    with pytest.raises(ValueError):
        run_backtest(p, buy_and_hold_positions(p), cost_bps=-1.0)


def test_result_to_dict_and_str():
    p = _geometric(0.01, 60)
    res = run_backtest(p, buy_and_hold_positions(p), cost_bps=0.0, name="테스트전략")
    d = res.to_dict()
    assert d["name"] == "테스트전략"
    assert "equity_curve" not in d
    assert set(d) >= {"total_return_pct", "sharpe", "max_drawdown_pct", "n_trades"}
    assert "테스트전략" in str(res)
    assert res.equity_curve.iloc[-1] == pytest.approx(1 + res.total_return_pct / 100)


# ---------------- 신호 헬퍼 ----------------


def test_ma_crossover_positions_values():
    vals = [1.0, 1.0, 1.0, 10.0, 10.0, 10.0, 1.0, 1.0, 1.0]
    pos = ma_crossover_positions(_prices(vals), window=3)
    assert set(pos.unique()) <= {0.0, 1.0}
    assert (pos.iloc[:2] == 0.0).all()             # MA 미정의 초반부는 무포지션
    assert pos.iloc[3] == 1.0                      # 10 > mean(1,1,10)=4 → 매수
    assert pos.iloc[6] == 0.0                      # 1 < mean(10,10,1)=7 → 현금


def test_ma_crossover_insufficient_raises():
    with pytest.raises(InsufficientDataError):
        ma_crossover_positions(_geometric(0.01, 50), window=200)


def test_momentum_score_fn_known_value():
    # p[i] = 1.01^i → p[-21]/p[-147] = 1.01^126
    p = _geometric(0.01, 200)
    score = momentum_score_fn(p, lookback=126, skip=21)
    assert score == pytest.approx(1.01 ** 126 - 1)


def test_momentum_score_fn_insufficient_raises():
    with pytest.raises(InsufficientDataError):
        momentum_score_fn(_geometric(0.01, 100))   # 기본 요구치 147 미달


# ---------------- walk_forward_topn ----------------


def _wf_universe(n: int = 300) -> dict[str, pd.Series]:
    return {
        "A": _geometric(0.005, n),     # 강한 상승 — 항상 최고 모멘텀
        "B": _prices([100.0] * n),     # 횡보
        "C": _geometric(-0.005, n),    # 하락
    }


def _short_momentum(p: pd.Series) -> float:
    return momentum_score_fn(p, lookback=40, skip=5)


def test_walk_forward_picks_winner_and_beats_benchmark():
    result = walk_forward_topn(
        _wf_universe(), score_fn=_short_momentum, top_n=1, cost_bps=0.0,
    )
    assert result.picks                                        # 리밸런스 발생
    assert all(chosen == ("A",) for _, chosen in result.picks)  # 항상 상승 종목 선택
    assert result.strategy.total_return_pct > result.benchmark.total_return_pct
    assert result.strategy.n_trades >= 1
    assert result.benchmark.cost_bps == 0.0

    d = result.to_dict()
    assert d["top_n"] == 1
    assert d["picks"][0][1] == ["A"]
    assert "초과수익" in str(result)


def test_walk_forward_costs_reduce_return():
    kwargs = {"score_fn": _short_momentum, "top_n": 1}
    free = walk_forward_topn(_wf_universe(), cost_bps=0.0, **kwargs)
    costly = walk_forward_topn(_wf_universe(), cost_bps=100.0, **kwargs)
    assert costly.strategy.total_return_pct < free.strategy.total_return_pct
    assert costly.picks == free.picks              # 비용은 선택에 영향 없음


def test_walk_forward_invalid_args():
    universe = _wf_universe(60)
    with pytest.raises(ValueError):
        walk_forward_topn(universe, top_n=0)
    with pytest.raises(ValueError):
        walk_forward_topn(universe, top_n=5)       # 종목 3개 < top_n


def test_walk_forward_insufficient_history_raises():
    # 30일 데이터로 기본 스코어(147일 룩백) → 유효 리밸런스 0회
    with pytest.raises(InsufficientDataError):
        walk_forward_topn(_wf_universe(30), top_n=1)


# ---------------- evaluate_lead_lag_oos ----------------


def _monthly_noise(n: int = 80, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2016-01-31", periods=n, freq="ME")
    return pd.Series(rng.normal(0.0, 3.0, n), index=idx)


def test_lead_lag_perfect_predictor_accuracy_near_one():
    leading = _monthly_noise()
    target = leading.shift(3).dropna()             # 선행지표가 정확히 3개월 선행
    res = evaluate_lead_lag_oos(leading, target, lag=3, min_train=24)
    assert res.accuracy_pct == pytest.approx(100.0)
    assert res.accuracy_pct > res.baseline_accuracy_pct
    assert res.beats_baseline is True
    assert res.n_oos == len(target) - 24
    assert res.strategy_return_pct is None         # target_returns 미제공 → 생략


def test_lead_lag_strategy_equity_beats_buyhold_when_predictions_perfect():
    leading = _monthly_noise(seed=1)
    target = leading.shift(3).dropna()
    # 목표 자산 월수익률: 방향은 target 부호와 일치 (상승월 +2%, 하락월 -2%)
    rets = pd.Series(np.where(target > 0, 0.02, -0.02), index=target.index)
    res = evaluate_lead_lag_oos(
        leading, target, lag=3, target_returns=rets, min_train=24, cost_bps=0.0,
    )
    # 완벽 예측 long/flat 은 하락월을 전부 피함 → 매수후보유보다 우월
    assert res.strategy_return_pct > res.buyhold_return_pct
    assert res.equity_curve is not None
    assert res.equity_curve.iloc[-1] == pytest.approx(1 + res.strategy_return_pct / 100)


def test_lead_lag_random_predictor_near_baseline():
    # 서로 무관한 노이즈 → 적중률이 100% 일 수 없음 (과최적화 감지 목적의 회귀 방지선)
    leading = _monthly_noise(n=120, seed=2)
    target = _monthly_noise(n=120, seed=3)
    res = evaluate_lead_lag_oos(leading, target, lag=2, min_train=24)
    assert res.accuracy_pct < 75.0


def test_lead_lag_insufficient_data_raises():
    leading = _monthly_noise(n=20)
    target = leading.shift(2).dropna()
    with pytest.raises(InsufficientDataError):
        evaluate_lead_lag_oos(leading, target, lag=2, min_train=24)


def test_lead_lag_rejects_nonpositive_lag():
    leading = _monthly_noise()
    with pytest.raises(ValueError):
        evaluate_lead_lag_oos(leading, leading, lag=0)


def test_lead_lag_to_dict_and_str():
    leading = _monthly_noise(seed=4)
    target = leading.shift(2).dropna()
    res = evaluate_lead_lag_oos(
        leading, target, lag=2, leading_name="X", target_name="Y", min_train=24,
    )
    d = res.to_dict()
    assert d["leading_name"] == "X"
    assert d["lag"] == 2
    assert "equity_curve" not in d
    assert "X → Y" in str(res)


# ---------------- 오케스트레이터 스크립트 — 오프라인 import 보장 ----------------


def test_check_backtest_script_imports_offline():
    import scripts.check_backtest as mod

    assert callable(mod.main)


# ---------------- 리뷰 회귀: MDD 초기자본 앵커 / 발표지연 보정 ----------------


def test_max_drawdown_anchored_at_initial_capital():
    """표본 첫 기간부터 시작되는 하락도 MDD 에 잡혀야 함 (초기자본 1.0 앵커).

    회귀: equity 가 (1+r0) 부터 시작하면 100→90 후 횡보가 MDD 0% 로 나왔음.
    """
    vals = [100.0, 90.0] + [90.0] * 28          # 첫날 -10% 후 횡보
    prices = _prices(vals)
    res = run_backtest(prices, buy_and_hold_positions(prices), cost_bps=0.0)
    assert res.max_drawdown_pct == pytest.approx(-10.0)
    assert res.total_return_pct == pytest.approx(-10.0)


def test_max_drawdown_monotone_decline_full_depth():
    """단조 하락 100→70 은 MDD -30% (앵커 없으면 -30/(1+r0) 로 과소보고)."""
    vals = list(np.linspace(100.0, 70.0, 30))
    prices = _prices(vals)
    res = run_backtest(prices, buy_and_hold_positions(prices), cost_bps=0.0)
    assert res.max_drawdown_pct == pytest.approx(-30.0)


def test_lead_lag_publication_lag_delays_tradable_signal():
    """lag=1 신호는 발표지연(기본 1개월) 때문에 한 달 늦게 체결되어야 함.

    회귀: 미발표 데이터로 매매하는 선견편향 — lag ≤ publication_lag 이면
    전략 수익이 비현실적으로 완벽하게 나왔음. 적중률(통계 측정)은 보정 무관.
    """
    leading = _monthly_noise(seed=2)
    target = leading.shift(1).dropna()             # 정확히 1개월 선행
    rets = pd.Series(np.where(target > 0, 0.02, -0.02), index=target.index)

    biased = evaluate_lead_lag_oos(
        leading, target, lag=1, target_returns=rets, min_train=24,
        cost_bps=0.0, publication_lag_months=0,    # 보정 끔 = 종전 동작
    )
    realistic = evaluate_lead_lag_oos(
        leading, target, lag=1, target_returns=rets, min_train=24, cost_bps=0.0,
    )                                              # 기본값 publication_lag_months=1
    # 보정 전: 완벽 예측이 그대로 체결 → 하락월 전부 회피
    assert biased.strategy_return_pct > biased.buyhold_return_pct
    # 보정 후: 신호가 한 달 밀려 더 이상 '완벽' 하지 않음 — 수익이 달라져야 함
    assert realistic.strategy_return_pct != pytest.approx(biased.strategy_return_pct)
    assert realistic.strategy_return_pct < biased.strategy_return_pct
    # 적중률은 통계 측정이라 보정과 무관하게 동일
    assert realistic.accuracy_pct == pytest.approx(biased.accuracy_pct)


def test_lead_lag_rejects_negative_publication_lag():
    leading = _monthly_noise()
    target = leading.shift(3).dropna()
    with pytest.raises(ValueError):
        evaluate_lead_lag_oos(leading, target, lag=3, publication_lag_months=-1)
