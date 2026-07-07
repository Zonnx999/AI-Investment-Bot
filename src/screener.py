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

import math
from dataclasses import dataclass, field
from typing import Iterable, TypedDict

from src.data_fetcher import (
    fetch_crypto_top,
    fetch_key_metrics,
    fetch_quote,
    fetch_ratios,
)
from src.exceptions import DataFetchError
from src.logger import get_logger
from src.utils import clip as _clip  # 공용 순수 헬퍼 (utils 로 통합 — universe 와 공유)

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# 점수 분해 (ScoreCard) — 총점뿐 아니라 '왜 이 점수인지'를 항목별로 보관.
# scan --check / 텔레그램 /stock 이 이 분해를 그대로 보여줌.
# ----------------------------------------------------------------------


@dataclass
class Component:
    """점수 한 항목: 라벨, 획득점수, 만점, 원시값 설명 (예: ROE '15.2%')."""

    label: str
    points: float
    max_points: float
    detail: str

    def as_tuple(self) -> list:
        return [self.label, round(self.points, 1), self.max_points, self.detail]


@dataclass
class ScoreCard:
    total: int
    components: list[Component] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": self.total, "components": [c.as_tuple() for c in self.components]}


def latest_fundamentals(ticker: str) -> dict:
    """key-metrics + ratios 최신 행 병합 — 점수 계산용 통합 dict.

    grossProfitMargin·priceToBookRatio 는 ratios 에만, 나머지는 key-metrics 에 있어
    둘 다 받아 병합 (충돌 시 key-metrics 우선). 둘 다 비면 {} 반환.
    """
    merged: dict = {}
    km = fetch_key_metrics(ticker, limit=1)
    if not km.empty:
        merged.update(km.iloc[-1].to_dict())
    rt = fetch_ratios(ticker, limit=1)
    if not rt.empty:
        for k, v in rt.iloc[-1].to_dict().items():
            merged.setdefault(k, v)
    return merged


def has_fundamentals(metrics: dict) -> bool:
    """metrics 에 '실제 데이터' 가 하나라도 있는지 판별 (순수 함수).

    빈 dict / 값이 전부 None·NaN·Inf·빈 문자열이면 False — 이런 종목을 그대로
    점수 함수에 태우면 default 기반의 '조용한 0점' 이 되어 랭킹 바닥에 깔림
    → 호출부(screen_one / universe._compute_enrich)가 **점수 생략(skip)** 하도록.

    주의: 값이 '정당한 0' (예: netDebtToEBITDA=0.0) 이면 True — 결측과 0 을 구분.
    문자열은 전부 비데이터 취급 — 실제 FMP 행은 수치 메트릭이 전부 null 이어도
    식별자 문자열(symbol/fiscalYear/period/reportedCurrency)을 항상 포함하므로,
    문자열을 데이터로 세면 이 가드가 실데이터에서 절대 발화하지 않음.
    """
    for v in metrics.values():
        if v is None:
            continue
        if isinstance(v, str):
            continue   # 식별자·"N/A" 류 — 점수 입력은 전부 수치
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            continue
        return True
    return False


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
    """dict.get 후 None/비숫자/NaN/Inf 를 default 로 치환 + float 변환.

    FMP/pandas 응답은 결측을 None 뿐 아니라 ``"N/A"`` 같은 문자열이나
    ``float('nan')`` (pandas ``to_dict()``) 으로도 준다. 거르지 않으면
    (1) ``float("N/A")`` → ValueError 크래시, (2) NaN 이 ``_clip`` 을 그대로
    통과해(``min(hi, nan)==hi``) 해당 컴포넌트에 **만점**을 줘 총점이 과대계상된다.
    """
    v = d.get(key)
    if v is None:
        return float(default)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(f) or math.isinf(f):
        return float(default)
    return f


def health_scorecard(metrics: dict) -> ScoreCard:
    """재무 건전성 (0~100) — 학술 근거 기반 Quality 팩터 (병합 metrics 필요).

    1. 총이익률 (Gross Profitability, Novy-Marx 2013) 25 — 가장 강력한 Quality 신호
    2. ROIC (투하자본수익률) 20 — 자본비용 초과 여부 = 진짜 가치창출
    3. ROE 20
    4. 순부채/EBITDA 20 (낮을수록↑)
    5. 이익의 질 (incomeQuality, CFO/순이익) 10
    6. 유동비율 5
    """
    gp = _safe(metrics, "grossProfitMargin") * 100        # ratios
    roic = _safe(metrics, "returnOnInvestedCapital") * 100  # key-metrics
    roe = _safe(metrics, "returnOnEquity") * 100
    nde = _safe(metrics, "netDebtToEBITDA", 10.0)
    iq = _safe(metrics, "incomeQuality")
    cr = _safe(metrics, "currentRatio", 1.0)

    # 음수 NDE 의 두 얼굴: 순현금(netDebt<0, 좋음) vs 적자(EBITDA<0, 최악).
    # 비율만으론 구분 불가 → evToEBITDA 부호로 판별 (EBITDA<0 이면 음수).
    # 적자발 음수를 그대로 클립하면 최악의 레버리지가 만점(§4.10 #5 부호 함정).
    ev_eb = _safe(metrics, "evToEBITDA", 1.0)
    nde_loss = nde < 0 and ev_eb < 0

    comps = [
        Component("총이익률(GP)", _clip(gp * 0.625, 0, 25), 25, f"{gp:.0f}%"),
        Component("ROIC", _clip(roic * 1.0, 0, 20), 20, f"{roic:.1f}%"),
        Component("ROE", _clip(roe * 1.67, 0, 20), 20, f"{roe:.1f}%"),
        Component("순부채/EBITDA",
                  0.0 if nde_loss else _clip(20.0 - nde * 2.5, 0, 20),
                  20, f"{nde:.1f}x" + (" (적자)" if nde_loss else "")),
        Component("이익의 질", _clip(iq * 2.0, 0, 10), 10, f"{iq:.2f}"),
        Component("유동비율", _clip(min(cr, 3.0) * 1.67, 0, 5), 5, f"{cr:.2f}"),
    ]
    return ScoreCard(round(min(100.0, sum(c.points for c in comps))), comps)


def value_scorecard(quote: dict, metrics: dict) -> ScoreCard:
    """저평가도 (0~100) — 무배당 성장주 페널티 제거 + EV/EBITDA·PBR 추가.

    1. EV/EBITDA 25 (자본구조 중립) 2. EV/Sales 15(금융 중립) 3. PBR 15
    4. 이익수익률(=1/PER) 25  5. 배당수익률 10(과거 35→축소)  6. FCF수익률 10
    """
    ev_ebitda = _safe(metrics, "evToEBITDA", 20.0)
    ev_sales = _safe(metrics, "evToSales", 10.0)
    pbr = _safe(metrics, "priceToBookRatio", 5.0)            # ratios
    ey = _safe(metrics, "earningsYield") * 100
    fcf = _safe(metrics, "freeCashFlowYield") * 100

    price = _safe(quote, "price", 1.0)
    last_div = _safe(quote, "lastDividend") or _safe(quote, "lastAnnualDividend")
    dy = (last_div / max(price, 0.01)) * 100 if price else 0.0

    industry = (quote.get("industry") or "") + (quote.get("sector") or "")
    is_fin = any(x in industry for x in ("Bank", "Financial", "Insurance"))
    ev_sales_pts = 7.5 if is_fin else _clip(15.0 - ev_sales * 2.0, 0, 15)

    # 음수 배수(적자 EBITDA / 자본잠식 음수 PBR)는 '저평가' 아님 → 0점.
    # 안 그러면 "25 − (음수)×1.25 > 25" 로 적자기업이 만점 받는 역설.
    ev_ebitda_pts = _clip(25.0 - ev_ebitda * 1.25, 0, 25) if ev_ebitda > 0 else 0.0
    pbr_pts = _clip(15.0 - pbr * 1.5, 0, 15) if pbr > 0 else 0.0

    comps = [
        Component("EV/EBITDA", ev_ebitda_pts, 25,
                  f"{ev_ebitda:.1f}x" + ("" if ev_ebitda > 0 else " (적자)")),
        Component("EV/Sales", ev_sales_pts, 15, ("금융중립" if is_fin else f"{ev_sales:.1f}x")),
        Component("PBR", pbr_pts, 15, f"{pbr:.2f}" + ("" if pbr > 0 else " (자본잠식)")),
        Component("이익수익률", _clip(ey * 5.0, 0, 25), 25, f"{ey:.1f}%"),
        Component("배당수익률", _clip(dy * 2.0, 0, 10), 10, f"{dy:.1f}%"),
        Component("FCF수익률", _clip(fcf * 1.5, 0, 10), 10, f"{fcf:.1f}%"),
    ]
    return ScoreCard(round(min(100.0, sum(c.points for c in comps))), comps)


def calculate_health_score(metrics: dict) -> int:
    """건전성 점수 int (하위호환 wrapper)."""
    return health_scorecard(metrics).total


def calculate_value_score(quote: dict, metrics: dict) -> int:
    """저평가도 점수 int (하위호환 wrapper)."""
    return value_scorecard(quote, metrics).total


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


class ScreenedStock(TypedDict):
    """screen_one() 의 반환 스키마 — dashboard/index.html 의 JSON 필드와 1:1."""

    symbol: str
    company_name: str
    price: float
    dividend: float
    market_cap: float
    health_score: int
    value_score: int
    total_score: int
    roe: float
    ev_to_sales: float
    net_debt_to_ebitda: float


def screen_one(ticker: str) -> ScreenedStock | None:
    """한 종목의 데이터를 받아 dashboard 호환 dict 으로 반환.

    실패 시 None (호출자가 skip). 광범위 except 는 호출자 루프에서 처리.
    fundamentals 가 아예 없으면(빈/전부 결측) 0점으로 랭킹 바닥에 깔리는 대신
    **skip(None)** — '데이터 없음' 과 '나쁜 점수' 를 구분.
    """
    try:
        quote = fetch_quote(ticker)
    except DataFetchError as e:
        logger.warning("Quote 실패 — %s: %s", ticker, e)
        return None

    metrics: dict = {}
    try:
        metrics = latest_fundamentals(ticker)   # key-metrics + ratios 병합
    except DataFetchError as e:
        logger.info("Fundamentals 미가용 — %s: %s", ticker, e)

    if not has_fundamentals(metrics):
        logger.info("Fundamentals 없음 — %s: 점수 생략 (skip)", ticker)
        return None

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
) -> list[ScreenedStock]:
    """워치리스트 일괄 스크리닝. 실패 종목은 skip + 로그."""
    results: list[ScreenedStock] = []
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
