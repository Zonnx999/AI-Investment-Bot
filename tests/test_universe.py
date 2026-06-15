"""src/universe.py — DB upsert/scan/lookup 로직 (tmp DB, 네트워크 없음)."""

from __future__ import annotations

from datetime import timedelta

import pytest

import src.storage as storage_mod
from src import universe


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """universe 가 쓰는 storage 싱글톤을 tmp DB 로 격리."""
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "uni.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


def _seed_stock(conn, symbol, market, total, enriched=1, mcap=5e9, sector="Tech"):
    universe._upsert_universe_row(
        conn, symbol=symbol, market=market, name=f"{symbol} Inc",
        sector=sector, industry="x", price=100.0, market_cap=mcap, dividend_yield=1.0,
    )
    if enriched:
        conn.execute(
            "UPDATE screened SET enriched=1, total_score=?, value_score=?, "
            "health_score=?, roe=? WHERE symbol=?",
            (total, total, total, 12.0, symbol),
        )
    conn.commit()


def test_scan_orders_by_total_score_desc(fresh_db):
    conn = universe._conn()
    _seed_stock(conn, "LOW", "US", 40)
    _seed_stock(conn, "HIGH", "US", 90)
    _seed_stock(conn, "MID", "US", 65)
    rows = universe.scan(limit=10)
    assert [r.symbol for r in rows] == ["HIGH", "MID", "LOW"]


def test_scan_excludes_unenriched(fresh_db):
    conn = universe._conn()
    _seed_stock(conn, "DONE", "US", 80, enriched=1)
    _seed_stock(conn, "PENDING", "US", 0, enriched=0)
    symbols = [r.symbol for r in universe.scan(limit=10)]
    assert "DONE" in symbols
    assert "PENDING" not in symbols


def test_scan_market_and_sector_filters(fresh_db):
    conn = universe._conn()
    _seed_stock(conn, "USTECH", "US", 70, sector="Tech")
    _seed_stock(conn, "USFIN", "US", 75, sector="Financial")
    _seed_stock(conn, "KRONE", "KR", 80, sector="Tech")
    assert {r.symbol for r in universe.scan(market="US")} == {"USTECH", "USFIN"}
    assert {r.symbol for r in universe.scan(sector="Tech")} == {"USTECH", "KRONE"}


def test_scan_min_total_filter(fresh_db):
    conn = universe._conn()
    _seed_stock(conn, "A", "US", 50)
    _seed_stock(conn, "B", "US", 80)
    assert {r.symbol for r in universe.scan(min_total=70)} == {"B"}


def test_discover_upsert_preserves_enrichment(fresh_db):
    # 재발굴(가격 갱신) 시 기존 보강 점수가 날아가면 안 됨
    conn = universe._conn()
    _seed_stock(conn, "KEEP", "US", 88, enriched=1)
    universe._upsert_universe_row(
        conn, symbol="KEEP", market="US", name="KEEP Inc", sector="Tech",
        industry="x", price=222.0, market_cap=9e9, dividend_yield=2.0,
    )
    conn.commit()
    row = universe.lookup("KEEP")
    assert row is not None
    assert row.total_score == 88          # 점수 보존
    assert row.price == 222.0             # 가격은 갱신


def test_symbols_needing_enrichment(fresh_db):
    conn = universe._conn()
    _seed_stock(conn, "FRESH", "US", 70, enriched=1)
    _seed_stock(conn, "NEW", "US", 0, enriched=0)
    pending = [s for s, _ in universe.symbols_needing_enrichment(timedelta(days=7))]
    assert "NEW" in pending
    assert "FRESH" not in pending


def test_crypto_stored_scored_at_discovery(fresh_db):
    conn = universe._conn()
    universe._store_crypto_scored(
        conn, symbol="BTC", name="Bitcoin", price=60000.0, market_cap=1.2e12,
        scores={"total_score": 95, "volatility_score": 60, "rank_score": 99},
    )
    conn.commit()
    row = universe.lookup("BTC")
    assert row is not None and row.market == "CRYPTO" and row.total_score == 95


def test_top_symbols(fresh_db):
    conn = universe._conn()
    _seed_stock(conn, "AAA", "US", 90)
    _seed_stock(conn, "BBB", "US", 70)
    _seed_stock(conn, "CCC", "US", 50)
    assert universe.top_symbols(n=2, market="US") == ["AAA", "BBB"]


def test_enrich_calls_progress_and_enriches(fresh_db, monkeypatch):
    import pandas as pd

    conn = universe._conn()
    for sym in ["AAA", "BBB", "CCC"]:
        universe._upsert_universe_row(
            conn, symbol=sym, market="US", name=f"{sym} Inc", sector="Tech",
            industry="x", price=100.0, market_cap=5e9, dividend_yield=1.0,
        )
    conn.commit()

    # 병합 메트릭(key-metrics+ratios) 네트워크 호출을 합성 dict 로 대체
    # (_enrich_one 은 src.screener.latest_fundamentals 를 호출)
    fake = {
        "returnOnEquity": 0.20, "returnOnInvestedCapital": 0.15, "grossProfitMargin": 0.40,
        "evToSales": 2.0, "evToEBITDA": 9.0, "priceToBookRatio": 1.5,
        "freeCashFlowYield": 0.05, "netDebtToEBITDA": 1.0, "earningsYield": 0.08,
        "currentRatio": 2.0, "incomeQuality": 1.0,
    }
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker: fake)

    seen: list[tuple[int, int]] = []
    stats = universe.enrich(
        commit_every=2, on_progress=lambda i, total, s: seen.append((i, total))
    )
    assert stats["enriched"] == 3
    assert seen == [(1, 3), (2, 3), (3, 3)]      # 매 종목 콜백
    # 배치 커밋(2개)+마지막 커밋 후 모두 반영됐는지
    assert {r.symbol for r in universe.scan(market="US")} == {"AAA", "BBB", "CCC"}


def test_discover_kr_filters_common_stocks_and_floor(fresh_db, monkeypatch):
    # KRX 일별 (코스피 2 + 코스닥 1) + 기본정보(보통주 필터)
    kospi = [
        {"ISU_CD": "005930", "ISU_NM": "삼성전자", "TDD_CLSPRC": "70000",
         "MKTCAP": "400000000000000", "SECT_TP_NM": ""},        # 보통주, 대형 → 포함
        {"ISU_CD": "005935", "ISU_NM": "삼성전자우", "TDD_CLSPRC": "60000",
         "MKTCAP": "50000000000000", "SECT_TP_NM": ""},          # 우선주 → 제외
    ]
    kosdaq = [
        {"ISU_CD": "035720", "ISU_NM": "카카오", "TDD_CLSPRC": "50000",
         "MKTCAP": "200000000000", "SECT_TP_NM": "벤처"},        # 보통주지만 시총<5천억 → 제외
    ]
    base = {
        "KOSPI": [
            {"ISU_SRT_CD": "005930", "SECUGRP_NM": "주권", "KIND_STKCERT_TP_NM": "보통주"},
            {"ISU_SRT_CD": "005935", "SECUGRP_NM": "주권", "KIND_STKCERT_TP_NM": "우선주"},
        ],
        "KOSDAQ": [
            {"ISU_SRT_CD": "035720", "SECUGRP_NM": "주권", "KIND_STKCERT_TP_NM": "보통주"},
        ],
    }
    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily",
                        lambda market, bas_dd: kospi if market == "KOSPI" else kosdaq)
    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info",
                        lambda market, bas_dd: base[market])

    n = universe.discover(markets=("KR",))["KR"]
    assert n == 1                                   # 삼성전자만 (우선주·소형 제외)
    row = universe._conn().execute(
        "SELECT symbol, market, name, price, enriched FROM screened WHERE market='KR'"
    ).fetchall()
    assert len(row) == 1
    assert row[0][0] == "005930" and row[0][4] == 0   # 발굴만, 미보강 (DART 대기)


def test_kr_not_targeted_by_fmp_enrichment(fresh_db):
    # KR 행은 FMP key-metrics 보강 대상에서 제외 (DART 로 별도)
    conn = universe._conn()
    universe._upsert_universe_row(
        conn, symbol="005930", market="KR", name="삼성", sector="", industry="",
        price=70000.0, market_cap=4e14, dividend_yield=0.0,
    )
    conn.commit()
    pending = universe.symbols_needing_enrichment()
    assert all(m != "KR" for _, m in pending)


def test_enrich_empty_metrics_counts_no_data(fresh_db, monkeypatch):
    conn = universe._conn()
    universe._upsert_universe_row(
        conn, symbol="ZZZ", market="US", name="Z", sector="x", industry="x",
        price=10.0, market_cap=2e9, dividend_yield=0.0,
    )
    conn.commit()
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker: {})
    stats = universe.enrich()
    assert stats["no_data"] == 1 and stats["enriched"] == 0
