"""
src/utils.py
============
모듈 경계에 속하지 않는 작은 공용 헬퍼 모음 (4단계 리팩토링 결과물).

여기 들어올 자격: 두 개 이상의 모듈/스크립트에서 똑같이 반복되던 코드.
도메인 로직(거시/리스크/스크리닝)은 각자의 모듈에 — 여기는 순수 유틸만.
"""

from __future__ import annotations

import pandas as pd

from src.exceptions import DataValidationError


def close_series(df: pd.DataFrame) -> pd.Series:
    """OHLCV DataFrame 에서 종가 시리즈 추출.

    yfinance 는 옵션에 따라 'Adj Close' 가 있기도 없기도 함 →
    'Adj Close' 우선, 없으면 'Close' fallback. (기존에 macro_analyzer /
    risk_engine / hello_world 세 곳에 복붙되어 있던 로직)

    Raises
    ------
    DataValidationError
        둘 다 없는 경우 (가격 데이터 스키마 위반).
    """
    if "Adj Close" in df.columns:
        series = df["Adj Close"]
    elif "Close" in df.columns:
        series = df["Close"]
    else:
        raise DataValidationError(
            "가격 데이터에 'Adj Close'/'Close' 컬럼이 모두 없음", source="yfinance"
        )
    return series.squeeze()  # 혹시 2D 로 와도 1D 로 평탄화


def pick_first(row: pd.Series, candidates: list[str]):
    """행(Series)에서 후보 컬럼명들 중 처음으로 '존재하고 NaN 아닌' 값을 반환.

    FMP 와 yfinance 가 같은 항목을 다른 이름으로 주기 때문에
    ("revenue" vs "Total Revenue") 후보 리스트로 흡수. 전부 없으면 None.
    """
    for name in candidates:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return None
