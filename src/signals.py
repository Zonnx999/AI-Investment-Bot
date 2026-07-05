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
from src.screener import calculate_health_score, calculate_value_score, latest_fundamentals
from src.storage import get_storage
from src.utils import TRADING_DAYS_PER_YEAR, close_series

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 상수 (튜닝 지점)
# ----------------------------------------------------------------------
DEFAULT_SIGNAL_TICKERS = ("CPNG", "NVDA")

MOMENTUM_LOOKBACK_LONG_D = 126    # ~6개월
MOMENTUM_LOOKBACK_SHORT_D = 63    # ~3개월
MOMENTUM_SKIP_D = 21             # 스킵-먼스 (최근 1개월 제외, Jegadeesh-Titman)
MOMENTUM_12M_D = 252             # ~12개월
MA_LONG_WINDOW = 200              # 장기 추세선

# 종합 점수 가중치 — 합 1.0 (4팩터: 모멘텀/밸류/퀄리티/로우볼)
COMPOSITE_WEIGHTS = {"momentum": 0.25, "value": 0.25, "quality": 0.25, "low_vol": 0.25}

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
    low_vol: int = 0   # 4번째 팩터 (Low Volatility)
    vol_pct: float | None = None   # 연환산 변동성 % — 단일 계산 지점 (변동성 급등 알림 재사용)


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
    """가격 시리즈 → 모멘텀 점수 (0~100, 연속) + 근거.

    연속 점수 (구버전 0/33/67/100 → 세분화):
      A. 스킵-먼스 모멘텀 (70%): 12개월-1개월 수익률 (Jegadeesh-Titman 1993 —
         최근 1개월 제외가 단기 평균회귀 노이즈를 걷어내 신호를 강화).
         데이터 부족 시 3개월 수익률로 대체.
      B. 200일선 위치 (30%): 이동평균 대비 ±% 거리.
    매핑: 0% → 50점, +30%(A)/+10%(B) → ~83점 (선형, 0~100 클램프).
    """
    p = prices.dropna()
    if len(p) < MOMENTUM_LOOKBACK_SHORT_D + 1:
        raise InsufficientDataError(
            f"모멘텀 평가에 데이터 {len(p)}개 — 최소 {MOMENTUM_LOOKBACK_SHORT_D + 1}개 필요",
            n_points=len(p), required=MOMENTUM_LOOKBACK_SHORT_D + 1,
        )
    notes: list[str] = []
    scores: list[float] = []
    weights: list[float] = []
    SKIP, LONG = MOMENTUM_SKIP_D, MOMENTUM_12M_D  # 모듈 상수 (테스트 오버라이드 가능)

    if len(p) > LONG + SKIP:
        r = float(p.iloc[-SKIP] / p.iloc[-(LONG + SKIP)] - 1) * 100
        scores.append(max(0.0, min(100.0, 50.0 + r * (50.0 / 30.0))))
        weights.append(0.70)
        notes.append(f"12-1개월(skip) 수익률 {r:+.1f}% {'↑' if r > 0 else '↓'}")
    else:
        r = float(p.iloc[-1] / p.iloc[-(MOMENTUM_LOOKBACK_SHORT_D + 1)] - 1) * 100
        scores.append(max(0.0, min(100.0, 50.0 + r * (50.0 / 15.0))))
        weights.append(0.70)
        notes.append(f"3개월 수익률 {r:+.1f}% (데이터 부족, 단기 대체)")

    if len(p) >= MA_LONG_WINDOW:
        ma = float(p.rolling(MA_LONG_WINDOW).mean().iloc[-1])
        last = float(p.iloc[-1])
        pct = (last / ma - 1) * 100
        scores.append(max(0.0, min(100.0, 50.0 + pct * (50.0 / 10.0))))
        weights.append(0.30)
        notes.append(f"200일선 {'위' if pct > 0 else '아래'} {pct:+.1f}%")

    final = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
    return round(final), notes


def annualized_vol_pct(prices: pd.Series) -> float | None:
    """연환산 변동성 % — 팩터/알림이 공유하는 **단일 계산 지점**.

    252(거래일) 연환산 — 주식 기준. 크립토(365일 거래)엔 적용되지 않음
    (크립토는 calculate_crypto_scores 사용). 추후 크립토가 이 경로를 타면 365 필요.
    수익률이 비면(가격 <2개) None.
    """
    returns = prices.dropna().pct_change().dropna()
    if returns.empty:
        return None
    return float(returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100)


def low_vol_score(prices: pd.Series) -> tuple[int, list[str], float | None]:
    """Low Volatility 팩터 (0~100) — 변동성 낮을수록 높음.

    Robeco/MSCI 가 독립적 알파 원천으로 확인한 팩터. 연환산 변동성 기준
    ~15% → 100, ~30% → 50, ~60% → 25 (score = clip(30/vol × 50)).

    Returns (점수, 근거, vol_pct) — vol_pct 는 데이터 부족 시 None (중립 50점).
    호출부(factor_scores)가 vol_pct 를 FactorScores 에 실어 변동성 급등 알림이
    재계산 없이 재사용 (계산 중복 제거).
    """
    p = prices.dropna()
    if len(p) < MOMENTUM_LOOKBACK_SHORT_D:
        return 50, ["변동성 데이터 부족 — 중립(50)"], None
    vol = annualized_vol_pct(p)
    if vol is None:
        return 50, ["변동성 데이터 부족 — 중립(50)"], None
    score = max(0.0, min(100.0, (30.0 / max(vol, 1.0)) * 50.0))
    return round(score), [f"연환산 변동성 {vol:.1f}% → Low Vol {round(score)}"], vol


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

    # 정렬: 저PER 우선이되 같은 PER 대역이면 고ROE 우선 (PER만 보면 value-trap 위험).
    # (이미 ROE>10% 필터를 통과한 후보군이라 1차 가드는 있음)
    passing.sort(key=lambda r: (r.get("pe") or float("inf"), -(r.get("roe") or 0.0)))
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
    """한 종목의 4팩터 점수 (모멘텀/밸류/퀄리티/로우볼). 데이터 미가용 시 0점 + note."""
    from src.data_fetcher import fetch_prices, fetch_quote

    closes = close_series(fetch_prices(ticker, period="1y"))
    momentum, notes = momentum_score(closes)
    low_vol, lv_notes, vol_pct = low_vol_score(closes)
    notes.extend(lv_notes)

    quote: dict = {}
    metrics: dict = {}
    try:
        quote = fetch_quote(ticker)
    except DataFetchError as e:
        notes.append(f"quote 미가용 ({type(e).__name__}) — value 점수 불완전")
    try:
        metrics = latest_fundamentals(ticker)   # key-metrics + ratios 병합
        if not metrics:
            notes.append("fundamentals 빈 응답 — value/quality 점수 불완전")
    except DataFetchError as e:
        notes.append(f"fundamentals 미가용 ({type(e).__name__}) — value/quality 점수 불완전")

    value = calculate_value_score(quote, metrics)
    quality = calculate_health_score(metrics)
    composite = round(
        momentum * COMPOSITE_WEIGHTS["momentum"]
        + value * COMPOSITE_WEIGHTS["value"]
        + quality * COMPOSITE_WEIGHTS["quality"]
        + low_vol * COMPOSITE_WEIGHTS["low_vol"]
    )
    return FactorScores(ticker, momentum, value, quality, composite, notes,
                        low_vol=low_vol, vol_pct=vol_pct)


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
        # FMP stable 은 P/E 를 earningsYield 로 줌 → 역수. 음수 yield(적자)의 역수는
        # '음수 PER' 이라는 무의미한 저평가 신호가 되므로 **명시적으로 None** —
        # apply_screen_rules 가 '데이터 없음(P/E 룰 면제)' 경로로 처리 (§4.10 #5).
        rows.append({
            "ticker": t,
            "pe": (1.0 / ey) if (ey and ey > 0) else None,
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
    market: str = "US",
) -> SignalReport:
    """일일 신호 리포트 생성 + 다음 실행을 위한 상태 저장.

    screen_tickers=None 이면 스크리닝 섹션 생략 (빈 리스트).
    market: 변화 알림 baseline 을 시장별로 분리 — KR/US 다이제스트가 각자 별도
      timeline 으로 돌아 서로의 vols/regime baseline 을 덮어쓰지 않도록. US 는 기존
      키 유지(연속성), 그 외 시장은 suffix. tickers=() 면 팩터는 비고 국면/알림만.
    """
    from src.macro_analyzer import classify_regime, current_drawdown, fetch_cross_asset_panel

    store = get_storage()
    state_key = _STATE_KEY if market == "US" else f"{_STATE_KEY}:{market}"
    prev = store.get_state(_STATE_NS, state_key) or {}
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
            fs = factor_scores(t)
            factors.append(fs)
            if fs.vol_pct is not None:   # 팩터 계산 때 이미 산출 — 재계산/재fetch 없음
                vols[t] = fs.vol_pct
        except (DataFetchError, InsufficientDataError) as e:
            logger.warning("팩터 스킵 %s — %s", t, e)

    # --- 알림 (이전 실행 대비 변화) ---
    # drawdown_alerts 는 순수 함수라 한 번만 호출 — 알림과 새 breach 상태를 함께 받고,
    # 첫 실행이면 알림만 버리고 상태는 시딩에 사용 (구현 전엔 같은 인자로 두 번 호출했었음).
    dd_alerts, dd_state = drawdown_alerts(dd, prev.get("dd_breached", {}))
    alerts: list[Alert] = []
    if not first_run:
        # 첫 실행은 비교 기준이 없으므로 변화 알림 일괄 생략 (regime/낙폭/변동성 일관 처리),
        # 상태만 시딩 → 다음 실행부터 실제 변화에만 알림.
        a = regime_change_alert(regime.regime, prev.get("regime"))
        if a:
            alerts.append(a)
        alerts.extend(dd_alerts)
        alerts.extend(vol_spike_alerts(vols, prev.get("vols", {})))

    candidates = screen_candidates(screen_tickers) if screen_tickers else []

    store.put_state(_STATE_NS, state_key, {
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
