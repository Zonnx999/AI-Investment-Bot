"""
src/findings.py
===============
Phase 13a — 구조화 리서치 결과 (공통 shape).

국면(macro_analyzer)·팩터(signals)·예측(predictors)·스크리닝(signals) 이
각자 dict/객체를 다이제스트와 대시보드에 넘기던 것을 **하나의 불변
`Finding`** 으로 통일합니다. 다이제스트(src/digest.py)와 대시보드
내보내기(scripts/export_dashboard.py)가 같은 shape 를 소비합니다.

설계
----
- `Finding` 은 frozen dataclass — 조립 후 어느 소비자도 변형 불가.
- `from_*` 어댑터는 **순수 함수** (이미 계산된 결과 객체 → Finding,
  fetch 없음, 오프라인 테스트 가능). 기존 결과 타입의 필드를 그대로
  옮기며, 다이제스트가 현재 보여주는 정보를 잃지 않습니다.
- 새 통계를 발명하지 않음 — confidence 는 기존 개념 재사용
  (예: 예측의 R² 기반 reliable 여부).

의존성: 표준 라이브러리만 import (원본 타입 힌트는 TYPE_CHECKING 뒤로)
→ 다른 모듈 없이도 import + 실행 가능 (§4.2 모듈 독립성).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 타입 힌트 전용 — 런타임 의존성 없음
    from src.macro_analyzer import RegimeReport
    from src.predictors import LeadLagResult
    from src.signals import FactorScores

# Finding.kind 허용값 — 섹션(결과 출처) 구분
KINDS = frozenset({"regime", "factor", "prediction", "screen"})

# 예측 confidence 라벨 — 기존 LeadLagResult.reliable (R² >= STRONG_R2) 재사용.
# 다이제스트/대시보드가 문자열 비교로 분기하므로 상수로 고정.
CONFIDENCE_RELIABLE = "신뢰"
CONFIDENCE_WEAK = "약함"


@dataclass(frozen=True)
class Finding:
    """다이제스트·대시보드가 공통으로 소비하는 리서치 결과 한 건.

    Attributes
    ----------
    kind : "regime" | "factor" | "prediction" | "screen"
    title : 항목 식별 제목 (팩터/스크리닝은 티커, 국면은 국면 라벨,
            예측은 관계 이름 "선행 → 목표")
    score : 항목의 대표 수치 (국면 점수, 팩터 종합, 예측 R², 스크리닝 P/E).
            없으면 None (예: 적자 종목의 P/E).
    confidence : 신뢰도 라벨 — 기존 개념 재사용 (예측의 reliable 여부).
            해당 개념이 없는 kind 는 None.
    summary : 사용자용 한 줄 요약 (한국어) — 다이제스트가 그대로 렌더.
    evidence : 근거 목록 (기존 notes/signals/reasons 를 그대로 보존).
    """

    kind: str
    title: str
    score: float | None
    confidence: str | None
    summary: str
    evidence: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"알 수 없는 Finding.kind: {self.kind!r} (허용: {sorted(KINDS)})")

    def to_dict(self) -> dict:
        """JSON 직렬화 가능 dict (evidence 는 list 로)."""
        return {
            "kind": self.kind,
            "title": self.title,
            "score": self.score,
            "confidence": self.confidence,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }


# ----------------------------------------------------------------------
# 어댑터 (순수 함수 — 기존 결과 객체 → Finding)
# ----------------------------------------------------------------------


def from_regime(report: "RegimeReport") -> Finding:
    """macro_analyzer.RegimeReport → Finding.

    score 는 -3~+3 국면 점수, evidence 는 지표별 근거 한 줄들.
    (failures 는 '결과' 가 아니라 진단 정보라 Finding 에 싣지 않음 —
    대시보드는 RegimeReport.failures 를 별도 키로 계속 내보냄.)
    """
    return Finding(
        kind="regime",
        title=report.regime,
        score=float(report.score),
        confidence=None,   # 국면엔 기존 신뢰도 개념 없음 (score 가 강도)
        summary=f"시장 국면: {report.regime} (점수 {report.score:+d})",
        evidence=tuple(report.signals),
    )


def from_factor_scores(scores: "FactorScores") -> Finding:
    """signals.FactorScores → Finding.

    score 는 4팩터 종합(0~100, 정수값), summary 는 다이제스트의 팩터
    내역 줄과 동일 포맷, evidence 는 팩터 계산 근거 notes.
    """
    return Finding(
        kind="factor",
        title=scores.ticker,
        score=float(scores.composite),
        confidence=None,   # 팩터엔 기존 신뢰도 개념 없음
        summary=(
            f"모멘텀 {scores.momentum} · 밸류 {scores.value} · "
            f"퀄리티 {scores.quality} · 로우볼 {scores.low_vol}"
        ),
        evidence=tuple(scores.notes),
    )


def from_prediction(result: "LeadLagResult", name: str | None = None) -> Finding:
    """predictors.LeadLagResult → Finding.

    name: PREDICTORS 레지스트리 키 (예: "M2 → 비트코인"). 없으면
    "선행 → 목표" 로 구성. score 는 R², confidence 는 기존 reliable
    (R² >= STRONG_R2) 를 라벨로, summary 는 다이제스트 예측 줄과 동일 포맷.
    """
    return Finding(
        kind="prediction",
        title=name or f"{result.leading_name} → {result.target_name}",
        score=float(result.r_squared),
        confidence=CONFIDENCE_RELIABLE if result.reliable else CONFIDENCE_WEAK,
        summary=(
            f"{result.target_name}: {result.direction} "
            f"({result.best_lag_months}개월 선행, 신뢰도 R² {result.r_squared:.2f})"
        ),
        evidence=tuple(result.notes),
    )


def from_screen_candidate(candidate: dict) -> Finding:
    """signals.screen_candidates() 통과 종목 dict → Finding.

    candidate: {"ticker", "pe", "roe", "fcf_yield", "reasons"}.
    P/E 가 없거나 0 이하(적자·무의미)면 score=None + '적자' 표기
    (§4.10 #5 — 음수/0 P/E 는 저평가 신호가 아님).
    """
    pe = candidate.get("pe")
    has_pe = bool(pe)   # 기존 다이제스트와 동일한 truthy 판정 (None/0 → 적자)
    return Finding(
        kind="screen",
        title=candidate["ticker"],
        score=float(pe) if has_pe else None,
        confidence=None,   # 스크리닝엔 기존 신뢰도 개념 없음 (룰 통과 자체가 판정)
        summary=f"P/E {pe:.1f}" if has_pe else "적자",
        evidence=tuple(candidate.get("reasons") or ()),
    )
