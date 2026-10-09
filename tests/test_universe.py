"""src/universe.py — DB upsert/scan/lookup 로직 (tmp DB, 네트워크 없음)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

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
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker, **_k: fake)

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
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker, **_k: {})
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
        lambda ticker, **_k: {"returnOnEquity": None, "evToSales": None,
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


# ======================================================================
# 수정 라운드 (WP-UNIVERSE) — US 보통주 분류기 · 스키마 마이그레이션 · 제외 처리 ·
# TTM 보강 · 중단 규칙 · KR 지배주주/위생 · pick_top/freshness/yf_symbol
# (전부 오프라인 — 실응답 픽스처 + 합성, 네트워크 없음)
# ======================================================================

import json as _json
from pathlib import Path as _Path

from src.exceptions import ApiAuthorizationError, RateLimitError

_FIXTURES = _Path(__file__).parent / "fixtures"


def _screener_fixture() -> dict:
    return _json.loads((_FIXTURES / "screener_us_rows.json").read_text(encoding="utf-8"))


def _dart_fixture() -> dict:
    return _json.loads((_FIXTURES / "dart_fnlttSinglAcntAll_trimmed.json").read_text(encoding="utf-8"))


def _nrow(symbol, name, price, adv, exchange="NYSE", industry="x"):
    return {"symbol": symbol, "name": name, "price": price, "industry": industry,
            "exchange": exchange, "adv_usd": adv}


# ---------------- classify_us_securities ----------------


def test_classify_fixture_rows_match_expected_reasons():
    """실응답 61행(상장채·우선주·OTC·형제·저유동 + 감시 정상주) — 기대 사유와 정확히 일치."""
    fx = _screener_fixture()
    norm = [universe.normalize_screener_row(r) for r in fx["rows"]]
    got = universe.classify_us_securities(norm)
    for sym, expected in fx["expected_reason"].items():
        assert got.get(sym) == expected, f"{sym}: got {got.get(sym)!r}, expected {expected!r}"
    assert got["MGR"] == "sibling" and got["AFGE"] == "sibling"
    assert "AMG" not in got and "AFG" not in got


def test_classify_false_positive_guards():
    rows = [
        _nrow("GOOGL", "Alphabet Inc.", 359.68, 8.9e9), _nrow("GOOG", "Alphabet Inc.", 347.37, 6.2e9),
        _nrow("BRK-A", "Berkshire Hathaway Inc.", 7.4e5, 1.1e9), _nrow("BRK-B", "Berkshire Hathaway Inc.", 490.0, 1.6e9),
        _nrow("FOX", "Fox Corporation", 52.0, 6e7), _nrow("FOXA", "Fox Corporation", 57.0, 1.5e8),
        _nrow("NWS", "News Corporation", 31.0, 2e7), _nrow("NWSA", "News Corporation", 27.0, 8e7),
        _nrow("UA", "Under Armour, Inc.", 5.5, 1.5e7), _nrow("UAA", "Under Armour, Inc.", 6.0, 4e7),
        _nrow("PFBC", "Preferred Bank", 103.03, 1.25e7),
        _nrow("BKD", "Brookdale Senior Living Inc.", 7.0, 2e7),
        _nrow("PTRN", "Pattern Group Inc. Series A Common Stock", 17.71, 3e7),
        _nrow("BMNR", "Bitmine Immersion Technologies, Inc.", 24.66, 9.46e8),
        _nrow("BMNP", "Bitmine Immersion Technologies, Inc.", 99.69, 3.86e6),
    ]
    got = universe.classify_us_securities(rows)
    assert got == {"BMNP": "sibling"}        # 정상 이중클래스·'Preferred Bank'·'Series A Common Stock' 보호


def test_classify_whitelist_overrides():
    rows = [_nrow("AMG", "Affiliated Managers Group, Inc.", 377.09, 1.04e8),
            _nrow("MGR", "Affiliated Managers Group, Inc.", 18.73, 5.9e5)]
    assert universe.classify_us_securities(rows) == {"MGR": "sibling"}
    assert universe.classify_us_securities(rows, whitelist=frozenset({"MGR"})) == {}
    monkey_wl = frozenset({"MGR"})
    assert universe.classify_us_securities(rows, whitelist=monkey_wl) == {}


def test_classify_skips_rules_when_fields_missing():
    """exchange/adv_usd 가 None 이면 otc·sibling·illiquid 는 건너뜀 (name/suffix 만) — 오탐 방지."""
    rows = [_nrow("AFG", "American Financial Group, Inc.", 139.02, None, exchange=None),
            _nrow("AFGE", "American Financial Group, Inc.", 16.83, None, exchange=None),
            _nrow("OAK-PA", "Oaktree Capital Group", 25.0, None, exchange=None),
            _nrow("TBB", "AT&T Inc. 5.350% Global Notes due 2066", 24.0, None, exchange=None)]
    got = universe.classify_us_securities(rows)
    assert got == {"OAK-PA": "suffix", "TBB": "name"}
    # adv 가 주어지면 형제 규칙 적용
    rows2 = [_nrow("AFG", "American Financial Group, Inc.", 139.02, 5.3e7),
             _nrow("AFGE", "American Financial Group, Inc.", 16.83, 1.7e5)]
    assert universe.classify_us_securities(rows2) == {"AFGE": "sibling"}


def test_classify_boundaries():
    assert universe.classify_us_securities([_nrow("A", "Alpha Co", 10.0, 1_000_000)]) == {}
    assert universe.classify_us_securities([_nrow("B", "Beta Co", 10.0, 999_999)]) == {"B": "illiquid"}
    # 형제: 주력 ADV 가 후보의 정확히 5배 → 적용, 4.9배 → 미적용 (가격 조건 충족 상태)
    prim = _nrow("P", "Gamma Co", 100.0, 5_000_000)
    assert universe.classify_us_securities([prim, _nrow("C", "Gamma Co", 20.0, 1_000_000)]) == {"C": "sibling"}
    prim49 = _nrow("P", "Gamma Co", 100.0, 4_900_000)
    assert universe.classify_us_securities([prim49, _nrow("C", "Gamma Co", 20.0, 1_000_000)]) == {}
    # 가격 상한 29.99 vs 30.0 (주력 100, ADV 지배 충족, thin 아님)
    big = _nrow("P", "Delta Co", 100.0, 1e9)
    assert universe.classify_us_securities([big, _nrow("D1", "Delta Co", 29.99, 1e8)]) == {"D1": "sibling"}
    assert universe.classify_us_securities([big, _nrow("D2", "Delta Co", 30.0, 1e8)]) == {}


def test_name_is_noncommon_patterns():
    assert universe.name_is_noncommon("Prudential Financial, Inc. 5.95")       # 잘린 이름 소수
    assert universe.name_is_noncommon("Southern Company (The) Series 2")        # WEAK series
    assert not universe.name_is_noncommon("Pattern Group Inc. Series A Common Stock")
    assert not universe.name_is_noncommon("Preferred Bank")
    assert not universe.name_is_noncommon("C3.ai, Inc.")


def test_normalize_screener_row_prefers_avg_volume():
    r = {"symbol": "X", "companyName": "X Co", "price": 10.0, "avgVolume": 100, "volume": 1,
         "exchangeShortName": "NYSE", "industry": "y"}
    assert universe.normalize_screener_row(r)["adv_usd"] == 1000.0
    r.pop("avgVolume")
    assert universe.normalize_screener_row(r)["adv_usd"] == 10.0
    r.pop("volume")
    assert universe.normalize_screener_row(r)["adv_usd"] is None


# ---------------- discover(US) 통합 ----------------


def test_discover_us_integration_marks_excluded_and_reevaluates(fresh_db, monkeypatch):
    fx = _screener_fixture()
    monkeypatch.setattr("src.data_fetcher.fetch_company_screener", lambda **kw: fx["rows"])
    n = universe.discover(markets=("US",))["US"]
    assert n == len(fx["rows"])

    conn = universe._conn()
    row = conn.execute("SELECT exchange, adv_value, excluded, seen_at FROM screened "
                       "WHERE symbol='MGR' AND market='US'").fetchone()
    assert row[0] == "NYSE" and row[1] == pytest.approx(18.73 * 31566) and row[2] == "sibling"
    assert row[3] is not None
    assert conn.execute("SELECT excluded FROM screened WHERE symbol='AMG'").fetchone()[0] is None

    # 보강 대상·스캔·선정에서 제외 행이 빠짐 (보강 완료 표시 후 확인)
    conn.execute("UPDATE screened SET enriched=1, total_score=90, value_score=90, health_score=90")
    conn.commit()
    assert "MGR" not in {s for s, _ in universe.symbols_needing_enrichment(timedelta(days=0))}
    scanned = {r.symbol for r in universe.scan(market="US", limit=1000)}
    assert "MGR" not in scanned and "AFGE" not in scanned and "AMG" in scanned
    assert "MGR" not in universe.top_symbols(n=100, market="US")
    assert universe.stats()["US_excluded"] == len(fx["expected_reason"]) - sum(
        1 for v in fx["expected_reason"].values() if v is None)

    # lookup 은 제외 행도 반환 (사유 노출)
    mgr = universe.lookup("MGR")
    assert mgr is not None and mgr.excluded == "sibling" and mgr.exchange == "NYSE"
    assert mgr.price_asof is not None and mgr.fund_asof is not None

    # 두 번째 발굴에서 WHITELIST 변경 → 재평가로 NULL 복귀 (점수는 보존)
    monkeypatch.setattr(universe, "WHITELIST", frozenset({"MGR"}))
    universe.discover(markets=("US",))
    row = conn.execute("SELECT excluded, total_score FROM screened WHERE symbol='MGR'").fetchone()
    assert row[0] is None and row[1] == 90


def test_discover_us_does_not_auto_exclude_unseen_rows(fresh_db, monkeypatch, caplog):
    conn = universe._conn()
    _seed_stock(conn, "HG", "US", 88)                   # 오늘 응답에 없는 기존 행
    fx = _screener_fixture()
    monkeypatch.setattr("src.data_fetcher.fetch_company_screener",
                        lambda **kw: [r for r in fx["rows"] if r["symbol"] != "HG"])
    universe.discover(markets=("US",))
    assert conn.execute("SELECT excluded FROM screened WHERE symbol='HG'").fetchone()[0] is None
    assert any("미확인" in rec.getMessage() for rec in caplog.records)


def test_build_upsert_reclassify_us_updates_only_changed(fresh_db, monkeypatch):
    fx = _screener_fixture()
    monkeypatch.setattr("src.data_fetcher.fetch_company_screener", lambda **kw: fx["rows"])
    universe.discover(markets=("US",))
    conn = universe._conn()
    assert universe.reclassify_us(conn)["changed"] == 0            # 같은 규칙 → 변경 0
    monkeypatch.setattr(universe, "WHITELIST", frozenset({"MGR", "AFGE"}))
    out = universe.reclassify_us(conn)
    assert out["changed"] == 2 and "MGR" not in out["excluded_symbols"]
    assert conn.execute("SELECT excluded FROM screened WHERE symbol='AFGE'").fetchone()[0] is None


# ---------------- 스키마 마이그레이션 (구 DB, 멱등, PRAGMA 가드) ----------------

_OLD_SCHEMA = """
CREATE TABLE screened (
    symbol TEXT NOT NULL, market TEXT NOT NULL, name TEXT, sector TEXT, industry TEXT,
    price REAL, market_cap REAL, dividend_yield REAL, roe REAL, ev_to_sales REAL,
    fcf_yield REAL, net_debt_ebitda REAL, health_score INTEGER, value_score INTEGER,
    total_score INTEGER, enriched INTEGER NOT NULL DEFAULT 0, discovered_at TEXT,
    updated_at TEXT, per REAL, pbr REAL, detail TEXT,
    PRIMARY KEY (symbol, market)
);
"""


def test_migration_adds_columns_idempotently(fresh_db):
    raw = storage_mod.get_storage().conn
    raw.executescript(_OLD_SCHEMA)
    raw.execute("INSERT INTO screened (symbol, market, name, enriched, total_score, value_score, "
                "health_score, market_cap, price, updated_at) VALUES "
                "('OLD','US','Old Co',1,70,70,70,1e9,10.0,'2026-07-10T00:00:00+00:00')")
    raw.commit()
    before = universe._cols(raw)
    assert not (set(universe._NEW_COLS) & before)
    conn = universe._conn()
    assert set(universe._NEW_COLS) <= universe._cols(conn)
    universe._conn()                                                # 두 번째 호출 멱등
    rows = universe.scan(market="US")
    assert [r.symbol for r in rows] == ["OLD"]
    assert rows[0].excluded is None and rows[0].exchange is None and rows[0].price_asof is None
    assert rows[0].fund_asof == "2026-07-10"


def test_scan_without_new_columns_uses_pragma_guard(fresh_db, monkeypatch):
    """stale 레플리카(PRAGMA 에 새 컬럼 없음)를 흉내 — SELECT 가 새 컬럼을 참조하지 않아야 함."""
    conn = universe._conn()
    _seed_stock(conn, "AAA", "US", 80)
    old_cols = universe._cols(conn) - set(universe._NEW_COLS)
    monkeypatch.setattr(universe, "_cols", lambda c: old_cols)
    assert universe._excluded_clause(old_cols) == ""
    assert "excluded" not in universe._scan_select(old_cols)
    rows = universe.scan(market="US")
    assert rows and rows[0].symbol == "AAA" and rows[0].excluded is None
    assert universe.lookup("AAA").excluded is None
    assert universe.stats()["US_excluded"] == 0
    assert universe.freshness("US")["price_asof"] is None
    assert universe.reclassify_us(conn)["changed"] == 0            # 컬럼 없으면 스킵
    assert [s for s, _ in universe.symbols_needing_enrichment(timedelta(days=0))] == ["AAA"]


def test_scanrow_positional_construction_compat():
    r = universe.ScanRow("A", "US", "A Co", "Tech", 1.0, 2.0, 50, 50, 50, 1.0, None, None)
    assert r.exchange is None and r.excluded is None and r.flags == ()
    assert r.fund_asof is None and r.price_asof is None


# ---------------- _compute_enrich (TTM 계약 소비 — monkeypatch) ----------------

_TTM_FAKE = {
    "returnOnEquity": 0.20, "returnOnInvestedCapital": 0.15, "grossProfitMargin": 0.40,
    "evToSales": 2.0, "evToEBITDA": 9.0, "priceToBookRatio": 1.5, "freeCashFlowYield": 0.05,
    "netDebtToEBITDA": 1.0, "earningsYield": 0.08, "currentRatio": 2.0, "incomeQuality": 1.0,
    "dividendPerShare": 4.08, "marketCap": 7.6e10, "_basis": "ttm",
}


def test_compute_enrich_uses_ttm_basis_flags_and_dividend(fresh_db, monkeypatch):
    conn = universe._conn()
    universe._upsert_universe_row(conn, symbol="EOG", market="US", name="EOG", sector="Energy",
                                  industry="Oil & Gas E&P", price=120.0, market_cap=7.6e10,
                                  dividend_yield=1.0)
    universe._upsert_universe_row(conn, symbol="HG", market="US", name="Hamilton",
                                  sector="Financial Services", industry="Insurance - Reinsurance",
                                  price=20.0, market_cap=2e9, dividend_yield=0.0)
    conn.commit()
    captured = {}

    def fake_latest(ticker, *, basis="ttm"):
        captured[ticker] = basis
        return dict(_TTM_FAKE)

    monkeypatch.setattr("src.screener.latest_fundamentals", fake_latest)
    seen_quote = {}
    real_value = universe._scorecards

    def spy_scorecards(quote_like, m, is_fin):
        seen_quote[quote_like.get("sector")] = (dict(quote_like), is_fin)
        return real_value(quote_like, m, is_fin)

    monkeypatch.setattr(universe, "_scorecards", spy_scorecards)

    cols = universe._compute_enrich(conn, "EOG", "US")
    assert captured["EOG"] == "ttm"
    detail = _json.loads(cols["detail"])
    assert detail["basis"] == "ttm" and detail["fin"] is False and "fin_neutral" not in detail["flags"]
    assert seen_quote["Energy"][0]["lastAnnualDividend"] == 4.08       # TTM dividendPerShare 우선
    assert seen_quote["Energy"][1] is False
    assert cols["roe"] == pytest.approx(20.0)

    cols_hg = universe._compute_enrich(conn, "HG", "US")
    assert "fin_neutral" in _json.loads(cols_hg["detail"])["flags"]
    assert seen_quote["Financial Services"][1] is True


def test_compute_enrich_roe_missing_is_none_and_outlier_flags(fresh_db, monkeypatch):
    conn = universe._conn()
    universe._upsert_universe_row(conn, symbol="AFGE", market="US", name="AFG", sector="Financial Services",
                                  industry="Insurance - Property & Casualty", price=16.8,
                                  market_cap=1.4e9, dividend_yield=5.0)
    conn.commit()
    m = dict(_TTM_FAKE)
    m.pop("returnOnEquity"); m.pop("dividendPerShare")
    m["earningsYield"] = 0.596; m["freeCashFlowYield"] = 0.99; m["currentRatio"] = 0
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker, *, basis="ttm": m)
    cols = universe._compute_enrich(conn, "AFGE", "US")
    assert cols["roe"] is None                                           # 결측 ≠ 0 (UNI-10)
    flags = _json.loads(cols["detail"])["flags"]
    assert {"ey_outlier", "fcf_outlier", "cr_missing", "fin_neutral"} <= set(flags)


def test_compute_enrich_records_annual_basis_when_ttm_unavailable(fresh_db, monkeypatch):
    """screener 가 TTM 불가로 연간 폴백하면(_basis='annual') detail 에 그대로 기록."""
    conn = universe._conn()
    universe._upsert_universe_row(conn, symbol="OLD", market="US", name="Old", sector="Tech",
                                  industry="x", price=10.0, market_cap=2e9, dividend_yield=0.0)
    conn.commit()
    annual = {**{k: v for k, v in _TTM_FAKE.items() if k != "_basis"}, "_basis": "annual"}
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker, *, basis="ttm": annual)
    cols = universe._compute_enrich(conn, "OLD", "US")
    assert _json.loads(cols["detail"])["basis"] == "annual"


def test_rule_version_is_set_for_ledger():
    """원장 성과 판정은 규칙 버전 단위 — 점수·필터 규칙을 바꾸면 이 값을 올려야 한다."""
    assert universe.RULE_VERSION == "v1"


# ---------------- enrich 중단 규칙 ----------------


def _seed_many(conn, n):
    for i in range(n):
        universe._upsert_universe_row(conn, symbol=f"S{i:02d}", market="US", name=f"S{i}",
                                      sector="Tech", industry="x", price=10.0,
                                      market_cap=1e9 * (n - i), dividend_yield=0.0)
    conn.commit()


def test_enrich_aborts_after_consecutive_rate_limits(fresh_db, monkeypatch):
    conn = universe._conn()
    _seed_many(conn, 8)
    calls = []

    def boom(ticker, *, basis="ttm"):
        calls.append(ticker)
        raise RateLimitError("429", source="FMP")

    monkeypatch.setattr("src.screener.latest_fundamentals", boom)
    stats = universe.enrich()
    assert stats["aborted"] is True and stats["failed"] == universe.ABORT_AFTER_RATE_LIMITS
    assert len(calls) == 5 and "S05" not in calls                       # 6번째는 호출 안 됨


def test_enrich_consecutive_counter_resets_on_success(fresh_db, monkeypatch):
    conn = universe._conn()
    _seed_many(conn, 10)
    calls = []

    def flaky(ticker, *, basis="ttm"):
        calls.append(ticker)
        if ticker == "S03":
            return dict(_TTM_FAKE)
        raise RateLimitError("429", source="FMP")

    monkeypatch.setattr("src.screener.latest_fundamentals", flaky)
    stats = universe.enrich()
    assert stats["aborted"] is True and stats["enriched"] == 1
    assert len(calls) == 9                                              # 3 실패 + 1 성공 + 5 실패
    assert universe.lookup("S03") is not None                           # 중단 전 분 flush 됨


def test_enrich_aborts_immediately_on_authorization_error(fresh_db, monkeypatch):
    conn = universe._conn()
    _seed_many(conn, 4)
    calls = []

    def forbidden(ticker, *, basis="ttm"):
        calls.append(ticker)
        raise ApiAuthorizationError("403", source="FMP")

    monkeypatch.setattr("src.screener.latest_fundamentals", forbidden)
    stats = universe.enrich()
    assert stats["aborted"] is True and len(calls) == 1


# ---------------- KR: 지배주주 귀속 점수 (실응답 픽스처) ----------------


def _kr_comp(sc, side, label):
    return next(c for c in sc["detail"][side]["components"] if c[0] == label)


def test_kr_scores_fnf_holdings_attributed_golden():
    import src.data_fetcher as df

    fin = df._parse_dart_all(_dart_fixture()["FNF_holdings"]["list"], "CFS")
    sc = universe.calculate_kr_scores(fin, 644_995_911_830, name="F&F홀딩스")
    assert sc["per"] == pytest.approx(5.13, abs=0.01)
    assert sc["pbr"] == pytest.approx(0.28, abs=0.005)
    assert sc["roe"] == pytest.approx(5.44, abs=0.01)
    assert sc["total_score"] == 84 and "holding" in sc["flags"]
    # 부채비율은 총자본(equity_total) 분모: (assets-equity_total)/equity_total
    expected_debt_ratio = fin["debt"] / fin["equity_total"] * 100
    assert _kr_comp(sc, "health", "부채비율")[3] == f"{expected_debt_ratio:.0f}%"


def test_kr_scores_sm_one_off_guard_golden():
    import src.data_fetcher as df

    fin = df._parse_dart_all(_dart_fixture()["SM"]["list"], "CFS")
    sc = universe.calculate_kr_scores(fin, 1_913_996_084_000, name="에스엠")
    assert sc["per"] == pytest.approx(12.68, abs=0.01)                  # 5.33 → 12.68 (3년 평균)
    assert "one_off" in sc["flags"]
    assert "3년평균" in _kr_comp(sc, "value", "PER")[3]
    # 스펙 골든 78 은 6/12 시총 기준, 여기 시총은 7/10 DB 값 → ±1 허용 (구 점수 90 에서 하락)
    assert 78 <= sc["total_score"] <= 79


def test_kr_scores_cuckoo_unchanged_by_guard():
    import src.data_fetcher as df

    fin = df._parse_dart_all(_dart_fixture()["cuckoo"]["list"], "CFS")
    sc = universe.calculate_kr_scores(fin, 1e12, name="쿠쿠홈시스")
    assert sc["flags"] == []                                             # 정상 종목은 안 건드림


def test_kr_scores_old_dict_falls_back_equity_for_debt_ratio():
    old = {"net_income": 1e10, "equity": 1e11, "debt": 5e10, "revenue": 2e11, "op_income": 2e10}
    sc = universe.calculate_kr_scores(old, 5e11)
    assert _kr_comp(sc, "health", "부채비율")[3] == "50%"
    assert sc["flags"] == []


def test_kr_scores_financial_neutralizes_debt_and_margin():
    fin = {"net_income": 5e11, "equity": 5e12, "equity_total": 5.2e12, "debt": 4.5e13,
           "assets": 5e13, "revenue": 1e13, "op_income": 7e11}
    sc = universe.calculate_kr_scores(fin, 3e12, name="삼성증권")
    assert "fin_neutral" in sc["flags"]
    assert _kr_comp(sc, "health", "부채비율")[1] == 15.0 and _kr_comp(sc, "health", "부채비율")[3] == "금융중립"
    assert _kr_comp(sc, "health", "영업이익률")[1] == 10.0
    non_fin = universe.calculate_kr_scores(fin, 3e12, name="아시아나항공")
    assert _kr_comp(non_fin, "health", "부채비율")[1] == 0.0               # 부채비율 865% → 0점


def test_kr_scores_attrib_total_flag():
    fin = {"net_income": 1e10, "equity": 1e11, "equity_total": 1e11, "debt": 5e10,
           "revenue": 2e11, "op_income": 2e10, "attrib": "total"}
    assert "attrib_total" in universe.calculate_kr_scores(fin, 5e11)["flags"]


@pytest.mark.parametrize("name, ratio, expected", [
    ("삼성증권", 0.90, True), ("아시아나항공", 0.93, False), ("KB금융", 0.92, True),
    ("현대해상", 0.90, True), ("삼성증권", 0.50, False), ("다우기술", 0.9, False),
])
def test_kr_is_financial(name, ratio, expected):
    assert universe.kr_is_financial(name, ratio * 100.0, 100.0) is expected


def test_kr_is_financial_missing_values():
    assert universe.kr_is_financial("삼성증권", None, 100.0) is False
    assert universe.kr_is_financial("삼성증권", 90.0, 0.0) is False
    assert universe.kr_is_financial(None, 90.0, 100.0) is False


@pytest.mark.parametrize("name, expected", [
    ("F&F홀딩스", True), ("영원무역홀딩스", True), ("SK", False), ("한국금융지주", False),
    ("KB금융지주", False), ("대웅", False), ("CJ Holdings", True), (None, False),
])
def test_kr_is_holding(name, expected):
    assert universe.kr_is_holding(name) is expected


@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 8), 2025), (date(2027, 2, 15), 2025),
    (date(2027, 3, 31), 2025), (date(2027, 4, 1), 2026),
])
def test_dart_target_year(today, expected):
    assert universe.dart_target_year(today) == expected


# ---------------- KR 위생 (ADR / not_listed / halted / below_floor) ----------------


def _krx_row(code, name, price, mcap, trdval="1000000", sect="", mkt="KOSPI"):
    return {"ISU_CD": code, "ISU_NM": name, "TDD_CLSPRC": str(price), "MKTCAP": str(mcap),
            "ACC_TRDVAL": trdval, "SECT_TP_NM": sect, "MKT_NM": mkt}


def _base(code):
    return {"ISU_SRT_CD": code, "SECUGRP_NM": "주권", "KIND_STKCERT_TP_NM": "보통주"}


def _seed_kr(conn, *symbols):
    for s in symbols:
        universe._upsert_universe_row(conn, symbol=s, market="KR", name=s, sector="", industry="",
                                      price=1.0, market_cap=1e12, dividend_yield=0.0)
    conn.commit()


def _excluded_map(conn):
    return dict(conn.execute("SELECT symbol, excluded FROM screened WHERE market='KR'").fetchall())


def test_discover_kr_hygiene_rules(fresh_db, monkeypatch):
    conn = universe._conn()
    _seed_kr(conn, "KT", "PKX", "SSNLF", "0126Z0", "057050", "006380", "035720", "005930")
    big = universe.KR_MARKET_CAP_FLOOR * 2
    kospi = [_krx_row("005930", "삼성전자", 70000, big), _krx_row("0126Z0", "삼성에피스홀딩스", 1000, big),
             _krx_row("006380", "카프로", 100, big, trdval="0"),
             _krx_row("035720", "소형", 10, universe.KR_MARKET_CAP_FLOOR / 10)]
    # 목록 크기 가드(≥1500)를 넘기기 위한 더미 행 (보통주 아님 → upsert 대상 아님, 목록에는 포함)
    kosdaq = [_krx_row(f"D{i:05d}", "dummy", 1, 1, mkt="KOSDAQ") for i in range(1500)]
    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily",
                        lambda market, bas_dd: kospi if market == "KOSPI" else kosdaq)
    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info",
                        lambda market, bas_dd: [_base(c) for c in ("005930", "0126Z0", "006380", "035720")]
                        if market == "KOSPI" else [])

    n = universe.discover(markets=("KR",))["KR"]
    assert n == 2                                                        # 005930, 0126Z0
    ex = _excluded_map(conn)
    assert ex["KT"] == "kr_adr" and ex["PKX"] == "kr_adr" and ex["SSNLF"] == "kr_adr"
    assert ex["0126Z0"] is None and ex["005930"] is None               # 알파뉴메릭 6자리 정상
    assert ex["057050"] == "not_listed"
    assert ex["006380"] == "halted"
    assert ex["035720"] == "below_floor"
    row = conn.execute("SELECT price, market_cap, exchange, seen_at FROM screened "
                       "WHERE symbol='035720'").fetchone()
    assert row[0] == 10.0 and row[1] == universe.KR_MARKET_CAP_FLOOR / 10   # 가격·시총은 갱신
    assert row[2] == "KOSPI" and len(row[3]) == 10                       # seen_at = KRX 기준일

    # 복귀: 거래 재개·시총 회복·재상장 → NULL
    kospi2 = [_krx_row("005930", "삼성전자", 70000, big), _krx_row("0126Z0", "삼성에피스홀딩스", 1000, big),
              _krx_row("006380", "카프로", 100, big, trdval="5000"),
              _krx_row("035720", "소형", 10, big), _krx_row("057050", "현대홈쇼핑", 100, big)]
    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily",
                        lambda market, bas_dd: kospi2 if market == "KOSPI" else kosdaq)
    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info",
                        lambda market, bas_dd: [_base(c) for c in ("005930", "0126Z0", "006380", "035720", "057050")]
                        if market == "KOSPI" else [])
    assert universe.discover(markets=("KR",))["KR"] == 5
    ex = _excluded_map(conn)
    assert ex["006380"] is None and ex["035720"] is None and ex["057050"] is None
    assert ex["KT"] == "kr_adr"


def test_discover_kr_not_listed_requires_full_list(fresh_db, monkeypatch, caplog):
    conn = universe._conn()
    _seed_kr(conn, "057050")
    big = universe.KR_MARKET_CAP_FLOOR * 2
    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily",
                        lambda market, bas_dd: [_krx_row("005930", "삼성전자", 70000, big)]
                        if market == "KOSPI" else [])
    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info",
                        lambda market, bas_dd: [_base("005930")] if market == "KOSPI" else [])
    universe.discover(markets=("KR",))
    assert _excluded_map(conn)["057050"] is None                         # 목록 부분 수신 → 보류
    assert any("판정 보류" in rec.getMessage() for rec in caplog.records)


def test_discover_kr_missing_trdval_is_not_halted(fresh_db, monkeypatch):
    conn = universe._conn()
    big = universe.KR_MARKET_CAP_FLOOR * 2
    row = _krx_row("005930", "삼성전자", 70000, big)
    row.pop("ACC_TRDVAL")
    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily",
                        lambda market, bas_dd: [row] if market == "KOSPI" else [])
    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info",
                        lambda market, bas_dd: [_base("005930")] if market == "KOSPI" else [])
    assert universe.discover(markets=("KR",))["KR"] == 1
    assert _excluded_map(conn)["005930"] is None


# ---------------- enrich_kr / rescore_kr_offline / refresh_kr_prices ----------------


def test_enrich_kr_stores_raw_and_uses_target_year(fresh_db, monkeypatch):
    conn = universe._conn()
    universe._upsert_universe_row(conn, symbol="007700", market="KR", name="F&F홀딩스", sector="",
                                  industry="", price=1.0, market_cap=644_995_911_830, dividend_yield=0.0)
    conn.commit()
    fin = __import__("src.data_fetcher", fromlist=["_parse_dart_all"])._parse_dart_all(
        _dart_fixture()["FNF_holdings"]["list"], "CFS")
    years = []

    def fake_fin(corp_code, year):
        years.append((corp_code, year))
        return fin

    monkeypatch.setattr("src.data_fetcher.fetch_dart_corp_codes", lambda: {"007700": "00126487"})
    monkeypatch.setattr("src.data_fetcher.fetch_dart_financials", fake_fin)
    stats = universe.enrich_kr()
    assert stats["enriched"] == 1 and years == [("00126487", universe.dart_target_year())]
    detail = universe.lookup_detail("007700")
    assert detail["raw"]["equity_total"] == fin["equity_total"] and detail["basis"].startswith("FY")
    row = universe.lookup("007700")
    assert row.per == pytest.approx(5.13, abs=0.01) and "holding" in row.flags


def test_rescore_kr_offline_without_dart_calls(fresh_db, monkeypatch):
    import src.data_fetcher as df

    conn = universe._conn()
    fin = df._parse_dart_all(_dart_fixture()["FNF_holdings"]["list"], "CFS")
    universe._upsert_universe_row(conn, symbol="007700", market="KR", name="F&F홀딩스", sector="",
                                  industry="", price=1.0, market_cap=644_995_911_830, dividend_yield=0.0)
    universe._upsert_universe_row(conn, symbol="999999", market="KR", name="구형", sector="",
                                  industry="", price=1.0, market_cap=1e12, dividend_yield=0.0)
    sc = universe.calculate_kr_scores(fin, 644_995_911_830, name="F&F홀딩스")
    universe._flush_updates(conn, [
        universe._build_update(universe._kr_update_cols(sc, fin, 2025), "007700", "KR"),
        universe._build_update({"enriched": 1, "total_score": 50, "value_score": 50, "health_score": 50,
                                "detail": '{"health": {}, "value": {}}', "updated_at": universe._utcnow()},
                               "999999", "KR"),
    ])

    def must_not_call(*a, **k):
        raise AssertionError("DART 호출되면 안 됨")

    monkeypatch.setattr("src.data_fetcher.fetch_dart_financials", must_not_call)
    out = universe.rescore_kr_offline({"007700": 644_995_911_830 * 2})
    assert out == {"rescored": 1, "skipped": 1}
    row = universe.lookup("007700")
    assert row.per == pytest.approx(10.26, abs=0.02)                      # 시총 2배 → PER 2배
    assert universe.lookup("999999").total_score == 50                    # raw 없는 구형 행은 건너뜀


def test_refresh_kr_prices_combines_discover_and_rescore(fresh_db, monkeypatch):
    import src.data_fetcher as df

    conn = universe._conn()
    fin = df._parse_dart_all(_dart_fixture()["FNF_holdings"]["list"], "CFS")
    universe._upsert_universe_row(conn, symbol="007700", market="KR", name="F&F홀딩스", sector="",
                                  industry="", price=1.0, market_cap=644_995_911_830, dividend_yield=0.0)
    sc = universe.calculate_kr_scores(fin, 644_995_911_830, name="F&F홀딩스")
    universe._flush_updates(conn, [universe._build_update(universe._kr_update_cols(sc, fin, 2025), "007700", "KR")])
    new_mcap = 644_995_911_830 * 2
    monkeypatch.setattr("src.data_fetcher.fetch_krx_daily",
                        lambda market, bas_dd: [_krx_row("007700", "F&F홀딩스", 30000, new_mcap)]
                        if market == "KOSPI" else [])
    monkeypatch.setattr("src.data_fetcher.fetch_krx_base_info",
                        lambda market, bas_dd: [_base("007700")] if market == "KOSPI" else [])
    monkeypatch.setattr("src.data_fetcher.fetch_dart_financials",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("DART 호출 금지")))
    out = universe.refresh_kr_prices()
    assert out["discovered"] == 1 and out["rescored"] == 1
    row = universe.lookup("007700")
    assert row.per == pytest.approx(10.26, abs=0.02) and row.market_cap == new_mcap


# ---------------- pick_top / top_symbols / freshness ----------------


def test_pick_top_us_sector_cap(fresh_db):
    conn = universe._conn()
    for i, (sym, sector, total) in enumerate([
        ("E1", "Energy", 95), ("E2", "Energy", 94), ("E3", "Energy", 93), ("E4", "Energy", 92),
        ("E5", "Energy", 91), ("T1", "Tech", 90), ("F1", "Financial", 89), ("H1", "Health", 88),
    ]):
        _seed_stock(conn, sym, "US", total, sector=sector, mcap=1e10 - i)
    picked = universe.pick_top("US", 6)
    assert [r.symbol for r in picked] == ["E1", "E2", "T1", "F1", "H1"]   # 에너지 2개 상한, 나머지 채움
    assert universe.top_symbols(n=6, market="US") == [r.symbol for r in picked]
    assert [r.symbol for r in universe.pick_top("US", 6, max_per_sector=None)][:5] == ["E1", "E2", "E3", "E4", "E5"]


def test_pick_top_kr_holding_cap(fresh_db):
    conn = universe._conn()
    for i, (sym, name, total) in enumerate([
        ("H1", "A홀딩스", 95), ("H2", "B지주", 94), ("H3", "C홀딩스", 93), ("H4", "D Holdings", 92),
        ("N1", "삼성전자", 91), ("N2", "에스엠", 90), ("F1", "KB금융지주", 89),
    ]):
        universe._upsert_universe_row(conn, symbol=sym, market="KR", name=name, sector="우량기업부",
                                      industry="", price=1.0, market_cap=1e12 - i, dividend_yield=0.0)
        conn.execute("UPDATE screened SET enriched=1, total_score=?, value_score=?, health_score=? "
                     "WHERE symbol=? AND market='KR'", (total, total, total, sym))
    conn.commit()
    picked = [r.symbol for r in universe.pick_top("KR", 6)]
    assert picked == ["H1", "H2", "N1", "N2", "F1"]        # 지주 2개 상한 (금융지주는 지주 아님), 섹터 상한 미적용
    assert [r.symbol for r in universe.pick_top("KR", 6, max_holding=None)][:4] == ["H1", "H2", "H3", "H4"]


def test_pick_top_uses_detail_holding_flag(fresh_db):
    conn = universe._conn()
    for sym, name, flags in [("X1", "대웅", ["holding"]), ("X2", "한화", ["holding"]), ("X3", "CJ", ["holding"]),
                             ("Y1", "현대차", [])]:
        universe._upsert_universe_row(conn, symbol=sym, market="KR", name=name, sector="", industry="",
                                      price=1.0, market_cap=1e12, dividend_yield=0.0)
        conn.execute("UPDATE screened SET enriched=1, total_score=90, value_score=90, health_score=90, "
                     "detail=? WHERE symbol=? AND market='KR'",
                     (_json.dumps({"health": {}, "value": {}, "flags": flags}), sym))
    conn.commit()
    picked = universe.pick_top("KR", 6)
    assert sum("holding" in r.flags for r in picked) == 2 and "Y1" in [r.symbol for r in picked]


def test_pick_top_empty_and_min_total(fresh_db):
    assert universe.pick_top("US", 6) == [] and universe.top_symbols(6, "US") == []
    conn = universe._conn()
    _seed_stock(conn, "LOW", "US", 40)
    assert universe.pick_top("US", 6, min_total=50) == []


def test_freshness_reports_dates_and_age(fresh_db):
    conn = universe._conn()
    _seed_stock(conn, "A", "US", 80)
    _seed_stock(conn, "X", "US", 99)
    conn.execute("UPDATE screened SET updated_at='2026-07-10T16:50:00+00:00', seen_at='2026-07-10T10:00:00+00:00'")
    conn.execute("UPDATE screened SET excluded='sibling', updated_at='2026-09-01T00:00:00+00:00' WHERE symbol='X'")
    conn.commit()
    f = universe.freshness("US")
    assert f["fund_asof"] == "2026-07-10" and f["price_asof"] == "2026-07-10"       # 제외 행은 범위 밖
    assert f["age_days"] == (datetime.now(timezone.utc).date() - date(2026, 7, 10)).days
    assert universe.freshness("KR") == {"fund_asof": None, "price_asof": None, "age_days": None}


# ---------------- yf_symbol ----------------


@pytest.mark.parametrize("args, expected", [
    (("007700", "KR", "KOSPI"), "007700.KS"),
    (("041510", "KR", "KOSDAQ"), "041510.KQ"),
    (("007700", "KR", None, ""), "007700.KS"),
    (("041510", "KR", None, "우량기업부"), "041510.KQ"),
    (("0126Z0", "KR", None, ""), "0126Z0.KS"),
    (("BRK-B", "US"), "BRK-B"),
    (("BTC", "CRYPTO"), "BTC-USD"),
])
def test_yf_symbol(args, expected):
    assert universe.yf_symbol(*args) == expected


def test_yf_symbol_alternates():
    assert universe.yf_symbol_alternates("041510", "KR", "KOSDAQ") == ["041510.KS"]
    assert universe.yf_symbol_alternates("007700", "KR", None, "") == ["007700.KQ"]
    assert universe.yf_symbol_alternates("BRK-B", "US") == []


# ---------------- scripts/build_universe.py 보조 (종료코드 규칙) ----------------


def test_build_universe_helpers():
    from scripts import build_universe as bu

    assert bu._enrich_outcome({"enriched": 10, "no_data": 0, "failed": 0, "aborted": False}) is False
    assert bu._enrich_outcome({"enriched": 1, "no_data": 0, "failed": 0, "aborted": True}) is True
    assert bu._enrich_outcome({"enriched": 7, "no_data": 0, "failed": 3, "aborted": False}) is True   # 30% > 20%
    assert bu._enrich_outcome({"enriched": 8, "no_data": 1, "failed": 1, "aborted": False}) is False  # 10%
    assert bu._enrich_outcome({"enriched": 0, "no_data": 0, "failed": 0, "aborted": False}) is False  # 대상 0
    assert bu._parse_markets(None) == ("US", "KR", "CRYPTO")
    assert bu._parse_markets("us, kr") == ("US", "KR")
    with pytest.raises(SystemExit):
        bu._parse_markets("US,MARS")
    assert bu.EXIT_PARTIAL == 5 and bu.EXIT_REMOTE_UNAVAILABLE == 2


def test_build_universe_require_remote_guard(fresh_db, monkeypatch):
    """WP-OPS-CORE 계약 `require_remote(what) -> None` (실패 시 StorageError) 소비.

    없으면 True(진행) · None 반환이면 True(정상 연결) · StorageError 면 False.
    반환값을 bool 로 읽으면 None → False 가 되어 정상 연결에서도 종료코드 2 가 나는 사고(리뷰 지적).
    """
    from scripts import build_universe as bu

    store = storage_mod.get_storage()
    monkeypatch.delattr(type(store), "require_remote", raising=False)
    assert bu._require_remote("x") is True                                   # 병합 전: 메서드 없음

    seen = []
    monkeypatch.setattr(type(store), "require_remote",
                        lambda self, what: seen.append(what), raising=False)   # -> None
    assert bu._require_remote("build_universe") is True and seen == ["build_universe"]

    def boom(self, what):
        raise storage_mod.StorageError("Turso 연결 실패 (테스트)")

    monkeypatch.setattr(type(store), "require_remote", boom, raising=False)
    assert bu._require_remote("x") is False


# ---------------- scripts/build_universe.py main() 종료코드 경로 (오프라인) ----------------


def _patch_build_universe_pipeline(monkeypatch, *, kr_stats=None, us_stats=None):
    """universe 의 네트워크 단계 전부를 가짜로 대체하고 호출 기록을 돌려준다."""
    calls = []
    ok = {"enriched": 5, "no_data": 1, "failed": 0, "aborted": False}

    def rec(name, ret):
        def fn(*a, **k):
            calls.append(name)
            return ret
        return fn

    monkeypatch.setattr(universe, "discover", rec("discover", {"US": 3, "KR": 2, "CRYPTO": 1}))
    monkeypatch.setattr(universe, "enrich", rec("enrich", us_stats or dict(ok)))
    monkeypatch.setattr(universe, "enrich_kr", rec("enrich_kr", kr_stats or dict(ok)))
    monkeypatch.setattr(universe, "reclassify_us",
                        rec("reclassify_us", {"changed": 1, "excluded": 1, "by_reason": {"sibling": 1}}))
    monkeypatch.setattr(universe, "refresh_kr_prices",
                        rec("refresh_kr_prices", {"discovered": 2, "rescored": 2, "skipped": 0}))
    monkeypatch.setattr(universe, "symbols_needing_enrichment", lambda *a, **k: [])
    monkeypatch.setattr(universe, "_kr_symbols_needing_enrichment", lambda *a, **k: [])
    return calls


def test_build_universe_main_ok_when_require_remote_returns_none(fresh_db, monkeypatch, capsys):
    from scripts import build_universe as bu

    store = storage_mod.get_storage()
    monkeypatch.setattr(type(store), "require_remote", lambda self, what: None, raising=False)
    calls = _patch_build_universe_pipeline(monkeypatch)
    monkeypatch.setattr("sys.argv", ["build_universe.py"])
    assert bu.main() == bu.EXIT_OK
    assert calls == ["discover", "enrich", "enrich_kr"]
    assert "연결 불가" not in capsys.readouterr().out


def test_build_universe_main_exit2_when_remote_unavailable(fresh_db, monkeypatch, capsys):
    from scripts import build_universe as bu

    store = storage_mod.get_storage()

    def boom(self, what):
        raise storage_mod.StorageError("Turso 연결 실패 (테스트)")

    monkeypatch.setattr(type(store), "require_remote", boom, raising=False)
    calls = _patch_build_universe_pipeline(monkeypatch)
    monkeypatch.setattr("sys.argv", ["build_universe.py"])
    assert bu.main() == bu.EXIT_REMOTE_UNAVAILABLE
    assert calls == []                                              # 쓰기 작업 시작 안 함
    assert "연결 불가" in capsys.readouterr().out


def test_build_universe_main_exit5_on_kr_abort(fresh_db, monkeypatch, capsys):
    from scripts import build_universe as bu

    monkeypatch.delattr(type(storage_mod.get_storage()), "require_remote", raising=False)
    _patch_build_universe_pipeline(
        monkeypatch, kr_stats={"enriched": 0, "no_data": 0, "failed": 0, "aborted": True})
    monkeypatch.setattr("sys.argv", ["build_universe.py", "--enrich"])
    assert bu.main() == bu.EXIT_PARTIAL
    assert "중단됨" in capsys.readouterr().out


def test_build_universe_main_exit5_on_failure_rate(fresh_db, monkeypatch):
    from scripts import build_universe as bu

    monkeypatch.delattr(type(storage_mod.get_storage()), "require_remote", raising=False)
    calls = _patch_build_universe_pipeline(
        monkeypatch, us_stats={"enriched": 7, "no_data": 0, "failed": 3, "aborted": False})   # 30%
    monkeypatch.setattr("sys.argv", ["build_universe.py", "--enrich"])
    assert bu.main() == bu.EXIT_PARTIAL
    assert calls == ["enrich", "enrich_kr"]                         # --enrich 는 discover 생략


def test_build_universe_main_standalone_modes(fresh_db, monkeypatch):
    from scripts import build_universe as bu

    monkeypatch.delattr(type(storage_mod.get_storage()), "require_remote", raising=False)
    calls = _patch_build_universe_pipeline(monkeypatch)
    monkeypatch.setattr("sys.argv", ["build_universe.py", "--reclassify", "--kr-refresh"])
    assert bu.main() == bu.EXIT_OK
    assert calls == ["reclassify_us", "refresh_kr_prices"]          # 발굴/보강은 실행 안 함


# ---------------- 금융중립 symbol 예외 (WP-SCORE FIN_EXEMPT_SYMBOLS 소비) ----------------


def test_is_balance_sheet_financial_passes_symbol_exemption(fresh_db, monkeypatch):
    """V(Credit Services)는 symbol 예외로 비금융 — screener 판정과 universe 판정이 같아야 한다."""
    assert universe._is_balance_sheet_financial("Financial Services", "Financial - Credit Services", "V") is False
    assert universe._is_balance_sheet_financial("Financial Services", "Financial - Credit Services", "SOFI") is True
    assert universe._is_balance_sheet_financial("Financial Services", "Banks - Regional", None) is True

    # 병합 후 계약: screener 함수가 있으면 symbol 까지 3인자로 넘긴다
    from src import screener
    seen = []
    monkeypatch.setattr(screener, "is_balance_sheet_financial",
                        lambda sector, industry, symbol=None: seen.append((sector, industry, symbol)) or False,
                        raising=False)
    assert universe._is_balance_sheet_financial("Financial Services", "x", "MA") is False
    assert seen == [("Financial Services", "x", "MA")]


def test_compute_enrich_v_is_not_fin_neutral(fresh_db, monkeypatch):
    conn = universe._conn()
    universe._upsert_universe_row(conn, symbol="V", market="US", name="Visa",
                                  sector="Financial Services", industry="Financial - Credit Services",
                                  price=300.0, market_cap=5e11, dividend_yield=0.7)
    conn.commit()
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda ticker, *, basis="ttm": dict(_TTM_FAKE))
    seen_quote = {}
    real = universe._scorecards

    def spy(quote_like, m, is_fin):
        seen_quote.update(quote_like, is_fin=is_fin)
        return real(quote_like, m, is_fin)

    monkeypatch.setattr(universe, "_scorecards", spy)
    detail = _json.loads(universe._compute_enrich(conn, "V", "US")["detail"])
    assert detail["fin"] is False and "fin_neutral" not in detail["flags"]
    assert seen_quote["symbol"] == "V" and seen_quote["is_fin"] is False   # quote_like 에 symbol 전달


# ---------------- enrich_kr: corpCode 실패는 aborted ----------------


@pytest.mark.parametrize("exc", [
    __import__("src.exceptions", fromlist=["DataValidationError"]).DataValidationError("DART 점검 중 (status 800)"),
    __import__("src.exceptions", fromlist=["ApiAuthError"]).ApiAuthError("DART 키 오류 (010)"),
])
def test_enrich_kr_corp_code_failure_sets_aborted(fresh_db, monkeypatch, exc):
    conn = universe._conn()
    universe._upsert_universe_row(conn, symbol="005930", market="KR", name="삼성전자", sector="",
                                  industry="", price=1.0, market_cap=4e14, dividend_yield=0.0)
    conn.commit()

    def boom():
        raise exc

    called = []
    monkeypatch.setattr("src.data_fetcher.fetch_dart_corp_codes", boom)
    monkeypatch.setattr("src.data_fetcher.fetch_dart_financials", lambda *a, **k: called.append(a))
    stats = universe.enrich_kr()
    assert stats["aborted"] is True and stats["enriched"] == 0 and called == []

    from scripts import build_universe as bu
    assert bu._enrich_outcome(stats) is True                          # → 종료코드 5


def test_build_universe_calls_snapshot_hooks():
    """재점수는 시작 시 pre_rescore, 끝에서 rescore 스냅샷을 남긴다 (원장 소급·백테스트 재료).

    훅이 빠지면 07-10 동결 DB 같은 '덮어쓰기 전 상태'를 영구히 잃는다(RPP-06).
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "scripts" / "build_universe.py").read_text()
    assert "ledger.ensure_pre_snapshot()" in src
    assert 'ledger.snapshot_universe("rescore", rule_version=universe.RULE_VERSION)' in src


# ---------------- WP-POLISH: Stage 1 리뷰 low 정리 ----------------


def test_rescore_kr_offline_preserves_detail_basis(fresh_db, monkeypatch):
    """오프라인 재점수가 enrich_kr 가 저장한 detail.basis('FY2025') 를 지우지 않는다."""
    import json

    import src.data_fetcher as df

    conn = universe._conn()
    fin = df._parse_dart_all(_dart_fixture()["FNF_holdings"]["list"], "CFS")
    universe._upsert_universe_row(conn, symbol="007700", market="KR", name="F&F홀딩스", sector="",
                                  industry="", price=1.0, market_cap=644_995_911_830, dividend_yield=0.0)
    sc = universe.calculate_kr_scores(fin, 644_995_911_830, name="F&F홀딩스")
    universe._flush_updates(conn, [
        universe._build_update(universe._kr_update_cols(sc, fin, 2025), "007700", "KR")])
    universe.rescore_kr_offline({"007700": 644_995_911_830 * 2})
    detail = json.loads(conn.execute(
        "SELECT detail FROM screened WHERE symbol='007700' AND market='KR'").fetchone()[0])
    assert detail["basis"] == "FY2025"
    assert detail["raw"]                                              # raw 도 유지


def test_kr_update_cols_basis_fallback():
    import json

    sc = {"detail": {"health": {}, "value": {}}, "roe": 1, "per": 2, "pbr": 3,
          "health_score": 1, "value_score": 1, "total_score": 1}
    assert json.loads(universe._kr_update_cols(sc, {}, None)["detail"]).get("basis") is None
    assert json.loads(universe._kr_update_cols(sc, {}, None, "FY2024")["detail"])["basis"] == "FY2024"
    assert json.loads(universe._kr_update_cols(sc, {}, 2025, "FY2024")["detail"])["basis"] == "FY2025"


def test_kr_scores_one_off_none_labels_suspect_not_loss():
    """op ≤ 0 이고 3개년 이력 없으면 ni=None(0점) — 표기는 '적자' 가 아니라 '일회성 의심'."""
    fin = {"net_income": 500.0, "op_income": -10.0, "equity": 1000.0, "equity_total": 1000.0,
           "debt": 100.0, "assets": 1100.0, "revenue": 1000.0}
    comp = _kr_comp(universe.calculate_kr_scores(fin, 5000.0, name="테스트"), "health", "흑자")
    assert comp[1] == 0 and comp[3] == "일회성 의심"
    # 실제 적자는 그대로 '적자'
    comp2 = _kr_comp(universe.calculate_kr_scores(dict(fin, net_income=-50.0), 5000.0, name="테스트"),
                     "health", "흑자")
    assert comp2[3] == "적자"
    # 정상 흑자
    comp3 = _kr_comp(universe.calculate_kr_scores(dict(fin, op_income=400.0), 5000.0, name="테스트"),
                     "health", "흑자")
    assert comp3[3] == "흑자"


def test_discover_crypto_batches_single_flush(fresh_db, monkeypatch):
    """CRYPTO 발굴은 행마다 execute 하지 않고 배치(executescript)로 쓴다."""
    import src.data_fetcher as df

    coins = [{"symbol": f"c{i}", "name": f"Coin{i}", "current_price": 1.0 + i,
              "market_cap": 5e9 + i, "market_cap_rank": i + 1,
              "price_change_percentage_24h": 1.0} for i in range(5)]
    coins.append({"symbol": "tiny", "name": "Tiny", "current_price": 1.0, "market_cap": 1.0})
    monkeypatch.setattr(df, "fetch_crypto_top", lambda top_n=50: coins)
    flushes = []
    real = universe._flush_updates

    def spy(conn, stmts):
        flushes.append(len(stmts))
        real(conn, stmts)

    monkeypatch.setattr(universe, "_flush_updates", spy)
    counts = universe.discover(markets=("CRYPTO",))
    assert counts["CRYPTO"] == 5
    assert flushes == [5]                                              # 한 번에 flush
    assert universe.lookup("C3").market == "CRYPTO"
    universe.discover(markets=("CRYPTO",))                             # 재실행(upsert) 멱등
    n = universe._conn().execute("SELECT COUNT(*) FROM screened WHERE market='CRYPTO'").fetchone()[0]
    assert n == 5


def test_build_universe_markets_applies_to_enrich(fresh_db, monkeypatch):
    from scripts import build_universe as bu

    monkeypatch.delattr(type(storage_mod.get_storage()), "require_remote", raising=False)
    calls = _patch_build_universe_pipeline(monkeypatch)
    monkeypatch.setattr("sys.argv", ["build_universe.py", "--enrich", "--markets", "US"])
    assert bu.main() == bu.EXIT_OK
    assert calls == ["enrich"]                                         # KR DART 보강 안 함

    calls = _patch_build_universe_pipeline(monkeypatch)
    monkeypatch.setattr("sys.argv", ["build_universe.py", "--markets", "KR"])
    assert bu.main() == bu.EXIT_OK
    assert calls == ["discover", "enrich_kr"]

    calls = _patch_build_universe_pipeline(monkeypatch)
    monkeypatch.setattr("sys.argv", ["build_universe.py", "--enrich", "--markets", "CRYPTO"])
    assert bu.main() == bu.EXIT_OK
    assert calls == []


def test_build_universe_kr_refresh_zero_discovered_is_partial(fresh_db, monkeypatch, capsys):
    from scripts import build_universe as bu

    monkeypatch.delattr(type(storage_mod.get_storage()), "require_remote", raising=False)
    _patch_build_universe_pipeline(monkeypatch)
    monkeypatch.setattr(universe, "refresh_kr_prices",
                        lambda: {"discovered": 0, "rescored": 0, "skipped": 0})
    monkeypatch.setattr("sys.argv", ["build_universe.py", "--kr-refresh"])
    assert bu.main() == bu.EXIT_PARTIAL
    assert "발굴 0건" in capsys.readouterr().out


# ---------------- S2FIX-SURFACE: S2 지주사 판정 · S3 기준일 · S8 시장 지정 lookup · S11 문구 ----------------


def _scan_row(**kw):
    base = dict(symbol="X", market="KR", name="테스트", sector="", price=1.0, market_cap=1.0,
                total_score=50, value_score=50, health_score=50, roe=None)
    base.update(kw)
    return universe.ScanRow(**base)


def test_is_holding_row_flag_or_name_rule():
    """flags 'holding' 이거나 이름 규칙(홀딩스/지주) — pick_top 과 표면 꼬리표가 공유하는 판정."""
    assert universe.is_holding_row(_scan_row(flags=("holding",)))
    assert universe.is_holding_row(_scan_row(name="영원무역홀딩스"))           # 이름 규칙만
    assert not universe.is_holding_row(_scan_row(name="쿠쿠홈시스"))
    assert not universe.is_holding_row(_scan_row(name="KB금융지주"))           # 금융지주는 금융으로 판정


def _seed_sui(conn):
    _seed_stock(conn, "SUI", "US", 60)
    universe._store_crypto_scored(
        conn, symbol="SUI", name="Sui", price=3.0, market_cap=1e10,
        scores={"total_score": 70, "volatility_score": 50, "rank_score": 80},
    )
    conn.commit()


def test_lookup_market_filter_and_other_markets(fresh_db):
    """CMAP-11: US 티커 SUI 와 크립토 SUI — 시장 미지정이면 US 우선, 지정하면 그 시장."""
    conn = universe._conn()
    _seed_sui(conn)
    assert universe.lookup("sui").market == "US"                          # 하위호환: 주식 우선
    assert universe.lookup("SUI", "crypto").market == "CRYPTO"             # 대소문자 무관
    assert universe.lookup("SUI", "KR") is None
    assert universe.lookup_markets("SUI") == ["US", "CRYPTO"]
    assert universe.lookup_markets("NOPE") == []


def test_lookup_detail_market_filter(fresh_db):
    conn = universe._conn()
    _seed_sui(conn)
    conn.execute("UPDATE screened SET detail=? WHERE symbol='SUI' AND market='US'", ('{"tag": "us"}',))
    conn.execute("UPDATE screened SET detail=? WHERE symbol='SUI' AND market='CRYPTO'", ('{"tag": "cr"}',))
    conn.commit()
    assert universe.lookup_detail("SUI")["tag"] == "us"
    assert universe.lookup_detail("SUI", "CRYPTO")["tag"] == "cr"


def test_asof_status_thresholds_use_settings_and_boundaries():
    """경계: 8일 → 경고, 7일 → 없음, 14일 → 보류 아님, 15일 → 보류 (settings 임계, 다이제스트와 동일)."""
    from src.config import settings

    today = date(2026, 10, 9)
    assert settings.universe_warn_days == 8 and settings.universe_hold_days == 15
    st8 = universe.asof_status("2026-10-01", today=today)
    assert st8 == {"age_days": 8, "warn": True, "hold": False}
    assert universe.asof_status("2026-10-02", today=today)["warn"] is False
    assert universe.asof_status("2026-09-25", today=today) == {"age_days": 14, "warn": True, "hold": False}
    assert universe.asof_status("2026-09-24", today=today) == {"age_days": 15, "warn": True, "hold": True}
    assert universe.asof_status(None, today=today) == {"age_days": None, "warn": False, "hold": False}
    assert universe.asof_status("garbage", today=today)["age_days"] is None


def test_kr_scores_one_off_with_nonpositive_three_year_avg_labels_suspect():
    """ni>0·op≤0 이고 3개년 이력이 있어 min(ni, 평균)이 ≤0 이 되는 경로 — '적자' 가 아니라 '일회성 의심'."""
    fin = {"net_income": 500.0, "op_income": -10.0, "ni_prev1": -3000.0, "ni_prev2": -2000.0,
           "equity": 1000.0, "equity_total": 1000.0, "debt": 100.0, "assets": 1100.0, "revenue": 1000.0}
    ni, flag = universe.normalize_net_income(fin)
    assert flag == "one_off" and ni is not None and ni <= 0               # 가드 결과가 None 이 아닌 비양수
    sc = universe.calculate_kr_scores(fin, 5000.0, name="테스트")
    comp = _kr_comp(sc, "health", "흑자")
    assert comp[1] == 0 and comp[3] == "일회성 의심"
