"""src/macro_analyzer.py — 통계 함수 + 국면 분류기의 실패 격리 (오프라인)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.macro_analyzer import (
    classify_regime,
    correlation_matrix,
    cumulative_returns,
    current_drawdown,
    daily_returns,
)


@pytest.fixture
def panel() -> pd.DataFrame:
    """2자산 합성 패널 — 하나는 상승, 하나는 하락."""
    dates = pd.date_range("2024-01-01", periods=100)
    up = pd.Series(np.linspace(100, 150, 100), index=dates)
    down = pd.Series(np.linspace(100, 80, 100), index=dates)
    return pd.DataFrame({"up": up, "down": down})


def test_daily_returns_shape_and_sign(panel):
    r = daily_returns(panel)
    assert len(r) == len(panel) - 1
    assert (r["up"] > 0).all()
    assert (r["down"] < 0).all()


def test_cumulative_returns(panel):
    c = cumulative_returns(panel)
    assert c["up"] == pytest.approx(50.0)
    assert c["down"] == pytest.approx(-20.0)


def test_correlation_matrix_diagonal_and_mirror():
    # 수익률이 정확히 거울상인 두 자산 → 상관계수 -1
    rng = np.random.default_rng(3)
    r = rng.normal(0, 0.01, 100)
    dates = pd.date_range("2024-01-01", periods=101)
    a = pd.Series(100 * np.exp(np.concatenate([[0], np.cumsum(r)])), index=dates)
    b = pd.Series(100 * np.exp(np.concatenate([[0], np.cumsum(-r)])), index=dates)
    m = correlation_matrix(pd.DataFrame({"a": a, "b": b}))
    assert m.loc["a", "a"] == pytest.approx(1.0)
    assert m.loc["a", "b"] == pytest.approx(-1.0, abs=0.01)


def test_current_drawdown_handles_trailing_nan(panel):
    # 회귀 테스트: BTC(24/7) vs 주식(평일) 캘린더 차이로 마지막 행에 NaN 이
    # 끼면 결과가 NaN 으로 떨어지던 버그 — ffill 보정 확인
    p = panel.copy()
    p.iloc[-1, p.columns.get_loc("up")] = np.nan
    dd = current_drawdown(p)
    assert not dd.isna().any()
    assert dd["up"] == pytest.approx(0.0, abs=1e-9)   # 단조 상승 → 낙폭 없음
    assert dd["down"] == pytest.approx(-20.0)


def test_classify_regime_isolates_failures_when_no_keys(no_api_keys):
    # 회귀 테스트: FRED 키 미설정 시 MissingApiKeyError(ConfigError) 가
    # 리포트 전체를 죽이지 않고 failures 로 격리되는지 (3단계에서 잡은 버그)
    report = classify_regime()
    assert len(report.failures) == 3
    assert report.signals == []
    assert report.score == 0
    # 에러 메시지가 사용자용 signals 에 섞이면 안 됨
    assert all("실패" not in s and "Error" not in s for s in report.signals)
