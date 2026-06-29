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
        lambda text, chat_id=None, parse_mode="Markdown": sent.append((chat_id, text)) or True,
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
                        lambda text, chat_id=None, parse_mode="Markdown": True)
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
        lambda text, chat_id=None, parse_mode="Markdown": sent.append((chat_id, text)) or True,
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
