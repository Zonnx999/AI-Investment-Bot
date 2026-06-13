"""src/digest.py — format_digest 순수 포매터 (오프라인, 합성 리포트 객체)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.digest import format_digest
from src.predictors import LeadLagResult
from src.signals import Alert, FactorScores, SignalReport

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 6, 14, 7, 0, tzinfo=KST)


def _report(**kw) -> SignalReport:
    base = dict(
        generated_at="2026-06-14T07:00:00+09:00",
        regime_label="🟢 위험선호 (Risk-on)",
        factors=[FactorScores("NVDA", 100, 14, 77, 64), FactorScores("CPNG", 0, 30, 57, 29)],
        candidates=[],
        alerts=[],
        first_run=False,
    )
    base.update(kw)
    return SignalReport(**base)


def _pred(target, reliable, r2, lag=6) -> LeadLagResult:
    return LeadLagResult(
        leading_name="X", target_name=target, best_lag_months=lag,
        correlation=0.6, r_squared=r2, slope=1.0, intercept=0.0, n_obs=120,
        latest_leading_value=5.0, predicted_change_pct=10.0,
        direction="상승 ↑", reliable=reliable,
    )


def test_digest_includes_regime_and_factors():
    out = format_digest(_report(), [], now=NOW)
    assert "위험선호" in out
    assert "NVDA" in out and "CPNG" in out
    assert "2026-06-14" in out


def test_digest_factors_sorted_by_composite():
    out = format_digest(_report(), [], now=NOW)
    assert out.index("NVDA") < out.index("CPNG")   # 종합 64 > 29


def test_digest_shows_alerts_when_present():
    rep = _report(alerts=[Alert("warning", "regime", "국면 전환: A → B")])
    out = format_digest(rep, [], now=NOW)
    assert "🔔" in out
    assert "국면 전환" in out


def test_digest_no_alert_line_when_none():
    out = format_digest(_report(alerts=[]), [], now=NOW)
    assert "변화 알림 없음" in out


def test_digest_reliable_predictions_shown_weak_counted():
    preds = [
        _pred("BTC 수익률", True, 0.63),
        _pred("SOXX 수익률", True, 0.34),
        _pred("XHB 수익률", False, 0.11),
    ]
    out = format_digest(_report(), preds, now=NOW)
    assert "BTC 수익률" in out
    assert "SOXX 수익률" in out
    assert "그 외 1개 약함" in out


def test_digest_all_weak_predictions_message():
    out = format_digest(_report(), [_pred("BTC", False, 0.05)], now=NOW)
    assert "신뢰할 만한 예측 없음" in out


def test_digest_shows_candidates():
    rep = _report(candidates=[{"ticker": "AAPL", "pe": 12.5, "reasons": []}])
    out = format_digest(rep, [], now=NOW)
    assert "AAPL" in out
    assert "P/E 12.5" in out


def test_digest_handles_loss_maker_candidate():
    rep = _report(candidates=[{"ticker": "RIVN", "pe": None, "reasons": []}])
    out = format_digest(rep, [], now=NOW)
    assert "적자" in out
