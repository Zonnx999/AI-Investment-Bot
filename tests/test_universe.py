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
