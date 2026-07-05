"""src/subscribers.py — 승인제 구독: 파싱(순수) + DB + 권한 오케스트레이터 (tmp DB, 네트워크 없음)."""

from __future__ import annotations

import pytest

import src.storage as storage_mod
from src import subscribers
from src.config import settings


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """storage 싱글톤을 tmp DB 로 격리 (로컬 sqlite3)."""
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "sub.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


@pytest.fixture
def owner_set():
    """소유자 chat_id = '1' (frozen dataclass → object.__setattr__)."""
    backup = settings.telegram_chat_id
    object.__setattr__(settings, "telegram_chat_id", "1")
    yield "1"
    object.__setattr__(settings, "telegram_chat_id", backup)


def _upd(update_id, text, chat_id=100, username="alice"):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id, "username": username}, "text": text}}


# ---------------- 순수 파싱 ----------------


def test_parse_commands_kinds_and_targets():
    updates = [
        _upd(1, "/start"),
        _upd(2, "/stop"),
        _upd(3, "/approve 555"),
        _upd(4, "/deny 555"),
        _upd(5, "/pending"),
        _upd(6, "/approve"),          # 인자 누락
        _upd(7, "그냥 잡담"),
    ]
    events, offset = subscribers.parse_updates(updates)
    assert [(e.kind, e.target) for e in events] == [
        ("request", None), ("unsubscribe", None),
        ("approve", "555"), ("deny", "555"),
        ("pending", None), ("approve", None), ("ignore", None),
    ]
    assert offset == 8                 # max update_id + 1


def test_parse_group_command_form_and_chat_id_str():
    events, _ = subscribers.parse_updates([_upd(1, "/start@QuantBot", chat_id=77)])
    assert events[0].kind == "request" and events[0].chat_id == "77"


def test_parse_empty_and_non_message():
    e0, off0 = subscribers.parse_updates([])
    assert e0 == [] and off0 is None
    # message 없는 update — 이벤트 없지만 offset 전진
    events, offset = subscribers.parse_updates(
        [{"update_id": 9, "callback_query": {"id": "x"}}, _upd(10, "/stop")]
    )
    assert [e.kind for e in events] == ["unsubscribe"] and offset == 11


# ---------------- DB 연산 ----------------


def test_request_then_approve_lifecycle(fresh_db):
    conn = subscribers._conn()
    subscribers.upsert_request(conn, "100", "alice")
    conn.commit()
    assert subscribers.get_status(conn, "100") == "pending"
    assert subscribers.active_subscribers() == []            # 아직 미승인
    assert subscribers.pending_requests() == [("100", "alice")]

    subscribers.set_status(conn, "100", "active")
    conn.commit()
    assert subscribers.active_subscribers() == [("100", "alice")]
    assert subscribers.pending_requests() == []


def test_ensure_owner_active_without_approval(fresh_db):
    backup = settings.telegram_chat_id
    object.__setattr__(settings, "telegram_chat_id", "555")
    try:
        subscribers.ensure_owner()
        assert ("555", "owner") in subscribers.active_subscribers()
    finally:
        object.__setattr__(settings, "telegram_chat_id", backup)


def test_subscriber_status_public_api(fresh_db):
    """scripts/bot.py 가 private _conn 없이 쓰는 파사드 — 상태별 반환 확인."""
    assert subscribers.subscriber_status("100") is None      # 기록 없음
    conn = subscribers._conn()
    subscribers.upsert_request(conn, "100", "alice")
    conn.commit()
    assert subscribers.subscriber_status("100") == "pending"
    subscribers.set_status(conn, "100", "active")
    conn.commit()
    assert subscribers.subscriber_status("100") == "active"


def test_updates_offset_public_api_roundtrip(fresh_db):
    assert subscribers.get_updates_offset() is None          # 초기값 없음
    subscribers.set_updates_offset(42)
    assert subscribers.get_updates_offset() == 42
    subscribers.set_updates_offset(43)                       # 전진
    assert subscribers.get_updates_offset() == 43


def test_conn_schema_init_once_per_storage(fresh_db, monkeypatch):
    """_conn 은 같은 storage 인스턴스에 스키마 초기화를 1회만 (Turso 왕복 절감).

    storage 싱글톤이 교체되면(테스트 tmp DB 등) 자동 재초기화되어야 함.
    """
    import src.storage as storage_mod

    # 초기화 횟수를 실제로 계수 — memo 포인터만 봐서는 '매번 초기화하면서 memo 도
    # 매번 갱신' 하는 회귀를 못 잡음. add_column_if_missing 은 init 블록에서만 불림.
    init_calls: list[int] = []
    real_add = subscribers.add_column_if_missing
    monkeypatch.setattr(
        subscribers, "add_column_if_missing",
        lambda *a, **k: init_calls.append(1) or real_add(*a, **k),
    )
    monkeypatch.setattr(subscribers, "_schema_ready_store", None)   # memo 초기화

    c1 = subscribers._conn()
    c2 = subscribers._conn()
    assert c1 is c2                                          # 동일 connection 재사용
    assert len(init_calls) == 1                              # 스키마 초기화는 딱 1회
    store_before = storage_mod.get_storage()
    assert subscribers._schema_ready_store is store_before   # memo 가 현재 store 를 가리킴

    # 싱글톤 리셋 → 새 storage 에 다시 초기화 (stale memo 로 스키마 누락되면 안 됨)
    monkeypatch.setattr(storage_mod, "_storage", None)
    c3 = subscribers._conn()
    assert len(init_calls) == 2                              # 새 store 에 재초기화 1회
    assert subscribers._schema_ready_store is storage_mod.get_storage()
    assert subscribers.get_status(c3, "nobody") is None      # subscribers 테이블 존재


def test_stats_counts_by_status(fresh_db):
    conn = subscribers._conn()
    subscribers.upsert_request(conn, "1", "a")               # pending
    subscribers.set_status(conn, "2", "active", name="b")
    subscribers.set_status(conn, "3", "inactive", name="c")
    conn.commit()
    s = subscribers.stats()
    assert s["total"] == 3 and s["active"] == 1 and s["pending"] == 1 and s["inactive"] == 1


# ---------------- 오케스트레이터 (오프라인) ----------------


def _wire(monkeypatch, updates):
    """get_updates → updates, send_safe → 기록. (sent 리스트 반환)"""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("src.notifier.get_updates", lambda offset=None: updates)
    monkeypatch.setattr(
        "src.notifier.send_safe",
        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
            sent.append((chat_id, text)) or True,
    )
    return sent


def test_sync_request_notifies_owner_and_pends(fresh_db, owner_set, monkeypatch):
    sent = _wire(monkeypatch, [_upd(10, "/start", chat_id=100, username="alice")])
    st = subscribers.sync_subscribers()
    assert st["requests"] == 1
    conn = subscribers._conn()
    assert subscribers.get_status(conn, "100") == "pending"     # 자동 가입 아님
    # 소유자(1)에게 승인 요청 알림이 갔는지
    assert any(cid == "1" and "가입 요청" in txt for cid, txt in sent)
    # offset 저장
    from src.storage import get_storage
    assert get_storage().get_state("telegram", "updates_offset") == 11


def test_sync_owner_approve_activates(fresh_db, owner_set, monkeypatch):
    updates = [
        _upd(10, "/start", chat_id=100, username="alice"),
        _upd(11, "/approve 100", chat_id=1, username="owner"),   # 소유자 승인
    ]
    _wire(monkeypatch, updates)
    st = subscribers.sync_subscribers()
    assert st["approved"] == 1
    assert subscribers.active_subscribers() == [("100", "alice")]


def test_sync_nonowner_approve_ignored(fresh_db, owner_set, monkeypatch):
    updates = [
        _upd(10, "/start", chat_id=100, username="alice"),
        _upd(11, "/approve 100", chat_id=999, username="mallory"),  # 비소유자 → 무시
    ]
    _wire(monkeypatch, updates)
    st = subscribers.sync_subscribers()
    assert st["ignored_admin"] == 1 and st["approved"] == 0
    assert subscribers.active_subscribers() == []                # 승인 안 됨

    conn = subscribers._conn()
    assert subscribers.get_status(conn, "100") == "pending"


def test_sync_deny_and_stop(fresh_db, owner_set, monkeypatch):
    updates = [
        _upd(10, "/start", chat_id=100, username="alice"),
        _upd(11, "/approve 100", chat_id=1),
        _upd(12, "/stop", chat_id=100),                          # 본인 해지
        _upd(13, "/start", chat_id=200, username="bob"),
        _upd(14, "/deny 200", chat_id=1),                        # 소유자 거절
    ]
    _wire(monkeypatch, updates)
    st = subscribers.sync_subscribers()
    assert st["unsubscribed"] == 1 and st["denied"] == 1
    assert subscribers.active_subscribers() == []
    conn = subscribers._conn()
    assert subscribers.get_status(conn, "100") == "inactive"
    assert subscribers.get_status(conn, "200") == "inactive"


def test_sync_approve_unknown_target_warns_owner(fresh_db, owner_set, monkeypatch):
    sent = _wire(monkeypatch, [_upd(10, "/approve 12345", chat_id=1)])
    st = subscribers.sync_subscribers()
    assert st["approved"] == 0
    assert any(cid == "1" and "요청 기록이 없" in txt for cid, txt in sent)


def test_sync_pending_command_lists_for_owner(fresh_db, owner_set, monkeypatch):
    updates = [
        _upd(10, "/start", chat_id=100, username="alice"),
        _upd(11, "/pending", chat_id=1),
    ]
    sent = _wire(monkeypatch, updates)
    subscribers.sync_subscribers()
    assert any(cid == "1" and "alice" in txt for cid, txt in sent)


def test_sync_already_active_no_duplicate_request(fresh_db, owner_set, monkeypatch):
    conn = subscribers._conn()
    subscribers.set_status(conn, "100", "active", name="alice")
    conn.commit()
    sent = _wire(monkeypatch, [_upd(10, "/start", chat_id=100, username="alice")])
    st = subscribers.sync_subscribers()
    assert st["requests"] == 0
    assert any(cid == "100" and "이미 구독" in txt for cid, txt in sent)


def test_sync_survives_getupdates_failure(fresh_db, owner_set, monkeypatch):
    from src.exceptions import ApiTimeoutError

    def boom(offset=None):
        raise ApiTimeoutError("timeout", source="Telegram")

    monkeypatch.setattr("src.notifier.get_updates", boom)
    st = subscribers.sync_subscribers()
    assert st == {"requests": 0, "approved": 0, "denied": 0, "unsubscribed": 0,
                  "ignored_admin": 0, "announced": 0}


def test_apply_events_handles_authorization_and_state(fresh_db, owner_set, monkeypatch):
    # apply_events 는 fetch/offset 무관 — 폴링 루프(scripts/bot.py)가 재사용하는 경로
    monkeypatch.setattr("src.notifier.send_safe",
                        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None: True)
    events = [
        subscribers.SubEvent(1, "100", "alice", "request"),
        subscribers.SubEvent(2, "1", "owner", "approve", "100"),    # 소유자 승인
        subscribers.SubEvent(3, "999", "mallory", "approve", "100"),  # 비소유자 → 무시
    ]
    st = subscribers.apply_events(events)
    assert st["requests"] == 1 and st["approved"] == 1 and st["ignored_admin"] == 1
    assert subscribers.active_subscribers() == [("100", "alice")]


def test_parse_subscribers_command_is_list():
    events, _ = subscribers.parse_updates([_upd(1, "/subscribers")])
    assert events[0].kind == "list"


def test_apply_events_list_owner_only(fresh_db, owner_set, monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.notifier.send_safe",
        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
            sent.append((chat_id, text)) or True,
    )
    conn = subscribers._conn()
    subscribers.set_status(conn, "100", "active", name="alice")
    conn.commit()

    subscribers.apply_events([subscribers.SubEvent(1, "1", "owner", "list")])      # 소유자
    assert any(cid == "1" and "alice" in txt and "구독자" in txt for cid, txt in sent)

    sent.clear()
    st = subscribers.apply_events([subscribers.SubEvent(2, "999", "x", "list")])   # 비소유자
    assert st["ignored_admin"] == 1 and sent == []


# ---------------- /announce (소유자 공지 브로드캐스트) ----------------


def test_classify_announce_captures_full_body():
    events, _ = subscribers.parse_updates([_upd(1, "/announce 서버 점검 안내 입니다")])
    assert events[0].kind == "announce"
    assert events[0].target == "서버 점검 안내 입니다"   # 여러 단어 본문 전체
    e2, _ = subscribers.parse_updates([_upd(2, "/announce")])
    assert e2[0].kind == "announce" and e2[0].target is None   # 본문 없음


def _capture_send(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.notifier.send_safe",
        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
            sent.append((chat_id, text)) or True,
    )
    return sent


def test_announce_owner_broadcasts_to_active(fresh_db, owner_set, monkeypatch):
    sent = _capture_send(monkeypatch)
    conn = subscribers._conn()
    subscribers.set_status(conn, "100", "active", name="alice")
    subscribers.set_status(conn, "200", "active", name="bob")
    conn.commit()

    st = subscribers.apply_events(
        [subscribers.SubEvent(1, "1", "owner", "announce", "서버 업데이트 완료")]
    )
    assert st["announced"] == 1
    bodies = {cid: txt for cid, txt in sent}
    assert "📢 공지" in bodies["100"] and "서버 업데이트 완료" in bodies["100"]
    assert "📢 공지" in bodies["200"]                       # active 전원 수신
    assert any(cid == "1" and "공지 전송" in txt for cid, txt in sent)   # 소유자에 결과 보고


def test_announce_non_owner_ignored(fresh_db, owner_set, monkeypatch):
    sent = _capture_send(monkeypatch)
    conn = subscribers._conn()
    subscribers.set_status(conn, "100", "active", name="alice")
    conn.commit()

    st = subscribers.apply_events(
        [subscribers.SubEvent(1, "999", "mallory", "announce", "해킹 공지")]
    )
    assert st["ignored_admin"] == 1 and st["announced"] == 0
    assert sent == []                                       # 비소유자 → 아무에게도 전송 안 됨


def test_announce_empty_body_shows_usage(fresh_db, owner_set, monkeypatch):
    sent = _capture_send(monkeypatch)
    conn = subscribers._conn()
    subscribers.set_status(conn, "100", "active", name="alice")
    conn.commit()

    st = subscribers.apply_events([subscribers.SubEvent(1, "1", "owner", "announce", None)])
    assert st["announced"] == 0
    assert any(cid == "1" and "사용법" in txt for cid, txt in sent)   # 소유자에 사용법
    assert all(cid != "100" for cid, _ in sent)                       # 구독자 전송 없음


# ---------------- 인라인 승인/거절 버튼 (Phase 11b) ----------------


def test_request_owner_notification_carries_inline_buttons(fresh_db, owner_set, monkeypatch):
    """가입 요청 시 소유자 알림에 [✅ 승인][❌ 거절] 인라인 키보드가 붙는지 (payload 검증)."""
    sent: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(
        "src.notifier.send_safe",
        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
            sent.append((chat_id, text, reply_markup)) or True,
    )
    subscribers.apply_events([subscribers.SubEvent(1, "100", "alice", "request")],
                             interactive_buttons=True)

    owner_msgs = [(txt, kb) for cid, txt, kb in sent if cid == "1"]
    assert len(owner_msgs) == 1
    text, kb = owner_msgs[0]
    assert "가입 요청" in text and "/approve 100" in text           # 텍스트 명령도 계속 안내
    buttons = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["approve:100", "deny:100"]
    assert buttons[0]["text"] == "✅ 승인" and buttons[1]["text"] == "❌ 거절"
    # 신청자에게 가는 접수 안내에는 버튼 없음
    applicant = [(txt, kb) for cid, txt, kb in sent if cid == "100"]
    assert applicant and applicant[0][1] is None


def test_cron_path_does_not_attach_inline_buttons(fresh_db, owner_set, monkeypatch):
    """기본값(interactive_buttons=False) = cron 경로 — callback_query 처리자가 없어
    버튼을 붙이면 소유자의 탭이 조용히 유실되므로 버튼 미부착이어야 함."""
    sent: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(
        "src.notifier.send_safe",
        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
            sent.append((chat_id, text, reply_markup)) or True,
    )
    subscribers.apply_events([subscribers.SubEvent(1, "200", "bob", "request")])

    owner_msgs = [(txt, kb) for cid, txt, kb in sent if cid == "1"]
    assert len(owner_msgs) == 1
    assert owner_msgs[0][1] is None                       # 버튼 없음
    assert "/approve 200" in owner_msgs[0][0]             # 텍스트 명령 안내는 유지


def test_decide_request_shared_core(fresh_db, monkeypatch):
    """decide_request — 텍스트 명령과 버튼이 공유하는 상태 변경 + 신청자 알림 코어."""
    sent = _capture_send(monkeypatch)
    assert subscribers.decide_request("100", approve=True) == "missing"   # 기록 없음 → 변경 없음
    assert subscribers.subscriber_status("100") is None

    conn = subscribers._conn()
    subscribers.upsert_request(conn, "100", "alice")
    conn.commit()

    assert subscribers.decide_request("100", approve=True) == "approved"
    assert subscribers.subscriber_status("100") == "active"
    assert any(cid == "100" and "승인되었습니다" in txt for cid, txt in sent)

    assert subscribers.decide_request("100", approve=False) == "denied"
    assert subscribers.subscriber_status("100") == "inactive"
    assert any(cid == "100" and "거절" in txt for cid, txt in sent)


def test_decide_request_silent_mode(fresh_db, monkeypatch):
    sent = _capture_send(monkeypatch)
    conn = subscribers._conn()
    subscribers.upsert_request(conn, "100", "alice")
    conn.commit()
    assert subscribers.decide_request("100", approve=True, send_notifications=False) == "approved"
    assert sent == []                                             # 알림 억제 모드
