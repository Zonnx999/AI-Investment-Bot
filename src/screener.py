"""
src/screener.py
===============
가치주 스크리너 — Haiku 가 만든 점수 공식을 우리 인프라(_fmp_get, logger, exceptions)
위에 클린하게 얹어 재구현.

구조
----
- 워치리스트: 미국/한국 각각 ~30개 핵심 종목 (전종목 스캔 X — API 콜 폭주 방지)
- 점수: 재무 건전성 + 저평가도 → 종합점수
- 출력: list[dict] (dashboard/index.html 의 JSON 스키마와 호환)

데이터 소스
----------
- 미국: FMP /stable/quote + /stable/key-metrics (정밀)
- 한국: FMP 가 한국 종목을 제한적으로 지원 → 일단 동일 경로 시도, 실패 시 스킵
- 암호화폐: CoinGecko markets endpoint (시총 상위 N)

JSON 스키마 (dashboard/index.html 호환)
--------------------------------------
주식:
    {symbol, company_name, price, dividend, market_cap,
     health_score, value_score, total_score,
     roe, ev_to_sales, net_debt_to_ebitda}

암호화폐:
    {symbol, name, rank, price, rank_score, volatility_score, total_score,
     change_24h, market_cap}
"""

from __future__ import annotations

from typing import Iterable

from src.data_fetcher import (
    fetch_crypto_top,
    fetch_key_metrics,
    fetch_quote,
)
from src.exceptions import DataFetchError
from src.logger import get_logger

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# 워치리스트 (전종목 스캔 대신 핵심 종목만 — API 한도 보호)
# ----------------------------------------------------------------------

US_WATCHLIST: tuple[str, ...] = (
    # 메가테크
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    # 금융
    "BRK-B", "JPM", "BAC", "V", "MA", "GS",
    # 헬스케어
    "UNH", "JNJ", "LLY", "PFE", "ABBV",
    # 소비재
    "WMT", "PG", "KO", "PEP", "COST", "MCD", "NKE",
    # 산업/에너지
    "XOM", "CVX", "BA", "CAT", "GE",
    # 통신/기타
    "DIS", "NFLX", "T", "VZ",
    # 한국 인터넷 미국 상장
    "CPNG",
    # AI/반도체 추가
    "AMD", "AVGO", "INTC", "QCOM",
)

# 한국 종목 — FMP 가 .KS suffix 로 지원하는 경우가 있음
KR_WATCHLIST: tuple[str, ...] = (
    "005930.KS",  # 삼성전자
    "000660.KS",  # SK하이닉스
    "035420.KS",  # 네이버
    "035720.KS",  # 카카오
    "005380.KS",  # 현대차
    "051910.KS",  # LG화학
    "006400.KS",  # 삼성SDI
    "207940.KS",  # 삼성바이오로직스
    "068270.KS",  # 셀트리온
    "012330.KS",  # 현대모비스
    "055550.KS",  # 신한지주
    "105560.KS",  # KB금융
    "017670.KS",  # SK텔레콤
    "030200.KS",  # KT
    "066570.KS",  # LG전자
)


# ----------------------------------------------------------------------
# 점수 공식 (Haiku 의 공식을 보존)
# ----------------------------------------------------------------------


def _safe(d: dict, key: str, default: float = 0.0) -> float:
    """dict.get 후 None 을 default 로 치환 + float 변환."""
    v = d.get(key)
    return float(default if v is None else v)


def calculate_health_score(metrics: dict) -> int:
    """재무 건전성 점수 (0~100). FMP /stable/key-metrics 응답 dict 기준.

    - Net Debt/EBITDA  : 낮을수록 좋음 → 최대 25점
    - ROE              : 높을수록 좋음 → 최대 30점
    - FCF Yield        : 높을수록 좋음 → 최대 25점
    - Current Ratio    : 1~3 적정 → 최대 15점
    - Income Quality   : 영업이익 대비 영업CF → 최대 5점
    """
    net_debt_ebitda = _safe(metrics, "netDebtToEBITDA", 10.0)
    roe_pct = _safe(metrics, "returnOnEquity") * 100  # 비율 → %
    fcf_yield_pct = _safe(metrics, "freeCashFlowYield") * 100
    current_ratio = _safe(metrics, "currentRatio", 1.0)
    income_quality = _safe(metrics, "incomeQuality")

    score = 0.0
    score += max(0.0, 25.0 - net_debt_ebitda * 3.0)
    score += min(30.0, max(0.0, roe_pct * 2.5))
    score += min(25.0, max(0.0, fcf_yield_pct * 3.0))
    score += min(15.0, min(current_ratio, 3.0) * 5.0)
    score += min(5.0, max(0.0, income_quality))
    return round(min(100.0, score))


def calculate_value_score(quote: dict, metrics: dict) -> int:
    """저평가도 점수 (0~100). quote 와 key-metrics 둘 다 사용.

    - EV/Sales        : 낮을수록 좋음 → 최대 30점 (금융사 예외)
    - 배당수익률      : 최대 35점
    - FCF Yield       : 최대 20점
    - Earnings Yield  : 최대 15점
    """
    ev_to_sales = _safe(metrics, "evToSales", 10.0)
    fcf_yield_pct = _safe(metrics, "freeCashFlowYield") * 100
    earnings_yield_pct = _safe(metrics, "earningsYield") * 100

    # 배당수익률 = lastDividend / price * 100
    price = _safe(quote, "price", 1.0)
    last_dividend = _safe(quote, "lastDividend") or _safe(quote, "lastAnnualDividend")
    dividend_yield_pct = (last_dividend / max(price, 0.01)) * 100 if price else 0.0

    # 금융업종은 EV/Sales 의미 적음 → 중립 처리
    industry = (quote.get("industry") or "") + (quote.get("sector") or "")
    is_financial = "Bank" in industry or "Financial" in industry or "Insurance" in industry

    score = 0.0
    if is_financial:
        score += 15.0  # 중립값
    else:
        score += max(0.0, 30.0 - ev_to_sales * 4.0)
    score += min(35.0, dividend_yield_pct * 5.0)
    score += min(20.0, max(0.0, fcf_yield_pct * 3.0))
    score += min(15.0, max(0.0, earnings_yield_pct * 3.0))
    return round(min(100.0, score))


def calculate_crypto_scores(coin: dict) -> dict:
    """암호화폐 점수 — 시총 순위 + 24h 변동률 기반 (펀더멘털 부재).

    rank_score      : 상위일수록 높음 (0~100)
    volatility_score: -50%~+50% 변동률을 0~100 으로 매핑
    total_score     : rank 70% + vol 30%
    """
    rank = int(coin.get("market_cap_rank") or 999)
    change_24h = float(coin.get("price_change_percentage_24h") or 0.0)

    rank_score = max(0.0, 100.0 - rank)
    volatility_score = max(0.0, min(100.0, 50.0 + change_24h * 2.0))
    total_score = rank_score * 0.7 + volatility_score * 0.3
    return {
        "rank_score": round(rank_score),
        "volatility_score": round(volatility_score),
        "total_score": round(total_score),
    }


# ----------------------------------------------------------------------
# 종목 단위 screen — quote + key-metrics 받아서 dict 한 개 생성
# ----------------------------------------------------------------------


def screen_one(ticker: str) -> dict | None:
    """한 종목의 데이터를 받아 dashboard 호환 dict 으로 반환.

    실패 시 None (호출자가 skip). 광범위 except 는 호출자 루프에서 처리.
    """
    try:
        quote = fetch_quote(ticker)
    except DataFetchError as e:
        logger.warning("Quote 실패 — %s: %s", ticker, e)
        return None

    metrics_df = None
    metrics: dict = {}
    try:
        metrics_df = fetch_key_metrics(ticker, limit=1)
    except DataFetchError as e:
        # 한국 종목 등 FMP plan 에서 막힌 케이스 — 기본값으로 점수 산출
        logger.info("Key-metrics 미가용 — %s: %s (기본값으로 진행)", ticker, e)

    if metrics_df is not None and not metrics_df.empty:
        metrics = metrics_df.iloc[-1].to_dict()

    health = calculate_health_score(metrics)
    value = calculate_value_score(quote, metrics)
    total = round((health + value) / 2)

    price = _safe(quote, "price", 0.0)
    last_dividend = _safe(quote, "lastDividend") or _safe(quote, "lastAnnualDividend")
    dividend_pct = (last_dividend / max(price, 0.01)) * 100 if price else 0.0

    return {
        "symbol": ticker,
        "company_name": quote.get("name", ""),
        "price": price,
        "dividend": dividend_pct,
        "market_cap": _safe(quote, "marketCap", 0.0),
        "health_score": health,
        "value_score": value,
        "total_score": total,
        "roe": _safe(metrics, "returnOnEquity") * 100,
        "ev_to_sales": _safe(metrics, "evToSales"),
        "net_debt_to_ebitda": _safe(metrics, "netDebtToEBITDA"),
    }


def screen_watchlist(
    tickers: Iterable[str], country_label: str = "미국"
) -> list[dict]:
    """워치리스트 일괄 스크리닝. 실패 종목은 skip + 로그."""
    results: list[dict] = []
    tickers_list = list(tickers)
    logger.info("[%s] 스크리닝 시작 — %d 종목", country_label, len(tickers_list))

    for i, t in enumerate(tickers_list, 1):
        row = screen_one(t)
        if row is not None:
            results.append(row)
        if i % 10 == 0:
            logger.info("[%s] 진행 %d/%d", country_label, i, len(tickers_list))

    # total_score 내림차순 정렬
    results.sort(key=lambda r: r.get("total_score", 0), reverse=True)
    logger.info(
        "[%s] 완료 — %d/%d 종목 성공", country_label, len(results), len(tickers_list)
    )
    return results


def screen_crypto(top_n: int = 50) -> list[dict]:
    """암호화폐 시총 상위 N개 스크리닝."""
    logger.info("[크립토] 스크리닝 시작 — top %d", top_n)
    try:
        coins = fetch_crypto_top(top_n=top_n)
    except DataFetchError as e:
        logger.error("크립토 목록 가져오기 실패: %s", e)
        return []

    results: list[dict] = []
    for coin in coins:
        scores = calculate_crypto_scores(coin)
        results.append(
            {
                "symbol": (coin.get("symbol") or "").upper(),
                "name": coin.get("name", ""),
                "rank": int(coin.get("market_cap_rank") or 999),
                "price": float(coin.get("current_price") or 0.0),
                "market_cap": float(coin.get("market_cap") or 0.0),
                "change_24h": float(coin.get("price_change_percentage_24h") or 0.0),
                **scores,
            }
        )

    results.sort(key=lambda r: r.get("total_score", 0), reverse=True)
    logger.info("[크립토] 완료 — %d 종목", len(results))
    return results
