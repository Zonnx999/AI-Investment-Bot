"""
macro_analyzer.py
=================
레이 달리오 식 거시 조망 모듈 (Phase 2 에서 본격 구현).

핵심 책임:
- 자산군별(주식/채권/금/원자재/암호화폐) 가격을 받아 상관관계 매트릭스 계산
- 시장 국면 (확장 / 둔화 / 수축 / 회복) 분류
- 자산배분 추천 (All Weather 벤치마크 대비 갭)
"""

from __future__ import annotations

import pandas as pd


def correlation_matrix(prices: pd.DataFrame) -> pd.DataFrame:
    """여러 자산의 가격 데이터프레임을 받아 일별 수익률 기준 상관계수 매트릭스 반환.

    Parameters
    ----------
    prices : pd.DataFrame
        컬럼이 자산명, 인덱스가 날짜, 값이 종가인 데이터프레임.
    """
    returns = prices.pct_change().dropna()
    return returns.corr()


# TODO(Phase 2):
# - classify_regime(macro_indicators)
# - all_weather_gap(portfolio)
