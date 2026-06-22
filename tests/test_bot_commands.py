"""src/bot_commands.py — 조회 명령 파싱/포매팅/핸들러/rate limit (tmp DB, 네트워크 없음)."""

from __future__ import annotations

import json

import pytest

import src.storage as storage_mod
from src import bot_commands as bc
from src import universe
from src.universe import ScanRow


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


def _row(symbol="AAPL", market="US", name="Apple", total=75, value=80, health=70,
         roe=15.0, per=None, pbr=None) -> ScanRow:
    return ScanRow(symbol=symbol, market=market, name=name, sector="Tech", price=100.0,
                   market_cap=5e9, total_score=total, value_score=value,
                   health_score=health, roe=roe, per=per, pbr=pbr)


def _seed(conn, symbol, market="US", name="Apple", total=75, value=80, health=70,
          roe=15.0, per=None, pbr=None, detail=None):
    conn.execute(
        "INSERT INTO screened (symbol, market, name, sector, price, market_cap, roe, per, "
        "pbr, health_score, value_score, total_score, detail, enriched, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (symbol, market, name, "Tech", 100.0, 5e9, roe, per, pbr, health, value, total,
         json.dumps(detail, ensure_ascii=False) if detail else None, "t", "t"),
    )
    conn.commit()


_DETAIL = {
    "value": {"total": 80, "components": [["EV/EBITDA", 20.0, 25, "9.0"], ["배당", 5.0, 10, "2.0%"]]},
    "health": {"total": 70, "components": [["ROE", 12.0, 20, "15%"], ["순부채", 8.0, 10, "낮음"]]},
}


# ---------------- 파싱 ----------------


def test_parse_command_kinds_and_args():
    assert bc.parse_command("/stock AAPL") == bc.Command("stock", "AAPL")
    assert bc.parse_command("/scan kr") == bc.Command("scan", "kr")
    assert bc.parse_command("/scan") == bc.Command("scan", None)
    assert bc.parse_command("/help") == bc.Command("help", None)
    assert bc.parse_command("/stock@MyBot TSLA") == bc.Command("stock", "TSLA")
    assert bc.parse_command("/start") == bc.Command("unknown", None)   # 구독 명령은 여기서 unknown
    assert bc.parse_command("안녕") == bc.Command("unknown", None)
    assert bc.parse_command("") == bc.Command("unknown", None)


# ---------------- 포매팅 (순수) ----------------


def test_format_stock_with_detail_breakdown():
    out = bc.format_stock(_row("AAPL", name="Apple", total=75, value=80, health=70), _DETAIL)
    assert "AAPL" in out and "Apple" in out
    assert "종합 *75*" in out
    assert "EV/EBITDA 20.0/25 (9.0)" in out      # 밸류 구성요소
    assert "ROE 12.0/20 (15%)" in out            # 건전성 구성요소


def test_format_stock_without_detail_is_safe():
    out = bc.format_stock(_row("AAPL"), None)
    assert "AAPL" in out and "종합 *75*" in out
    assert "•" not in out                         # 구성요소 섹션 없음


def test_format_stock_kr_shows_per_pbr():
    out = bc.format_stock(_row("005930", market="KR", name="삼성전자", per=8.6, pbr=1.06), None)
    assert "🇰🇷" in out and "PER 8.6" in out and "PBR 1.06" in out


def test_format_scan_lists_rows_and_handles_empty():
    rows = [_row("AAA", total=90), _row("BBB", total=70)]
    out = bc.format_scan("US", rows)
    assert "🇺🇸 미국" in out and "AAA" in out and "BBB" in out
    assert "발굴 종목이 없" in bc.format_scan("KR", [])


# ---------------- 핸들러 (DB) ----------------


def test_handle_stock_found_and_missing(fresh_db):
    conn = universe._conn()
    _seed(conn, "AAPL", detail=_DETAIL)
    out = bc.handle_stock("aapl")                # 소문자도 대문자로
    assert "AAPL" in out and "EV/EBITDA" in out
    assert "찾지 못" in bc.handle_stock("ZZZZ")   # 미발굴
    assert "사용법" in bc.handle_stock(None)


def test_handle_scan_market_and_usage(fresh_db):
    conn = universe._conn()
    _seed(conn, "AAA", market="US", total=90)
    _seed(conn, "BBB", market="US", total=60)
    out = bc.handle_scan("us")
    assert "AAA" in out and out.index("AAA") < out.index("BBB")   # 점수 내림차순
    assert "사용법" in bc.handle_scan("jp")       # 미지원 시장


# ---------------- Rate limit ----------------


def test_rate_limiter_window_and_per_user():
    now = [0.0]
    rl = bc.RateLimiter(max_calls=2, window_sec=10, clock=lambda: now[0])
    assert rl.allow("u") and rl.allow("u")        # 2회 허용
    assert not rl.allow("u")                       # 3회차 차단
    assert rl.allow("other")                       # 유저별 분리
    now[0] = 11                                    # 윈도우 경과
    assert rl.allow("u")                           # 다시 허용


# ---------------- 디스패치 ----------------


def test_respond_unknown_and_subscription_return_none():
    assert bc.respond("hello", "1") is None
    assert bc.respond("/start", "1") is None       # 구독 명령은 호출부(subscribers)에서


def test_respond_help_and_rate_limit_drop():
    rl = bc.RateLimiter(max_calls=1, window_sec=60, clock=lambda: 0.0)
    assert "명령어" in bc.respond("/help", "1", rl)
    assert bc.respond("/help", "1", rl) is None    # 한도 초과 → 드롭


def test_respond_stock_dispatch(fresh_db):
    conn = universe._conn()
    _seed(conn, "AAPL", detail=_DETAIL)
    out = bc.respond("/stock AAPL", "1")
    assert out is not None and "AAPL" in out


# ---------------- 버튼 / 메뉴 ----------------


def test_menu_alias_is_help():
    assert bc.parse_command("/menu").kind == "help"


def test_button_labels_map_to_commands():
    assert bc.BUTTON_TO_COMMAND["🇺🇸 미국 추천"] == "/scan us"
    assert bc.BUTTON_TO_COMMAND["🇰🇷 한국 추천"] == "/scan kr"
    assert bc.BUTTON_TO_COMMAND["📋 구독자"] == "/subscribers"


def test_main_keyboard_owner_gets_admin_row():
    base = bc.main_keyboard(is_owner=False)
    assert base["resize_keyboard"] is True
    flat_user = [b for row in base["keyboard"] for b in row]
    assert "📋 구독자" not in flat_user                 # 일반 사용자엔 관리자 버튼 없음
    flat_owner = [b for row in bc.main_keyboard(is_owner=True)["keyboard"] for b in row]
    assert "📋 구독자" in flat_owner and "⏳ 승인 대기" in flat_owner
