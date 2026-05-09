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
from typing import Iterable

import numpy as np
import pandas as pd

from src.data_fetcher import fetch_macro, fetch_prices


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
            close = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
            frames[label] = close.squeeze()
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  {label} ({ticker}) 실패: {e}")
    return pd.DataFrame(frames).dropna(how="all")


# ----------------------------------------------------------------------
# 통계 유틸
# ----------------------------------------------------------------------


def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """가격 → 일별 수익률 (%변화)."""
    return prices.pct_change().dropna(how="all")


def correlation_matrix(prices: pd.DataFrame) -> pd.DataFrame:
    """가격 패널을 받아 일별 수익률 기준 상관계수 매트릭스 반환."""
    return daily_returns(prices).corr()


def annualized_volatility(prices: pd.DataFrame, periods_per_year: int = 252) -> pd.Series:
    """자산별 연환산 변동성(표준편차).

    주식 기준으로 1년 = 252 거래일을 가정. 암호화폐는 365 가 더 정확하지만
    일관성 위해 252 로 통일 (혼합 패널이라).
    """
    returns = daily_returns(prices)
    return returns.std() * np.sqrt(periods_per_year)


def cumulative_returns(prices: pd.DataFrame) -> pd.Series:
    """기간 시작 → 끝의 누적 수익률 (각 자산별 단일 값)."""
    first = prices.bfill().iloc[0]
    last = prices.ffill().iloc[-1]
    return (last / first - 1) * 100


# ----------------------------------------------------------------------
# 시장 국면 분류기 (규칙 기반)
# ----------------------------------------------------------------------


@dataclass
class RegimeReport:
    """시장 국면 분석 결과."""

    regime: str
    score: int  # -3 (강한 위험회피) ~ +3 (강한 위험선호)
    signals: list[str] = field(default_factory=list)
    raw: dict[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        lines = [f"국면 판정: {self.regime}  (점수 {self.score:+d} / -3 ~ +3)"]
        lines.append("")
        lines.append("근거:")
        lines.extend(f"  · {s}" for s in self.signals)
        return "\n".join(lines)


def classify_regime() -> RegimeReport:
    """3개 거시 지표로 시장 국면을 단순 분류.

    사용 지표:
    - T10Y2Y       (장단기 금리차) — 음수면 침체 신호
    - BAMLH0A0HYM2 (하이일드 스프레드) — 급등하면 신용 경색
    - ICSA         (주간 신규 실업수당) — 급등하면 노동시장 악화

    각 신호별로 -1 / 0 / +1 점을 주고 합산 → 국면 라벨링.
    """
    signals: list[str] = []
    raw: dict[str, float] = {}
    score = 0

    # --- 1. 장단기 금리차 ---
    try:
        t10y2y = fetch_macro("T10Y2Y").tail(60)
        latest = float(t10y2y.iloc[-1])
        avg_60d = float(t10y2y.mean())
        raw["T10Y2Y"] = latest

        if latest < 0:
            score -= 1
            signals.append(
                f"장단기 금리차 {latest:+.2f}%p 로 역전 상태 — 침체 경고 (-1)"
            )
        elif latest < avg_60d - 0.2:
            signals.append(
                f"장단기 금리차 {latest:+.2f}%p, 60일 평균 대비 하락 — 주의 (0)"
            )
        else:
            score += 1
            signals.append(
                f"장단기 금리차 {latest:+.2f}%p 로 정상 우상향 — 위험선호 (+1)"
            )
    except Exception as e:  # noqa: BLE001
        signals.append(f"T10Y2Y 조회 실패: {e}")

    # --- 2. 하이일드 스프레드 ---
    try:
        hy = fetch_macro("BAMLH0A0HYM2").tail(252)  # 최근 1년
        latest = float(hy.iloc[-1])
        median_1y = float(hy.median())
        p90 = float(hy.quantile(0.9))
        raw["HY_spread"] = latest

        if latest > p90:
            score -= 1
            signals.append(
                f"하이일드 스프레드 {latest:.2f}%, 1년 90분위 초과 — 신용 경색 (-1)"
            )
        elif latest > median_1y * 1.1:
            signals.append(
                f"하이일드 스프레드 {latest:.2f}%, 중간값 대비 상승 — 주의 (0)"
            )
        else:
            score += 1
            signals.append(
                f"하이일드 스프레드 {latest:.2f}% 안정 — 위험선호 (+1)"
            )
    except Exception as e:  # noqa: BLE001
        signals.append(f"BAMLH0A0HYM2 조회 실패: {e}")

    # --- 3. 주간 신규 실업수당 청구 ---
    try:
        ic = fetch_macro("ICSA").tail(52)  # 최근 1년 (주간)
        latest = float(ic.iloc[-1])
        mean_1y = float(ic.mean())
        std_1y = float(ic.std())
        raw["ICSA"] = latest

        z = (latest - mean_1y) / std_1y if std_1y > 0 else 0

        if z > 1.5:
            score -= 1
            signals.append(
                f"신규 실업수당 청구 {int(latest):,}건, z={z:+.1f} — 노동시장 악화 (-1)"
            )
        elif z < -1:
            score += 1
            signals.append(
                f"신규 실업수당 청구 {int(latest):,}건, z={z:+.1f} — 고용 강세 (+1)"
            )
        else:
            signals.append(
                f"신규 실업수당 청구 {int(latest):,}건, z={z:+.1f} — 중립 (0)"
            )
    except Exception as e:  # noqa: BLE001
        signals.append(f"ICSA 조회 실패: {e}")

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

    return RegimeReport(regime=regime, score=score, signals=signals, raw=raw)


# ----------------------------------------------------------------------
# 한 번에 보고서 만들기
# ----------------------------------------------------------------------


def market_summary(period: str = "6mo") -> dict:
    """패널 + 상관관계 + 변동성 + 국면을 한 dict 으로 묶어서 반환."""
    prices = fetch_cross_asset_panel(period=period)
    return {
        "period": period,
        "prices": prices,
        "cumulative_returns_pct": cumulative_returns(prices),
        "correlation": correlation_matrix(prices),
        "annualized_vol_pct": annualized_volatility(prices) * 100,
        "regime": classify_regime(),
    }
