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

from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from src.config import settings


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


# ----------------------------------------------------------------------
# 거시 지표 (FRED)
# ----------------------------------------------------------------------

# 자주 쓰는 FRED 시리즈 ID 모음. 한국어 이름 → FRED 시리즈 코드.
# 새 지표 추가하고 싶으면 https://fred.stlouisfed.org 에서 검색해 ID 확인.
FRED_SERIES = {
    "장단기 금리차 (10Y-2Y)": "T10Y2Y",
    "주간 신규 실업수당 청구": "ICSA",
    "하이일드 스프레드": "BAMLH0A0HYM2",
    "연방기금금리(실효)": "DFF",
    "CPI (전년동월대비 %)": "CPIAUCSL",
    "ISM 제조업 PMI": "MANEMP",  # 대용 (정식 PMI는 라이선스 이슈로 FRED에 미게재)
    "S&P500 지수": "SP500",
}


def fetch_macro(series_id: str, start: str | None = None) -> pd.Series:
    """FRED 에서 단일 시계열 가져오기.

    Parameters
    ----------
    series_id : str
        FRED 시리즈 ID. 예: "T10Y2Y", "ICSA".
        FRED_SERIES dict 의 값들을 참고하세요.
    start : str | None
        시작일 (YYYY-MM-DD). None 이면 최근 5년.

    Returns
    -------
    pd.Series
        인덱스가 날짜, 값이 지표 수치.
    """
    # fredapi 는 import 시점에 키를 묻지 않아서 여기서 lazy import
    from fredapi import Fred

    api_key = settings.require("fred_api_key")
    fred = Fred(api_key=api_key)

    if start is None:
        start = (datetime.utcnow() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")

    series = fred.get_series(series_id, observation_start=start)
    series.name = series_id
    return series.dropna()


def fetch_macro_dashboard(start: str | None = None) -> pd.DataFrame:
    """FRED_SERIES 에 정의된 주요 거시 지표를 한 번에 받아 DataFrame 으로 반환."""
    frames = {}
    for korean_name, series_id in FRED_SERIES.items():
        try:
            frames[korean_name] = fetch_macro(series_id, start=start)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  {korean_name} ({series_id}) 실패: {e}")
    return pd.DataFrame(frames)


# ----------------------------------------------------------------------
# 암호화폐 (CoinGecko)
# ----------------------------------------------------------------------


def fetch_crypto(coin_id: str = "bitcoin", days: int = 180) -> pd.DataFrame:
    """CoinGecko 에서 암호화폐 일별 가격·거래량 가져오기.

    Parameters
    ----------
    coin_id : str
        CoinGecko 코인 ID (티커가 아님). "bitcoin", "ethereum", "solana" 등.
        전체 목록: https://api.coingecko.com/api/v3/coins/list
    days : int
        오늘부터 며칠 전까지. 무료 티어는 최대 365일.

    Returns
    -------
    pd.DataFrame
        date 인덱스, columns=[price, volume, market_cap].
    """
    from pycoingecko import CoinGeckoAPI

    cg = CoinGeckoAPI()
    raw = cg.get_coin_market_chart_by_id(
        id=coin_id, vs_currency="usd", days=days, interval="daily"
    )
    df = pd.DataFrame(
        {
            "price": [p[1] for p in raw["prices"]],
            "volume": [v[1] for v in raw["total_volumes"]],
            "market_cap": [m[1] for m in raw["market_caps"]],
        },
        index=pd.to_datetime([p[0] for p in raw["prices"]], unit="ms").date,
    )
    df.index.name = "date"
    return df


# TODO(Phase 1 마무리):
# - fetch_korea_exports()  : 관세청 월간 수출 (특히 반도체)
# - SQLite 캐시 레이어 추가 (같은 데이터 두 번 안 부르도록)
