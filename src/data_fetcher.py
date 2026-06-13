"""
data_fetcher.py
===============
모든 외부 데이터 소스에서 가격/재무/거시 데이터를 가져오는 전담 모듈.

다른 모듈은 외부 API 를 직접 부르지 않고 반드시 여기를 거칩니다.
소스: yfinance (가격/재무), FRED (거시/한국무역), CoinGecko (암호화폐),
FMP (재무제표 시계열/시세/프로필 — /stable/ 엔드포인트).

HTTP 정책 (retry/timeout/키 마스킹) 은 src/http.py 의 표준 세션이 담당.
캐싱 (Phase 4): 모든 fetch 함수는 src/storage.py 의 SQLite 캐시를 투명하게
사용 — 소스별 TTL 은 아래 TTL_* 상수, 제어는 QUANT_BOT_CACHE (on/off/refresh).

빈 데이터 컨벤션 (8단계 통일)
-----------------------------
1. **단일 대상 fetch** — fetch_prices / fetch_macro / fetch_crypto /
   fetch_quote / fetch_profile / fetch_crypto_top:
   빈 응답 = 잘못된 입력 또는 소스 장애 → ``DataValidationError`` raise.
2. **폴백 전제 시계열** — fetch_financial_statements / fetch_key_metrics /
   fetch_ratios / fetch_financials_yf:
   빈 응답 → **빈 DataFrame 반환**. 호출부가 ``df.empty`` 로 폴백 판단
   (check_fundamentals 의 FMP → yfinance 폴백이 이 컨벤션에 의존).
3. **배치 fetch** — fetch_macro_dashboard / fetch_korea_trade:
   개별 시리즈 실패는 경고 로그 + 스킵, **부분 DataFrame 반환**
   (일일 배치에서 한 시리즈 장애가 전체를 막지 않도록).

사용 예::

    from src.data_fetcher import fetch_prices, fetch_key_metrics
    df = fetch_prices("CPNG", period="6mo")
    metrics = fetch_key_metrics("CPNG", limit=5)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from src.storage import cached

from src.config import settings
from src.exceptions import (
    ApiAuthError,
    ApiAuthorizationError,
    ApiConnectionError,
    ApiHttpError,
    ApiTimeoutError,
    DataFetchError,
    DataValidationError,
    RateLimitError,
)
from src.http import DEFAULT_TIMEOUT, RETRY_TOTAL, get_http_session, is_timeout
from src.logger import get_logger

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 캐시 TTL (Phase 4) — 소스별 갱신 주기에 맞춤. QUANT_BOT_CACHE 로 제어.
# ----------------------------------------------------------------------
TTL_PRICES = timedelta(hours=6)        # 일별 시세 — 같은 날 재호출 방지
TTL_MACRO = timedelta(hours=12)        # FRED — 일/주 단위 갱신
TTL_CRYPTO = timedelta(hours=1)        # 암호화폐 — 변동 빠름
TTL_QUOTE = timedelta(minutes=30)      # FMP 실시간 시세
TTL_STATEMENTS = timedelta(days=7)     # 재무제표 — 분기에 한 번 바뀜 (FMP 한도 보호 핵심)
TTL_PROFILE = timedelta(days=30)       # 회사 프로필 — 거의 불변
TTL_SNAPSHOT = timedelta(hours=6)      # yfinance .info 스냅샷
TTL_WIKI = timedelta(days=1)           # 위키피디아 페이지뷰 — 일 단위 갱신
TTL_SCREENER = timedelta(hours=12)     # company-screener 유니버스 발굴 (구성 거의 불변)


@cached("prices", TTL_PRICES, "dataframe")
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

    Raises
    ------
    DataFetchError
        yfinance 호출 자체가 실패할 때.
    DataValidationError
        호출은 성공했지만 빈 데이터(잘못된 티커 등) 가 돌아왔을 때.
    """
    try:
        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            multi_level_index=False,
        )
    except Exception as e:  # noqa: BLE001  yfinance 는 여러 종류의 예외를 던짐
        raise DataFetchError(
            f"yfinance 다운로드 실패: ticker={ticker} period={period}", source="yfinance"
        ) from e

    if df.empty:
        raise DataValidationError(
            f"yfinance 가 빈 데이터 반환: ticker={ticker} (티커 오타 가능성)",
            source="yfinance",
        )
    return df


@cached("financials_yf", TTL_STATEMENTS, "dataframe")
def fetch_financials_yf(
    ticker: str,
    statement: str = "income",
) -> pd.DataFrame:
    """yfinance 기반 재무제표 (FMP 무료 티어가 막혔을 때 fallback).

    Parameters
    ----------
    statement : str
        "income"   : 손익계산서  (income_stmt)
        "balance"  : 재무상태표  (balance_sheet)
        "cashflow" : 현금흐름표  (cashflow)

    yfinance 는 4년치 연간 데이터를 컬럼으로 반환. 이 함수는 그것을
    행=날짜 순서로 뒤집어 FMP 와 비슷한 모양으로 맞춰줍니다.
    """
    t = yf.Ticker(ticker)
    table = {
        "income": t.income_stmt,
        "balance": t.balance_sheet,
        "cashflow": t.cashflow,
    }.get(statement)

    if table is None or table.empty:
        return pd.DataFrame()

    df = table.T.sort_index()  # 행=날짜, 열=항목
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df


@cached("fundamentals_yf", TTL_SNAPSHOT, "json")
def fetch_fundamentals(ticker: str) -> dict:
    """기업 펀더멘털 '스냅샷' (yfinance .info, API 키 불필요).

    용도: hello_world 같은 키 없는 빠른 확인용. 본격 펀더멘털 분석은
    FMP 시계열 함수들 (fetch_key_metrics / fetch_financial_statements) 사용.
    4단계 리팩토링에서 실제 사용되는 필드만 남기고 축소.
    """
    t = yf.Ticker(ticker)
    info = t.info or {}

    return {
        "ticker": ticker,
        "name": info.get("shortName") or info.get("longName"),
        "market_cap": info.get("marketCap"),
        "forward_pe": info.get("forwardPE"),
        "return_on_equity": info.get("returnOnEquity"),
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


@cached("macro", TTL_MACRO, "series")
def fetch_macro(series_id: str, start: str | None = None) -> pd.Series:
    """FRED 에서 단일 시계열 가져오기.

    참고: fredapi 는 내부적으로 urllib.request.urlopen 을 사용해서
    표준 HTTP 세션(src/http.py 의 retry/timeout)을 주입할 수 없습니다.
    여기는 도메인 예외 wrapping 만 적용 (3단계 리팩토링 결정).

    Raises
    ------
    MissingApiKeyError
        FRED_API_KEY 미설정.
    DataFetchError
        fredapi 호출 자체 실패.
    DataValidationError
        시리즈 ID 가 존재하지 않거나 빈 응답.
    """
    try:
        from fredapi import Fred
    except ImportError as e:
        raise DataFetchError(
            "fredapi 패키지가 설치되어 있지 않습니다. pip install fredapi", source="FRED"
        ) from e

    api_key = settings.require("fred_api_key")  # raises MissingApiKeyError
    fred = Fred(api_key=api_key)

    if start is None:
        start = (datetime.utcnow() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")

    try:
        series = fred.get_series(series_id, observation_start=start)
    except ValueError as e:
        # fredapi 가 잘못된 시리즈 ID 에 ValueError 던짐
        raise DataValidationError(
            f"FRED 시리즈 '{series_id}' 가 존재하지 않거나 잘못됨", source="FRED"
        ) from e
    except Exception as e:  # noqa: BLE001  fredapi 는 다양한 네트워크 예외를 던질 수 있음
        raise DataFetchError(
            f"FRED 호출 실패: series_id={series_id}", source="FRED"
        ) from e

    if series is None or series.empty:
        raise DataValidationError(
            f"FRED 시리즈 '{series_id}' 가 빈 응답을 돌려줌", source="FRED"
        )

    series.name = series_id
    return series.dropna()


def fetch_macro_dashboard(start: str | None = None) -> pd.DataFrame:
    """FRED_SERIES 에 정의된 주요 거시 지표를 한 번에 받아 DataFrame 으로 반환.

    개별 시리즈 실패는 warning 로그 + 스킵 처리 (배치 운영에서 한 시리즈 실패가
    전체를 막지 않도록). 단, 예상치 못한 예외는 그대로 bubble up.
    """
    frames = {}
    for korean_name, series_id in FRED_SERIES.items():
        try:
            frames[korean_name] = fetch_macro(series_id, start=start)
        except DataFetchError as e:
            logger.warning(
                "FRED 시리즈 '%s' (%s) 스킵 — %s", korean_name, series_id, e
            )
    return pd.DataFrame(frames)


# ----------------------------------------------------------------------
# 암호화폐 (CoinGecko)
# ----------------------------------------------------------------------


def _coingecko_client():
    """프로젝트 표준 HTTP 세션이 주입된 CoinGecko 클라이언트.

    pycoingecko 기본값은 자체 세션(retry 가 502/503/504 만 커버) +
    timeout 120s 라서, 우리 표준 정책(429 포함 retry, connect 5s/read 25s)
    으로 교체합니다. pycoingecko 3.2.0 의 ``session``/``request_timeout``
    속성이 public 이라 주입 가능 — 버전 올릴 때 이 가정 재확인 필요.
    """
    try:
        from pycoingecko import CoinGeckoAPI
    except ImportError as e:
        raise DataFetchError(
            "pycoingecko 패키지가 없습니다. pip install pycoingecko", source="CoinGecko"
        ) from e

    cg = CoinGeckoAPI()
    cg.session = get_http_session()
    cg.request_timeout = DEFAULT_TIMEOUT
    return cg


@cached("crypto", TTL_CRYPTO, "dataframe")
def fetch_crypto(coin_id: str = "bitcoin", days: int = 180) -> pd.DataFrame:
    """CoinGecko 에서 암호화폐 일별 가격·거래량 가져오기.

    인덱스는 자정으로 정규화된 DatetimeIndex (Phase 4: 캐시 직렬화
    호환을 위해 datetime.date 객체에서 변경 — 소비처는 iloc 기반이라 무영향).

    Raises
    ------
    DataFetchError
        pycoingecko 호출 실패 (네트워크, rate limit 등).
    DataValidationError
        잘못된 coin_id 또는 빈 응답.
    """
    cg = _coingecko_client()
    try:
        raw = cg.get_coin_market_chart_by_id(
            id=coin_id, vs_currency="usd", days=days, interval="daily"
        )
    except ValueError as e:
        # pycoingecko 가 404 를 ValueError 로 감쌈
        raise DataValidationError(
            f"CoinGecko: coin_id='{coin_id}' 가 존재하지 않을 가능성", source="CoinGecko"
        ) from e
    except Exception as e:  # noqa: BLE001
        raise DataFetchError(
            f"CoinGecko 호출 실패: coin_id={coin_id}", source="CoinGecko"
        ) from e

    if not raw or "prices" not in raw or not raw["prices"]:
        raise DataValidationError(
            f"CoinGecko 가 빈 응답: coin_id={coin_id}", source="CoinGecko"
        )

    df = pd.DataFrame(
        {
            "price": [p[1] for p in raw["prices"]],
            "volume": [v[1] for v in raw["total_volumes"]],
            "market_cap": [m[1] for m in raw["market_caps"]],
        },
        index=pd.to_datetime([p[0] for p in raw["prices"]], unit="ms").normalize(),
    )
    df.index.name = "date"
    return df


# ----------------------------------------------------------------------
# 한국 수출입 통계 (FRED 경유, OECD 출처)
# ----------------------------------------------------------------------

KOREA_TRADE_SERIES = {
    "수출(금액, USD)":     "XTEXVA01KRM664S",
    "수입(금액, USD)":     "XTIMVA01KRM664S",
    "무역수지(USD)":       "XTNTVA01KRM664S",
}


def fetch_korea_trade(start: str | None = None) -> pd.DataFrame:
    """FRED 기반 한국 월간 수출/수입/무역수지 (USD).

    OECD가 정리해둔 데이터라 관세청 원본보다 1~2개월 지연이 있지만,
    매크로 조망용으로는 충분합니다.
    """
    frames = {}
    for label, series_id in KOREA_TRADE_SERIES.items():
        try:
            frames[label] = fetch_macro(series_id, start=start)
        except DataFetchError as e:
            logger.warning(
                "한국 무역 시리즈 '%s' (%s) 스킵 — %s", label, series_id, e
            )
    return pd.DataFrame(frames)


# ----------------------------------------------------------------------
# FMP (Financial Modeling Prep) — 재무제표 시계열
# ----------------------------------------------------------------------
#
# 2025-08-31 이후 가입한 계정은 무조건 /stable/ 엔드포인트를 사용해야 합니다.
# /api/v3/* 는 그 이전 가입자 전용 레거시이고, 신규 사용자에겐 403 을 줍니다.
# 형식 차이:
#     /api/v3/income-statement/CPNG?period=annual&limit=5         (옛날)
#     /stable/income-statement?symbol=CPNG&period=annual&limit=5  (현재)
#
# 무료 플랜: US 주식 일부, 호출 한도 있음
# Starter+ : 모든 종목 + 풍부한 시계열
# ----------------------------------------------------------------------

FMP_BASE_URL = "https://financialmodelingprep.com/stable"


def _fmp_get(endpoint: str, params: dict | None = None) -> list:
    """FMP stable API GET 헬퍼.

    Raises
    ------
    MissingApiKeyError    FMP_API_KEY 미설정
    ApiAuthError          HTTP 401 — 키 무효
    ApiAuthorizationError HTTP 403 — 플랜 제한
    RateLimitError        HTTP 429 — 호출 한도
    ApiHttpError          기타 HTTP 4xx/5xx
    ApiTimeoutError       타임아웃 (자동 재시도 소진 후)
    ApiConnectionError    네트워크 접속 실패 (자동 재시도 소진 후)
    DataValidationError   응답이 비어있음

    참고: 429/5xx 는 표준 세션(src/http.py)이 backoff 로 최대 3회 자동
    재시도한 뒤에도 실패한 경우에만 여기 도메인 예외로 변환됩니다.
    """
    import requests

    api_key = settings.require("fmp_api_key")
    p = dict(params or {})
    p["apikey"] = api_key
    url = f"{FMP_BASE_URL}/{endpoint}"

    try:
        response = get_http_session().get(url, params=p)  # timeout/retry 는 세션이 강제
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        # 주의: Retry 가 개입하면 read timeout 도 ConnectionError 로 나옴 → is_timeout 으로 구분
        if is_timeout(e):
            raise ApiTimeoutError(
                f"FMP {endpoint} 타임아웃 (connect {DEFAULT_TIMEOUT[0]:.0f}s / "
                f"read {DEFAULT_TIMEOUT[1]:.0f}s, 재시도 {RETRY_TOTAL}회 포함)",
                source="FMP",
            ) from e
        raise ApiConnectionError(
            f"FMP {endpoint} 연결 실패 (재시도 {RETRY_TOTAL}회 포함)", source="FMP"
        ) from e
    except requests.exceptions.RequestException as e:
        raise DataFetchError(
            f"FMP {endpoint} 요청 실패", source="FMP"
        ) from e

    code = response.status_code
    if code == 401:
        raise ApiAuthError(
            f"FMP {endpoint}: 키가 무효합니다 (401)", source="FMP"
        )
    if code == 403:
        raise ApiAuthorizationError(
            f"FMP {endpoint}: 현재 플랜에서 차단된 엔드포인트 (403)", source="FMP"
        )
    if code == 429:
        raise RateLimitError(
            f"FMP {endpoint}: 호출 한도 초과 (429, 재시도 후에도 지속)", source="FMP"
        )
    if not response.ok:
        raise ApiHttpError(
            f"FMP {endpoint}: HTTP {code}", status_code=code, source="FMP"
        )

    return response.json()


def _fmp_to_dataframe(data: list) -> pd.DataFrame:
    """FMP 응답(list[dict]) → 날짜 인덱스 DataFrame 공통 후처리.

    빈 응답이면 빈 DataFrame. 'date' 컬럼이 있으면 datetime 변환 후
    오름차순 정렬 + 인덱스 설정. (기존 3개 fetch 함수에 복붙되어 있던 로직)
    """
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
    return df


@cached("fmp_statements", TTL_STATEMENTS, "dataframe")
def fetch_financial_statements(
    ticker: str,
    statement: str = "income-statement",
    period: str = "annual",
    limit: int = 5,
) -> pd.DataFrame:
    """FMP 재무제표 시계열 (각 행 = 한 회계기간).

    Parameters
    ----------
    ticker : str
        예: "CPNG", "NVDA", "AAPL".
    statement : str
        "income-statement"          : 손익계산서
        "balance-sheet-statement"   : 재무상태표
        "cash-flow-statement"       : 현금흐름표
    period : str
        "annual" 또는 "quarter".
    limit : int
        과거 몇 기간을 가져올지 (annual 기준 보통 5~10).
    """
    data = _fmp_get(
        statement,
        {"symbol": ticker, "period": period, "limit": limit},
    )
    return _fmp_to_dataframe(data)


@cached("fmp_key_metrics", TTL_STATEMENTS, "dataframe")
def fetch_key_metrics(
    ticker: str,
    period: str = "annual",
    limit: int = 5,
) -> pd.DataFrame:
    """ROE, ROA, 부채비율, P/E, P/FCF 등 핵심 비율 시계열."""
    data = _fmp_get(
        "key-metrics",
        {"symbol": ticker, "period": period, "limit": limit},
    )
    return _fmp_to_dataframe(data)


@cached("fmp_quote", TTL_QUOTE, "json")
@cached("company_screener", TTL_SCREENER, "json")
def fetch_company_screener(
    country: str = "US",
    market_cap_min: float = 1_000_000_000,
    actively_trading: bool = True,
    limit: int = 10000,
    exclude_funds: bool = True,
) -> list[dict]:
    """FMP /stable/company-screener — 서버사이드 필터로 유니버스 발굴 (Phase 8).

    전 종목 펀더멘털을 종목별로 받지 않고, 시총·거래중 등으로 한 번에 후보군을
    압축. 응답에 price/marketCap/sector/industry/lastAnnualDividend 포함 →
    이걸로 universe 테이블을 채우고, 점수용 ROE/EV-Sales 는 별도 key-metrics 보강.

    참고: country="KR" 은 FMP 가 미국 상장 ADR/OTC 한국 대기업만 줌 (~십수 개).
    실제 KOSPI/KOSDAQ 전 종목은 이 플랜에 없음.
    """
    params: dict = {
        "country": country,
        "marketCapMoreThan": int(market_cap_min),
        "limit": limit,
    }
    if actively_trading:
        params["isActivelyTrading"] = "true"
    data = _fmp_get("company-screener", params)
    if not data:
        return []
    if exclude_funds:
        data = [d for d in data if not (d.get("isEtf") or d.get("isFund"))]
    return data


def fetch_quote(ticker: str) -> dict:
    """FMP /stable/quote — 단일 종목의 실시간 시세 + 시가총액.

    Returns
    -------
    dict
        price, marketCap, change, name, exchange 등 포함.
    """
    data = _fmp_get("quote", {"symbol": ticker})
    if not data:
        raise DataValidationError(
            f"FMP quote 빈 응답: ticker={ticker}", source="FMP"
        )
    return data[0] if isinstance(data, list) else data


@cached("fmp_profile", TTL_PROFILE, "json")
def fetch_profile(ticker: str) -> dict:
    """FMP /stable/profile — 회사 프로필 (산업, 섹터, 배당, 시총)."""
    data = _fmp_get("profile", {"symbol": ticker})
    if not data:
        raise DataValidationError(
            f"FMP profile 빈 응답: ticker={ticker}", source="FMP"
        )
    return data[0] if isinstance(data, list) else data


@cached("crypto_top", TTL_CRYPTO, "json")
def fetch_crypto_top(top_n: int = 50) -> list[dict]:
    """CoinGecko 시가총액 상위 N개 암호화폐 목록.

    Returns
    -------
    list[dict]
        각 dict: id, symbol, name, current_price, market_cap,
        market_cap_rank, price_change_percentage_24h 등.
    """
    cg = _coingecko_client()
    try:
        data = cg.get_coins_markets(
            vs_currency="usd",
            order="market_cap_desc",
            per_page=min(top_n, 250),
            page=1,
        )
    except Exception as e:  # noqa: BLE001
        raise DataFetchError(
            f"CoinGecko top markets 호출 실패: top_n={top_n}", source="CoinGecko"
        ) from e

    if not data:
        raise DataValidationError(
            "CoinGecko top markets 빈 응답", source="CoinGecko"
        )
    return data


@cached("fmp_ratios", TTL_STATEMENTS, "dataframe")
def fetch_ratios(
    ticker: str,
    period: str = "annual",
    limit: int = 5,
) -> pd.DataFrame:
    """수익성·유동성·레버리지 비율 시계열 (key_metrics 와 일부 중복, 더 광범위).

    참고: stable 에서는 `ratios` 와 `metrics-ratios` 둘 다 존재 — 후자가 약간 더 풍부.
    여기선 호환성 위해 `ratios` 사용.
    """
    data = _fmp_get(
        "ratios",
        {"symbol": ticker, "period": period, "limit": limit},
    )
    return _fmp_to_dataframe(data)


# ----------------------------------------------------------------------
# 위키피디아 페이지뷰 (Wikimedia REST API — 키 불필요, Phase 6 대체 데이터)
# ----------------------------------------------------------------------
#
# 대중 관심도 프록시. 예: "Bitcoin" 페이지뷰 → 리테일 암호화폐 관심 → 가격.
# REST API 는 키가 필요 없지만 **descriptive User-Agent 가 필수** (없으면 403/429).
# 데이터는 2015-07 부터, 일별 granularity 직접 제공.
# ----------------------------------------------------------------------

WIKI_PAGEVIEWS_BASE = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
)
# Wikimedia 정책상 연락처가 담긴 식별 가능한 UA 요구
WIKI_USER_AGENT = "AI-Investment-Bot/0.6 (personal quant research; https://github.com/Zonnx999/AI-Investment-Bot)"


def _parse_wikipedia_items(items: list[dict], article: str) -> pd.Series:
    """Wikimedia 응답 items → 일별 페이지뷰 Series (순수 함수, 오프라인 테스트용)."""
    s = pd.Series(
        {
            pd.to_datetime(it["timestamp"][:8], format="%Y%m%d"): float(it["views"])
            for it in items
        },
        name=article,
        dtype=float,
    )
    s.index.name = "date"
    return s.sort_index()


@cached("wikipedia", TTL_WIKI, "series")
def fetch_wikipedia_pageviews(
    article: str = "Bitcoin",
    days: int = 3650,
    project: str = "en.wikipedia",
) -> pd.Series:
    """위키피디아 문서의 일별 조회수 (대중 관심도 프록시).

    Parameters
    ----------
    article : str
        문서 제목. 공백은 밑줄. 예: "Bitcoin", "Tesla,_Inc.".
    days : int
        오늘로부터 며칠 전까지. 기본 10년 (lead-lag 분석용 장기 시계열).

    Raises
    ------
    DataValidationError   문서 없음(404) 또는 빈 응답.
    ApiHttpError          기타 HTTP 4xx/5xx.
    ApiTimeoutError / ApiConnectionError   네트워크 실패.
    """
    import requests
    from urllib.parse import quote

    end = datetime.utcnow()
    start = end - timedelta(days=days)
    url = (
        f"{WIKI_PAGEVIEWS_BASE}/{project}/all-access/user/"
        f"{quote(article, safe='')}/daily/{start:%Y%m%d}/{end:%Y%m%d}"
    )

    try:
        response = get_http_session().get(url, headers={"User-Agent": WIKI_USER_AGENT})
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if is_timeout(e):
            raise ApiTimeoutError(
                f"위키피디아 '{article}' 타임아웃 (재시도 {RETRY_TOTAL}회 포함)",
                source="Wikipedia",
            ) from e
        raise ApiConnectionError(
            f"위키피디아 '{article}' 연결 실패", source="Wikipedia"
        ) from e

    if response.status_code == 404:
        raise DataValidationError(
            f"위키피디아 문서 '{article}' 없음 (제목/언어 확인)", source="Wikipedia"
        )
    if not response.ok:
        raise ApiHttpError(
            f"위키피디아 '{article}': HTTP {response.status_code}",
            status_code=response.status_code, source="Wikipedia",
        )

    items = response.json().get("items", [])
    if not items:
        raise DataValidationError(
            f"위키피디아 '{article}' 빈 응답", source="Wikipedia"
        )
    return _parse_wikipedia_items(items, article)


# TODO(향후 대체 데이터 — 새 의존성 필요, 사용자 우선순위 대기):
# - Google Trends (pytrends, fragile), SEC EDGAR 13F (파싱 부담)
# - 한국은행 ECOS API (한국 거시지표 더 풍부 — 금리/환율/물가/산업생산)
