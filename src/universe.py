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
from src.storage import get_storage

logger = get_logger(__name__)

# 시장별 발굴 설정 (시총 floor 는 사용자 합의: 중대형주만)
MARKET_CAP_FLOOR = 1_000_000_000          # $1B
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = get_storage().conn
    conn.executescript(_SCHEMA)
    return conn


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


def discover(
    markets: tuple[str, ...] = ("US", "KR", "CRYPTO"),
    market_cap_floor: float = MARKET_CAP_FLOOR,
) -> dict[str, int]:
    """유니버스 발굴 → screened 테이블 upsert. 시장별 발굴 종목 수 반환."""
    from src.data_fetcher import fetch_company_screener, fetch_crypto_top

    conn = _conn()
    counts: dict[str, int] = {}

    for mkt in markets:
        if mkt in ("US", "KR"):
            try:
                rows = fetch_company_screener(country=mkt, market_cap_min=market_cap_floor)
            except DataFetchError as e:
                logger.warning("발굴 실패 %s — %s", mkt, e)
                counts[mkt] = 0
                continue
            for r in rows:
                price = r.get("price") or 0.0
                last_div = r.get("lastAnnualDividend") or 0.0
                div_yield = (last_div / price * 100) if price else 0.0
                _upsert_universe_row(
                    conn, symbol=r["symbol"], market=mkt,
                    name=r.get("companyName", ""), sector=r.get("sector", ""),
                    industry=r.get("industry", ""), price=price,
                    market_cap=r.get("marketCap") or 0.0, dividend_yield=div_yield,
                )
            counts[mkt] = len(rows)

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
    """보강이 필요한 (symbol, market) — 미보강이거나 오래된 주식(US/KR)."""
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - max_age).isoformat()
    rows = conn.execute(
        """
        SELECT symbol, market FROM screened
        WHERE market IN ('US','KR')
          AND (enriched = 0 OR updated_at < ?)
        ORDER BY market_cap DESC
        """,
        (cutoff,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _enrich_one(conn, symbol: str, market: str) -> bool:
    """한 종목 key-metrics → 점수 → DB. 성공 True. 데이터 없으면 False."""
    from src.data_fetcher import fetch_key_metrics

    df = fetch_key_metrics(symbol, limit=1)
    if df.empty:
        return False
    m = df.iloc[-1].to_dict()

    # 발굴 단계에서 저장한 가격/섹터를 quote 대용으로 사용 (value 점수 일부에 필요)
    base = conn.execute(
        "SELECT price, sector, industry FROM screened WHERE symbol=? AND market=?",
        (symbol, market),
    ).fetchone()
    price, sector, industry = base if base else (0.0, "", "")
    quote_like = {"price": price, "sector": sector, "industry": industry}

    health = calculate_health_score(m)
    value = calculate_value_score(quote_like, m)
    total = round((health + value) / 2)

    conn.execute(
        """
        UPDATE screened SET
            roe=?, ev_to_sales=?, fcf_yield=?, net_debt_ebitda=?,
            health_score=?, value_score=?, total_score=?, enriched=1, updated_at=?
        WHERE symbol=? AND market=?
        """,
        (
            (m.get("returnOnEquity") or 0) * 100,
            m.get("evToSales"),
            (m.get("freeCashFlowYield") or 0) * 100,
            m.get("netDebtToEBITDA"),
            health, value, total, _utcnow(), symbol, market,
        ),
    )
    return True


def enrich(
    max_age: timedelta = ENRICH_MAX_AGE,
    limit: int | None = None,
    commit_every: int = 50,
    on_progress: "Callable[[int, int, dict], None] | None" = None,
) -> dict[str, int]:
    """보강 필요한 주식들을 key-metrics 로 채움. 재개 가능.

    commit_every: N종목마다 커밋 (Turso 는 쓰기가 클라우드 왕복이라 종목별 커밋이
      매우 느림 → 배치로 왕복 횟수를 줄임. 중단 시 최대 N개만 재작업, 캐시로 저렴).
    on_progress(i, total, stats): 매 종목 호출 (CLI 진행바 등). None 이면 미사용.

    Returns {"enriched": n, "no_data": n, "failed": n}.
    """
    conn = _conn()
    targets = symbols_needing_enrichment(max_age)
    if limit:
        targets = targets[:limit]

    stats = {"enriched": 0, "no_data": 0, "failed": 0}
    total = len(targets)
    logger.info("보강 시작: %d종목 (commit_every=%d)", total, commit_every)

    for i, (symbol, market) in enumerate(targets, 1):
        try:
            ok = _enrich_one(conn, symbol, market)
            stats["enriched" if ok else "no_data"] += 1
        except DataFetchError as e:
            logger.warning("보강 실패 %s — %s", symbol, e)
            stats["failed"] += 1
        if i % commit_every == 0:
            conn.commit()  # 배치 커밋 → Turso 왕복 최소화
        if on_progress is not None:
            on_progress(i, total, stats)
        if i % 100 == 0:
            logger.info("보강 진행 %d/%d (%s)", i, total, stats)

    conn.commit()  # 남은 분 커밋
    logger.info("보강 완료: %s", stats)
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
           "       value_score, health_score, roe",
           "FROM screened WHERE enriched=1 AND total_score >= ?"]
    args: list = [min_total]
    if market:
        sql.append("AND market = ?"); args.append(market)
    if sector:
        sql.append("AND sector = ?"); args.append(sector)
    sql.append("ORDER BY total_score DESC, market_cap DESC LIMIT ?"); args.append(limit)

    rows = conn.execute(" ".join(sql), args).fetchall()
    return [ScanRow(*r) for r in rows]


def lookup(symbol: str) -> ScanRow | None:
    """특정 종목의 점수·순위 조회 (내 종목이 저평가인지 확인용).

    같은 심볼이 여러 시장에 있으면(주식 vs 동명 크립토) 주식(US→KR) 우선.
    """
    conn = _conn()
    r = conn.execute(
        """SELECT symbol, market, name, sector, price, market_cap, total_score,
                  value_score, health_score, roe
           FROM screened WHERE symbol = ? AND enriched = 1
           ORDER BY CASE market WHEN 'US' THEN 0 WHEN 'KR' THEN 1 ELSE 2 END
           LIMIT 1""",
        (symbol.upper(),),
    ).fetchone()
    return ScanRow(*r) if r else None


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
