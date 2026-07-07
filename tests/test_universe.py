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


def test_enrich_all_none_metrics_counts_no_data_not_zero_score(fresh_db, monkeypatch):
    """전부 None 인 metrics 는 0점 보강이 아니라 no_data(미보강 유지) — scan 에서 제외."""
    conn = universe._conn()
    universe._upsert_universe_row(
        conn, symbol="NULLY", market="US", name="N", sector="x", industry="x",
        price=10.0, market_cap=2e9, dividend_yield=0.0,
    )
    conn.commit()
    monkeypatch.setattr(
        "src.screener.latest_fundamentals",
        lambda ticker: {"returnOnEquity": None, "evToSales": None,
                        "earningsYield": float("nan")},
    )
    stats = universe.enrich()
    assert stats["no_data"] == 1 and stats["enriched"] == 0
    assert universe.scan(market="US") == []          # 랭킹에 안 나타남 (0점 바닥 아님)


def test_symbols_needing_enrichment_market_param(fresh_db):
    conn = universe._conn()
    universe._upsert_universe_row(
        conn, symbol="AAPL", market="US", name="Apple", sector="Tech", industry="x",
        price=200.0, market_cap=3e12, dividend_yield=0.5,
    )
    universe._upsert_universe_row(
        conn, symbol="005930", market="KR", name="삼성전자", sector="", industry="",
        price=70000.0, market_cap=4e14, dividend_yield=0.0,
    )
    conn.commit()
    assert universe.symbols_needing_enrichment() == [("AAPL", "US")]          # 기본 US
    assert universe.symbols_needing_enrichment(market="KR") == [("005930", "KR")]
    assert universe.symbols_needing_enrichment(market="CRYPTO") == []


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


# ----------------------------------------------------------------------
# KR 점수 — PBR 곡선 완화 (backlog: PBR 2.0→0점 은 중대형주에 과가혹)
# ----------------------------------------------------------------------


def test_kr_pbr_points_breakpoints():
    assert universe._kr_pbr_points(0.3) == 35.0        # 딥밸류 → 만점
    assert universe._kr_pbr_points(0.5) == 35.0        # 만점 상한 경계
    assert universe._kr_pbr_points(4.5) == 0.0         # 0점 도달 지점
    assert universe._kr_pbr_points(6.0) == 0.0         # 그 이상도 0
    # 완화 확인: 구곡선에서 0점이던 PBR 2.0 이 이제 유의미한 점수
    assert universe._kr_pbr_points(2.0) == pytest.approx(35.0 * 2.5 / 4.0)   # ≈21.9
    assert universe._kr_pbr_points(1.0) == pytest.approx(35.0 * 3.5 / 4.0)   # ≈30.6


def test_kr_pbr_points_monotonic_decreasing():
    pts = [universe._kr_pbr_points(p) for p in (0.2, 0.5, 0.8, 1.5, 2.5, 3.5, 4.5, 5.5)]
    assert all(a >= b for a, b in zip(pts, pts[1:]))
    assert all(0.0 <= p <= 35.0 for p in pts)


def test_kr_pbr_points_negative_zero_none_guard():
    """자본잠식(음수)·0·None 은 명시적 0점 (CLAUDE.md §4.10 #5)."""
    assert universe._kr_pbr_points(None) == 0.0
    assert universe._kr_pbr_points(0.0) == 0.0
    assert universe._kr_pbr_points(-1.2) == 0.0


def test_calculate_kr_scores_negative_equity_pbr_zero():
    """자본잠식 기업(equity<0 → PBR 음수)이 PBR 만점을 받으면 안 됨."""
    fin = {"net_income": -1e9, "equity": -5e9, "debt": 1e10,
           "revenue": 1e10, "op_income": -5e8}
    sc = universe.calculate_kr_scores(fin, market_cap=1e12)
    pbr_comp = next(c for c in sc["detail"]["value"]["components"] if c[0] == "PBR")
    assert pbr_comp[1] == 0.0


def test_calculate_kr_scores_midcap_pbr_not_floored():
    """PBR 2.0(한국 중대형주 흔함)이 0점 바닥이 아니어야 함 (완화 목적 자체)."""
    fin = {"net_income": 1e11, "equity": 1e12, "debt": 5e11,
           "revenue": 1e12, "op_income": 1.5e11}
    sc = universe.calculate_kr_scores(fin, market_cap=2e12)   # PBR = 2.0
    pbr_comp = next(c for c in sc["detail"]["value"]["components"] if c[0] == "PBR")
    assert pbr_comp[1] > 15.0   # 구곡선(0점) 대비 유의미한 점수


# ---------------- 전수 리뷰 회귀 (2026-07-06) ----------------


def test_calculate_kr_scores_negative_equity_zeroes_roe_and_debt():
    """자본잠식(eq<0): 음수NI/음수EQ = 양수 ROE 만점, 부채/음수EQ = 음수
    부채비율 만점이던 부호 함정 회귀 — 둘 다 0점·'—' 처리."""
    sc = universe.calculate_kr_scores(
        {"net_income": -50e9, "equity": -20e9, "debt": 300e9,
         "revenue": 100e9, "op_income": -10e9},
        market_cap=500e9,
    )
    comps = {c[0]: c for c in sc["detail"]["health"]["components"]}
    assert comps["ROE"][1] == 0.0 and comps["ROE"][3] == "—"
    assert comps["부채비율"][1] == 0.0 and comps["부채비율"][3] == "—"
    assert comps["흑자"][1] == 0.0                          # 적자


def _upsert_oldy(conn, price):
    universe._upsert_universe_row(
        conn, symbol="OLDY", market="US", name="Old Co", sector="Tech",
        industry="SW", price=price, market_cap=1e9, dividend_yield=0.0)


def test_discover_upsert_preserves_enrichment_freshness(fresh_db):
    """discover 재발견이 updated_at 을 밀어올려 만기 종목의 재보강을 영원히
    스킵시키던 회귀 — upsert 후에도 symbols_needing_enrichment 에 남아야 함."""
    from datetime import timedelta

    conn = universe._conn()
    _upsert_oldy(conn, 10.0)
    # 보강 완료로 표시하되 30일 전으로 백데이트
    conn.execute("UPDATE screened SET enriched=1, "
                 "updated_at=datetime('now', '-30 days') "
                 "WHERE symbol='OLDY' AND market='US'")
    conn.commit()
    before = universe.symbols_needing_enrichment(timedelta(days=7))
    assert any("OLDY" in str(row) for row in before)

    # 주간 배치처럼 enrich 직전에 discover 가 같은 종목을 재발견(upsert)
    _upsert_oldy(conn, 11.0)
    conn.commit()
    after = universe.symbols_needing_enrichment(timedelta(days=7))
    assert any("OLDY" in str(row) for row in after)
