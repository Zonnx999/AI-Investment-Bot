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



# ---------------- WP-VALID: 운영 모멘텀 어댑터 · 공통 시뮬레이터 · 유의성 ----------------


def _random_walk(n: int = 400, seed: int = 0, drift: float = 0.0005, vol: float = 0.01) -> pd.Series:
    rng = np.random.default_rng(seed)
    return _prices(100.0 * np.cumprod(1.0 + rng.normal(drift, vol, n)))


def test_operational_momentum_equals_signals_momentum_plus_tiebreak():
    """동등성: 운영 signals.momentum_score 정수 점수 + 1e-4 × 12-1개월 원수익률."""
    from src.backtest import operational_momentum_fn
    from src.signals import MOMENTUM_12M_D, MOMENTUM_SKIP_D, momentum_score

    for seed in range(5):
        p = _random_walk(seed=seed)
        score, _ = momentum_score(p)
        raw = float(p.iloc[-MOMENTUM_SKIP_D] / p.iloc[-(MOMENTUM_12M_D + MOMENTUM_SKIP_D)] - 1.0)
        got = operational_momentum_fn(p)
        assert got == pytest.approx(score + 1e-4 * raw, abs=1e-12)
        assert round(got - 1e-4 * raw) == score              # 타이브레이크는 정수 점수를 못 바꿈


def test_operational_momentum_breaks_saturation_ties_by_raw_return():
    from src.backtest import operational_momentum_fn
    from src.signals import momentum_score

    slow, fast = _geometric(0.004, 300), _geometric(0.005, 300)
    assert momentum_score(slow)[0] == momentum_score(fast)[0] == 100     # 운영 점수는 포화
    assert operational_momentum_fn(fast) > operational_momentum_fn(slow)


def test_operational_momentum_min_points_and_constant():
    from src.backtest import MOM_MIN_POINTS, operational_momentum_fn
    from src.signals import MOMENTUM_12M_D, MOMENTUM_SKIP_D

    assert MOM_MIN_POINTS == 274 == MOMENTUM_12M_D + MOMENTUM_SKIP_D + 1
    with pytest.raises(InsufficientDataError):
        operational_momentum_fn(_geometric(0.001, MOM_MIN_POINTS - 1))
    operational_momentum_fn(_geometric(0.001, MOM_MIN_POINTS))           # 경계에서는 계산됨


def test_walk_forward_default_score_is_operational_and_picks_winner():
    result = walk_forward_topn(_wf_universe(420), top_n=1, cost_bps=0.0)
    assert result.picks and all(chosen == ("A",) for _, chosen in result.picks)
    assert "운영 함수" in result.strategy.name
    assert result.stats is not None
    assert result.to_dict()["stats"]["n"] == result.stats.n


def test_walk_forward_top_n_all_means_zero_excess():
    """전 종목 보유 == 같은 날 동일가중 벤치 → 초과수익이 정확히 0, 판정 불가."""
    result = walk_forward_topn(_wf_universe(), score_fn=_short_momentum, top_n=3, cost_bps=10.0)
    assert result.strategy.total_return_pct == pytest.approx(result.benchmark.total_return_pct, abs=1e-9)
    assert result.stats.mean_ann_pct == pytest.approx(0.0, abs=1e-9)
    assert result.stats.verdict == "판정 불가"


def test_walk_forward_benchmark_uses_same_cost_and_rebalance():
    kwargs = {"score_fn": _short_momentum, "top_n": 1}
    free = walk_forward_topn(_wf_universe(), cost_bps=0.0, **kwargs)
    costly = walk_forward_topn(_wf_universe(), cost_bps=100.0, **kwargs)
    assert costly.benchmark.cost_bps == costly.strategy.cost_bps == 100.0
    assert costly.benchmark.total_return_pct < free.benchmark.total_return_pct     # 벤치도 비용을 낸다
    assert costly.benchmark.n_trades >= 1                                          # 같은 진입 거래


def test_walk_forward_rank_ic_positive_when_momentum_persists():
    rng = np.random.default_rng(7)
    n = 700
    universe = {
        f"S{i}": _prices(100.0 * np.cumprod(1.0 + rng.normal((i - 3.5) * 0.0006, 0.002, n)))
        for i in range(8)
    }
    result = walk_forward_topn(universe, top_n=2)
    assert result.stats.n_ic > 10
    assert result.stats.ic_mean > 0.3
    assert result.picks[-1][1][0] == "S7"                                   # 드리프트 최고 종목이 상위


def test_excess_stats_summary_line_handles_nan():
    from src.backtest import ExcessStats

    line = ExcessStats(10, float("nan"), (float("nan"), float("nan")), float("nan"), "판정 불가").summary_line()
    assert "계산 불가" in line and "nan" not in line


def test_deprecated_apis_are_marked_but_still_importable():
    import src.backtest as bt

    for fn in (bt.evaluate_lead_lag_oos, bt.momentum_score_fn, bt.LeadLagBacktest):
        assert "deprecated" in (fn.__doc__ or "").lower()


def test_check_backtest_table_is_wide_char_aligned():
    import scripts.check_backtest as cb

    assert cb._width("M2 -> BTC") == 9
    assert cb._width("한국수출") == 8
    assert cb._width(cb._pad("한국수출", 12)) == 12


# ---------------- check_backtest.main() 계약: 종료코드 / --save / 원장 폴백 ----------------


def _ll_case(seed: int = 0, n: int = 200) -> tuple[pd.Series, pd.Series]:
    """합성 선행지표/목표 쌍 (잡음 — 통과 여부는 무관, 파이프라인만 돌린다)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2005-01-31", periods=n, freq="ME")
    lead = pd.Series(rng.normal(0, 1, n), index=idx)
    target = pd.Series(100 * np.cumprod(1 + rng.normal(0.01, 0.05, n)), index=idx)
    return lead, target


class _FakeStore:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def put_state(self, ns, key, value):
        self.calls.append((ns, key))


class _FakeWF:
    def to_dict(self):
        return {"ok": True}


@pytest.fixture
def cb_env(monkeypatch):
    """check_backtest.main() 을 네트워크·DB 없이 돌리는 환경. (모듈, 가짜 저장소) 반환."""
    import sys

    import scripts.check_backtest as cb
    from src import storage
    from src.predictors import PredictorSpec

    specs = tuple(
        PredictorSpec(f"S{i}", f"선행{i} 증가율", f"T{i} 수익률", 1, lambda i=i: _ll_case(seed=i))
        for i in range(2)
    )
    store = _FakeStore()
    monkeypatch.setattr(cb, "PREDICTOR_SPECS", specs)
    monkeypatch.setattr(cb, "_section_walk_forward", lambda *a, **k: _FakeWF())
    monkeypatch.setattr(cb, "_section_trend", lambda *a, **k: None)
    monkeypatch.setattr(storage, "get_storage", lambda: store)
    monkeypatch.setitem(sys.modules, "src.ledger", None)   # 원장 모듈 없음
    cb._LOGGED_ONCE.clear()
    return cb, store


def test_check_backtest_main_exit_zero_and_no_save_by_default(cb_env, capsys):
    cb, store = cb_env
    assert cb.main([]) == 0                     # '통과 0/N' 은 실패가 아님
    assert store.calls == []                    # --save 없으면 state 쓰기 0회
    out = capsys.readouterr().out
    assert "원장 없음" in out and "관측 대기" in out   # 원장 모듈이 없을 때 폴백


def test_check_backtest_main_save_writes_validation_state(cb_env):
    cb, store = cb_env
    assert cb.main(["--save"]) == 0
    assert store.calls == [("validation", "lead_lag"), ("validation", "walk_forward")]


def test_check_backtest_main_infra_failure_returns_one(cb_env, capsys):
    from src.exceptions import DataFetchError
    from src.predictors import PredictorSpec

    cb, _ = cb_env

    def boom():
        raise DataFetchError("FRED 장애", source="FRED")

    cb.PREDICTOR_SPECS = cb.PREDICTOR_SPECS + (PredictorSpec("B", "고장 증가율", "X 수익률", 0, boom),)
    assert cb.main([]) == 1
    assert "DataFetchError" in capsys.readouterr().out


def test_check_backtest_ledger_wiring(cb_env, monkeypatch, capsys):
    """원장 모듈이 있으면 report_text 출력, 함수 이름 불일치는 '연동 오류' (원장 없음과 구분)."""
    import sys
    import types

    cb, _ = cb_env
    good = types.ModuleType("src.ledger")
    good.report_text = lambda market: f"LEDGER-{market}"
    monkeypatch.setitem(sys.modules, "src.ledger", good)
    cb._section_ledger()
    out = capsys.readouterr().out
    assert "LEDGER-us" in out and "LEDGER-kr" in out and "원장 없음" not in out

    bad = types.ModuleType("src.ledger")           # 모듈은 있으나 기대한 함수가 없음
    monkeypatch.setitem(sys.modules, "src.ledger", bad)
    cb._section_ledger()
    cb._section_universe_ic()
    out = capsys.readouterr().out
    assert out.count("원장 연동 오류") == 2 and "원장 없음" not in out


# ----------------------------------------------------------------------
# WP-POLISH: 월별 IC 에서 미완결 마지막 달 제외 · _simulate 한계 docstring
# ----------------------------------------------------------------------


def _ic_fixture(end: str):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2025-01-01", end)
    panel = pd.DataFrame(
        {f"T{i}": 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx))) for i in range(8)}, index=idx)
    reb = panel.index.to_series().resample("ME").last().dropna().tolist()
    scores = {d: {c: float(rng.normal()) for c in panel.columns} for d in reb}
    return panel, reb, scores


def test_monthly_rank_ic_excludes_incomplete_last_month():
    from src.backtest import _monthly_rank_ic

    panel, reb, scores = _ic_fixture("2025-10-08")          # 10월은 진행 중 (6거래일)
    assert reb[-1] == panel.index[-1] and reb[-1].month == 10
    got = _monthly_rank_ic(panel, scores)
    done = {d: v for d, v in scores.items() if d != reb[-1]}
    ref = _monthly_rank_ic(panel, done)
    assert got[2] == len(done) - 1                           # 09-30→10-08 부분 구간이 빠짐
    assert got == ref                                        # 값도 동일


def test_monthly_rank_ic_keeps_complete_last_month():
    from src.backtest import _monthly_rank_ic

    panel, reb, scores = _ic_fixture("2025-09-30")          # 9월 마지막 거래일 — 완결
    assert reb[-1] == panel.index[-1]
    assert _monthly_rank_ic(panel, scores)[2] == len(scores) - 1   # 마지막 쌍 포함


def test_month_is_complete_boundaries():
    import pandas as pd

    from src.backtest import _month_is_complete

    assert _month_is_complete(pd.Timestamp("2025-09-30")) is True
    assert _month_is_complete(pd.Timestamp("2025-10-31")) is True     # 금요일 월말
    assert _month_is_complete(pd.Timestamp("2025-10-08")) is False
    assert _month_is_complete(pd.Timestamp("2025-05-30")) is True     # 토요일 31일 — 다음 영업일은 6월


def test_simulate_docstring_states_drift_limitation():
    from src.backtest import _simulate

    assert "드리프트" in (_simulate.__doc__ or "")


# ---------------- WP-POLISH: check_backtest 저장 실패 집계 · [5] 연환산 헤드라인 ----------------


def test_check_backtest_save_failure_when_put_state_returns_false(cb_env, caplog):
    cb, store = cb_env
    store.put_state = lambda ns, key, value: False          # best-effort 쓰기 실패
    with caplog.at_level("INFO"):
        assert cb.main(["--save"]) == 1
    msgs = [r.message for r in caplog.records]
    assert any("검증 결과 저장 실패" in m for m in msgs)
    assert not any("검증 결과 저장 완료" in m for m in msgs)


def test_check_backtest_save_partial_failure_counts(cb_env):
    cb, store = cb_env
    store.put_state = lambda ns, key, value: key != "walk_forward"
    assert cb.main(["--save"]) == 1


def test_check_backtest_save_success_logs_done(cb_env, caplog):
    cb, store = cb_env
    store.put_state = lambda ns, key, value: True
    with caplog.at_level("INFO"):
        assert cb.main(["--save"]) == 0
    assert any("검증 결과 저장 완료" in r.message for r in caplog.records)


def test_check_backtest_trend_headline_is_annualized():
    import types

    import scripts.check_backtest as cb

    strat = types.SimpleNamespace(annualized_return_pct=7.8, max_drawdown_pct=-18.0,
                                  total_return_pct=1100.0)
    bench = types.SimpleNamespace(annualized_return_pct=10.9, max_drawdown_pct=-55.0,
                                  total_return_pct=3071.0)
    line = cb._trend_headline(strat, bench)
    assert "연환산 +7.8% vs +10.9%" in line and "연 -3.1%p" in line
    assert "-1971" not in line                               # 누적 %p 차이는 더 이상 헤드라인이 아님
    assert "MDD -18.0% vs -55.0%" in line
