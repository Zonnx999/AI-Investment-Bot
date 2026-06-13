"""
src/signals.py
==============
Phase 5 — Signal Engine. 친구 C 봇("저평가 종목 알림")의 진화 버전.

세 가지 신호를 결정론적으로 생성합니다 (LLM 없음):
  1. **팩터 점수** — momentum / value / quality 각 0~100 + 종합
  2. **스크리닝 룰** — "ROE > 10%, FCF yield 양수, P/E ≤ 동료 중간값" 필터로 종목 발굴
  3. **알림 룰** — 시장 국면 전환, 자산 낙폭 임계 돌파, 변동성 급등
     (모두 '지난 실행 대비 변화' 기반 — storage 의 state 테이블에 이전 값 보관)

설계: 점수/룰/알림 판정은 **순수 함수** (데이터를 인자로 받음 → 오프라인 테스트 가능).
데이터 fetch 와 state 관리는 generate_signal_report() 오케스트레이터만 담당.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import numpy as np
import pandas as pd

from src.exceptions import DataFetchError, InsufficientDataError
from src.logger import get_logger
from src.screener import calculate_health_score, calculate_value_score
from src.storage import get_storage
from src.utils import TRADING_DAYS_PER_YEAR, close_series

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 상수 (튜닝 지점)
# ----------------------------------------------------------------------
DEFAULT_SIGNAL_TICKERS = ("CPNG", "NVDA")

MOMENTUM_LOOKBACK_LONG_D = 126    # ~6개월
MOMENTUM_LOOKBACK_SHORT_D = 63    # ~3개월
MA_LONG_WINDOW = 200              # 장기 추세선

# 종합 점수 가중치 — 합 1.0
COMPOSITE_WEIGHTS = {"momentum": 1 / 3, "value": 1 / 3, "quality": 1 / 3}

# 스크리닝 룰
SCREEN_MIN_ROE = 0.10             # ROE > 10%
SCREEN_MIN_FCF_YIELD = 0.0        # FCF yield 양수
# P/E 는 절대 임계 대신 '유효 P/E 의 중간값 이하' (업종 중간값의 워치리스트 근사)

# 알림 룰
DRAWDOWN_ALERT_THRESHOLD_PCT = -10.0   # 자산 낙폭 임계
VOL_SPIKE_RATIO = 1.25                 # 직전 실행 대비 변동성 +25% 이상

Severity = Literal["info", "warning", "critical"]


# ----------------------------------------------------------------------
# 데이터 구조
# ----------------------------------------------------------------------


@dataclass
class FactorScores:
    """한 종목의 팩터 점수 (각 0~100)."""

    ticker: str
    momentum: int
    value: int
    quality: int
    composite: int
    notes: list[str] = field(default_factory=list)


@dataclass
class Alert:
    severity: Severity
    category: str      # "regime" | "drawdown" | "volatility"
    message: str

    def __str__(self) -> str:
        icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}[self.severity]
        return f"{icon} [{self.category}] {self.message}"


@dataclass
class SignalReport:
    generated_at: str
    regime_label: str
    factors: list[FactorScores]
    candidates: list[dict]          # 스크리닝 통과 종목
    alerts: list[Alert]
    first_run: bool = False         # 첫 실행 — 비교 기준이 없어 변화 알림 생략됨


# ----------------------------------------------------------------------
# 1. 팩터 — momentum (순수 함수)
# ----------------------------------------------------------------------


def momentum_score(prices: pd.Series) -> tuple[int, list[str]]:
    """가격 시리즈 → 모멘텀 점수 (0~100) + 근거.

    세 요소를 각각 통과/탈락으로 평가, 점수 = 100 × 통과 / 평가가능 요소 수:
      · 6개월 수익률 > 0
      · 3개월 수익률 > 0
      · 현재가 > 200일 이동평균

    데이터가 짧으면 평가 가능한 요소만 사용. 하나도 평가 불가면
    InsufficientDataError.
    """
    p = prices.dropna()
    notes: list[str] = []
    passed = 0
    evaluated = 0

    for label, lookback in (("6개월", MOMENTUM_LOOKBACK_LONG_D), ("3개월", MOMENTUM_LOOKBACK_SHORT_D)):
        if len(p) > lookback:
            r = float(p.iloc[-1] / p.iloc[-(lookback + 1)] - 1) * 100
            ok = r > 0
            evaluated += 1
            passed += ok
            notes.append(f"{label} 수익률 {r:+.1f}% {'↑' if ok else '↓'}")

    if len(p) >= MA_LONG_WINDOW:
        ma = float(p.rolling(MA_LONG_WINDOW).mean().iloc[-1])
        last = float(p.iloc[-1])
        ok = last > ma
        evaluated += 1
        passed += ok
        notes.append(f"200일선 {'위' if ok else '아래'} (현재 {last:,.2f} vs MA {ma:,.2f})")

    if evaluated == 0:
        raise InsufficientDataError(
            f"모멘텀 평가에 데이터 {len(p)}개 — 최소 {MOMENTUM_LOOKBACK_SHORT_D + 1}개 필요",
            n_points=len(p), required=MOMENTUM_LOOKBACK_SHORT_D + 1,
        )
    return round(100 * passed / evaluated), notes


# ----------------------------------------------------------------------
# 2. 스크리닝 룰 (순수 함수)
# ----------------------------------------------------------------------


def apply_screen_rules(rows: list[dict]) -> list[dict]:
    """지표 행들에 발굴 룰 적용 → 통과 종목만 (통과 사유 포함).

    rows 의 각 dict: {"ticker", "pe", "roe", "fcf_yield"} (pe 는 None 가능).
    룰: ROE > 10% AND FCF yield > 0 AND (P/E 유효 시) P/E ≤ 유효 P/E 중간값.
    P/E 중간값 비교는 '업종 중간값' 의 워치리스트 근사 — 적자(P/E 없음) 종목은
    P/E 룰만 면제하고 나머지 룰로 평가.
    """
    valid_pes = [r["pe"] for r in rows if r.get("pe") and r["pe"] > 0]
    pe_median = statistics.median(valid_pes) if valid_pes else None

    passing: list[dict] = []
    for r in rows:
        reasons: list[str] = []
        if r.get("roe") is None or r["roe"] <= SCREEN_MIN_ROE:
            continue
        reasons.append(f"ROE {r['roe'] * 100:.1f}% > {SCREEN_MIN_ROE * 100:.0f}%")

        if r.get("fcf_yield") is None or r["fcf_yield"] <= SCREEN_MIN_FCF_YIELD:
            continue
        reasons.append(f"FCF yield {r['fcf_yield'] * 100:.1f}% 양수")

        pe = r.get("pe")
        if pe and pe > 0 and pe_median is not None:
            if pe > pe_median:
                continue
            reasons.append(f"P/E {pe:.1f} ≤ 중간값 {pe_median:.1f}")

        passing.append({**r, "reasons": reasons})

    passing.sort(key=lambda r: (r.get("pe") or float("inf")))
    return passing


# ----------------------------------------------------------------------
# 3. 알림 룰 (순수 함수 — 이전 상태를 인자로 받음)
# ----------------------------------------------------------------------


def regime_change_alert(current_label: str, prev_label: str | None) -> Alert | None:
    """시장 국면이 지난 실행과 달라졌으면 알림."""
    if prev_label is None or current_label == prev_label:
        return None
    return Alert("warning", "regime", f"시장 국면 전환: {prev_label} → {current_label}")


def drawdown_alerts(
    dd_pct: dict[str, float],
    prev_breached: dict[str, bool],
) -> tuple[list[Alert], dict[str, bool]]:
    """자산별 현재 낙폭의 임계 '돌파/회복' 알림 (신규 변화만 — 중복 알림 방지).

    Returns (알림 리스트, 새 breached 상태) — 새 상태는 다음 실행의 비교 기준.
    """
    alerts: list[Alert] = []
    new_state: dict[str, bool] = {}
    for asset, dd in dd_pct.items():
        breached = dd <= DRAWDOWN_ALERT_THRESHOLD_PCT
        was = prev_breached.get(asset, False)
        if breached and not was:
            alerts.append(Alert(
                "warning", "drawdown",
                f"{asset} 낙폭 {dd:+.1f}% — 임계 {DRAWDOWN_ALERT_THRESHOLD_PCT:.0f}% 돌파",
            ))
        elif not breached and was:
            alerts.append(Alert("info", "drawdown", f"{asset} 낙폭 {dd:+.1f}% — 임계 위로 회복"))
        new_state[asset] = breached
    return alerts, new_state


def vol_spike_alerts(
    vol_pct: dict[str, float],
    prev_vol_pct: dict[str, float],
) -> list[Alert]:
    """직전 실행 대비 연환산 변동성이 VOL_SPIKE_RATIO 배 이상이면 알림."""
    alerts: list[Alert] = []
    for ticker, vol in vol_pct.items():
        prev = prev_vol_pct.get(ticker)
        if prev and prev > 0 and vol / prev >= VOL_SPIKE_RATIO:
            alerts.append(Alert(
                "warning", "volatility",
                f"{ticker} 변동성 급등: {prev:.0f}% → {vol:.0f}% (×{vol / prev:.2f})",
            ))
    return alerts


# ----------------------------------------------------------------------
# 오케스트레이션 — fetch + state 관리는 여기서만
# ----------------------------------------------------------------------

_STATE_NS = "signals"
_STATE_KEY = "last_run"


def factor_scores(ticker: str) -> FactorScores:
    """한 종목의 팩터 점수. value/quality 데이터 미가용 시 0점 + note."""
    from src.data_fetcher import fetch_key_metrics, fetch_prices, fetch_quote

    closes = close_series(fetch_prices(ticker, period="1y"))
    momentum, notes = momentum_score(closes)

    quote: dict = {}
    metrics: dict = {}
    try:
        quote = fetch_quote(ticker)
    except DataFetchError as e:
        notes.append(f"quote 미가용 ({type(e).__name__}) — value 점수 불완전")
    try:
        metrics_df = fetch_key_metrics(ticker, limit=1)
        if not metrics_df.empty:
            metrics = metrics_df.iloc[-1].to_dict()
        else:
            notes.append("key-metrics 빈 응답 — value/quality 점수 불완전")
    except DataFetchError as e:
        notes.append(f"key-metrics 미가용 ({type(e).__name__}) — value/quality 점수 불완전")

    value = calculate_value_score(quote, metrics)
    quality = calculate_health_score(metrics)
    composite = round(
        momentum * COMPOSITE_WEIGHTS["momentum"]
        + value * COMPOSITE_WEIGHTS["value"]
        + quality * COMPOSITE_WEIGHTS["quality"]
    )
    return FactorScores(ticker, momentum, value, quality, composite, notes)


def screen_candidates(tickers: list[str]) -> list[dict]:
    """워치리스트에서 발굴 룰 통과 종목 추출 (key-metrics 는 7일 캐시 — 쿼터 보호)."""
    from src.data_fetcher import fetch_key_metrics
    from src.utils import pick_first

    rows: list[dict] = []
    for t in tickers:
        try:
            df = fetch_key_metrics(t, limit=1)
        except DataFetchError as e:
            logger.info("스크리닝 스킵 %s — %s", t, e)
            continue
        if df.empty:
            continue
        m = df.iloc[-1]
        ey = pick_first(m, ["earningsYield"])
        rows.append({
            "ticker": t,
            "pe": (1.0 / ey) if ey else None,   # FMP stable 은 P/E 를 yield 로 줌
            "roe": pick_first(m, ["returnOnEquity", "roe"]),
            "fcf_yield": pick_first(m, ["freeCashFlowYield"]),
        })
    return apply_screen_rules(rows)


def select_screened_tickers(n: int = 6, market: str = "US") -> list[str]:
    """스크리너가 뽑은 '오늘의 발굴 종목' 상위 N개 티커 (value+health 종합 점수순).

    매일 시장 상황에 따라 결과가 바뀜 → 고정 워치리스트 대신 다이제스트의
    팩터 표 주제로 사용. 스크리너 전체 실패 시 빈 리스트 (호출부가 폴백).
    """
    from src.screener import KR_WATCHLIST, US_WATCHLIST, screen_watchlist

    watchlist = list(US_WATCHLIST if market == "US" else KR_WATCHLIST)
    rows = screen_watchlist(watchlist, country_label=market)  # total_score 내림차순 정렬됨
    return [r["symbol"] for r in rows[:n]]


def generate_signal_report(
    tickers: tuple[str, ...] = DEFAULT_SIGNAL_TICKERS,
    screen_tickers: list[str] | None = None,
) -> SignalReport:
    """일일 신호 리포트 생성 + 다음 실행을 위한 상태 저장.

    screen_tickers=None 이면 스크리닝 섹션 생략 (빈 리스트).
    """
    from src.data_fetcher import fetch_prices
    from src.macro_analyzer import classify_regime, current_drawdown, fetch_cross_asset_panel

    store = get_storage()
    prev = store.get_state(_STATE_NS, _STATE_KEY) or {}
    first_run = not prev

    # --- 시장 수준 ---
    regime = classify_regime()
    panel = fetch_cross_asset_panel(period="6mo")
    dd = {k: float(v) for k, v in current_drawdown(panel).items()}

    # --- 종목 수준 ---
    factors: list[FactorScores] = []
    vols: dict[str, float] = {}
    for t in tickers:
        try:
            factors.append(factor_scores(t))
            closes = close_series(fetch_prices(t, period="1y"))  # 캐시 적중 — 추가 호출 없음
            returns = closes.pct_change().dropna()
            vols[t] = float(returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100)
        except (DataFetchError, InsufficientDataError) as e:
            logger.warning("팩터 스킵 %s — %s", t, e)

    # --- 알림 (이전 실행 대비 변화) ---
    # 낙폭 breach 상태는 알림 억제와 무관하게 항상 계산 → 상태 저장에 사용.
    _, dd_state = drawdown_alerts(dd, prev.get("dd_breached", {}))
    alerts: list[Alert] = []
    if not first_run:
        # 첫 실행은 비교 기준이 없으므로 변화 알림 일괄 생략 (regime/낙폭/변동성 일관 처리),
        # 상태만 시딩 → 다음 실행부터 실제 변화에만 알림.
        a = regime_change_alert(regime.regime, prev.get("regime"))
        if a:
            alerts.append(a)
        dd_alerts, _ = drawdown_alerts(dd, prev.get("dd_breached", {}))
        alerts.extend(dd_alerts)
        alerts.extend(vol_spike_alerts(vols, prev.get("vols", {})))

    candidates = screen_candidates(screen_tickers) if screen_tickers else []

    store.put_state(_STATE_NS, _STATE_KEY, {
        "regime": regime.regime,
        "dd_breached": dd_state,
        "vols": vols,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    return SignalReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        regime_label=regime.regime,
        factors=factors,
        candidates=candidates,
        alerts=alerts,
        first_run=first_run,
    )
