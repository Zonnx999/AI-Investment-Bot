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
    # 전부 실패 = '중립/혼조' 가 아니라 판정 보류 (T3a §4 완전성 규칙)
    assert report.regime == "⚪ 판정 보류 (지표 0/3)"
    assert report.available == 0 and not report.complete
    # 에러 메시지가 사용자용 signals 에 섞이면 안 됨
    assert all("실패" not in s and "Error" not in s for s in report.signals)


# ---------------- 전수 리뷰 회귀 (2026-07-06) ----------------


def test_daily_returns_mixed_calendar_keeps_monday_returns():
    """혼합 캘린더 패널(BTC 주말 거래 + ETF 평일)에서 ETF 의 금→월 수익률이
    NaN 으로 유실되지 않아야 함 (변동성·상관 편향 회귀)."""
    all_days = pd.date_range("2024-01-01", periods=28, freq="D")
    weekdays = all_days[all_days.weekday < 5]
    rng = np.random.default_rng(3)
    btc = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(all_days))), index=all_days)
    spy = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(weekdays))), index=weekdays)
    panel = pd.DataFrame({"BTC": btc, "SPY": spy})

    rets = daily_returns(panel)
    mondays = rets.index[rets.index.weekday == 0]
    assert len(mondays) > 0
    assert rets.loc[mondays, "SPY"].notna().all()       # 금→월 수익률 보존
    # 자산별 표본 수: SPY 는 자기 캘린더 기준 (첫 관측 제외 전부)
    assert rets["SPY"].notna().sum() == len(weekdays) - 1


def test_cumulative_and_drawdown_empty_panel_domain_error():
    """빈 패널 → raw IndexError 가 아니라 InsufficientDataError (호출부 강등 가능)."""
    from src.exceptions import InsufficientDataError

    empty = pd.DataFrame()
    with pytest.raises(InsufficientDataError):
        cumulative_returns(empty)
    with pytest.raises(InsufficientDataError):
        current_drawdown(empty)


# ---------------- 국면 완전성 · 근거 줄 · stale-if-error (T3a §4, §8) ----------------


def _daily(values, end="2026-10-07") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(end=end, periods=len(values)))


def _weekly(values, end="2026-10-03") -> pd.Series:
    return pd.Series(values, index=pd.date_range(end=end, periods=len(values), freq="W-SAT"))


_GOOD_SERIES = {
    "T10Y2Y": lambda: _daily(np.full(80, 0.36)),
    "BAMLH0A0HYM2": lambda: _daily(np.full(260, 3.24)),
    "ICSA": lambda: _weekly(np.linspace(220_000, 230_000, 60)),
}


def _fake_resilient(fail=(), stale=()):
    from src.exceptions import ApiHttpError

    def fake(series_id):
        if series_id in fail:
            raise ApiHttpError(f"FRED {series_id}: HTTP 502", status_code=502, source="FRED")
        return _GOOD_SERIES[series_id](), ("2026-10-06" if series_id in stale else None)

    return fake


def test_classify_regime_one_failure_is_hold_with_partial_score(monkeypatch):
    import src.macro_analyzer as ma

    monkeypatch.setattr(ma, "_fetch_resilient", _fake_resilient(fail=("T10Y2Y",)))
    report = ma.classify_regime()
    assert report.regime == "⚪ 판정 보류 (지표 2/3)"
    assert report.available == 2 and report.total == 3 and not report.complete
    assert report.failures == ["yield_curve"]               # 대시보드 호환 이름 유지
    assert ma.failed_indicator_names(report) == ["장단기 금리차"]
    # score 는 부분합 그대로 (HY +1, 실업수당 0 근처)
    assert report.score == sum(o.score_delta for o in report.outcomes)


def test_classify_regime_complete_uses_label_bands(monkeypatch):
    import src.macro_analyzer as ma

    monkeypatch.setattr(ma, "_fetch_resilient", _fake_resilient())
    report = ma.classify_regime()
    assert report.complete and report.available == 3
    assert report.regime == ma.regime_label_for_score(report.score)
    assert report.asof == {"T10Y2Y": "2026-10-07", "BAMLH0A0HYM2": "2026-10-07",
                           "ICSA": "2026-10-03"}


def test_reason_line_markdown_safe_with_stale_and_count(monkeypatch):
    import src.macro_analyzer as ma

    monkeypatch.setattr(ma, "_fetch_resilient", _fake_resilient(stale=("BAMLH0A0HYM2",)))
    report = ma.classify_regime()
    line = report.reason_line()
    assert line.startswith("근거: 금리차 +0.36%p(+1) · HY 3.24%(")
    assert "10-06 캐시" in line and "3/3 지표" in line and "10-07 기준" in line
    assert not any(c in line for c in "_*`[]")
    assert report.stale == ["BAMLH0A0HYM2"]

    monkeypatch.setattr(ma, "_fetch_resilient", _fake_resilient(fail=("ICSA",)))
    partial = ma.classify_regime().reason_line()
    assert "실업수당 실패" in partial and "2/3 지표" in partial
    assert not any(c in partial for c in "_*`[]")           # jobless_claims 의 _ 가 새지 않음


def test_observation_age_issues_flags_old_series():
    from datetime import date

    import src.macro_analyzer as ma

    rep = ma.RegimeReport(regime="x", score=0,
                          asof={"T10Y2Y": "2026-09-20", "ICSA": "2026-09-26"})
    issues = ma.observation_age_issues(rep, date(2026, 10, 8))
    assert issues == ["T10Y2Y 최신 관측 18일 전"]           # ICSA 12일 ≤ 21일 → 정상


@pytest.fixture
def macro_cache_on(tmp_path, monkeypatch):
    """fetch_macro 의 실제 @cached 경로를 tmp DB 로 격리 (네트워크는 _fred_get 스텁)."""
    import src.storage as storage_mod

    monkeypatch.setenv("QUANT_BOT_CACHE", "on")
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "macro.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield storage_mod
    if storage_mod._storage is not None:
        storage_mod._storage.close()
    monkeypatch.setattr(storage_mod, "_storage", None)


_OBS = [{"date": "2026-10-06", "value": "0.40"}, {"date": "2026-10-07", "value": "."},
        {"date": "2026-10-08", "value": "0.36"}]


def _age_cache(monkeypatch, storage_mod, days):
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(days=days)
    monkeypatch.setattr(storage_mod, "_utcnow", lambda: future)


def test_fetch_resilient_uses_stale_cache_on_502(macro_cache_on, monkeypatch):
    import src.data_fetcher as df
    import src.macro_analyzer as ma
    from src.exceptions import ApiHttpError

    monkeypatch.setattr(df, "_fred_get", lambda sid, start: list(_OBS))
    fresh, since = ma._fetch_resilient("T10Y2Y")               # 캐시 채움 (정규 키)
    assert since is None and list(fresh) == [0.40, 0.36]

    _age_cache(monkeypatch, macro_cache_on, days=2)            # TTL(12h) 지남, 7일 이내

    def down(sid, start):
        raise ApiHttpError("FRED T10Y2Y: HTTP 502", status_code=502, source="FRED")

    monkeypatch.setattr(df, "_fred_get", down)
    stale, since = ma._fetch_resilient("T10Y2Y")
    assert list(stale) == [0.40, 0.36]
    assert since is not None and len(since) == 10              # 캐시 받은 KST 날짜


@pytest.mark.parametrize("exc_name", ["DataValidationError", "ApiAuthError"])
def test_fetch_resilient_does_not_mask_config_or_id_errors(macro_cache_on, monkeypatch, exc_name):
    import src.data_fetcher as df
    import src.exceptions as ex
    import src.macro_analyzer as ma

    monkeypatch.setattr(df, "_fred_get", lambda sid, start: list(_OBS))
    ma._fetch_resilient("T10Y2Y")
    _age_cache(monkeypatch, macro_cache_on, days=2)

    def bad(sid, start):
        raise getattr(ex, exc_name)("FRED T10Y2Y: bad", source="FRED")

    monkeypatch.setattr(df, "_fred_get", bad)
    with pytest.raises(getattr(ex, exc_name)):
        ma._fetch_resilient("T10Y2Y")


def test_fetch_resilient_reraises_when_stale_older_than_7_days(macro_cache_on, monkeypatch):
    import src.data_fetcher as df
    import src.macro_analyzer as ma
    from src.exceptions import ApiTimeoutError

    monkeypatch.setattr(df, "_fred_get", lambda sid, start: list(_OBS))
    ma._fetch_resilient("T10Y2Y")
    _age_cache(monkeypatch, macro_cache_on, days=8)

    def slow(sid, start):
        raise ApiTimeoutError("FRED T10Y2Y 타임아웃", source="FRED")

    monkeypatch.setattr(df, "_fred_get", slow)
    with pytest.raises(ApiTimeoutError):
        ma._fetch_resilient("T10Y2Y")


def test_fetch_panel_with_failures_reports_failed_labels(monkeypatch):
    import src.macro_analyzer as ma
    from src.exceptions import DataValidationError

    idx = pd.bdate_range("2026-01-01", periods=5)

    def fake_prices(ticker, period="1y", **k):
        if ticker == "BTC-USD":
            raise DataValidationError("empty", source="yfinance")
        return pd.DataFrame({"Close": np.arange(5.0) + 1}, index=idx)

    monkeypatch.setattr(ma, "fetch_prices", fake_prices)
    panel, failed = ma.fetch_panel_with_failures({"S&P 500": "SPY", "비트코인": "BTC-USD"})
    assert failed == ["비트코인"] and list(panel.columns) == ["S&P 500"]
    assert list(ma.fetch_cross_asset_panel({"S&P 500": "SPY", "비트코인": "BTC-USD"}).columns) \
        == ["S&P 500"]
