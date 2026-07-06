"""
src/universe.py
===============
Phase 8 — 전 종목 유니버스 DB + 오프라인 전수 스크리닝.

고정 워치리스트(~40종목) 대신, FMP company-screener 로 투자가능 유니버스를
발굴하고 SQLite 에 영속 저장 → 종목별 key-metrics 로 보강(주 1회 배치) →
이후 스캔은 **API 0콜 오프라인**으로 전수조사.

3단계
-----
1. discover()  : company-screener(US/KR) + CoinGecko(crypto) → `screened` 테이블 upsert
                 (할인 없이 시총·가격·섹터; 크립토는 이때 바로 점수)
2. enrich()    : 보강 안 됐거나 오래된 주식만 key-metrics → 점수 → DB 갱신 (재개 가능)
3. scan()      : 순수 SQL 정렬 → 저평가 상위 발굴 / 특정 종목 순위 조회

점수 공식은 screener.py 의 calculate_* 재사용 (단일 진실 공급원).
"""

from __future__ import annotations

import math
import numbers
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.exceptions import DataFetchError
from src.logger import get_logger
from src.screener import (
    calculate_crypto_scores,
    calculate_health_score,
    calculate_value_score,
)
from src.storage import add_column_if_missing, get_storage

logger = get_logger(__name__)

# 시장별 발굴 설정 (시총 floor 는 사용자 합의: 중대형주만)
MARKET_CAP_FLOOR = 1_000_000_000          # $1B (US)
KR_MARKET_CAP_FLOOR = 500_000_000_000     # 5천억 KRW (한국 중대형, MKTCAP 는 원화)
CRYPTO_MCAP_FLOOR = 1_000_000_000         # $1B
CRYPTO_TOP_N = 100
ENRICH_MAX_AGE = timedelta(days=7)        # 펀더멘털 재보강 주기

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screened (
    symbol           TEXT NOT NULL,
    market           TEXT NOT NULL,          -- US | KR | CRYPTO
    name             TEXT,
    sector           TEXT,
    industry         TEXT,
    price            REAL,
    market_cap       REAL,
    dividend_yield   REAL,
    roe              REAL,
    ev_to_sales      REAL,
    fcf_yield        REAL,
    net_debt_ebitda  REAL,
    health_score     INTEGER,
    value_score      INTEGER,
    total_score      INTEGER,
    per              REAL,                          -- KR(DART): 주가수익비율
    pbr              REAL,                          -- KR(DART): 주가순자산비율
    detail           TEXT,                          -- 점수 분해 JSON (--check/텔레그램)
    enriched         INTEGER NOT NULL DEFAULT 0,   -- 0=발굴만, 1=보강+점수 완료
    discovered_at    TEXT,
    updated_at       TEXT,
    PRIMARY KEY (symbol, market)   -- 주식 'M'(Macy's)과 크립토 'M' 충돌 방지
);
CREATE INDEX IF NOT EXISTS idx_screened_market_score
    ON screened (market, total_score DESC);
"""


@dataclass
class ScanRow:
    symbol: str
    market: str
    name: str
    sector: str
    price: float
    market_cap: float
    total_score: int
    value_score: int
    health_score: int
    roe: float | None
    per: float | None = None
    pbr: float | None = None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = get_storage().conn
    conn.executescript(_SCHEMA)
    # 기존 DB 마이그레이션 (9b 도입) — Turso stale 레플리카 안전(중복 컬럼 오류 삼킴)
    for col, coltype in (("per", "REAL"), ("pbr", "REAL"), ("detail", "TEXT")):
        add_column_if_missing(conn, "screened", col, coltype)
    return conn


# ----------------------------------------------------------------------
# 배치 쓰기 (Turso 왕복 최소화)
# ----------------------------------------------------------------------
#
# libsql 임베디드 레플리카는 쓰기 statement 마다 클라우드(태평양) 왕복을 한다
# (실측 ~1s/statement). executemany 도 내부적으로 statement 루프라 효과 없음.
# 반면 executescript 는 다중 statement 를 **한 요청으로 배치** 전송 → 실측 ~40ms/row.
# 따라서 재점수 UPDATE 들을 모아 executescript 한 방으로 흘려보낸다 (전수 재점수
# 240분 → ~1-2분). executescript 는 파라미터 바인딩을 못 받으므로 값은 _sql_lit 로
# 직렬화한다 (회사명 아포스트로피·detail JSON 따옴표·한글 안전 처리).
#
# 주의: 레플리카는 자기 쓰기를 sync() 전에는 로컬에 안 비춘다 → 루프 도중 방금 쓴
# 행을 다시 읽지 말 것. 최종 반영은 호출부(build_universe)의 get_storage().sync() 가 담당.


def _sql_lit(v) -> str:
    """파이썬 값을 SQLite 리터럴 문자열로 직렬화 (executescript 용, 파라미터 불가).

    None→NULL, 정수→그대로, 실수→유한치만(NaN/inf→NULL), 그 외→작은따옴표 문자열
    (내부 작은따옴표는 '' 로 이스케이프 — SQLite 표준). numpy 스칼라도 numbers ABC 로 포착.
    """
    if v is None:
        return "NULL"
    if isinstance(v, numbers.Integral):       # int / numpy 정수 / bool
        return str(int(v))
    if isinstance(v, numbers.Real):            # float / numpy 실수
        f = float(v)
        return repr(f) if math.isfinite(f) else "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def _build_update(set_cols: dict, symbol: str, market: str) -> str:
    """screened 한 행 UPDATE 문 생성 (값은 리터럴로 인라인). 복합키로 한정."""
    sets = ", ".join(f"{col}={_sql_lit(val)}" for col, val in set_cols.items())
    return (f"UPDATE screened SET {sets} "
            f"WHERE symbol={_sql_lit(symbol)} AND market={_sql_lit(market)}")


def _flush_updates(conn, statements: list[str]) -> None:
    """모인 UPDATE 들을 executescript 한 방으로 전송 (Turso 왕복 1회). 빈 리스트는 no-op."""
    if not statements:
        return
    conn.executescript(";\n".join(statements) + ";")
    conn.commit()


# ----------------------------------------------------------------------
# 1. 발굴 (discover)
# ----------------------------------------------------------------------


def _upsert_universe_row(conn, *, symbol, market, name, sector, industry,
                         price, market_cap, dividend_yield) -> None:
    """발굴 필드만 upsert — 기존 보강 점수(roe/scores/enriched)는 보존."""
    conn.execute(
        """
        INSERT INTO screened (symbol, market, name, sector, industry, price,
                              market_cap, dividend_yield, discovered_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, market) DO UPDATE SET
            name=excluded.name, sector=excluded.sector,
            industry=excluded.industry, price=excluded.price,
            market_cap=excluded.market_cap, dividend_yield=excluded.dividend_yield,
            updated_at=excluded.updated_at
        """,
        (symbol, market, name, sector, industry, price, market_cap,
         dividend_yield, _utcnow(), _utcnow()),
    )


def _store_crypto_scored(conn, *, symbol, name, price, market_cap, scores) -> None:
    """크립토는 발굴 즉시 점수까지 (CoinGecko 가 필요한 데이터 다 줌)."""
    conn.execute(
        """
        INSERT INTO screened (symbol, market, name, price, market_cap,
                              total_score, value_score, health_score,
                              enriched, discovered_at, updated_at)
        VALUES (?, 'CRYPTO', ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(symbol, market) DO UPDATE SET
            name=excluded.name, price=excluded.price, market_cap=excluded.market_cap,
            total_score=excluded.total_score, value_score=excluded.value_score,
            health_score=excluded.health_score, enriched=1, updated_at=excluded.updated_at
        """,
        (symbol, name, price, market_cap, scores["total_score"],
         scores["volatility_score"], scores["rank_score"], _utcnow(), _utcnow()),
    )


def _latest_kr_trading_day() -> tuple[str | None, list[dict]]:
    """오늘(KST)부터 거슬러 올라가며 KRX 데이터가 있는 영업일 찾기 (최대 10일).

    Returns (basDd, KOSPI 일별데이터) — 못 찾으면 (None, []).
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from src.data_fetcher import fetch_krx_daily

    now = _dt.now(ZoneInfo("Asia/Seoul"))
    for back in range(10):
        bas_dd = (now - timedelta(days=back)).strftime("%Y%m%d")
        try:
            data = fetch_krx_daily("KOSPI", bas_dd)
        except DataFetchError as e:
            logger.warning("KRX 일별 조회 실패 (%s) — %s", bas_dd, e)
            return None, []
        if data:
            return bas_dd, data
    return None, []


def _common_stock_codes(bas_dd: str) -> set[str]:
    """기본정보에서 '주권 + 보통주' 6자리 코드만 (ETF/리츠/우선주 제외)."""
    from src.data_fetcher import fetch_krx_base_info

    codes: set[str] = set()
    for market in ("KOSPI", "KOSDAQ"):
        for b in fetch_krx_base_info(market, bas_dd):
            if b.get("SECUGRP_NM") == "주권" and b.get("KIND_STKCERT_TP_NM") == "보통주":
                codes.add(b.get("ISU_SRT_CD"))
    return codes


def _discover_kr(conn, floor: float = KR_MARKET_CAP_FLOOR) -> int:
    """KRX 한국 전 종목 발굴 (코스피+코스닥). 보통주만, 시총 floor 이상.

    재무지표는 KRX 에 없음 → enriched=0 (점수는 Phase 9b DART 에서). 발굴 종목 수 반환.
    """
    from src.data_fetcher import fetch_krx_daily

    bas_dd, kospi = _latest_kr_trading_day()
    if not bas_dd:
        logger.warning("KRX 최근 영업일 데이터를 찾지 못함 — KR 발굴 스킵")
        return 0
    try:
        kosdaq = fetch_krx_daily("KOSDAQ", bas_dd)
    except DataFetchError as e:
        # KOSPI 실패가 (None, []) 로 우아하게 처리되는 것과 일관 — KOSDAQ 만 실패하면
        # KOSPI 발굴은 계속한다 (이 예외가 위로 새면 discover() KR 블록을 거쳐 CRYPTO 발굴까지 중단).
        logger.warning("KOSDAQ 발굴 실패 — KOSPI 만으로 진행: %s", e)
        kosdaq = []
    common = _common_stock_codes(bas_dd)

    n = 0
    for row in kospi + kosdaq:
        code = row.get("ISU_CD")
        if code not in common:                       # 우선주/리츠/ETF 제외
            continue
        try:
            price = float(row.get("TDD_CLSPRC") or 0)
            mcap = float(row.get("MKTCAP") or 0)
        except (TypeError, ValueError):
            continue
        if mcap < floor:                             # 중대형주만
            continue
        _upsert_universe_row(
            conn, symbol=code, market="KR", name=row.get("ISU_NM", ""),
            sector=row.get("SECT_TP_NM", ""), industry="",
            price=price, market_cap=mcap, dividend_yield=0.0,  # 배당은 DART(9b)
        )
        n += 1
    logger.info("KR 발굴: %d종목 (기준일 %s)", n, bas_dd)
    return n


def discover(
    markets: tuple[str, ...] = ("US", "KR", "CRYPTO"),
    market_cap_floor: float = MARKET_CAP_FLOOR,
) -> dict[str, int]:
    """유니버스 발굴 → screened 테이블 upsert. 시장별 발굴 종목 수 반환."""
    from src.data_fetcher import fetch_company_screener, fetch_crypto_top

    conn = _conn()
    counts: dict[str, int] = {}

    for mkt in markets:
        if mkt == "US":
            try:
                rows = fetch_company_screener(country="US", market_cap_min=market_cap_floor)
            except DataFetchError as e:
                logger.warning("발굴 실패 US — %s", e)
                counts["US"] = 0
                continue
            for r in rows:
                price = r.get("price") or 0.0
                last_div = r.get("lastAnnualDividend") or 0.0
                div_yield = (last_div / price * 100) if price else 0.0
                _upsert_universe_row(
                    conn, symbol=r["symbol"], market="US",
                    name=r.get("companyName", ""), sector=r.get("sector", ""),
                    industry=r.get("industry", ""), price=price,
                    market_cap=r.get("marketCap") or 0.0, dividend_yield=div_yield,
                )
            counts["US"] = len(rows)

        elif mkt == "KR":
            # US/CRYPTO 와 동일하게 시장 단위로 격리 — 기본정보(_common_stock_codes)
            # 등 다른 KRX 호출이 실패해도 CRYPTO 발굴까지 막지 않도록.
            try:
                counts["KR"] = _discover_kr(conn)
            except DataFetchError as e:
                logger.warning("발굴 실패 KR — %s", e)
                counts["KR"] = 0

        elif mkt == "CRYPTO":
            try:
                coins = fetch_crypto_top(top_n=CRYPTO_TOP_N)
            except DataFetchError as e:
                logger.warning("발굴 실패 CRYPTO — %s", e)
                counts["CRYPTO"] = 0
                continue
            n = 0
            for c in coins:
                if (c.get("market_cap") or 0) < CRYPTO_MCAP_FLOOR:
                    continue
                _store_crypto_scored(
                    conn, symbol=(c.get("symbol") or "").upper(),
                    name=c.get("name", ""), price=c.get("current_price") or 0.0,
                    market_cap=c.get("market_cap") or 0.0,
                    scores=calculate_crypto_scores(c),
                )
                n += 1
            counts["CRYPTO"] = n

    conn.commit()
    logger.info("발굴 완료: %s", counts)
    return counts


# ----------------------------------------------------------------------
# 2. 보강 (enrich) — 주식만, key-metrics 종목별
# ----------------------------------------------------------------------


def symbols_needing_enrichment(max_age: timedelta = ENRICH_MAX_AGE) -> list[tuple[str, str]]:
    """FMP key-metrics 보강이 필요한 (symbol, market) — 미보강/오래된 **US** 주식.

    KR 은 FMP 가 6자리 코드를 모름 → DART 로 별도 보강 (Phase 9b). 여기선 US 만.
    """
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - max_age).isoformat()
    rows = conn.execute(
        """
        SELECT symbol, market FROM screened
        WHERE market = 'US'
          AND (enriched = 0 OR updated_at < ?)
        ORDER BY market_cap DESC
        """,
        (cutoff,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _compute_enrich(conn, symbol: str, market: str) -> dict | None:
    """한 종목 key-metrics+ratios → 점수카드 → UPDATE 할 컬럼 dict. 데이터 없으면 None.

    순수 계산만 (DB 쓰기는 호출부가 배치로 — executescript 왕복 최소화).
    base SELECT 는 발굴 단계 데이터(로컬 레플리카에 이미 sync 됨)라 읽기 안전.
    """
    import json

    from src.screener import health_scorecard, latest_fundamentals, value_scorecard

    m = latest_fundamentals(symbol)   # key-metrics + ratios 병합
    if not m:
        return None

    # 발굴 단계에서 저장한 가격/섹터/배당을 quote 대용으로 사용 (value 점수에 필요).
    # value_scorecard 는 lastAnnualDividend(절대 배당액)로 배당수익률을 재계산하므로,
    # 저장된 dividend_yield(%) 에서 절대액을 역산해 넣어준다 (없으면 배당점수 0).
    base = conn.execute(
        "SELECT price, sector, industry, dividend_yield FROM screened WHERE symbol=? AND market=?",
        (symbol, market),
    ).fetchone()
    price, sector, industry, div_yield = base if base else (0.0, "", "", 0.0)
    last_div = (div_yield / 100.0) * price if (div_yield and price) else 0.0
    quote_like = {"price": price, "sector": sector, "industry": industry,
                  "lastAnnualDividend": last_div}

    health = health_scorecard(m)
    value = value_scorecard(quote_like, m)
    total = round((health.total + value.total) / 2)
    detail = json.dumps({"health": health.to_dict(), "value": value.to_dict()},
                        ensure_ascii=False)

    return {
        "roe": (m.get("returnOnEquity") or 0) * 100,
        "ev_to_sales": m.get("evToSales"),
        "fcf_yield": (m.get("freeCashFlowYield") or 0) * 100,
        "net_debt_ebitda": m.get("netDebtToEBITDA"),
        "health_score": health.total,
        "value_score": value.total,
        "total_score": total,
        "detail": detail,
        "enriched": 1,
        "updated_at": _utcnow(),
    }


def enrich(
    max_age: timedelta = ENRICH_MAX_AGE,
    limit: int | None = None,
    chunk_size: int = 200,
    on_progress: "Callable[[int, int, dict], None] | None" = None,
) -> dict[str, int]:
    """보강 필요한 주식들을 key-metrics 로 채움. 재개 가능.

    chunk_size: N종목마다 UPDATE 들을 executescript 한 방으로 flush (Turso 왕복 1회).
      종목별 쓰기는 태평양 왕복이라 매우 느림(~1s/행) → 배치로 왕복 횟수를 N분의 1 로.
      중단 시 마지막 미flush 분만 재작업, 원본은 캐시라 재계산 저렴.
    on_progress(i, total, stats): 매 종목 호출 (CLI 진행바 등). None 이면 미사용.

    Returns {"enriched": n, "no_data": n, "failed": n}.
    """
    conn = _conn()
    targets = symbols_needing_enrichment(max_age)
    if limit:
        targets = targets[:limit]

    stats = {"enriched": 0, "no_data": 0, "failed": 0}
    total = len(targets)
    logger.info("보강 시작: %d종목 (chunk_size=%d)", total, chunk_size)

    batch: list[str] = []
    for i, (symbol, market) in enumerate(targets, 1):
        try:
            cols = _compute_enrich(conn, symbol, market)
            if cols is None:
                stats["no_data"] += 1
            else:
                batch.append(_build_update(cols, symbol, market))
                stats["enriched"] += 1
        except DataFetchError as e:
            logger.warning("보강 실패 %s — %s", symbol, e)
            stats["failed"] += 1
        if len(batch) >= chunk_size:
            _flush_updates(conn, batch)
            batch.clear()
        if on_progress is not None:
            on_progress(i, total, stats)
        if i % 100 == 0:
            logger.info("보강 진행 %d/%d (%s)", i, total, stats)

    _flush_updates(conn, batch)  # 남은 분 flush
    logger.info("보강 완료: %s", stats)
    return stats


# ----------------------------------------------------------------------
# 2b. 한국 보강 (DART 펀더멘털) — Phase 9b
# ----------------------------------------------------------------------


def calculate_kr_scores(fin: dict, market_cap: float) -> dict:
    """DART 재무제표 + KRX 시총 → 한국 종목 점수 (순수 함수).

    health(건전성): ROE + 부채비율 + 영업이익률 + 흑자 보너스
    value(저평가): PER 낮을수록 + PBR 낮을수록 + 이익수익률(1/PER)
    임계·가중치는 튜닝 지점 (미국 screener.calculate_* 와 철학 동일, 입력만 다름).
    """
    ni, eq, debt = fin.get("net_income"), fin.get("equity"), fin.get("debt")
    rev, op = fin.get("revenue"), fin.get("op_income")

    # 자본잠식(equity<=0)은 ROE·부채비율·PBR 을 모두 무의미하게 만든다 — 음수 자기자본이
    # 비율 부호를 뒤집어 부실기업에 만점을 주는 역설을 막기 위해 strictly positive 만 인정
    # (§4.10(5)). eq<=0 이면 셋 다 None → 각 컴포넌트의 결측 처리(부채비율=0점)로 흘러간다.
    eq_ok = eq is not None and eq > 0

    roe = (ni / eq * 100) if (ni is not None and eq_ok) else None
    per = (market_cap / ni) if (ni and ni > 0) else None
    pbr = (market_cap / eq) if eq_ok else None
    debt_ratio = (debt / eq * 100) if (debt is not None and eq_ok) else None
    op_margin = (op / rev * 100) if (op is not None and rev) else None

    from src.screener import Component, ScoreCard

    def _clip(v, lo, hi):
        return max(lo, min(hi, v))

    h = [
        Component("ROE", _clip((roe or 0) * 2.5, 0, 35), 35,
                  f"{roe:.1f}%" if roe is not None else "—"),
        Component("부채비율", _clip(30.0 - (debt_ratio or 999) / 10, 0, 30), 30,
                  f"{debt_ratio:.0f}%" if debt_ratio is not None else "—"),
        Component("영업이익률", _clip((op_margin or 0) * 2, 0, 20), 20,
                  f"{op_margin:.1f}%" if op_margin is not None else "—"),
        Component("흑자", 15.0 if (ni is not None and ni > 0) else 0.0, 15,
                  "흑자" if (ni is not None and ni > 0) else "적자"),
    ]
    v = [
        Component("PER", _clip(40.0 - per * 1.5, 0, 40) if (per and per > 0) else 0, 40,
                  f"{per:.1f}" if per else "—"),
        Component("이익수익률", _clip((1.0 / per) * 100 * 2.5, 0, 25) if (per and per > 0) else 0, 25,
                  f"{100/per:.1f}%" if (per and per > 0) else "—"),
        Component("PBR", _clip(35.0 - pbr * 17.5, 0, 35) if (pbr and pbr > 0) else 0, 35,
                  f"{pbr:.2f}" if pbr else "—"),
    ]
    health_card = ScoreCard(round(min(100.0, sum(c.points for c in h))), h)
    value_card = ScoreCard(round(min(100.0, sum(c.points for c in v))), v)
    return {
        "health_score": health_card.total, "value_score": value_card.total,
        "total_score": round((health_card.total + value_card.total) / 2),
        "roe": roe, "per": per, "pbr": pbr,
        "detail": {"health": health_card.to_dict(), "value": value_card.to_dict()},
    }


def _kr_symbols_needing_enrichment(max_age: timedelta) -> list[str]:
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - max_age).isoformat()
    rows = conn.execute(
        "SELECT symbol FROM screened WHERE market='KR' AND (enriched=0 OR updated_at < ?) "
        "ORDER BY market_cap DESC",
        (cutoff,),
    ).fetchall()
    return [r[0] for r in rows]


def enrich_kr(
    year: int | None = None,
    max_age: timedelta = ENRICH_MAX_AGE,
    limit: int | None = None,
    chunk_size: int = 200,
    on_progress: "Callable[[int, int, dict], None] | None" = None,
) -> dict[str, int]:
    """한국 종목 DART 펀더멘털 보강 → ROE/PER/PBR 점수. 재개 가능.

    year=None 이면 직전 회계연도 (당해 사업보고서가 아직이면 build 시 조정).
    chunk_size: N종목마다 executescript 한 방으로 flush (Turso 왕복 최소화 — enrich() 참고).
    """
    import json
    from datetime import datetime as _dt

    from src.data_fetcher import fetch_dart_corp_codes, fetch_dart_financials

    conn = _conn()
    year = year or (_dt.now().year - 1)
    try:
        corp_map = fetch_dart_corp_codes()
    except DataFetchError as e:
        logger.warning("DART corpCode 실패 — KR 보강 중단: %s", e)
        return {"enriched": 0, "no_data": 0, "failed": 0}

    targets = _kr_symbols_needing_enrichment(max_age)
    if limit:
        targets = targets[:limit]
    stats = {"enriched": 0, "no_data": 0, "failed": 0}
    total = len(targets)
    logger.info("KR 보강 시작: %d종목 (DART %d년, chunk_size=%d)", total, year, chunk_size)

    batch: list[str] = []
    for i, symbol in enumerate(targets, 1):
        corp_code = corp_map.get(symbol)
        if not corp_code:
            stats["no_data"] += 1
        else:
            try:
                fin = fetch_dart_financials(corp_code, year)
                mcap = conn.execute(
                    "SELECT market_cap FROM screened WHERE symbol=? AND market='KR'", (symbol,)
                ).fetchone()
                if fin and fin.get("equity") and mcap:
                    sc = calculate_kr_scores(fin, float(mcap[0]))
                    batch.append(_build_update(
                        {"roe": sc["roe"], "per": sc["per"], "pbr": sc["pbr"],
                         "health_score": sc["health_score"], "value_score": sc["value_score"],
                         "total_score": sc["total_score"],
                         "detail": json.dumps(sc["detail"], ensure_ascii=False),
                         "enriched": 1, "updated_at": _utcnow()},
                        symbol, "KR",
                    ))
                    stats["enriched"] += 1
                else:
                    stats["no_data"] += 1
            except DataFetchError as e:
                logger.warning("KR 보강 실패 %s — %s", symbol, e)
                stats["failed"] += 1
        if len(batch) >= chunk_size:
            _flush_updates(conn, batch)
            batch.clear()
        if on_progress is not None:
            on_progress(i, total, stats)
        if i % 100 == 0:
            logger.info("KR 보강 진행 %d/%d (%s)", i, total, stats)

    _flush_updates(conn, batch)  # 남은 분 flush
    logger.info("KR 보강 완료: %s", stats)
    return stats


# ----------------------------------------------------------------------
# 3. 스캔 (scan) — 오프라인, API 0콜
# ----------------------------------------------------------------------


def scan(
    market: str | None = None,
    limit: int = 50,
    min_total: int = 0,
    sector: str | None = None,
) -> list[ScanRow]:
    """점수 내림차순 전수 스캔 (보강 완료 종목만). 저평가 상위 발굴용."""
    conn = _conn()
    sql = ["SELECT symbol, market, name, sector, price, market_cap, total_score,",
           "       value_score, health_score, roe, per, pbr",
           "FROM screened WHERE enriched=1 AND total_score >= ?"]
    args: list = [min_total]
    if market:
        sql.append("AND market = ?"); args.append(market)
    if sector:
        sql.append("AND sector = ?"); args.append(sector)
    sql.append("ORDER BY total_score DESC, market_cap DESC LIMIT ?"); args.append(limit)

    # libsql(Turso)은 파라미터를 tuple 로만 받음 (sqlite3 는 list 도 허용) → tuple 강제
    rows = conn.execute(" ".join(sql), tuple(args)).fetchall()
    return [ScanRow(*r) for r in rows]


def lookup(symbol: str) -> ScanRow | None:
    """특정 종목의 점수·순위 조회 (내 종목이 저평가인지 확인용).

    같은 심볼이 여러 시장에 있으면(주식 vs 동명 크립토) 주식(US→KR) 우선.
    """
    conn = _conn()
    r = conn.execute(
        """SELECT symbol, market, name, sector, price, market_cap, total_score,
                  value_score, health_score, roe, per, pbr
           FROM screened WHERE symbol = ? AND enriched = 1
           ORDER BY CASE market WHEN 'US' THEN 0 WHEN 'KR' THEN 1 ELSE 2 END
           LIMIT 1""",
        (symbol.upper(),),
    ).fetchone()
    return ScanRow(*r) if r else None


def lookup_detail(symbol: str) -> dict | None:
    """종목의 점수 분해 JSON (--check / 텔레그램 /stock 용). 없으면 None.

    반환 형태: {"health": {total, components:[[label,pts,max,detail],...]},
                "value": {...}}
    """
    import json

    conn = _conn()
    r = conn.execute(
        "SELECT detail FROM screened WHERE symbol=? AND enriched=1 AND detail IS NOT NULL "
        "ORDER BY CASE market WHEN 'US' THEN 0 WHEN 'KR' THEN 1 ELSE 2 END LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    if not r or not r[0]:
        return None
    try:
        return json.loads(r[0])
    except (json.JSONDecodeError, TypeError):
        return None


def top_symbols(n: int = 6, market: str = "US") -> list[str]:
    """스캔 상위 N 티커 (다이제스트 발굴 종목 연동용). DB 가 비면 빈 리스트."""
    return [row.symbol for row in scan(market=market, limit=n)]


def stats() -> dict[str, int]:
    """시장별 발굴/보강 현황."""
    conn = _conn()
    out: dict[str, int] = {}
    for mkt, total, enr in conn.execute(
        "SELECT market, COUNT(*), SUM(enriched) FROM screened GROUP BY market"
    ).fetchall():
        out[f"{mkt}_total"] = total
        out[f"{mkt}_enriched"] = enr or 0
    return out
