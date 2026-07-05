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


# ---------------- 인라인 승인/거절 버튼 (callback_query) ----------------


@pytest.fixture
def owner_set():
    """소유자 chat_id = '1' (test_subscribers 와 동일 패턴)."""
    from src.config import settings

    backup = settings.telegram_chat_id
    object.__setattr__(settings, "telegram_chat_id", "1")
    yield "1"
    object.__setattr__(settings, "telegram_chat_id", backup)


@pytest.fixture
def tg_capture(monkeypatch):
    """notifier 의 콜백/편집/전송 safe 함수들을 기록기로 대체 (네트워크 없음)."""
    calls = {"answers": [], "edits": [], "sent": []}
    monkeypatch.setattr(
        "src.notifier.answer_callback_safe",
        lambda cq_id, text=None, show_alert=False: calls["answers"].append((cq_id, text)) or True,
    )
    monkeypatch.setattr(
        "src.notifier.edit_message_safe",
        lambda chat_id, message_id, text, parse_mode=None, reply_markup=None:
            calls["edits"].append((chat_id, message_id, text)) or True,
    )
    monkeypatch.setattr(
        "src.notifier.send_safe",
        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
            calls["sent"].append((chat_id, text)) or True,
    )
    return calls


def _cq(data, from_id=1, cq_id="cbq1", message_id=42, chat_id=1,
        msg_text="🔔 가입 요청: alice (chat_id=100)"):
    """텔레그램 callback_query update 조각 (필요 필드만)."""
    return {"id": cq_id, "from": {"id": from_id}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}, "text": msg_text}}


def _seed_pending(chat_id="100", name="alice"):
    from src import subscribers

    conn = subscribers._conn()
    subscribers.upsert_request(conn, chat_id, name)
    conn.commit()
    return subscribers


def test_approval_keyboard_callback_data_format():
    kb = bc.approval_keyboard("12345")
    buttons = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["approve:12345", "deny:12345"]
    assert buttons[0]["text"] == "✅ 승인" and buttons[1]["text"] == "❌ 거절"


def test_parse_callback_valid_and_invalid():
    assert bc.parse_callback("approve:12345") == ("approve", "12345")
    assert bc.parse_callback("deny:12345") == ("deny", "12345")
    assert bc.parse_callback("approve:") is None      # 대상 누락
    assert bc.parse_callback("approve") is None       # 구분자 없음
    assert bc.parse_callback("hack:123") is None      # 미지원 action
    assert bc.parse_callback("") is None


def test_callback_approve_tap(fresh_db, owner_set, tg_capture):
    subs = _seed_pending("100")
    bc.handle_callback(_cq("approve:100"), owner_set)

    assert subs.subscriber_status("100") == "active"                 # /approve 와 동일 상태 변화
    assert tg_capture["answers"] == [("cbq1", "승인 완료")]           # 토스트
    assert len(tg_capture["edits"]) == 1                             # 원본 알림 편집(버튼 제거)
    chat, mid, text = tg_capture["edits"][0]
    assert chat == "1" and mid == 42
    assert text.startswith("🔔 가입 요청") and text.endswith("→ ✅ 승인됨")
    assert any(cid == "100" and "승인되었습니다" in txt
               for cid, txt in tg_capture["sent"])                   # 신청자 알림


def test_callback_deny_tap(fresh_db, owner_set, tg_capture):
    subs = _seed_pending("100")
    bc.handle_callback(_cq("deny:100"), owner_set)

    assert subs.subscriber_status("100") == "inactive"
    assert tg_capture["answers"] == [("cbq1", "거절 완료")]
    assert tg_capture["edits"][0][2].endswith("→ ❌ 거절됨")
    assert any(cid == "100" and "거절" in txt for cid, txt in tg_capture["sent"])


def test_callback_owner_gate_rejects_non_owner(fresh_db, owner_set, tg_capture):
    subs = _seed_pending("100")
    bc.handle_callback(_cq("approve:100", from_id=999), owner_set)   # 비소유자 탭

    assert subs.subscriber_status("100") == "pending"                # 상태 불변
    assert len(tg_capture["answers"]) == 1                           # 콜백엔 항상 응답
    assert "소유자만" in tg_capture["answers"][0][1]
    assert tg_capture["edits"] == [] and tg_capture["sent"] == []    # 편집/알림 없음


def test_callback_second_tap_is_idempotent(fresh_db, owner_set, tg_capture):
    subs = _seed_pending("100")
    bc.handle_callback(_cq("approve:100"), owner_set)                # 1차 탭 → 승인
    bc.handle_callback(_cq("approve:100", cq_id="cbq2"), owner_set)  # 더블탭
    bc.handle_callback(_cq("deny:100", cq_id="cbq3"), owner_set)     # 승인 후 거절 탭도 무변화

    assert subs.subscriber_status("100") == "active"                 # 1차 결과 유지
    assert tg_capture["answers"] == [("cbq1", "승인 완료"),
                                     ("cbq2", "이미 처리됨"), ("cbq3", "이미 처리됨")]
    assert len(tg_capture["edits"]) == 1                             # 재편집 없음


def test_callback_after_typed_approve_is_idempotent(fresh_db, owner_set, tg_capture):
    subs = _seed_pending("100")
    assert subs.decide_request("100", approve=True) == "approved"    # 텍스트 /approve 선처리
    bc.handle_callback(_cq("approve:100"), owner_set)                # 그 뒤 버튼 탭

    assert subs.subscriber_status("100") == "active"
    assert tg_capture["answers"] == [("cbq1", "이미 처리됨")]
    assert tg_capture["edits"] == []


def test_callback_unknown_target_and_bad_data(fresh_db, owner_set, tg_capture):
    bc.handle_callback(_cq("approve:777"), owner_set)                # 기록 없는 대상
    assert "기록이 없습니다" in tg_capture["answers"][-1][1]
    bc.handle_callback(_cq("hack:1", cq_id="cbq2"), owner_set)       # 형식 불일치 data
    assert tg_capture["answers"][-1] == ("cbq2", "알 수 없는 버튼입니다.")
    assert tg_capture["edits"] == []


def test_callback_answers_even_when_handler_errors(fresh_db, owner_set, tg_capture, monkeypatch):
    """decide_request 예외에도 answerCallbackQuery 는 반드시 호출 (finally 경로)."""
    from src import subscribers

    _seed_pending("100")

    def boom(target, approve, send_notifications=True):
        raise RuntimeError("db down")

    monkeypatch.setattr(subscribers, "decide_request", boom)
    with pytest.raises(RuntimeError):                                # 예외는 폴링 루프 가드가 처리
        bc.handle_callback(_cq("approve:100"), owner_set)
    assert len(tg_capture["answers"]) == 1
    assert "오류" in tg_capture["answers"][0][1]
    assert tg_capture["edits"] == []                                 # 실패 시 결과 표기 없음


def test_callback_without_message_still_processes(fresh_db, owner_set, tg_capture):
    """message 필드 없는 콜백(오래된 메시지 등)도 상태 변경 + 응답은 수행 (편집만 생략)."""
    subs = _seed_pending("100")
    cq = {"id": "cbq1", "from": {"id": 1}, "data": "approve:100"}    # message 없음
    bc.handle_callback(cq, owner_set)
    assert subs.subscriber_status("100") == "active"
    assert tg_capture["answers"] == [("cbq1", "승인 완료")]
    assert tg_capture["edits"] == []
