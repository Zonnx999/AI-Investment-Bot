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


# ---------------- Finding 기반 렌더링 — 기존 포맷 byte-identical (13a) ----------------


def test_digest_factor_lines_exact_format():
    # 팩터 섹션이 Finding 경로로 바뀌어도 줄 포맷은 리팩토링 전과 동일해야 함
    out = format_digest(_report(), [], now=NOW, names={"NVDA": "NVIDIA Corp"})
    assert "  `NVDA` *NVIDIA Corp* — 종합 *64*" in out       # 이름 있음 (int 렌더 유지)
    assert "  `CPNG` — 종합 *29*" in out                     # 이름 없음
    assert "     모멘텀 100 · 밸류 14 · 퀄리티 77 · 로우볼 0" in out


def test_digest_candidate_lines_exact_format():
    rep = _report(candidates=[{"ticker": "AAPL", "pe": 12.5, "reasons": []},
                              {"ticker": "RIVN", "pe": None, "reasons": []}])
    out = format_digest(rep, [], now=NOW, names={"AAPL": "Apple Inc."})
    assert "  `AAPL` *Apple Inc.* — P/E 12.5" in out
    assert "  `RIVN` — 적자" in out


def test_digest_prediction_lines_exact_format():
    preds = [_pred("BTC 수익률", True, 0.63), _pred("XHB 수익률", False, 0.11)]
    out = format_digest(_report(), preds, now=NOW)
    assert "  📈 BTC 수익률: 상승 ↑ (6개월 선행, 신뢰도 R² 0.63)" in out
    assert "  _그 외 1개 약함 (참고 제외)_" in out


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


# ---------------- LLM 한 줄 요약 (ROADMAP §2.1 — 표현 레이어) ----------------


def test_digest_renders_summary_at_top():
    out = format_digest(_report(), [], now=NOW, summary="오늘은 위험선호 분위기가 이어졌습니다.")
    assert out.startswith("🧠 오늘은 위험선호 분위기가 이어졌습니다.\n\n")
    assert "일일 투자 신호" in out             # 본문은 그대로 뒤따름
    assert "NVDA" in out


def test_digest_without_summary_unchanged():
    out = format_digest(_report(), [], now=NOW)
    assert "🧠" not in out
    assert out.startswith("*📊 일일 투자 신호*")


def test_with_summary_pure_helper():
    from src.digest import with_summary
    assert with_summary("본문", "요약") == "🧠 요약\n\n본문"
    assert with_summary("본문", None) == "본문"      # 실패/생략 시 원문 그대로
    assert with_summary("본문", "") == "본문"


# ---------------- send_daily_digest 배선 — LLM 실패해도 발송 불가침 ----------------


def _broadcast_env(monkeypatch, digest_text="본문 다이제스트"):
    """send_daily_digest 의 무거운 조립/전송/DB 를 전부 스텁. 전송된 텍스트 목록 반환."""
    import src.digest as digest_mod

    sent: list[str] = []

    class _FakeStorage:
        def sync(self):
            pass

    monkeypatch.setattr(digest_mod, "build_daily_digest", lambda **kw: digest_text)
    monkeypatch.setattr("src.subscribers.ensure_owner", lambda: None)
    monkeypatch.setattr("src.subscribers.active_subscribers", lambda: [("1", "leo")])
    monkeypatch.setattr("src.notifier.send_safe",
                        lambda text, chat_id: sent.append(text) or True)
    monkeypatch.setattr("src.storage.get_storage", lambda: _FakeStorage())
    return sent


def test_send_daily_digest_sends_even_when_llm_fails(monkeypatch):
    import src.digest as digest_mod
    import src.llm as llm_mod
    from src.exceptions import ApiHttpError

    sent = _broadcast_env(monkeypatch)

    def _boom(text):
        raise ApiHttpError("LLM down", status_code=500, source="NVIDIA-NIM")

    monkeypatch.setattr(llm_mod, "summarize", _boom)
    result = digest_mod.send_daily_digest(use_llm=True)
    assert result == {"sent": 1, "failed": 0, "recipients": 1}
    assert sent == ["본문 다이제스트"]           # 요약 실패 → 원문 그대로 발송


def test_send_daily_digest_prepends_summary_on_success(monkeypatch):
    import src.digest as digest_mod
    import src.llm as llm_mod

    sent = _broadcast_env(monkeypatch)
    monkeypatch.setattr(llm_mod, "summarize", lambda text: "조용한 하루였습니다.")
    digest_mod.send_daily_digest(use_llm=True)
    assert sent == ["🧠 조용한 하루였습니다.\n\n본문 다이제스트"]


def test_send_daily_digest_use_llm_false_skips_llm_entirely(monkeypatch):
    import src.digest as digest_mod
    import src.llm as llm_mod

    sent = _broadcast_env(monkeypatch)
    called: list[str] = []
    monkeypatch.setattr(llm_mod, "summarize_safe",
                        lambda text: called.append(text) or "요약")
    digest_mod.send_daily_digest(use_llm=False)
    assert called == []                          # --no-llm: 호출 자체가 없어야 함
    assert sent == ["본문 다이제스트"]
