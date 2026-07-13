"""scripts/bot.py — 폴링 루프의 callback_query 라우팅 + poison 가드 (tmp DB, 네트워크 없음)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import src.storage as storage_mod
from src import subscribers
from src.config import settings

_ROOT = Path(__file__).resolve().parents[1]


def _load_bot():
    """scripts/ 는 패키지가 아니므로 파일 경로로 직접 로드 (테스트 전용 모듈명)."""
    spec = importlib.util.spec_from_file_location("bot_script_under_test",
                                                  _ROOT / "scripts" / "bot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "loop.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


@pytest.fixture
def owner_set():
    backup = settings.telegram_chat_id
    object.__setattr__(settings, "telegram_chat_id", "1")
    yield "1"
    object.__setattr__(settings, "telegram_chat_id", backup)


def _one_batch_then_stop(batch):
    """1회차엔 batch 반환, 2회차엔 KeyboardInterrupt (루프 정상 종료 유도)."""
    batches = [batch]

    def fake_get_updates(offset=None, timeout=0):
        if batches:
            return batches.pop(0)
        raise KeyboardInterrupt

    return fake_get_updates


def test_poll_loop_routes_callback_and_survives_handler_error(fresh_db, owner_set, monkeypatch):
    """callback_query 가 handle_callback 으로 라우팅되고, 핸들러 예외에도 루프가 살아
    offset 이 전진하는지 (poison-callback 가드)."""
    bot = _load_bot()
    cq = {"id": "c1", "from": {"id": 1}, "data": "approve:100"}
    update = {"update_id": 7, "callback_query": cq}
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([update]))

    calls: list[tuple[dict, str | None]] = []

    def boom(callback_query, owner):
        calls.append((callback_query, owner))
        raise RuntimeError("poison callback")

    monkeypatch.setattr(bot.bot_commands, "handle_callback", boom)

    assert bot.main() == 0                              # 크래시 루프 없이 정상 종료
    assert calls == [(cq, "1")]                         # 소유자 id 와 함께 라우팅
    assert subscribers.get_updates_offset() == 8        # 예외에도 offset 전진 (재수신 방지)


def test_poll_loop_callback_approves_end_to_end(fresh_db, owner_set, monkeypatch):
    """루프 → handle_callback → decide_request 실경로: pending 이 active 로, 응답/편집 호출."""
    bot = _load_bot()
    conn = subscribers._conn()
    subscribers.upsert_request(conn, "100", "alice")
    conn.commit()

    answers, edits, sent = [], [], []
    monkeypatch.setattr(
        "src.notifier.answer_callback_safe",
        lambda cq_id, text=None, show_alert=False: answers.append((cq_id, text)) or True,
    )
    monkeypatch.setattr(
        "src.notifier.edit_message_safe",
        lambda chat_id, message_id, text, parse_mode=None, reply_markup=None:
            edits.append((chat_id, message_id, text)) or True,
    )
    monkeypatch.setattr(
        "src.notifier.send_safe",
        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
            sent.append((chat_id, text)) or True,
    )

    update = {"update_id": 7, "callback_query": {
        "id": "c1", "from": {"id": 1}, "data": "approve:100",
        "message": {"message_id": 5, "chat": {"id": 1}, "text": "🔔 가입 요청: alice"}}}
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([update]))

    assert bot.main() == 0
    assert subscribers.subscriber_status("100") == "active"
    assert ("c1", "승인 완료") in answers
    assert edits and edits[0][:2] == ("1", 5) and "✅ 승인됨" in edits[0][2]
    assert any(cid == "100" and "승인되었습니다" in txt for cid, txt in sent)
    assert subscribers.get_updates_offset() == 8
