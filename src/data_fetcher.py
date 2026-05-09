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


# ----------------------------------------------------------------------
# 한국 수출입 통계
# ----------------------------------------------------------------------
#
# 두 가지 경로:
#  (A) FRED — OECD가 한국 월간 통계를 그대로 호스팅. 받으신 FRED 키만 있으면 즉시 동작.
#  (B) UNIPASS (관세청) — 일별/HS코드별/국가별 세분화된 데이터. 별도 가입 필요.
#       https://unipass.customs.go.kr/openapi/ 에서 인증키 발급.
# ----------------------------------------------------------------------


# FRED 에 등록된 한국 무역통계 시리즈 (OECD 출처, USD 기준 월별)
KOREA_TRADE_SERIES = {
    "수출(금액, USD)":     "XTEXVA01KRM664S",
    "수입(금액, USD)":     "XTIMVA01KRM664S",
    "무역수지(USD)":       "XTNTVA01KRM664S",
}


def fetch_korea_trade(start: str | None = None) -> pd.DataFrame:
    """FRED 기반 한국 월간 수출/수입/무역수지 (USD).

    OECD가 정리해둔 데이터라 관세청 원본보다 1~2개월 지연이 있지만,
    매크로 조망용으로는 충분합니다. 더 빠른 잠정치가 필요하면 UNIPASS 함수 사용.
    """
    frames = {}
    for label, series_id in KOREA_TRADE_SERIES.items():
        try:
            frames[label] = fetch_macro(series_id, start=start)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  {label} ({series_id}) 실패: {e}")
    return pd.DataFrame(frames)


def fetch_korea_exports_unipass(
    start_yyyymm: str,
    end_yyyymm: str,
    hs_code: str | None = None,
) -> pd.DataFrame:
    """관세청 UNIPASS OpenAPI 로 한국 수출 통계 조회 (선택, 인증키 필요).

    Parameters
    ----------
    start_yyyymm, end_yyyymm : str
        조회 시작/종료 월. 예: "202401", "202504".
    hs_code : str | None
        HS 코드(품목분류). 예: "8542" (집적회로 = 반도체).
        None 이면 전체 합계.

    .env 필요:
        UNIPASS_API_KEY=...

    참고:
        실제 서비스명/엔드포인트는 가입 후 받는 인증서별 명세서를 따르세요.
        (관세청은 서비스 단위로 별도 인증을 줍니다 — 예: trrTotInfoQry 등)
        아래 URL 과 파라미터 키는 사용자께서 받으신 명세서대로 수정 필요.
    """
    import xml.etree.ElementTree as ET

    import requests

    api_key = settings.require("unipass_api_key")

    # ⚠️  아래 URL/서비스명은 발급받으신 서비스에 맞게 교체하세요.
    base_url = "https://unipass.customs.go.kr:38010/ext/rest/trrTotInfoQry/getTrrTotInfoQryList"
    params = {
        "crkyCn": api_key,                # 인증키
        "strtYymm": start_yyyymm,         # 시작 연월
        "endYymm": end_yyyymm,            # 종료 연월
    }
    if hs_code:
        params["hsSgn"] = hs_code         # HS 코드

    response = requests.get(base_url, params=params, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    rows = []
    for item in root.findall(".//item"):
        row = {child.tag: child.text for child in item}
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    return df


# TODO(다음 Phase 1.5 — 선택 사항):
# - SQLite 캐시 레이어 (같은 데이터 두 번 안 부르도록 src/storage.py 신설)
