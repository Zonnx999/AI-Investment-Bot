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
    # (_compute_enrich 가 src.screener.latest_fundamentals 를 호출)
    fake = {
        "returnOnEquity": 0.20, "returnOnInvestedCapital": 0.15, "grossProfitMargin": 0.40,
        "evToSales": 2.0, "evToEBITDA": 9.0, "priceToBookRatio": 1.5,
        "freeCashFlowYield": 0.05, "netDebtToEBITDA": 1.0, "earningsYield": 0.08,
        "currentRatio": 2.0, "incomeQuality": 1.0,
    }
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker: fake)

    seen: list[tuple[int, int]] = []
    stats = universe.enrich(
        chunk_size=2, on_progress=lambda i, total, s: seen.append((i, total))
    )
    assert stats["enriched"] == 3
    assert seen == [(1, 3), (2, 3), (3, 3)]      # 매 종목 콜백
    # 배치 flush(2개)+마지막 flush 후 모두 반영됐는지
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


def test_discover_kr_survives_kosdaq_failure(fresh_db, monkeypatch):
    # KOSDAQ 일별매매 API 실패 시 KOSPI 만으로 발굴 계속 — 예외가 위로 새면
    # discover() 가 통째 죽어 CRYPTO 발굴까지 중단됨 (#버그수정)
    from src.exceptions import DataFetchError

    kospi = [
        {"ISU_CD": "005930", "ISU_NM": "삼성전자", "TDD_CLSPRC": "70000",
         "MKTCAP": "400000000000000", "SECT_TP_NM": ""},
    ]
    base = {
        "KOSPI": [{"ISU_SRT_CD": "005930", "SECUGRP_NM": "주권", "KIND_STKCERT_TP_NM": "보통주"}],
        "KOSDAQ": [],
    }

    def fake_daily(market, bas_dd):
        if market == "KOSDAQ":
            raise DataFetchError("KOSDAQ 일별매매 실패", source="KRX")
        return kospi

    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily", fake_daily)
    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info", lambda market, bas_dd: base[market])

    n = universe.discover(markets=("KR",))["KR"]
    assert n == 1   # KOSDAQ 실패해도 KOSPI 삼성전자는 발굴됨 (예외 전파 안 함)


def test_discover_kr_block_isolates_failure(fresh_db, monkeypatch):
    # 기본정보(_common_stock_codes) 등 다른 KRX 호출 실패도 discover() 를 죽이지 않음
    from src.exceptions import DataFetchError

    kospi = [
        {"ISU_CD": "005930", "ISU_NM": "삼성전자", "TDD_CLSPRC": "70000",
         "MKTCAP": "400000000000000", "SECT_TP_NM": ""},
    ]
    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily",
                        lambda market, bas_dd: kospi if market == "KOSPI" else [])

    def boom(market, bas_dd):
        raise DataFetchError("KRX 기본정보 실패", source="KRX")

    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info", boom)

    counts = universe.discover(markets=("KR",))   # 예외 없이 반환되어야 함
    assert counts["KR"] == 0


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


# ----------------------------------------------------------------------
# 배치 쓰기 (executescript) — SQL 리터럴 이스케이프 안전성
# ----------------------------------------------------------------------


def test_sql_lit_escaping():
    import math

    assert universe._sql_lit(None) == "NULL"
    assert universe._sql_lit(42) == "42"
    assert universe._sql_lit(True) == "1"
    assert universe._sql_lit(3.5) == repr(3.5)
    assert universe._sql_lit(float("nan")) == "NULL"      # NaN → NULL (점수 깨짐 방지)
    assert universe._sql_lit(float("inf")) == "NULL"
    assert universe._sql_lit("plain") == "'plain'"
    assert universe._sql_lit("O'Brien") == "'O''Brien'"   # 아포스트로피 이스케이프
    assert universe._sql_lit('say "hi"') == "'say \"hi\"'"  # 큰따옴표는 그대로 (SQL 무해)


def test_sql_lit_handles_numpy_scalars():
    np = pytest.importorskip("numpy")
    assert universe._sql_lit(np.int64(7)) == "7"          # numpy 정수 → int 리터럴
    assert universe._sql_lit(np.float64(2.0)) == repr(2.0)
    assert universe._sql_lit(np.float64("nan")) == "NULL"


def test_build_update_and_flush_roundtrip_with_tricky_values(fresh_db):
    """배치 경로가 아포스트로피·따옴표·한글·NULL 을 손실/깨짐 없이 저장하는지."""
    conn = universe._conn()
    # 회사명에 아포스트로피, detail 에 JSON 따옴표·한글 — 이스케이프 안 하면 SQL 깨짐
    universe._upsert_universe_row(
        conn, symbol="MCD", market="US", name="McDonald's", sector="Food",
        industry="x", price=250.0, market_cap=1e11, dividend_yield=2.0,
    )
    conn.commit()

    tricky_detail = '{"health": {"note": "O\'Brien said \\"buy\\"", "메모": "저평가"}}'
    stmt = universe._build_update(
        {"name": "McDonald's Corp", "total_score": 88, "value_score": 90,
         "health_score": 86, "roe": 25.5, "ev_to_sales": None,  # None → NULL
         "detail": tricky_detail, "enriched": 1, "updated_at": universe._utcnow()},
        "MCD", "US",
    )
    universe._flush_updates(conn, [stmt])

    row = conn.execute(
        "SELECT name, total_score, roe, ev_to_sales, detail FROM screened "
        "WHERE symbol='MCD' AND market='US'"
    ).fetchone()
    assert row[0] == "McDonald's Corp"     # 아포스트로피 보존
    assert row[1] == 88
    assert row[2] == 25.5
    assert row[3] is None                  # None → NULL → None
    assert row[4] == tricky_detail         # 따옴표·한글 그대로


def test_flush_updates_empty_is_noop(fresh_db):
    conn = universe._conn()
    universe._flush_updates(conn, [])      # 예외 없이 통과해야 함


# ---------------- calculate_kr_scores 경계값 (순수 함수, DB 불필요) ----------------


def _kr_component(result, side, label):
    for c in result["detail"][side]["components"]:
        if c[0] == label:
            return c          # [label, points, max, detail]
    raise AssertionError(f"{label} 컴포넌트 없음")


def test_kr_scores_healthy_company():
    """정상 우량주: ROE·부채비율·흑자 모두 점수 획득."""
    fin = {"net_income": 1e10, "equity": 1e11, "debt": 3e10,
           "revenue": 2e11, "op_income": 3e10}
    r = universe.calculate_kr_scores(fin, market_cap=1.2e12)
    assert r["health_score"] > 0 and r["value_score"] > 0
    assert r["roe"] is not None and r["pbr"] is not None


def test_kr_negative_equity_not_rewarded():
    """자본잠식(equity<0): ROE/부채비율/PBR 이 음수 부호로 만점받던 역설 차단."""
    fin = {"net_income": -5e9, "equity": -2e10, "debt": 5e10,
           "revenue": 1e11, "op_income": -3e9}
    r = universe.calculate_kr_scores(fin, market_cap=1e11)
    # ROE/PBR 은 무의미 → None, 부채비율은 만점 아닌 0점이어야
    assert r["roe"] is None and r["pbr"] is None
    assert _kr_component(r, "health", "부채비율")[1] == 0.0
    assert _kr_component(r, "health", "ROE")[1] == 0.0
    assert _kr_component(r, "health", "흑자")[1] == 0.0   # 적자
    assert _kr_component(r, "value", "PBR")[1] == 0.0


def test_kr_zero_equity_safe():
    """equity=0: 나눗셈 0 방지 + 만점 금지."""
    fin = {"net_income": 1e9, "equity": 0, "debt": 1e10,
           "revenue": 5e10, "op_income": 2e9}
    r = universe.calculate_kr_scores(fin, market_cap=1e11)
    assert r["roe"] is None and r["pbr"] is None
    assert _kr_component(r, "health", "부채비율")[1] == 0.0


def test_kr_zero_revenue_safe():
    """매출 0: 영업이익률 None → 0점, 크래시 없음."""
    fin = {"net_income": -1e9, "equity": 1e10, "debt": 5e9,
           "revenue": 0, "op_income": -1e9}
    r = universe.calculate_kr_scores(fin, market_cap=5e10)
    assert _kr_component(r, "health", "영업이익률")[1] == 0.0


def test_kr_negative_net_income_no_per():
    """적자(net_income<0): PER 산출 안 함(None), 흑자 보너스 0."""
    fin = {"net_income": -2e9, "equity": 1e10, "debt": 5e9,
           "revenue": 5e10, "op_income": 1e9}
    r = universe.calculate_kr_scores(fin, market_cap=5e10)
    assert r["per"] is None
    assert _kr_component(r, "health", "흑자")[1] == 0.0
    assert _kr_component(r, "value", "PER")[1] == 0.0
