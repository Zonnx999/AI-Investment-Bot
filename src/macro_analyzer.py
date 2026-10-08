"""
macro_analyzer.py
=================
레이 달리오 식 거시 조망 모듈.

핵심 책임:
- 여러 자산군의 가격을 한 표로 모으는 cross-asset panel 빌더
- 자산 간 상관관계 매트릭스 (위험 분산 효과 측정)
- 자산별 연환산 변동성 (위험 크기)
- 시장 국면(Regime) 분류 — 규칙 기반, 설명 가능

Phase 4 에서 LLM 이 이 모듈의 함수들을 tool 로 호출하게 됩니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TypedDict
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.data_fetcher import fetch_macro, fetch_prices
from src.exceptions import (
    AnalysisError,
    ApiAuthError,
    ApiAuthorizationError,
    ConfigError,
    DataFetchError,
    DataValidationError,
    InsufficientDataError,
)
from src.utils import TRADING_DAYS_PER_YEAR, close_series

KST = ZoneInfo("Asia/Seoul")

# ----------------------------------------------------------------------
# 국면 분류 임계값 (7단계: 매직 넘버 → 상수. 튜닝/테스트 지점)
# ----------------------------------------------------------------------
YIELD_CURVE_LOOKBACK_DAYS = 60     # 장단기 금리차 비교 구간
YIELD_CURVE_CAUTION_DROP = 0.2     # 60일 평균 대비 이만큼(%p) 하락하면 '주의'
HY_SPREAD_LOOKBACK_DAYS = 252      # 하이일드 스프레드 1년 분포 구간
HY_SPREAD_STRESS_QUANTILE = 0.9    # 이 분위수 초과 = 신용 경색
HY_SPREAD_CAUTION_RATIO = 1.1      # 1년 중간값 대비 이 배율 초과 = 주의
JOBLESS_LOOKBACK_WEEKS = 52        # 실업수당 청구 1년(주간) 구간
JOBLESS_Z_BAD = 1.5                # z-score 가 이 이상이면 노동시장 악화
JOBLESS_Z_GOOD = -1.0              # z-score 가 이 이하면 고용 강세
from src.logger import get_logger

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# 자산군 패널: 어떤 티커를 한 화면에서 같이 볼지
# ----------------------------------------------------------------------

# 레이 달리오 All Weather 영감을 받은 7개 핵심 자산.
# 모두 yfinance 무료로 조회 가능.
DEFAULT_PANEL = {
    "S&P 500":          "SPY",      # 미국 주식
    "장기국채 (20Y+)":   "TLT",      # 듀레이션 자산
    "회사채 (투자등급)": "LQD",      # 크레딧
    "하이일드 채권":     "HYG",      # 신용 위험
    "금":               "GLD",
    "원자재":           "DBC",
    "비트코인":         "BTC-USD",
}


# 한국 시장 맥락 (KR 아침 스냅샷·알림용). DEFAULT_PANEL 은 market_summary·대시보드가
# 쓰므로 건드리지 않고, 알림 패널은 별도로 둔다.
KR_PANEL = {
    "KOSPI":   "^KS11",
    "KOSDAQ":  "^KQ11",
    "원/달러": "KRW=X",
}

# 낙폭·변동성 알림 대상 (미국 7 + 한국 지수 2). 환율은 낙폭 개념이 맞지 않아 제외.
ALERT_PANEL = {**DEFAULT_PANEL, "KOSPI": "^KS11", "KOSDAQ": "^KQ11"}


def fetch_panel_with_failures(
    tickers: dict[str, str],
    period: str = "2y",
) -> tuple[pd.DataFrame, list[str]]:
    """여러 자산의 종가 패널 + 조회 실패한 자산 라벨 목록.

    실패 자산은 경고 로그 후 스킵 — 호출부(신호 엔진)는 실패 목록을 사용자 메시지의
    data_issues 로 노출하고, 그 자산의 이전 알림 상태를 그대로 유지한다 (ALR-08).
    """
    frames = {}
    failures: list[str] = []
    for label, ticker in tickers.items():
        try:
            frames[label] = close_series(fetch_prices(ticker, period=period))
        except DataFetchError as e:
            logger.warning(
                "Cross-asset panel: '%s' (%s) 스킵 — %s", label, ticker, e
            )
            failures.append(label)
    return pd.DataFrame(frames).dropna(how="all"), failures


def fetch_cross_asset_panel(
    tickers: dict[str, str] | None = None,
    period: str = "6mo",
) -> pd.DataFrame:
    """여러 자산의 종가를 한 DataFrame 으로 모아 반환 (실패 자산은 스킵).

    Returns
    -------
    pd.DataFrame
        인덱스가 날짜, 컬럼이 자산 이름(한국어), 값이 Adj Close.
    """
    df, _ = fetch_panel_with_failures(tickers or DEFAULT_PANEL, period=period)
    return df


# ----------------------------------------------------------------------
# 통계 유틸
# ----------------------------------------------------------------------


def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """가격 → 일별 수익률 (%변화) — **자산별 자기 캘린더 기준**.

    패널이 혼합 캘린더(BTC 는 주말 거래, ETF 는 평일만)라 패널 전체에
    pct_change() 를 돌리면 ETF 의 월요일 수익률(금→월, 최고 분산 구간)이
    전부 NaN 이 되어 변동성·상관이 화·금 표본으로만 계산되는 편향 발생
    (pandas 3.x 는 fill_method=None 기본이라 조용히 유실). 컬럼별로
    자기 관측일 기준 변화율을 구해 이를 방지.
    """
    return prices.apply(lambda col: col.dropna().pct_change()).dropna(how="all")


def correlation_matrix(prices: pd.DataFrame) -> pd.DataFrame:
    """가격 패널을 받아 일별 수익률 기준 상관계수 매트릭스 반환."""
    return daily_returns(prices).corr()


def annualized_volatility(
    prices: pd.DataFrame, periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> pd.Series:
    """자산별 연환산 변동성(표준편차).

    주식 기준으로 1년 = 252 거래일을 가정. 암호화폐는 365 가 더 정확하지만
    일관성 위해 252 로 통일 (혼합 패널이라).
    """
    returns = daily_returns(prices)
    return returns.std() * np.sqrt(periods_per_year)


def cumulative_returns(prices: pd.DataFrame) -> pd.Series:
    """기간 시작 → 끝의 누적 수익률 (각 자산별 단일 값)."""
    if prices.empty:
        # 빈 패널에 .iloc[0] 는 raw IndexError — 도메인 예외로 (호출부 강등 가능)
        raise InsufficientDataError("가격 패널이 비어 있음 — 누적수익률 계산 불가",
                                    n_points=0, required=1)
    first = prices.bfill().iloc[0]
    last = prices.ffill().iloc[-1]
    return (last / first - 1) * 100


def rolling_correlation(
    prices: pd.DataFrame,
    asset_a: str,
    asset_b: str,
    window: int = 60,
) -> pd.Series:
    """두 자산 간 N일 이동 상관계수.

    스냅샷 상관계수는 평균만 보여주지만, 롤링 상관계수는 '시점에 따라
    상관관계가 어떻게 바뀌는지' 보여줍니다. 폭락장에서 상관계수가 1 로
    수렴하는 (분산 효과 붕괴) 현상을 포착하려면 이 함수가 필요합니다.
    """
    returns = daily_returns(prices)
    return returns[asset_a].rolling(window).corr(returns[asset_b])


def current_drawdown(prices: pd.DataFrame) -> pd.Series:
    """각 자산의 현재 낙폭 (기간 중 최고점 대비, %).

    예: -8.3 = 지금 가격이 기간 내 최고점 대비 8.3% 낮음.

    주의: 비트코인(24/7) 과 주식(평일 장중) 같이 거래시간이 다른 자산이
    한 패널에 섞이면 마지막 행에 NaN 이 끼어 결과가 NaN 으로 떨어짐.
    forward-fill 로 각 자산의 마지막 가용 종가까지 연장한 뒤 계산.
    """
    if prices.empty:
        raise InsufficientDataError("가격 패널이 비어 있음 — 낙폭 계산 불가",
                                    n_points=0, required=1)
    p = prices.ffill()
    peak = p.cummax()
    return ((p - peak) / peak).iloc[-1] * 100


def sharpe_ratio(
    prices: pd.DataFrame,
    risk_free_rate: float = 0.04,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pd.Series:
    """연환산 샤프 비율 = (수익률 − 무위험금리) / 변동성.

    1.0 이상이면 우수, 2.0 이상이면 매우 우수 (학계 기준).
    risk_free_rate 기본값 4% 는 미국 단기국채 근사치 (필요하면 수정).
    """
    returns = daily_returns(prices)
    daily_rf = risk_free_rate / periods_per_year
    excess = returns.mean() - daily_rf
    return excess / returns.std() * np.sqrt(periods_per_year)


# ----------------------------------------------------------------------
# 시장 국면 분류기 (규칙 기반)
# ----------------------------------------------------------------------


@dataclass
class IndicatorOutcome:
    """단일 거시 지표 평가 결과 (성공 케이스만).

    short_text : 근거 줄용 짧은 표기 (예: "금리차 +0.36%p(+1)") — Markdown 특수문자 없음.
    asof       : 마지막 관측일 (YYYY-MM-DD).
    stale_since: 일시 장애로 만료 캐시를 썼으면 그 캐시를 받은 날짜 (KST, YYYY-MM-DD).
    """

    series_id: str
    score_delta: int   # -1, 0, +1
    signal_text: str   # 사용자 리포트에 들어갈 한 줄
    raw_value: float   # 마지막 관측치
    short_text: str = ""
    asof: str = ""
    stale_since: str | None = None


# 사용자 메시지용 지표 한글명 — evaluator 이름(yield_curve 등)은 밑줄 때문에 legacy
# Markdown 을 깨뜨리므로 메시지에는 쓰지 않는다 (§4.10 #10). failures 는 대시보드 호환상 유지.
INDICATOR_NAMES_KO = {
    "T10Y2Y": "장단기 금리차",
    "BAMLH0A0HYM2": "하이일드 스프레드",
    "ICSA": "신규 실업수당 청구",
}
_SHORT_NAMES = {"yield_curve": "금리차", "hy_spread": "HY", "jobless_claims": "실업수당"}

REGIME_HOLD_LABEL = "⚪ 판정 보류"

# stale-if-error: FRED 일시 장애 때 만료 캐시를 이 기간까지 대신 사용
MACRO_STALE_MAX_AGE = timedelta(days=7)
# 관측 노후 가드 — 마지막 관측이 이보다 오래되면 data_issues 로 표기 (판정은 유지)
OBS_MAX_AGE_DAYS = {"T10Y2Y": 10, "BAMLH0A0HYM2": 10, "ICSA": 21}

_MD_UNSAFE = str.maketrans({c: "" for c in "_*`[]\\"})


def regime_label_for_score(score: int) -> str:
    """국면 점수(-3~+3) → 라벨 (지표가 전부 있을 때만 의미 있음)."""
    if score >= 2:
        return "🟢 위험선호 (Risk-on)"
    if score == 1:
        return "🟡 약한 위험선호"
    if score == 0:
        return "⚪ 중립 / 혼조"
    if score == -1:
        return "🟠 약한 위험회피"
    return "🔴 위험회피 (Risk-off / 침체 경고)"


def _fmt_delta(d: int) -> str:
    return "0" if d == 0 else f"{d:+d}"


@dataclass
class RegimeReport:
    """시장 국면 분석 결과.

    signals : 성공한 지표들의 사용자용 한 줄 설명만 모음 (에러 메시지 섞이지 않음).
    failures: 평가에 실패한 지표 이름의 리스트(yield_curve 등). 사용자에게는 별도로 표시.
    available/total: 평가에 성공한 지표 수 / 전체 지표 수. available < total 이면
      국면 라벨은 '판정 보류' (부분합 점수로 라벨을 내면 실패가 '중립' 으로 둔갑함).
    stale   : 만료 캐시(stale-if-error)로 평가한 series_id 목록 (available 에 포함).
    asof    : series_id → 마지막 관측일.
    """

    regime: str
    score: int                                       # -3 ~ +3
    signals: list[str] = field(default_factory=list)
    raw: dict[str, float] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    available: int = 0
    total: int = 3
    stale: list[str] = field(default_factory=list)
    asof: dict[str, str] = field(default_factory=dict)
    outcomes: list[IndicatorOutcome] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.available == self.total

    def reason_line(self) -> str:
        """국면 줄 바로 아래 근거 1줄 (들여쓰기 없음 — 렌더러가 붙임).

        예: "근거: 금리차 +0.36%p(+1) · HY 3.24%(0) · 실업수당 z+0.3(0) — 3/3 지표, 10-07 기준"
        만료 캐시 지표는 "HY 3.24%(0, 10-06 캐시)", 실패 지표는 "금리차 실패".
        Markdown 특수문자(_ * ` [ ])는 제거해 텔레그램 legacy Markdown 을 깨지 않음.
        """
        parts: list[str] = []
        for o in self.outcomes:
            text = o.short_text or o.series_id
            if o.stale_since:
                mark = f"{o.stale_since[5:]} 캐시"
                text = f"{text[:-1]}, {mark})" if text.endswith(")") else f"{text} ({mark})"
            parts.append(text)
        parts.extend(f"{_SHORT_NAMES.get(f, '지표')} 실패" for f in self.failures)
        tail = f"{self.available}/{self.total} 지표"
        if self.asof:
            tail += f", {max(self.asof.values())[5:]} 기준"
        body = " · ".join(parts) if parts else "지표 없음"
        return f"근거: {body} — {tail}".translate(_MD_UNSAFE)

    def __str__(self) -> str:
        rng = self.total
        lines = [f"국면 판정: {self.regime}  (점수 {self.score:+d} / -{rng} ~ +{rng})"]
        lines.append("")
        lines.append("근거:")
        lines.extend(f"  · {s}" for s in self.signals)
        if self.failures:
            lines.append("")
            lines.append(f"평가 실패 (로그 참조): {', '.join(self.failures)}")
        return "\n".join(lines)


# ---- 개별 지표 평가 (순수 함수: 시계열 → IndicatorOutcome) ----
# fetch 는 classify_regime 이 _REGIME_INPUTS 를 돌며 수행 (테스트는 합성 시리즈로 직접 호출).


def _require(s: pd.Series, series_id: str) -> pd.Series:
    s = s.dropna()
    if s.empty:
        raise InsufficientDataError(f"{series_id} 시계열이 비어 있음", n_points=0, required=1)
    return s


def _asof(s: pd.Series) -> str:
    return pd.Timestamp(s.index[-1]).strftime("%Y-%m-%d")


def _eval_yield_curve(s: pd.Series) -> IndicatorOutcome:
    """장단기 금리차 (T10Y2Y) — 음수면 경기침체 경고."""
    s = _require(s, "T10Y2Y").tail(YIELD_CURVE_LOOKBACK_DAYS)
    latest = float(s.iloc[-1])
    avg_60d = float(s.mean())

    if latest < 0:
        d, text = -1, f"장단기 금리차 {latest:+.2f}%p 로 역전 상태 — 침체 경고 (-1)"
    elif latest < avg_60d - YIELD_CURVE_CAUTION_DROP:
        d, text = 0, f"장단기 금리차 {latest:+.2f}%p, 60일 평균 대비 하락 — 주의 (0)"
    else:
        d, text = 1, f"장단기 금리차 {latest:+.2f}%p 로 정상 우상향 — 위험선호 (+1)"
    return IndicatorOutcome(
        "T10Y2Y", d, text, latest,
        short_text=f"금리차 {latest:+.2f}%p({_fmt_delta(d)})", asof=_asof(s),
    )


def _eval_hy_spread(s: pd.Series) -> IndicatorOutcome:
    """하이일드 스프레드 — 급등하면 신용 경색."""
    s = _require(s, "BAMLH0A0HYM2").tail(HY_SPREAD_LOOKBACK_DAYS)
    latest = float(s.iloc[-1])
    median_1y = float(s.median())
    p90 = float(s.quantile(HY_SPREAD_STRESS_QUANTILE))

    if latest > p90:
        d, text = -1, f"하이일드 스프레드 {latest:.2f}%, 1년 90분위 초과 — 신용 경색 (-1)"
    elif latest > median_1y * HY_SPREAD_CAUTION_RATIO:
        d, text = 0, f"하이일드 스프레드 {latest:.2f}%, 중간값 대비 상승 — 주의 (0)"
    else:
        d, text = 1, f"하이일드 스프레드 {latest:.2f}% 안정 — 위험선호 (+1)"
    return IndicatorOutcome(
        "BAMLH0A0HYM2", d, text, latest,
        short_text=f"HY {latest:.2f}%({_fmt_delta(d)})", asof=_asof(s),
    )


def _eval_jobless_claims(s: pd.Series) -> IndicatorOutcome:
    """주간 신규 실업수당 청구 — 급등하면 노동시장 악화."""
    s = _require(s, "ICSA").tail(JOBLESS_LOOKBACK_WEEKS)
    latest = float(s.iloc[-1])
    mean_1y = float(s.mean())
    std_1y = float(s.std())
    z = (latest - mean_1y) / std_1y if std_1y > 0 else 0.0

    if z > JOBLESS_Z_BAD:
        d, tag = -1, "노동시장 악화 (-1)"
    elif z < JOBLESS_Z_GOOD:
        d, tag = 1, "고용 강세 (+1)"
    else:
        d, tag = 0, "중립 (0)"
    return IndicatorOutcome(
        "ICSA", d, f"신규 실업수당 청구 {int(latest):,}건, z={z:+.1f} — {tag}", latest,
        short_text=f"실업수당 z{z:+.1f}({_fmt_delta(d)})", asof=_asof(s),
    )


# (failures 에 쓰는 이름, FRED series_id, 평가 함수) — 이름은 대시보드 호환상 유지
_REGIME_INPUTS = (
    ("yield_curve", "T10Y2Y", _eval_yield_curve),
    ("hy_spread", "BAMLH0A0HYM2", _eval_hy_spread),
    ("jobless_claims", "ICSA", _eval_jobless_claims),
)


def _fetch_resilient(series_id: str) -> tuple[pd.Series, str | None]:
    """fetch_macro + stale-if-error. 반환 (시계열, stale_since | None).

    설정·ID 문제(DataValidationError / ApiAuthError / ApiAuthorizationError / ConfigError)는
    캐시로 가리지 않고 그대로 raise — 고쳐야 할 문제를 7일간 숨기면 안 됨.
    그 외 DataFetchError(5xx·429·타임아웃·연결)는 MACRO_STALE_MAX_AGE 이내의 만료
    캐시로 대체하고 stale_since(캐시 받은 KST 날짜)를 돌려준다. 캐시도 없으면 raise.
    """
    try:
        return fetch_macro(series_id), None
    except (DataValidationError, ApiAuthError, ApiAuthorizationError):
        raise
    except DataFetchError as e:
        stale_fn = getattr(fetch_macro, "stale", None)
        hit = stale_fn(series_id, max_age=MACRO_STALE_MAX_AGE) if stale_fn else None
        if hit is None:
            raise
        series, fetched_at = hit
        since = fetched_at.astimezone(KST).strftime("%Y-%m-%d")
        logger.warning(
            "FRED %s 조회 실패 (%s) — %s 에 받은 만료 캐시로 대체 (stale-if-error)",
            series_id, type(e).__name__, since,
        )
        return series, since


def classify_regime() -> RegimeReport:
    """3개 거시 지표로 시장 국면을 단순 분류.

    각 지표는 fetch(_fetch_resilient) → 순수 평가 함수. 실패는 failures 리스트에
    분리 기록 → 사용자 리포트의 signals 에 에러 메시지 섞이지 않음.

    각 신호별로 -1 / 0 / +1 점을 주고 합산 → 국면 라벨링. 단 **지표가 하나라도
    빠지면 라벨은 '⚪ 판정 보류 (지표 n/3)'** — 부분합으로 고정 구간 라벨을 내면
    실패가 '중립/위험선호' 로 둔갑한다 (score 는 부분합 그대로 보존).
    """
    score = 0
    signals: list[str] = []
    raw: dict[str, float] = {}
    failures: list[str] = []
    stale: list[str] = []
    asof: dict[str, str] = {}
    outcomes: list[IndicatorOutcome] = []

    for name, series_id, evaluator in _REGIME_INPUTS:
        try:
            series, stale_since = _fetch_resilient(series_id)
            outcome = evaluator(series)
        except (ConfigError, DataFetchError, AnalysisError):
            # ConfigError 포함 이유: FRED 키 미설정 시 fetch_macro 가
            # MissingApiKeyError(ConfigError 계열) 를 던지는데, 이것도
            # "지표 평가 실패" 로 failures 에 기록돼야 리포트가 안 죽음
            logger.exception(
                "classify_regime: %s 평가 실패 — failures 에 기록", name
            )
            failures.append(name)
            continue
        outcome.stale_since = stale_since
        if stale_since:
            stale.append(series_id)
        score += outcome.score_delta
        signals.append(outcome.signal_text)
        raw[outcome.series_id] = outcome.raw_value
        asof[outcome.series_id] = outcome.asof
        outcomes.append(outcome)

    total = len(_REGIME_INPUTS)
    available = len(outcomes)
    if available == total:
        regime = regime_label_for_score(score)
    else:
        regime = f"{REGIME_HOLD_LABEL} (지표 {available}/{total})"

    return RegimeReport(
        regime=regime, score=score, signals=signals, raw=raw, failures=failures,
        available=available, total=total, stale=stale, asof=asof, outcomes=outcomes,
    )


def failed_indicator_names(report: RegimeReport) -> list[str]:
    """failures(evaluator 이름) → 사용자 메시지용 한글 지표명."""
    by_name = {name: INDICATOR_NAMES_KO.get(sid, sid) for name, sid, _ in _REGIME_INPUTS}
    return [by_name.get(f, "거시 지표") for f in report.failures]


def observation_age_issues(report: RegimeReport, today: date) -> list[str]:
    """관측 노후 가드 — 마지막 관측이 OBS_MAX_AGE_DAYS 보다 오래된 지표 (판정은 유지).

    예: "T10Y2Y 최신 관측 12일 전". series_id 는 밑줄이 없어 Markdown 안전.
    """
    issues: list[str] = []
    for series_id, asof in report.asof.items():
        try:
            age = (today - date.fromisoformat(asof)).days
        except ValueError:
            continue
        if age > OBS_MAX_AGE_DAYS.get(series_id, 10):
            issues.append(f"{series_id} 최신 관측 {age}일 전")
    return issues


# ----------------------------------------------------------------------
# 한 번에 보고서 만들기
# ----------------------------------------------------------------------


class MarketSummary(TypedDict):
    """market_summary() 의 반환 스키마 (런타임은 일반 dict)."""

    period: str
    prices: pd.DataFrame
    cumulative_returns_pct: pd.Series
    correlation: pd.DataFrame
    annualized_vol_pct: pd.Series
    current_drawdown_pct: pd.Series
    sharpe_ratio: pd.Series
    regime: RegimeReport


def market_summary(period: str = "6mo") -> MarketSummary:
    """패널 + 상관관계 + 변동성 + 낙폭 + 샤프 + 국면을 한 dict 으로 묶어서 반환."""
    prices = fetch_cross_asset_panel(period=period)
    return {
        "period": period,
        "prices": prices,
        "cumulative_returns_pct": cumulative_returns(prices),
        "correlation": correlation_matrix(prices),
        "annualized_vol_pct": annualized_volatility(prices) * 100,
        "current_drawdown_pct": current_drawdown(prices),
        "sharpe_ratio": sharpe_ratio(prices),
        "regime": classify_regime(),
    }
