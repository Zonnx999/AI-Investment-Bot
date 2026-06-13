"""
src/predictors.py
=================
Phase 6 — Alternative Data & Predictive Models.

선행지표(leading indicator)와 목표 자산(target) 사이의 lead-lag 관계를
찾아 "N개월 뒤 방향"을 예측합니다. 사용자가 가장 강조한 부분 —
가격만 보지 않고 거시·대체 데이터로 펀더멘털을 선행 예측.

현재 구현된 관계:
  · M2 통화량 증가율 → 비트코인 수익률      (유동성 → 위험자산)
  · 한국 수출 증가율 → 반도체 ETF 수익률    (한국 수출은 글로벌 반도체 사이클 선행)
  · 건축허가 증가율 → 주택건설 ETF(XHB)     (허가 → 착공 → 매출, 명확한 선행)
  · 소비자심리 증가율 → 소비재 ETF(XLY)     (심리 → 지출)
  · 달러지수 증가율 → 신흥국 ETF(EEM)       (달러 강세 → 신흥국 역풍, 음의 관계)
  · 구리/금 비율 증가율 → 경기민감(SPY)     ('닥터 코퍼' 실물경기 체온계)
  · 위키피디아 관심 → 비트코인              (대중 관심 — 대체 데이터, 키 불필요)

설계
----
판정 로직은 **순수 함수** (월간 시리즈 2개를 인자로 → 오프라인 테스트).
fetch 는 predict_* 오케스트레이터만 담당.

⚠️ 한계 (정직하게): 이건 상관관계 기반 통계 모델이지 인과·보장이 아닙니다.
표본이 작고(월간 수년) 과최적화 위험이 있어, lag 탐색 결과와 R² 를 항상
함께 보여줘 사용자가 신뢰도를 직접 판단하게 합니다. Phase 10 백테스트로 검증 예정.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.exceptions import InsufficientDataError
from src.logger import get_logger

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 상수 (튜닝 지점)
# ----------------------------------------------------------------------
DEFAULT_MAX_LAG_MONTHS = 12      # lead-lag 탐색 범위 (개월)
MIN_OVERLAP_MONTHS = 24          # 회귀에 필요한 최소 겹침 표본
YOY_PERIODS = 12                 # 전년동월대비 (월간 시리즈 기준)
STRONG_R2 = 0.30                 # 이 이상이면 '참고할 만한' 관계 (월간 거시 기준 현실적 임계)


# ----------------------------------------------------------------------
# 데이터 구조
# ----------------------------------------------------------------------


@dataclass
class LeadLagResult:
    """선행지표 → 목표 자산 lead-lag 회귀 결과."""

    leading_name: str
    target_name: str
    best_lag_months: int            # 선행지표가 목표를 며칠 앞서는지 (개월)
    correlation: float              # best_lag 에서의 상관계수
    r_squared: float                # 회귀 설명력
    slope: float
    intercept: float
    n_obs: int                      # 회귀에 사용된 표본 수
    latest_leading_value: float     # 가장 최근 선행지표 값 (예측 입력)
    predicted_change_pct: float     # best_lag 개월 뒤 목표 자산 예상 변화율(%)
    direction: str                  # "상승 ↑" / "하락 ↓" / "중립 →"
    reliable: bool                  # r_squared >= STRONG_R2
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 순수 변환 함수
# ----------------------------------------------------------------------


def to_monthly(series: pd.Series) -> pd.Series:
    """일/주간 시리즈를 월말(ME) 기준 마지막 값으로 리샘플. 월간이면 그대로 정렬."""
    s = series.dropna()
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index)
    return s.resample("ME").last().dropna()


def yoy_growth(monthly: pd.Series, periods: int = YOY_PERIODS) -> pd.Series:
    """전년동월대비 변화율(%). 추세 제거 → 레벨 시리즈의 허위 상관 방지."""
    return (monthly.pct_change(periods) * 100).dropna()


# ----------------------------------------------------------------------
# 순수 분석 함수 (이미 변환된 월간 시리즈 2개를 받음)
# ----------------------------------------------------------------------


def lagged_correlations(
    leading: pd.Series,
    target: pd.Series,
    max_lag: int = DEFAULT_MAX_LAG_MONTHS,
) -> dict[int, float]:
    """lag(개월)별 상관계수. leading 을 lag 만큼 미래로 밀어 target 과 정렬.

    lag=k 의 의미: '선행지표가 k개월 전 값' 과 '오늘 목표값' 의 상관 →
    선행지표가 목표를 k개월 앞선다는 가설의 강도.
    lag 0..max_lag 전부 반환 (호출자가 해석).
    """
    out: dict[int, float] = {}
    for lag in range(0, max_lag + 1):
        x = leading.shift(lag)
        joined = pd.concat([x, target], axis=1, join="inner").dropna()
        if len(joined) >= 3:
            c = joined.iloc[:, 0].corr(joined.iloc[:, 1])
            if pd.notna(c):
                out[lag] = float(c)
    return out


def _aligned_xy(leading: pd.Series, target: pd.Series, lag: int) -> tuple[np.ndarray, np.ndarray]:
    """leading.shift(lag) 과 target 을 inner join → (x, y) 배열."""
    joined = pd.concat([leading.shift(lag), target], axis=1, join="inner").dropna()
    return joined.iloc[:, 0].to_numpy(), joined.iloc[:, 1].to_numpy()


def analyze_lead_lag(
    leading: pd.Series,
    target: pd.Series,
    leading_name: str,
    target_name: str,
    max_lag: int = DEFAULT_MAX_LAG_MONTHS,
    min_obs: int = MIN_OVERLAP_MONTHS,
) -> LeadLagResult:
    """선행지표 → 목표 자산 lead-lag 회귀.

    1) lag 1..max_lag 중 |상관계수| 최대인 lag 선택 (lag 0=동시점은 '선행' 아니므로 제외)
    2) 그 lag 에서 OLS 회귀 (target ~ a·leading.shift(lag) + b)
    3) 최신 선행지표 값으로 best_lag 개월 뒤 목표 변화율 예측

    Raises
    ------
    InsufficientDataError
        겹치는 월간 표본이 min_obs 미만이거나 유효 lag 가 없을 때.
    """
    from scipy.stats import linregress

    corrs = lagged_correlations(leading, target, max_lag)
    predictive = {lag: c for lag, c in corrs.items() if lag >= 1}
    if not predictive:
        raise InsufficientDataError(
            f"{leading_name}→{target_name}: 유효한 선행 lag 없음 (데이터 부족)",
            n_points=len(corrs), required=2,
        )

    best_lag = max(predictive, key=lambda k: abs(predictive[k]))
    x, y = _aligned_xy(leading, target, best_lag)
    if len(x) < min_obs:
        raise InsufficientDataError(
            f"{leading_name}→{target_name}: 겹치는 표본 {len(x)}개 < 최소 {min_obs}개",
            n_points=len(x), required=min_obs,
        )

    reg = linregress(x, y)
    latest_leading = float(leading.dropna().iloc[-1])
    predicted = float(reg.slope * latest_leading + reg.intercept)
    r2 = float(reg.rvalue ** 2)

    if predicted > 1.0:
        direction = "상승 ↑"
    elif predicted < -1.0:
        direction = "하락 ↓"
    else:
        direction = "중립 →"

    notes = [
        f"최적 선행 {best_lag}개월 (상관 {predictive[best_lag]:+.2f})",
        f"최신 {leading_name} {latest_leading:+.1f}% → {best_lag}개월 뒤 {target_name} {predicted:+.1f}% 예상",
    ]
    if r2 < STRONG_R2:
        notes.append(f"⚠️ R²={r2:.2f} 낮음 — 참고용 (관계 약함)")

    return LeadLagResult(
        leading_name=leading_name,
        target_name=target_name,
        best_lag_months=best_lag,
        correlation=float(predictive[best_lag]),
        r_squared=r2,
        slope=float(reg.slope),
        intercept=float(reg.intercept),
        n_obs=len(x),
        latest_leading_value=latest_leading,
        predicted_change_pct=predicted,
        direction=direction,
        reliable=r2 >= STRONG_R2,
        notes=notes,
    )


# ----------------------------------------------------------------------
# 오케스트레이터 (fetch + 변환 → 순수 분석)
# ----------------------------------------------------------------------


def predict_btc_from_m2(max_lag: int = DEFAULT_MAX_LAG_MONTHS) -> LeadLagResult:
    """M2 통화량 증가율(YoY) → 비트코인 수익률(YoY).

    가설: 유동성이 풀리면 위험자산(BTC)이 시차를 두고 반응.
    """
    from src.data_fetcher import fetch_macro, fetch_prices
    from src.utils import close_series

    m2_yoy = yoy_growth(to_monthly(fetch_macro("M2SL", start="2014-01-01")))
    btc_yoy = yoy_growth(to_monthly(close_series(fetch_prices("BTC-USD", period="max"))))
    return analyze_lead_lag(m2_yoy, btc_yoy, "M2 증가율", "BTC 수익률", max_lag=max_lag)


def predict_semis_from_korea_exports(
    etf: str = "SOXX",
    max_lag: int = DEFAULT_MAX_LAG_MONTHS,
) -> LeadLagResult:
    """한국 수출 증가율(YoY) → 반도체 ETF 수익률(YoY).

    가설: 한국은 글로벌 반도체 공급망의 길목 → 한국 수출이 반도체 업황을 선행.
    """
    from src.data_fetcher import fetch_korea_trade, fetch_prices
    from src.utils import close_series

    trade = fetch_korea_trade(start="2014-01-01")
    if trade.empty:
        raise InsufficientDataError("한국 무역 데이터 비어있음", n_points=0, required=1)
    exports_yoy = yoy_growth(to_monthly(trade.iloc[:, 0]))  # 첫 컬럼 = 수출(금액)
    semis_yoy = yoy_growth(to_monthly(close_series(fetch_prices(etf, period="max"))))
    return analyze_lead_lag(
        exports_yoy, semis_yoy, "한국 수출 증가율", f"{etf} 수익률", max_lag=max_lag
    )


def _fred_to_etf(
    series_id: str, etf: str, leading_name: str, max_lag: int,
) -> LeadLagResult:
    """FRED 월간 지표(YoY) → ETF 수익률(YoY) 공통 헬퍼."""
    from src.data_fetcher import fetch_macro, fetch_prices
    from src.utils import close_series

    lead_yoy = yoy_growth(to_monthly(fetch_macro(series_id, start="2013-01-01")))
    etf_yoy = yoy_growth(to_monthly(close_series(fetch_prices(etf, period="max"))))
    return analyze_lead_lag(lead_yoy, etf_yoy, leading_name, f"{etf} 수익률", max_lag=max_lag)


def predict_homebuilders_from_permits(
    etf: str = "XHB", max_lag: int = DEFAULT_MAX_LAG_MONTHS,
) -> LeadLagResult:
    """건축허가 증가율(YoY) → 주택건설 ETF 수익률. 허가는 착공·매출보다 먼저 발생."""
    return _fred_to_etf("PERMIT", etf, "건축허가 증가율", max_lag)


def predict_consumer_from_sentiment(
    etf: str = "XLY", max_lag: int = DEFAULT_MAX_LAG_MONTHS,
) -> LeadLagResult:
    """소비자심리(YoY) → 소비재(임의소비) ETF 수익률. 심리가 지갑보다 먼저 움직임."""
    return _fred_to_etf("UMCSENT", etf, "소비자심리 증가율", max_lag)


def predict_em_from_dollar(
    em_etf: str = "EEM", max_lag: int = DEFAULT_MAX_LAG_MONTHS,
) -> LeadLagResult:
    """달러지수(YoY) → 신흥국 ETF 수익률. 달러 강세는 보통 신흥국에 역풍(음의 관계 기대)."""
    from src.data_fetcher import fetch_prices
    from src.utils import close_series

    dxy_yoy = yoy_growth(to_monthly(close_series(fetch_prices("DX-Y.NYB", period="max"))))
    em_yoy = yoy_growth(to_monthly(close_series(fetch_prices(em_etf, period="max"))))
    return analyze_lead_lag(dxy_yoy, em_yoy, "달러지수 증가율", f"{em_etf} 수익률", max_lag=max_lag)


def predict_cyclicals_from_copper_gold(
    target: str = "SPY", max_lag: int = DEFAULT_MAX_LAG_MONTHS,
) -> LeadLagResult:
    """구리/금 비율(YoY) → 경기민감 자산 수익률. '닥터 코퍼' — 실물경기 체온계."""
    from src.data_fetcher import fetch_prices
    from src.utils import close_series

    copper = to_monthly(close_series(fetch_prices("HG=F", period="max")))
    gold = to_monthly(close_series(fetch_prices("GC=F", period="max")))
    ratio_yoy = yoy_growth((copper / gold).dropna())
    tgt_yoy = yoy_growth(to_monthly(close_series(fetch_prices(target, period="max"))))
    return analyze_lead_lag(ratio_yoy, tgt_yoy, "구리/금 비율 증가율", f"{target} 수익률", max_lag=max_lag)


def predict_btc_from_wikipedia(
    article: str = "Bitcoin", max_lag: int = DEFAULT_MAX_LAG_MONTHS,
) -> LeadLagResult:
    """위키피디아 관심도(YoY) → 비트코인 수익률.

    대중 관심은 가격을 앞설 수도(리테일 FOMO 선행) 뒤따를 수도(가격 추종) 있어,
    엔진이 찾은 lag 부호로 판단. 페이지뷰는 월평균으로 집계 (합/마지막 아님).
    """
    from src.data_fetcher import fetch_prices, fetch_wikipedia_pageviews
    from src.utils import close_series

    views = fetch_wikipedia_pageviews(article, days=3650)
    views_yoy = yoy_growth(views.resample("ME").mean().dropna())  # 월평균 일일 조회수
    btc_yoy = yoy_growth(to_monthly(close_series(fetch_prices("BTC-USD", period="max"))))
    return analyze_lead_lag(
        views_yoy, btc_yoy, f"위키 '{article}' 관심", "BTC 수익률", max_lag=max_lag
    )


# 사용 가능한 예측 관계 레지스트리 (스크립트/오케스트레이터가 순회)
PREDICTORS: dict[str, callable] = {
    "M2 → 비트코인": predict_btc_from_m2,
    "한국수출 → 반도체(SOXX)": predict_semis_from_korea_exports,
    "건축허가 → 주택건설(XHB)": predict_homebuilders_from_permits,
    "소비자심리 → 소비재(XLY)": predict_consumer_from_sentiment,
    "달러 → 신흥국(EEM)": predict_em_from_dollar,
    "구리/금 → 경기(SPY)": predict_cyclicals_from_copper_gold,
    "위키관심 → 비트코인": predict_btc_from_wikipedia,
}
