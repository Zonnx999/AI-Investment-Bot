"""다이제스트 ↔ /scan ↔ /stock 표면 정합 통합 테스트 (S2FIX-SURFACE S4).

같은 합성 DB(tmp sqlite)를 두고 digest.assemble_daily_digest 와 bot_commands.handle_scan/handle_stock 이
(1) 같은 후보 심볼·종합 점수, (2) 같은 라벨 상수, (3) 같은 기준일 경고/보류 임계를 쓰는지 확인한다.
네트워크 경계(신호 리포트·yfinance 가격·간밤 지표)는 monkeypatch 로 차단 — digest.py 는 읽기만.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import src.digest as dg
import src.storage as storage_mod
from src import bot_commands as bc
from src import findings, universe
from tests.test_digest import OVERNIGHT, _metrics, _report


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    import src.ledger as ledger_mod

    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "surface.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    monkeypatch.setattr(ledger_mod, "_schema_ready_store", None)
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


def _seed(conn, symbol, market, name, total, value, health, sector, fund_asof, price_asof, detail=None):
    conn.execute(
        "INSERT INTO screened (symbol, market, name, sector, price, market_cap, roe, "
        "health_score, value_score, total_score, detail, enriched, discovered_at, updated_at, "
        "excluded, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
        (symbol, market, name, sector, 100.0, 5e9, 10.0, health, value, total,
         json.dumps(detail, ensure_ascii=False) if detail else None, "t",
         f"{fund_asof}T01:00:00+00:00", None, f"{price_asof}T00:00:00+00:00"),
    )
    conn.commit()


def _wire_network(monkeypatch, market):
    """assemble_daily_digest 의 네트워크/외부 경계만 차단 — 유니버스·원장은 실제 tmp DB."""
    import src.signals as sig

    rep = _report(market=market)
    monkeypatch.setattr(sig, "generate_signal_report", lambda **k: rep)
    monkeypatch.setattr(dg, "_price_metrics", lambda sym, mkt, ex, sec: _metrics(100.0, 1.0, -5.0, -10.0, 20.0))
    monkeypatch.setattr(dg, "_overnight_us_line", lambda: OVERNIGHT)
    monkeypatch.setattr(dg, "_bench_price_asof", lambda mkt: "2026-10-07")


def _now() -> datetime:
    return datetime.now(dg.KST).replace(hour=8, minute=30, second=0, microsecond=0)


def _iso_ago(now: datetime, days: int) -> str:
    return (now.date() - timedelta(days=days)).isoformat()


def _seed_market(market, now, days_old):
    conn = universe._conn()
    fund = _iso_ago(now, days_old)
    px = _iso_ago(now, 1)
    specs = [("AAA", "Alpha", 92, 90, 94, "Tech"), ("BBB", "Beta", 88, 85, 91, "Energy"),
             ("CCC", "Gamma", 80, 78, 82, "Utilities")]
    if market == "KR":
        specs = [("005930", "삼성전자", 92, 90, 94, ""), ("000660", "에스케이하이닉스", 88, 85, 91, ""),
                 ("105630", "영원무역홀딩스", 80, 78, 82, "")]
    for sym, name, total, value, health, sector in specs:
        _seed(conn, sym, market, name, total, value, health, sector, fund, px)
    return [s[0] for s in specs]


@pytest.mark.parametrize("market", ["US", "KR"])
def test_digest_picks_match_scan_symbols_and_scores(fresh_db, monkeypatch, market):
    now = _now()
    _seed_market(market, now, days_old=1)
    _wire_network(monkeypatch, market)
    bundle = dg.assemble_daily_digest(market.lower(), now=now)
    scan = bc.handle_scan(market.lower())
    assert bundle.picks, "합성 DB 에서 후보가 선정돼야 함"
    # 같은 심볼 (같은 순서) · 같은 종합 점수
    scan_lines = [ln for ln in scan.splitlines() if "종합" in ln]
    assert [r.symbol for r in bundle.picks] == [r.symbol for r in universe.pick_top(market, 6)]
    for p, line in zip(bundle.picks, scan_lines):
        assert p.symbol in line
        assert f"종합 *{p.total_score}*" in line
        assert str(p.total_score) in bundle.text            # 다이제스트에도 같은 점수가 표기됨
        assert p.symbol in bundle.text
        # /stock 도 같은 종합 점수
        assert f"종합 *{p.total_score}*" in bc.handle_stock(p.symbol, market)


def test_label_constants_are_one_object_across_surfaces():
    assert dg.LABEL_VALUE is findings.LABEL_VALUE is bc.LABEL_VALUE
    assert dg.LABEL_HEALTH is findings.LABEL_HEALTH is bc.LABEL_HEALTH
    assert dg.LABEL_CANDIDATES == findings.LABEL_CANDIDATES == "스크리닝 후보"
    assert "투자 자문 아님" in bc.DISCLAIMER and "투자 자문 아님" in dg.DISCLAIMER
    assert bc.score_labels("CRYPTO") == ("변동성", "순위")


@pytest.mark.parametrize("days_old,warn,hold", [
    (7, False, False),      # 7일 — 경고 없음
    (8, True, False),       # 8일 — 경고 (settings.universe_warn_days)
    (14, True, False),      # 14일 — 경고, 보류 아님
    (15, True, True),       # 15일 — 보류 (settings.universe_hold_days)
])
def test_asof_warn_and_hold_agree_between_digest_and_scan(fresh_db, monkeypatch, days_old, warn, hold):
    now = _now()
    _seed_market("US", now, days_old=days_old)
    _wire_network(monkeypatch, "US")
    bundle = dg.assemble_daily_digest("us", now=now)
    scan = bc.handle_scan("us")

    digest_warn = "⚠️ 점수" in bundle.text
    scan_warn = "⚠️ 점수가" in scan
    digest_hold = "후보 보류" in bundle.text
    scan_hold = "보류" in scan
    assert digest_warn == scan_warn == warn
    assert digest_hold == scan_hold == hold
    assert (bundle.record_ledger is False) == hold          # 보류일은 원장 미기록 (다이제스트 규칙)
