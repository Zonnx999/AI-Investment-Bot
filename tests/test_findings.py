"""
tests/test_findings.py
======================
Phase 13a — Finding 공통 shape + 어댑터 (오프라인, 합성 결과 객체).

네트워크/DB 없음. 어댑터가 기존 결과 타입의 필드를 손실 없이 옮기는지,
불변성(frozen)이 강제되는지 검증.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.findings import (
    CONFIDENCE_RELIABLE,
    CONFIDENCE_WEAK,
    Finding,
    from_factor_scores,
    from_prediction,
    from_regime,
    from_screen_candidate,
)
from src.macro_analyzer import RegimeReport
from src.predictors import LeadLagResult
from src.signals import FactorScores


# ----------------------------------------------------------------------
# 합성 결과 객체
# ----------------------------------------------------------------------


def _regime() -> RegimeReport:
    return RegimeReport(
        regime="🟢 위험선호 (Risk-on)",
        score=2,
        signals=["장단기 금리차 +0.50%p 로 정상 우상향 — 위험선호 (+1)",
                 "하이일드 스프레드 3.20% 안정 — 위험선호 (+1)"],
        raw={"T10Y2Y": 0.5},
        failures=["jobless_claims"],
    )


def _factors() -> FactorScores:
    return FactorScores(
        ticker="NVDA", momentum=100, value=14, quality=77, composite=64,
        notes=["12-1개월(skip) 수익률 +45.0% ↑", "연환산 변동성 42.1% → Low Vol 36"],
        low_vol=36, vol_pct=42.1,
    )


def _prediction(reliable: bool = True, r2: float = 0.42) -> LeadLagResult:
    return LeadLagResult(
        leading_name="M2 증가율", target_name="BTC 수익률", best_lag_months=3,
        correlation=0.62, r_squared=r2, slope=1.5, intercept=0.2, n_obs=48,
        latest_leading_value=5.3, predicted_change_pct=15.3,
        direction="상승 ↑", reliable=reliable,
        notes=["최적 선행 3개월 (상관 +0.62)", "예측 +15.3%"],
    )


# ----------------------------------------------------------------------
# Finding 기본 (불변성 / to_dict / kind 검증)
# ----------------------------------------------------------------------


def test_finding_is_immutable():
    fd = from_regime(_regime())
    with pytest.raises(FrozenInstanceError):
        fd.title = "변조"
    with pytest.raises(FrozenInstanceError):
        fd.score = 0.0


def test_finding_evidence_is_tuple():
    fd = from_factor_scores(_factors())
    assert isinstance(fd.evidence, tuple)   # list 였으면 내용 변형 가능 — tuple 강제


def test_finding_rejects_unknown_kind():
    with pytest.raises(ValueError):
        Finding(kind="alert", title="t", score=None, confidence=None, summary="s")


def test_finding_to_dict_keys_and_evidence_list():
    d = from_prediction(_prediction()).to_dict()
    assert set(d) == {"kind", "title", "score", "confidence", "summary", "evidence"}
    assert isinstance(d["evidence"], list)


# ----------------------------------------------------------------------
# from_regime
# ----------------------------------------------------------------------


def test_from_regime_fields():
    fd = from_regime(_regime())
    assert fd.kind == "regime"
    assert fd.title == "🟢 위험선호 (Risk-on)"
    assert fd.score == 2.0
    assert fd.confidence is None            # 국면엔 기존 신뢰도 개념 없음
    assert "시장 국면" in fd.summary and "+2" in fd.summary
    assert len(fd.evidence) == 2            # signals 손실 없음
    assert "장단기 금리차" in fd.evidence[0]


# ----------------------------------------------------------------------
# from_factor_scores
# ----------------------------------------------------------------------


def test_from_factor_scores_fields():
    fd = from_factor_scores(_factors())
    assert fd.kind == "factor"
    assert fd.title == "NVDA"
    assert fd.score == 64.0
    assert fd.confidence is None
    # 다이제스트 팩터 내역 줄과 동일 포맷 — 4팩터 전부 보존
    assert fd.summary == "모멘텀 100 · 밸류 14 · 퀄리티 77 · 로우볼 36"
    assert fd.evidence == ("12-1개월(skip) 수익률 +45.0% ↑",
                           "연환산 변동성 42.1% → Low Vol 36")


# ----------------------------------------------------------------------
# from_prediction
# ----------------------------------------------------------------------


def test_from_prediction_reliable():
    fd = from_prediction(_prediction(reliable=True, r2=0.42), name="M2 → 비트코인")
    assert fd.kind == "prediction"
    assert fd.title == "M2 → 비트코인"     # 레지스트리 키 우선
    assert fd.score == pytest.approx(0.42)
    assert fd.confidence == CONFIDENCE_RELIABLE
    # 다이제스트 예측 줄과 동일 포맷 — 방향/lag/R² 보존
    assert fd.summary == "BTC 수익률: 상승 ↑ (3개월 선행, 신뢰도 R² 0.42)"
    assert fd.evidence == ("최적 선행 3개월 (상관 +0.62)", "예측 +15.3%")


def test_from_prediction_weak_and_default_title():
    fd = from_prediction(_prediction(reliable=False, r2=0.08))
    assert fd.confidence == CONFIDENCE_WEAK
    assert fd.title == "M2 증가율 → BTC 수익률"   # name 없으면 "선행 → 목표"


# ----------------------------------------------------------------------
# from_screen_candidate
# ----------------------------------------------------------------------


def test_from_screen_candidate_with_pe():
    fd = from_screen_candidate({
        "ticker": "AAPL", "pe": 12.5, "roe": 0.2, "fcf_yield": 0.03,
        "reasons": ["ROE 20.0% > 10%", "FCF yield 3.0% 양수"],
    })
    assert fd.kind == "screen"
    assert fd.title == "AAPL"
    assert fd.score == pytest.approx(12.5)
    assert fd.summary == "P/E 12.5"
    assert fd.evidence == ("ROE 20.0% > 10%", "FCF yield 3.0% 양수")


def test_from_screen_candidate_loss_maker():
    # P/E None(적자) → score 없음 + '적자' 표기 (§4.10 #5 부호 가드)
    fd = from_screen_candidate({"ticker": "RIVN", "pe": None})
    assert fd.score is None
    assert fd.summary == "적자"
    assert fd.evidence == ()
