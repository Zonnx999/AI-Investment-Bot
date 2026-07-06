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
from src.findings import (
    CONFIDENCE_RELIABLE,
    from_factor_scores,
    from_prediction,
    from_screen_candidate,
)
from src.logger import get_logger
from src.predictors import LeadLagResult
from src.signals import SignalReport

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")


_MARKET_LABEL = {"KR": "🇰🇷 한국", "US": "🇺🇸 미국"}


def _direction_icon(direction: str) -> str:
    """예측 방향 문자열(예: '상승 ↑') → 한눈에 들어오는 아이콘."""
    if "상승" in direction or "↑" in direction:
        return "📈"
    if "하락" in direction or "↓" in direction:
        return "📉"
    return "🔮"


_ALERT_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


def _alert_line(alert) -> str:
    """알림 → 유저용 한 줄. Alert.__str__ 의 `[category]` 영문 태그(로그용)는
    빼고 심각도 아이콘 + 메시지만 — 다이제스트 가독성 위해 디스플레이에서만 정제."""
    return f"  {_ALERT_ICON.get(alert.severity, 'ℹ️')} {alert.message}"


def _titled_line(ticker: str, names: dict[str, str], rest: str) -> str:
    """발굴/스크리닝 공통 종목 줄: 티커(+회사명 있으면) — 나머지."""
    nm = names.get(ticker, "")
    if nm:
        return f"  `{ticker}` *{nm}* — {rest}"
    return f"  `{ticker}` — {rest}"


def with_summary(text: str, summary: str | None) -> str:
    """LLM 요약을 다이제스트 맨 위에 붙임 (순수 함수).

    요약은 선택적 표현 레이어(ROADMAP §2.1) — 없으면(None/빈 문자열) 원문 그대로.
    """
    if not summary:
        return text
    return f"🧠 {summary}\n\n{text}"


def format_digest(
    report: SignalReport,
    predictions: list[LeadLagResult],
    now: datetime | None = None,
    market: str = "US",
    kr_picks: "list | None" = None,
    names: "dict[str, str] | None" = None,
    summary: str | None = None,
) -> str:
    """리포트 객체들 → 텔레그램 Markdown 메시지 (순수 함수).

    구성: 헤더 → 오늘의 변화(알림, 가장 중요해 맨 위) → 시장 국면 →
    발굴 종목(시장별) → 선행지표 예측.
    market="KR" 이고 kr_picks(ScanRow 리스트)가 주어지면 한국 발굴 섹션을 렌더 —
    한국은 DART 기반 밸류/퀄리티 점수(모멘텀 미산출)라 표 구성이 미국과 다름.
    국면·알림·예측은 글로벌 매크로라 두 시장 공통.

    names: 티커 → 회사명 맵(선택). US 발굴/스크리닝 종목에 회사명을 곁들임
    (순수 함수 유지를 위해 조회는 build_daily_digest 가 담당, 여기엔 주입만).

    summary: LLM 한 줄 요약(선택). 주어지면 맨 위에 렌더 — 생성은 src/llm.py,
    호출은 오케스트레이터(send_daily_digest / scripts) 책임 (순수 함수 유지).
    """
    now = now or datetime.now(KST)
    market = (market or "US").upper()
    label = _MARKET_LABEL.get(market, "")
    names = names or {}
    lines: list[str] = []

    # ---- 헤더 ----
    lines.append(f"*📊 일일 투자 신호* · {label}")
    lines.append(f"_{now:%Y-%m-%d (%a) %H:%M KST}_")

    # ---- 오늘의 변화 (가장 중요 — 맨 위) ----
    if report.alerts:
        lines.append("")
        lines.append("*🔔 오늘의 변화*")
        for a in report.alerts:
            lines.append(_alert_line(a))
    elif not report.first_run:
        lines.append("")
        lines.append("✅ _어제와 큰 변화 없음_")

    # ---- 시장 국면 ----
    lines.append("")
    lines.append(f"*🌤 시장 국면:* {report.regime_label}")

    # ---- 오늘의 발굴 종목 (시장별) ----
    if market == "KR":
        if kr_picks:
            lines.append("")
            lines.append("*📈 오늘의 발굴 종목 (한국)*")
            for r in kr_picks:
                per = f"PER {r.per:.1f}" if r.per else "PER —"
                pbr = f"PBR {r.pbr:.2f}" if r.pbr else "PBR —"
                lines.append(f"  `{r.symbol}` *{r.name}* — 종합 *{r.total_score}*")
                lines.append(
                    f"     밸류 {r.value_score} · 퀄리티 {r.health_score}  ({per}, {pbr})"
                )
            lines.append("  └ _밸류·퀄리티 종합 (0~100, 높을수록 저평가·우량)_")
    else:
        if report.factors:
            lines.append("")
            lines.append("*📈 오늘의 발굴 종목*")
            # Finding 공통 shape 소비 (13a): title=티커, score=종합, summary=팩터 내역
            for fd in sorted(
                (from_factor_scores(f) for f in report.factors),
                key=lambda x: x.score, reverse=True,
            ):
                lines.append(_titled_line(fd.title, names, f"종합 *{fd.score:.0f}*"))
                lines.append(f"     {fd.summary}")
            lines.append("  └ _모멘텀·밸류·퀄리티·로우볼 종합 (0~100, 높을수록 매력적)_")
        # ---- 발굴 종목 (스크리닝 통과 — US 라이브 스크리너) ----
        if report.candidates:
            lines.append("")
            lines.append("*💎 스크리닝 통과*")
            for fd in (from_screen_candidate(c) for c in report.candidates[:5]):
                lines.append(_titled_line(fd.title, names, fd.summary))

    # ---- 선행지표 예측 ----
    if predictions:
        # Finding 공통 shape 소비 (13a): score=R², confidence=기존 reliable 라벨
        pred_findings = [from_prediction(p) for p in predictions]
        reliable = [f for f in pred_findings if f.confidence == CONFIDENCE_RELIABLE]
        weak = len(pred_findings) - len(reliable)
        lines.append("")
        lines.append("*🔮 선행지표 예측*")
        if reliable:
            for fd in sorted(reliable, key=lambda x: x.score, reverse=True):
                # summary 는 "목표: 방향 (…)" — 방향 아이콘은 summary 에서 판별
                lines.append(f"  {_direction_icon(fd.summary)} {fd.summary}")
        else:
            lines.append("  _신뢰할 만한 예측 없음 (R²<0.3)_")
        if weak:
            lines.append(f"  _그 외 {weak}개 약함 (참고 제외)_")

    lines.append("")
    lines.append("_상관 기반 통계 모델 — 투자 결정은 본인 판단_")
    return with_summary("\n".join(lines), summary)


def _us_names(symbols: list[str]) -> dict[str, str]:
    """US 티커 → 회사명 맵 (유니버스 DB 조회). 미보강/미발견은 생략.

    조회는 로컬 레플리카 읽기(원격 쓰기 아님)라 소수 종목엔 비용 미미.
    개별 조회 실패는 best-effort — 이름은 부가정보라 없으면 티커만 표시.
    """
    from src import universe

    out: dict[str, str] = {}
    for s in dict.fromkeys(symbols):   # 중복 제거(순서 유지)
        try:
            row = universe.lookup(s)
        except QuantBotError as e:
            logger.debug("회사명 조회 스킵 %s — %s", s, e)
            continue
        if row and row.name:
            out[s] = row.name
    return out


def _collect_predictions() -> list[LeadLagResult]:
    """선행지표 예측 — 글로벌(시장 무관). 개별 실패는 스킵(로그)."""
    from src.predictors import PREDICTORS

    out: list[LeadLagResult] = []
    for name, predict in PREDICTORS.items():
        try:
            out.append(predict())
        except QuantBotError as e:
            logger.warning("예측 스킵 %s — %s", name, e)
    return out


def build_daily_digest(
    market: str = "us",
    top_n: int = 6,
    tickers: tuple[str, ...] | None = None,
) -> str:
    """fetch + 분석 → 시장별 다이제스트 문자열.

    market="us": 유니버스 상위 N(없으면 라이브 스크리너→기본)으로 4팩터 신호.
    market="kr": KR 유니버스 DB(DART 점수) 상위 N을 발굴 섹션으로 — 모멘텀 미산출,
      오프라인(API 0콜). 한국 코드는 yfinance 가격 접미사(.KS/.KQ)가 필요해 라이브
      팩터 파이프라인을 안 태움 (Step 1 결정: DB 점수 사용).
    국면·알림·예측은 글로벌이라 두 시장 공통.
    """
    from src import universe
    from src.signals import (
        DEFAULT_SIGNAL_TICKERS,
        generate_signal_report,
        select_screened_tickers,
    )

    market = (market or "us").upper()
    predictions = _collect_predictions()

    if market == "KR":
        # 팩터(가격) 없이 국면/알림만 — KR baseline 으로 (US 알림 state 와 분리)
        report = generate_signal_report(tickers=(), screen_tickers=None, market="KR")
        kr_picks = universe.scan(market="KR", limit=top_n)
        return format_digest(report, predictions, market="KR", kr_picks=kr_picks)

    # --- US (기본) ---
    tk = tickers
    if tk is None:
        # 1순위: 유니버스 DB 전수스캔 상위 (오프라인). 비면 2순위 라이브 스크리너, 그다음 기본.
        screened = universe.top_symbols(n=top_n, market="US")
        if not screened:
            try:
                screened = select_screened_tickers(n=top_n)
            except QuantBotError as e:
                logger.warning("스크리너 실패 — 기본 종목으로 폴백: %s", e)
                screened = []
        tk = tuple(screened) or DEFAULT_SIGNAL_TICKERS

    report = generate_signal_report(tickers=tk, screen_tickers=None, market="US")
    names = _us_names(
        [f.ticker for f in report.factors] + [c["ticker"] for c in report.candidates]
    )
    return format_digest(report, predictions, market="US", names=names)


def send_daily_digest(
    market: str = "us",
    top_n: int = 6,
    tickers: tuple[str, ...] | None = None,
    use_llm: bool = True,
) -> dict[str, int]:
    """시장별 다이제스트 1회 조립 후 **active 구독자 전원**에게 브로드캐스트 (Phase 11a).

    무거운 조립(fetch+분석)은 한 번만, 전송은 경량 N회. 소유자는 ensure_owner 로 항상 포함.
    개별 전송 실패는 best-effort (한 명 실패가 나머지·파이프라인을 막지 않음).
    use_llm=True 면 LLM 한 줄 요약을 맨 위에 시도 (best-effort — 실패 시 요약만 생략,
    ROADMAP §2.1). ``QUANT_BOT_LLM=0`` / ``--no-llm`` 킬스위치로 끌 수 있음.
    Returns {"sent", "failed", "recipients"}.
    """
    from src import subscribers
    from src.notifier import send_safe
    from src.storage import get_storage

    subscribers.ensure_owner()
    text = build_daily_digest(market=market, top_n=top_n, tickers=tickers)   # 무거운 조립 1회
    if use_llm:
        from src import llm
        text = with_summary(text, llm.summarize_safe(text))   # 실패 시 None → 원문 그대로
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
