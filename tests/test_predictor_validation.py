"""선행지표 무선견 검증 프로토콜 (WP-VALID) — 합성 데이터, 네트워크 없음.

대상: backtest.walk_forward_lead_lag / summarize_lead_lag / judge_lead_lag /
sign_test_paired / holm_adjust / block_bootstrap_ci + predictors 레지스트리·complete_only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import backtest as bt
from src.backtest import (
    LL_MIN_OOS,
    VERDICT_PASS,
    VERDICT_UNPROVEN,
    block_bootstrap_ci,
    excess_significance,
    holm_adjust,
    judge_lead_lag,
    sign_test_paired,
    summarize_lead_lag,
    walk_forward_lead_lag,
)
from src.exceptions import InsufficientDataError
from src.predictors import (
    PREDICTOR_SPECS,
    PREDICTORS,
    drop_incomplete_month,
    to_monthly,
)

N_MONTHS = 200


def _idx(n: int = N_MONTHS, start: str = "2005-01-31") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="ME")


def _noise_case(seed: int, n: int = N_MONTHS) -> tuple[pd.Series, pd.Series]:
    """선행지표와 무관한 잡음 타깃 (월 +1% 드리프트로 '항상 상승' 베이스라인이 강하게)."""
    rng = np.random.default_rng(seed)
    idx = _idx(n)
    lead = pd.Series(rng.normal(0, 1, n), index=idx)
    ret = rng.normal(0.01, 0.05, n)
    target = pd.Series(100 * np.cumprod(1 + ret), index=idx)
    return lead, target


def _planted_case(seed: int = 0, n: int = N_MONTHS, true_lag: int = 3) -> tuple[pd.Series, pd.Series]:
    """r_t = 0.03·lead_{t-true_lag} + 작은 잡음 → 부호가 거의 완전히 예측 가능."""
    rng = np.random.default_rng(seed)
    idx = _idx(n)
    lead = pd.Series(rng.normal(0, 1, n), index=idx)
    ret = 0.03 * lead.shift(true_lag).fillna(0.0).to_numpy() + rng.normal(0, 0.004, n)
    target = pd.Series(100 * np.cumprod(1 + ret), index=idx)
    return lead, target


# ---------------- 통계 도구: 손계산 값 ----------------


def test_sign_test_paired_hand_computed_16_of_20():
    """20번 다른 달 중 16번 예측이 맞음 → P(X>=16 | n=20, p=.5) = 6196/2^20 ≈ 0.0059."""
    base_up = np.ones(20, dtype=bool)
    pred_up = np.zeros(20, dtype=bool)                 # 20번 모두 베이스라인과 다름
    actual_up = np.array([False] * 16 + [True] * 4)    # 예측(하락) 16번 적중
    wins, n, p = sign_test_paired(pred_up, base_up, actual_up)
    assert (wins, n) == (16, 20)
    assert p == pytest.approx(6196 / 2 ** 20)
    assert p == pytest.approx(0.0059, abs=1e-4)


def test_sign_test_paired_ignores_months_where_prediction_equals_baseline():
    base_up = np.array([True, True, True, True])
    pred_up = np.array([True, True, False, True])      # 다른 달은 3번째 하나뿐
    actual_up = np.array([True, False, False, True])
    wins, n, p = sign_test_paired(pred_up, base_up, actual_up)
    assert (wins, n) == (1, 1)
    assert p == pytest.approx(0.5)


def test_sign_test_paired_no_disagreement_is_uninformative():
    same = np.array([True, False, True])
    assert sign_test_paired(same, same, same) == (0, 0, 1.0)


def test_holm_adjust_known_values():
    # 정렬: 0.005(×4)=0.02, 0.01(×3)=0.03, 0.03(×2)=0.06, 0.04(×1)=0.04→단조 보정 0.06
    adj = holm_adjust([0.01, 0.04, 0.03, 0.005])
    assert adj == pytest.approx([0.03, 0.06, 0.06, 0.02])


def test_holm_adjust_caps_at_one_and_respects_n_tests():
    assert holm_adjust([0.4, 0.9]) == pytest.approx([0.8, 0.9])
    # 7개 중 2개만 실행돼도 보정 강도는 m=7 기준
    assert holm_adjust([0.01, 0.02], n_tests=7) == pytest.approx([0.07, 0.12])
    assert holm_adjust([0.9], n_tests=7) == [1.0]


def test_block_bootstrap_ci_is_deterministic_and_seed_sensitive():
    x = np.random.default_rng(1).normal(0.001, 0.01, 400)
    a = block_bootstrap_ci(x, 21, n_boot=300, seed=7)
    b = block_bootstrap_ci(x, 21, n_boot=300, seed=7)
    c = block_bootstrap_ci(x, 21, n_boot=300, seed=8)
    assert a == b
    assert a != c
    assert a[1] < a[0] < a[2]


def test_block_bootstrap_ci_short_sample_gives_nan_ci():
    mean, lo, hi = block_bootstrap_ci([0.01] * 10, 21)
    assert mean == pytest.approx(0.01)
    assert np.isnan(lo) and np.isnan(hi)


def test_excess_significance_undecided_when_zero_variance():
    stats = excess_significance(pd.Series(np.zeros(300)))
    assert stats.verdict == "판정 불가"
    assert np.isnan(stats.t_stat)


def test_excess_significance_flags_clear_edge():
    rng = np.random.default_rng(3)
    x = pd.Series(rng.normal(0.002, 0.005, 1000))       # t ≈ 12
    stats = excess_significance(x, n_boot=500)
    assert stats.verdict == "유의한 초과"
    assert stats.ci_ann_pct[0] > 0


# ---------------- walk_forward_lead_lag: 무선견 성질 ----------------


@pytest.mark.parametrize("cut", [90, 120, 160])
def test_walk_forward_prefix_invariance(cut):
    """입력을 T 에서 잘라 돌린 결과 == 전체 결과의 T 이전 행 (미래 데이터가 과거 예측에 새지 않음)."""
    lead, target = _planted_case(seed=5)
    full = walk_forward_lead_lag(lead, target, pub_lag=1)
    T = lead.index[cut]
    part = walk_forward_lead_lag(lead[:T], target[:T], pub_lag=1)
    assert len(part) > 10
    pd.testing.assert_frame_equal(part, full.loc[:T])


def test_walk_forward_prefix_invariance_even_if_future_is_garbage():
    """T 이후 값을 쓰레기로 바꿔도 T 이전 예측은 그대로 — 선견 경로가 없다는 직접 증거."""
    lead, target = _noise_case(seed=11)
    T = lead.index[130]
    base = walk_forward_lead_lag(lead, target, pub_lag=0).loc[:T]
    lead2, target2 = lead.copy(), target.copy()
    lead2[lead2.index > T] = 1e6
    target2[target2.index > T] *= 1e3
    changed = walk_forward_lead_lag(lead2, target2, pub_lag=0).loc[:T]
    pd.testing.assert_frame_equal(base, changed)


@pytest.mark.parametrize("pub", [0, 1, 3])
def test_selected_lag_respects_publication_lag(pub):
    lead, target = _planted_case(seed=2, true_lag=1)    # 진짜 관계는 lag 1 이지만
    df = walk_forward_lead_lag(lead, target, pub_lag=pub)
    assert len(df) > 50
    assert df["k"].min() >= 1 + pub                     # 공표 전 값은 쓸 수 없다


def test_publication_lag_blocks_unpublished_signal():
    """진짜 선행 lag(1) 가 공표지연(3)에 가려지면 적중률이 무작위 수준으로 떨어져야 함."""
    lead, target = _planted_case(seed=4, true_lag=1)
    free = summarize_lead_lag(walk_forward_lead_lag(lead, target, pub_lag=0), "free", 0)
    blocked = summarize_lead_lag(walk_forward_lead_lag(lead, target, pub_lag=3), "blocked", 3)
    assert free.acc > 90.0
    assert blocked.acc < free.acc - 20.0


def test_walk_forward_strategy_cost_matches_turnover():
    lead, target = _planted_case(seed=6)
    df0 = walk_forward_lead_lag(lead, target, pub_lag=0, cost_bps=0.0)
    df1 = walk_forward_lead_lag(lead, target, pub_lag=0, cost_bps=100.0)
    turnover = df0["pos"].diff().abs().fillna(df0["pos"].abs())
    expected = df0["net"] - turnover * 0.01
    pd.testing.assert_series_equal(df1["net"], expected, check_names=False)


def test_walk_forward_invalid_args():
    lead, target = _noise_case(0)
    with pytest.raises(ValueError):
        walk_forward_lead_lag(lead, target, pub_lag=-1)
    with pytest.raises(ValueError):
        walk_forward_lead_lag(lead, target, pub_lag=12)         # 1+12 > max_lag(12)


def test_walk_forward_short_history_returns_empty_and_summary_raises():
    lead, target = _noise_case(0, n=30)                         # min_train(36) 미달
    df = walk_forward_lead_lag(lead, target, pub_lag=0)
    assert df.empty
    with pytest.raises(InsufficientDataError):
        summarize_lead_lag(df, "x", 0)


# ---------------- 판정: 심은 신호 통과 / 잡음 탈락 ----------------


def _judge_one(lead, target, pub=1, n_tests=7):
    df = walk_forward_lead_lag(lead, target, pub_lag=pub)
    r = summarize_lead_lag(df, "case", pub)
    return judge_lead_lag([r], n_tests=n_tests)[0]


def test_planted_signal_passes_even_with_holm_k7():
    r = _judge_one(*_planted_case(seed=0))
    assert r.n_oos >= LL_MIN_OOS
    assert r.verdict == VERDICT_PASS, r.reasons
    assert r.p_adj < 0.05
    assert min(r.half_edges) > 0
    assert r.lag_mode == 3


def test_pure_noise_rarely_passes():
    """무관한 잡음 20시드 → 통과는 최대 1개 (거짓 양성 통제). 다중검정 보정 없이(K=1)도."""
    passed = sum(_judge_one(*_noise_case(seed), n_tests=1).passed for seed in range(20))
    assert passed <= 1


def test_judge_marks_unproven_with_reasons_and_never_says_no_power():
    r = _judge_one(*_noise_case(seed=1))
    assert r.verdict == VERDICT_UNPROVEN
    assert r.reasons
    assert "예측력 없음" not in r.verdict


def test_judge_flags_small_sample_and_few_disagreements():
    lead, target = _planted_case(seed=1, n=80)                  # OOS 44개월 < 60
    r = _judge_one(lead, target, pub=1)
    assert r.verdict == VERDICT_UNPROVEN
    assert any(x.startswith("OOS") for x in r.reasons)


def test_to_dict_is_json_serialisable():
    import json

    r = _judge_one(*_planted_case(seed=0))
    json.dumps(r.to_dict())                                     # nan 이 섞여도 None 으로 직렬화
    assert r.to_dict()["verdict"] == VERDICT_PASS


# ---------------- 모듈 위생 ----------------


def test_backtest_does_not_import_signals_at_module_level():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(bt))
    for node in tree.body:                                      # 모듈 최상위 import 만 검사
        if isinstance(node, ast.ImportFrom):
            assert node.module != "src.signals"
        if isinstance(node, ast.Import):
            assert all(a.name != "src.signals" for a in node.names)


# ---------------- predictors 레지스트리 ----------------


def test_predictor_specs_cover_all_seven_with_expected_pub_lags():
    pubs = {s.name: s.pub_lag_months for s in PREDICTOR_SPECS}
    assert len(PREDICTOR_SPECS) == 7
    assert pubs["M2 → 비트코인"] == 1
    assert pubs["건축허가 → 주택건설(XHB)"] == 1
    assert pubs["소비자심리 → 소비재(XLY)"] == 1
    assert pubs["한국수출 → 반도체(SOXX)"] == 3
    assert pubs["달러 → 신흥국(EEM)"] == 0
    assert pubs["구리/금 → 경기(SPY)"] == 0
    assert pubs["위키관심 → 비트코인"] == 0
    assert len({s.name for s in PREDICTOR_SPECS}) == 7


def test_predictors_keys_equal_non_research_spec_keys():
    assert set(PREDICTORS) == {s.name for s in PREDICTOR_SPECS if not s.research_only}
    assert all(callable(fn) for fn in PREDICTORS.values())


def test_wikipedia_btc_is_research_only_and_excluded_from_operational():
    wiki = next(s for s in PREDICTOR_SPECS if "위키" in s.name)
    assert wiki.research_only is True
    assert wiki.name not in PREDICTORS
    assert len(PREDICTORS) == 6
    assert all(not s.research_only for s in PREDICTOR_SPECS if s.name in PREDICTORS)


def test_every_spec_has_callable_build():
    assert all(callable(s.build) for s in PREDICTOR_SPECS)


# ---------------- to_monthly(complete_only) ----------------


def test_to_monthly_complete_only_drops_in_progress_month():
    daily = pd.Series(
        np.arange(1.0, 101.0), index=pd.date_range("2026-07-01", periods=100, freq="D")
    )                                                           # 07-01 ~ 10-08
    asof = pd.Timestamp("2026-10-08")
    assert to_monthly(daily).index[-1] == pd.Timestamp("2026-10-31")        # 기본은 그대로 (하위호환)
    done = to_monthly(daily, complete_only=True, as_of=asof)
    assert list(done.index) == [pd.Timestamp("2026-07-31"), pd.Timestamp("2026-08-31"),
                                pd.Timestamp("2026-09-30")]
    assert done.iloc[-1] == daily.loc["2026-09-30"]


def test_drop_incomplete_month_boundary():
    m = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-08-31", "2026-09-30"]))
    assert len(drop_incomplete_month(m, pd.Timestamp("2026-09-30"))) == 1       # 월말 당일은 아직 미완
    assert len(drop_incomplete_month(m, pd.Timestamp("2026-10-01"))) == 2


# ---------------- 한국수출 빌더: 수출 컬럼을 이름으로 선택 ----------------


def test_korea_exports_builder_selects_exports_by_name_not_position(monkeypatch):
    """수출 컬럼이 빠지면 첫 컬럼(수입)을 쓰지 말고 InsufficientDataError (ALR-09 동일 패턴 회귀)."""
    from src import data_fetcher as dfm
    from src import predictors as pr

    idx = pd.date_range("2014-01-31", periods=40, freq="ME")
    imports_only = pd.DataFrame({"수입(금액, USD)": np.linspace(100, 140, 40)}, index=idx)
    monkeypatch.setattr(dfm, "fetch_korea_trade", lambda start=None: imports_only)
    with pytest.raises(InsufficientDataError):
        pr._build_korea_exports_soxx()

    monkeypatch.setattr(dfm, "fetch_korea_trade", lambda start=None: pd.DataFrame())
    with pytest.raises(InsufficientDataError):
        pr._build_korea_exports_soxx()


def test_korea_exports_builder_uses_exports_column_even_when_not_first(monkeypatch):
    from src import data_fetcher as dfm
    from src import predictors as pr

    idx = pd.date_range("2014-01-31", periods=40, freq="ME")
    exports = pd.Series(np.linspace(100, 200, 40), index=idx)
    trade = pd.DataFrame({"수입(금액, USD)": np.linspace(1, 2, 40), "수출(금액, USD)": exports}, index=idx)
    target = pd.Series(np.linspace(50, 80, 40), index=idx)
    monkeypatch.setattr(dfm, "fetch_korea_trade", lambda start=None: trade)
    monkeypatch.setattr(pr, "_prices_monthly", lambda ticker: target)
    lead_yoy, tgt = pr._build_korea_exports_soxx()
    expected = pr.yoy_growth(pr._complete_monthly(exports))
    pd.testing.assert_series_equal(lead_yoy, expected, check_names=False)
    assert tgt is target
