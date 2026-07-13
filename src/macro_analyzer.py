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
from typing import TypedDict

import numpy as np
import pandas as pd

from src.data_fetcher import fetch_macro, fetch_prices
from src.exceptions import ConfigError, DataFetchError, InsufficientDataError
from src.utils import TRADING_DAYS_PER_YEAR, close_series

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


def fetch_cross_asset_panel(
    tickers: dict[str, str] | None = None,
    period: str = "6mo",
) -> pd.DataFrame:
    """여러 자산의 종가를 한 DataFrame 으로 모아 반환.

    Returns
    -------
    pd.DataFrame
        인덱스가 날짜, 컬럼이 자산 이름(한국어), 값이 Adj Close.
    """
    panel = tickers or DEFAULT_PANEL
    frames = {}
    for label, ticker in panel.items():
        try:
            df = fetch_prices(ticker, period=period)
        except DataFetchError as e:
            logger.warning(
                "Cross-asset panel: '%s' (%s) 스킵 — %s", label, ticker, e
            )
            continue
        frames[label] = close_series(df)
    return pd.DataFrame(frames).dropna(how="all")


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
    """단일 거시 지표 평가 결과 (성공 케이스만)."""

    series_id: str
    score_delta: int   # -1, 0, +1
    signal_text: str   # 사용자 리포트에 들어갈 한 줄
    raw_value: float   # 마지막 관측치


@dataclass
class RegimeReport:
    """시장 국면 분석 결과.

    signals : 성공한 지표들의 사용자용 한 줄 설명만 모음 (에러 메시지 섞이지 않음).
    failures: 평가에 실패한 지표 ID 의 리스트. 사용자에게는 별도로 표시.
    """

    regime: str
    score: int                                       # -3 ~ +3
    signals: list[str] = field(default_factory=list)
    raw: dict[str, float] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        # 점수 범위는 지표 개수에서 동적 계산 (Phase 2 에서 VIX 등 추가 시 자동 반영)
        rng = len(_REGIME_EVALUATORS)
        lines = [f"국면 판정: {self.regime}  (점수 {self.score:+d} / -{rng} ~ +{rng})"]
        lines.append("")
        lines.append("근거:")
        lines.extend(f"  · {s}" for s in self.signals)
        if self.failures:
            lines.append("")
            lines.append(f"평가 실패 (로그 참조): {', '.join(self.failures)}")
        return "\n".join(lines)


# ---- 개별 지표 평가 헬퍼 ----
# 각 헬퍼는 IndicatorOutcome 을 반환하거나, 데이터 호출 실패 시
# DataFetchError 를 그대로 bubble up. 호출자(classify_regime)가
# 잡아서 failures 에 기록.


def _eval_yield_curve() -> IndicatorOutcome:
    """장단기 금리차 (T10Y2Y) — 음수면 경기침체 경고."""
    s = fetch_macro("T10Y2Y").tail(YIELD_CURVE_LOOKBACK_DAYS)
    latest = float(s.iloc[-1])
    avg_60d = float(s.mean())

    if latest < 0:
        return IndicatorOutcome(
            "T10Y2Y", -1,
            f"장단기 금리차 {latest:+.2f}%p 로 역전 상태 — 침체 경고 (-1)",
            latest,
        )
    if latest < avg_60d - YIELD_CURVE_CAUTION_DROP:
        return IndicatorOutcome(
            "T10Y2Y", 0,
            f"장단기 금리차 {latest:+.2f}%p, 60일 평균 대비 하락 — 주의 (0)",
            latest,
        )
    return IndicatorOutcome(
        "T10Y2Y", 1,
        f"장단기 금리차 {latest:+.2f}%p 로 정상 우상향 — 위험선호 (+1)",
        latest,
    )


def _eval_hy_spread() -> IndicatorOutcome:
    """하이일드 스프레드 — 급등하면 신용 경색."""
    s = fetch_macro("BAMLH0A0HYM2").tail(HY_SPREAD_LOOKBACK_DAYS)
    latest = float(s.iloc[-1])
    median_1y = float(s.median())
    p90 = float(s.quantile(HY_SPREAD_STRESS_QUANTILE))

    if latest > p90:
        return IndicatorOutcome(
            "BAMLH0A0HYM2", -1,
            f"하이일드 스프레드 {latest:.2f}%, 1년 90분위 초과 — 신용 경색 (-1)",
            latest,
        )
    if latest > median_1y * HY_SPREAD_CAUTION_RATIO:
        return IndicatorOutcome(
            "BAMLH0A0HYM2", 0,
            f"하이일드 스프레드 {latest:.2f}%, 중간값 대비 상승 — 주의 (0)",
            latest,
        )
    return IndicatorOutcome(
        "BAMLH0A0HYM2", 1,
        f"하이일드 스프레드 {latest:.2f}% 안정 — 위험선호 (+1)",
        latest,
    )


def _eval_jobless_claims() -> IndicatorOutcome:
    """주간 신규 실업수당 청구 — 급등하면 노동시장 악화."""
    s = fetch_macro("ICSA").tail(JOBLESS_LOOKBACK_WEEKS)
    latest = float(s.iloc[-1])
    mean_1y = float(s.mean())
    std_1y = float(s.std())
    z = (latest - mean_1y) / std_1y if std_1y > 0 else 0

    if z > JOBLESS_Z_BAD:
        return IndicatorOutcome(
            "ICSA", -1,
            f"신규 실업수당 청구 {int(latest):,}건, z={z:+.1f} — 노동시장 악화 (-1)",
            latest,
        )
    if z < JOBLESS_Z_GOOD:
        return IndicatorOutcome(
            "ICSA", 1,
            f"신규 실업수당 청구 {int(latest):,}건, z={z:+.1f} — 고용 강세 (+1)",
            latest,
        )
    return IndicatorOutcome(
        "ICSA", 0,
        f"신규 실업수당 청구 {int(latest):,}건, z={z:+.1f} — 중립 (0)",
        latest,
    )


_REGIME_EVALUATORS = (
    _eval_yield_curve,
    _eval_hy_spread,
    _eval_jobless_claims,
)


def classify_regime() -> RegimeReport:
    """3개 거시 지표로 시장 국면을 단순 분류.

    각 평가 헬퍼는 IndicatorOutcome 또는 DataFetchError. 데이터 실패는
    failures 리스트에 분리해 기록 → 사용자 리포트의 signals 에 에러 메시지
    섞이지 않음.

    각 신호별로 -1 / 0 / +1 점을 주고 합산 → 국면 라벨링.
    """
    score = 0
    signals: list[str] = []
    raw: dict[str, float] = {}
    failures: list[str] = []

    for evaluator in _REGIME_EVALUATORS:
        try:
            outcome = evaluator()
        except (ConfigError, DataFetchError):
            # ConfigError 포함 이유: FRED 키 미설정 시 fetch_macro 가
            # MissingApiKeyError(ConfigError 계열) 를 던지는데, 이것도
            # "지표 평가 실패" 로 failures 에 기록돼야 리포트가 안 죽음
            # series_id 가 헬퍼 이름에 1:1 매핑되지는 않아 evaluator.__name__ 로 식별
            failed_name = evaluator.__name__.replace("_eval_", "")
            logger.exception(
                "classify_regime: %s 평가 실패 — failures 에 기록", failed_name
            )
            failures.append(failed_name)
            continue
        score += outcome.score_delta
        signals.append(outcome.signal_text)
        raw[outcome.series_id] = outcome.raw_value

    # --- 종합 라벨 ---
    if score >= 2:
        regime = "🟢 위험선호 (Risk-on)"
    elif score == 1:
        regime = "🟡 약한 위험선호"
    elif score == 0:
        regime = "⚪ 중립 / 혼조"
    elif score == -1:
        regime = "🟠 약한 위험회피"
    else:
        regime = "🔴 위험회피 (Risk-off / 침체 경고)"

    return RegimeReport(
        regime=regime, score=score, signals=signals, raw=raw, failures=failures
    )


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
