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

    # ---- 오늘의 발굴 종목 (스크리닝 상위) + 팩터 점수 ----
    if report.factors:
        lines.append("")
        lines.append("*📈 오늘의 발굴 종목* (모멘텀/밸류/퀄리티/로우볼 → 종합)")
        for f in sorted(report.factors, key=lambda x: x.composite, reverse=True):
            lines.append(
                f"  `{f.ticker:<5}` {f.momentum:>3}/{f.value:>3}/{f.quality:>3}/{f.low_vol:>3} "
                f"→ *{f.composite}*"
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
    top_n: int = 6,
) -> str:
    """fetch + 분석 → 다이제스트 문자열.

    tickers=None 이면 매일 **스크리너 발굴 상위 N개**를 팩터 대상으로 사용
    (고정 종목이 아니라 그날 저평가 상위 종목이 자동으로 올라옴).
    스크리너 실패 시 DEFAULT_SIGNAL_TICKERS 로 폴백. 예측 개별 실패는 스킵(로그).
    """
    from src.predictors import PREDICTORS
    from src.signals import (
        DEFAULT_SIGNAL_TICKERS,
        generate_signal_report,
        select_screened_tickers,
    )

    tk = tickers
    if tk is None:
        # 1순위: 유니버스 DB 전수스캔 상위 (오프라인, API 0콜). 비어있으면
        # 2순위: 라이브 스크리너(40종목 워치리스트). 그것도 실패하면 기본 종목.
        from src import universe

        screened = universe.top_symbols(n=top_n, market="US")
        if not screened:
            try:
                screened = select_screened_tickers(n=top_n)
            except QuantBotError as e:
                logger.warning("스크리너 실패 — 기본 종목으로 폴백: %s", e)
                screened = []
        tk = tuple(screened) or DEFAULT_SIGNAL_TICKERS

    report = generate_signal_report(tickers=tk, screen_tickers=None)

    predictions: list[LeadLagResult] = []
    for name, predict in PREDICTORS.items():
        try:
            predictions.append(predict())
        except QuantBotError as e:
            logger.warning("예측 스킵 %s — %s", name, e)

    return format_digest(report, predictions)


def send_daily_digest(
    tickers: tuple[str, ...] | None = None,
    top_n: int = 6,
) -> dict[str, int]:
    """다이제스트 1회 조립 후 **active 구독자 전원**에게 브로드캐스트 (Phase 11a).

    무거운 조립(fetch+분석)은 한 번만, 전송은 경량 N회. 소유자는 ensure_owner 로 항상 포함.
    개별 전송 실패는 best-effort (한 명 실패가 나머지·파이프라인을 막지 않음).
    Returns {"sent", "failed", "recipients"}.
    """
    from src import subscribers
    from src.notifier import send_safe
    from src.storage import get_storage

    subscribers.ensure_owner()
    text = build_daily_digest(tickers=tickers, top_n=top_n)   # 무거운 조립 1회
    recipients = subscribers.active_subscribers()

    sent = failed = 0
    for chat_id, _name in recipients:
        if send_safe(text, chat_id):
            sent += 1
        else:
            failed += 1

    get_storage().sync()  # 신호 상태 변경 + 구독 변경을 클라우드로 push (Turso 시)
    logger.info("브로드캐스트: 전송 %d / 실패 %d (대상 %d)", sent, failed, len(recipients))
    return {"sent": sent, "failed": failed, "recipients": len(recipients)}
