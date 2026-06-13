"""
src/digest.py
=============
Phase 7 — 일일 다이제스트 조립.

신호 엔진(Phase 5) + 선행지표 예측(Phase 6) + 시장 국면을 텔레그램용
한 메시지(Markdown)로 조립. 매일 아침 이 한 통이 "친구 C 봇의 완성형".

설계: `format_digest()` 는 **순수 함수** (이미 계산된 리포트 객체 → 문자열,
오프라인 테스트). fetch 는 `build_daily_digest()` 오케스트레이터만 담당.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.exceptions import QuantBotError
from src.logger import get_logger
from src.predictors import LeadLagResult
from src.signals import SignalReport

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")


def format_digest(
    report: SignalReport,
    predictions: list[LeadLagResult],
    now: datetime | None = None,
) -> str:
    """리포트 객체들 → 텔레그램 Markdown 메시지 (순수 함수).

    구성: 헤더 → 시장 국면 → 알림(있으면 최상단 강조) → 팩터 점수 →
    발굴 종목(있으면) → 선행지표 예측(신뢰할 만한 것 우선).
    """
    now = now or datetime.now(KST)
    lines: list[str] = []

    lines.append(f"*📊 일일 투자 신호* — {now:%Y-%m-%d (%a) %H:%M KST}")
    lines.append("")
    lines.append(f"*시장 국면:* {report.regime_label}")

    # ---- 알림 (가장 중요 — 변화가 있으면 맨 위로) ----
    if report.alerts:
        lines.append("")
        lines.append("*🔔 알림*")
        for a in report.alerts:
            lines.append(f"  {a}")
    elif not report.first_run:
        lines.append("_변화 알림 없음_")

    # ---- 팩터 점수 ----
    if report.factors:
        lines.append("")
        lines.append("*📈 팩터 점수* (모멘텀/밸류/퀄리티 → 종합)")
        for f in sorted(report.factors, key=lambda x: x.composite, reverse=True):
            lines.append(
                f"  `{f.ticker:<5}` {f.momentum:>3}/{f.value:>3}/{f.quality:>3} → *{f.composite}*"
            )

    # ---- 발굴 종목 ----
    if report.candidates:
        lines.append("")
        lines.append("*💎 발굴 종목* (스크리닝 통과)")
        for c in report.candidates[:5]:
            pe = f"P/E {c['pe']:.1f}" if c.get("pe") else "적자"
            lines.append(f"  `{c['ticker']}` — {pe}")

    # ---- 선행지표 예측 ----
    if predictions:
        reliable = [p for p in predictions if p.reliable]
        weak = len(predictions) - len(reliable)
        lines.append("")
        lines.append("*🔮 선행지표 예측*")
        if reliable:
            for p in sorted(reliable, key=lambda x: x.r_squared, reverse=True):
                lines.append(
                    f"  {p.target_name}: {p.direction} "
                    f"(선행 {p.best_lag_months}M, R² {p.r_squared:.2f})"
                )
        else:
            lines.append("  _신뢰할 만한 예측 없음 (R²<0.3)_")
        if weak:
            lines.append(f"  _그 외 {weak}개 약함 (참고 제외)_")

    lines.append("")
    lines.append("_상관 기반 통계 모델 — 투자 결정은 본인 판단_")
    return "\n".join(lines)


def build_daily_digest(
    tickers: tuple[str, ...] | None = None,
    screen: bool = False,
) -> str:
    """fetch + 분석 → 다이제스트 문자열. 예측 개별 실패는 스킵(로그)."""
    from src.predictors import PREDICTORS
    from src.signals import DEFAULT_SIGNAL_TICKERS, generate_signal_report

    tk = tickers or DEFAULT_SIGNAL_TICKERS
    screen_tickers = None
    if screen:
        from src.screener import US_WATCHLIST
        screen_tickers = list(US_WATCHLIST)

    report = generate_signal_report(tickers=tk, screen_tickers=screen_tickers)

    predictions: list[LeadLagResult] = []
    for name, predict in PREDICTORS.items():
        try:
            predictions.append(predict())
        except QuantBotError as e:
            logger.warning("예측 스킵 %s — %s", name, e)

    return format_digest(report, predictions)


def send_daily_digest(
    tickers: tuple[str, ...] | None = None,
    screen: bool = False,
) -> bool:
    """다이제스트 조립 + 텔레그램 전송 (best-effort). 성공 여부 반환."""
    from src.notifier import send_safe

    return send_safe(build_daily_digest(tickers=tickers, screen=screen))
