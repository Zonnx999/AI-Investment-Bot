"""src/digest.py — format_digest 순수 포매터 (오프라인, 합성 리포트 객체)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.digest import format_digest
from src.predictors import LeadLagResult
from src.signals import Alert, FactorScores, SignalReport
from src.universe import ScanRow

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
    assert "어제와 큰 변화 없음" in out


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


def test_digest_injects_us_company_names():
    # names 맵이 주어지면 US 발굴 종목에 회사명을 곁들임 (B)
    out = format_digest(_report(), [], now=NOW, names={"NVDA": "NVIDIA Corp"})
    assert "NVIDIA Corp" in out


def test_digest_factor_legend_present():
    # 점수 의미 범례 한 줄 (C)
    out = format_digest(_report(), [], now=NOW)
    assert "높을수록 매력적" in out


def test_digest_prediction_direction_icon():
    # 예측 가독성 (E): 상승 → 📈, 하락 → 📉
    up = format_digest(_report(), [_pred("BTC 수익률", True, 0.6)], now=NOW)
    assert "📈" in up
    down = _pred("XLE 수익률", True, 0.5)
    down.direction = "하락 ↓"
    out = format_digest(_report(), [down], now=NOW)
    assert "📉" in out
    assert "개월 선행" in out   # 'M' → '개월 선행' 풀어쓰기


# ---------------- 시장별 (Step 1: KR/US 발굴 분리) ----------------


def _kr_pick(symbol, name, total, value, health, per, pbr) -> ScanRow:
    return ScanRow(symbol=symbol, market="KR", name=name, sector="", price=0.0,
                   market_cap=0.0, total_score=total, value_score=value,
                   health_score=health, roe=None, per=per, pbr=pbr)


def test_us_digest_has_us_market_label():
    out = format_digest(_report(), [], now=NOW, market="us")
    assert "🇺🇸 미국" in out
    assert "모멘텀" in out          # US 는 4팩터(모멘텀 포함) 표


def test_kr_digest_renders_korean_picks_not_us_factors():
    picks = [_kr_pick("005930", "삼성전자", 77, 80, 74, 8.6, 1.06),
             _kr_pick("000270", "기아", 75, 88, 70, 4.2, 0.90)]
    out = format_digest(_report(), [], now=NOW, market="kr", kr_picks=picks)
    assert "🇰🇷 한국" in out
    assert "삼성전자" in out and "005930" in out
    assert "PER 8.6" in out and "PBR 1.06" in out
    # KR 다이제스트엔 US 팩터 표(모멘텀 포함)가 안 나와야 함
    assert "모멘텀" not in out
    assert "NVDA" not in out


def test_kr_digest_handles_missing_per_pbr():
    out = format_digest(_report(), [], now=NOW, market="kr",
                        kr_picks=[_kr_pick("123456", "적자기업", 30, 40, 20, None, None)])
    assert "PER —" in out and "PBR —" in out


def test_kr_digest_empty_picks_keeps_common_sections():
    out = format_digest(_report(), [_pred("SOXX 수익률", True, 0.34)], now=NOW,
                        market="kr", kr_picks=[])
    assert "발굴 종목 (한국)" not in out      # 빈 섹션 생략
    assert "위험선호" in out                   # 국면(공통) 유지
    assert "SOXX 수익률" in out                # 예측(공통) 유지


# -------- send_daily_digest: 수신자 0명이면 무거운 조립 생략 (#5) --------


def test_broadcast_skips_build_when_no_recipients(monkeypatch):
    """active 구독자 0명이면 build_daily_digest(fetch+분석) 를 호출하지 않고 즉시 반환."""
    import src.digest as digest
    from src import subscribers

    monkeypatch.setattr(subscribers, "ensure_owner", lambda: None)
    monkeypatch.setattr(subscribers, "active_subscribers", lambda: [])

    def _boom(*a, **k):
        raise AssertionError("수신자 0명인데 build_daily_digest 가 호출됨")

    monkeypatch.setattr(digest, "build_daily_digest", _boom)

    result = digest.send_daily_digest()
    assert result == {"sent": 0, "failed": 0, "recipients": 0}
