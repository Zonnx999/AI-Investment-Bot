"""
data_fetcher.py
===============
모든 외부 데이터 소스에서 가격/재무/거시 데이터를 가져오는 전담 모듈.

Phase 1 단계에서 가장 먼저 채울 모듈입니다. 지금은 yfinance 기반의
가장 단순한 함수 두 개만 들어있고, 앞으로 FRED · CoinGecko · FMP 함수가
순차적으로 추가될 예정입니다.

사용 예::

    from src.data_fetcher import fetch_prices, fetch_fundamentals
    df = fetch_prices("CPNG", period="6mo")
    info = fetch_fundamentals("CPNG")
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf


def fetch_prices(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """야후파이낸스에서 OHLCV 데이터 가져오기.

    Parameters
    ----------
    ticker : str
        종목 티커. 예: "CPNG", "NVDA", "BTC-USD", "GC=F" (금 선물).
    period : str
        조회 기간. "5d", "1mo", "6mo", "1y", "5y", "max" 등.
    interval : str
        봉 간격. "1d", "1wk", "1mo".

    Returns
    -------
    pd.DataFrame
        Open, High, Low, Close, Adj Close, Volume 컬럼을 가진 일별 데이터.
    """
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
        multi_level_index=False,  # 단일 티커여도 컬럼이 멀티레벨로 오는 것을 막음
    )
    if df.empty:
        raise ValueError(f"{ticker} 데이터를 가져오지 못했습니다. 티커를 확인하세요.")
    return df


def fetch_fundamentals(ticker: str) -> dict:
    """기업 펀더멘털 요약. yfinance .info 의 핵심 키만 추려서 dict 로 반환.

    무료 소스라 일부 값이 None 일 수 있으니 그대로 두고, Phase 1 후반에
    FMP API 로 보강할 예정입니다.
    """
    t = yf.Ticker(ticker)
    info = t.info or {}

    return {
        "ticker": ticker,
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "price_to_book": info.get("priceToBook"),
        "profit_margin": info.get("profitMargins"),
        "operating_margin": info.get("operatingMargins"),
        "return_on_equity": info.get("returnOnEquity"),
        "revenue_growth": info.get("revenueGrowth"),
        "free_cashflow": info.get("freeCashflow"),
        "total_cash": info.get("totalCash"),
        "total_debt": info.get("totalDebt"),
        "current_price": info.get("currentPrice"),
        "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
        "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
    }


# TODO(Phase 1 후반):
# - fetch_macro(series_id) : FRED 거시지표 (T10Y2Y, ICSA, BAMLH0A0HYM2 등)
# - fetch_crypto(coin_id)  : CoinGecko 시세 + 거래량
# - fetch_korea_exports()  : 관세청 월간 수출 데이터
