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


# ---------------- READY 순서 / 강등 자진 재시작 (WP-OPS-CORE) ----------------


def _record_startup(bot, monkeypatch, order: list[str]):
    real_ensure = bot.subscribers.ensure_owner
    real_offset = bot.subscribers.get_updates_offset
    monkeypatch.setattr(bot.subscribers, "ensure_owner",
                        lambda *a, **k: (order.append("ensure_owner"), real_ensure(*a, **k))[1])
    monkeypatch.setattr(bot.subscribers, "get_updates_offset",
                        lambda *a, **k: (order.append("get_updates_offset"), real_offset(*a, **k))[1])
    monkeypatch.setattr(bot, "_sd_notify", lambda state: order.append(state))


def test_ready_sent_after_schema_init_and_offset(fresh_db, owner_set, monkeypatch):
    """READY=1 이 스키마 초기화·offset 읽기 뒤 — 초기화 크래시가 '기동 성공' 으로 안 찍히게."""
    bot = _load_bot()
    order: list[str] = []
    _record_startup(bot, monkeypatch, order)
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([]))
    assert bot.main() == 0
    assert order.index("ensure_owner") < order.index("READY=1")
    assert order.index("get_updates_offset") < order.index("READY=1")
    assert order.index("READY=1") < order.index("WATCHDOG=1")


def test_ready_not_sent_when_schema_init_crashes(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    order: list[str] = []
    monkeypatch.setattr(bot, "_sd_notify", lambda state: order.append(state))

    def boom(*a, **k):
        raise RuntimeError("schema init 실패")

    monkeypatch.setattr(bot.subscribers, "ensure_owner", boom)
    with pytest.raises(RuntimeError):
        bot.main()
    assert "READY=1" not in order


def test_degraded_start_exits_3_after_deadline(fresh_db, owner_set, monkeypatch):
    """init_sync=timeout(오프라인 강등)으로 떴으면 deadline 뒤 SystemExit(3) → systemd 재프로브."""
    bot = _load_bot()
    store = storage_mod.get_storage()
    store.init_sync = "timeout"                       # 강등 상태 모사 (프로브 타임아웃)
    monkeypatch.setattr(bot, "DEGRADED_RESTART_SEC", 0)
    polled: list[int] = []
    monkeypatch.setattr(bot, "get_updates",
                        lambda offset=None, timeout=0: polled.append(1) or [])
    with pytest.raises(SystemExit) as exc:
        bot.main()
    assert exc.value.code == 3
    assert polled == []                               # deadline 이 지났으면 폴링 전에 종료


def test_healthy_start_has_no_restart_deadline(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    assert storage_mod.get_storage().init_sync == "local"
    monkeypatch.setattr(bot, "DEGRADED_RESTART_SEC", 0)
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([]))
    assert bot.main() == 0                            # 강등 아님 → 자진 종료 없음


# ---------------- WP-POLISH: 강등 중 구독 이벤트 처리 경고 ----------------


def _msg_update(uid: int, chat_id: int, text: str) -> dict:
    return {"update_id": uid, "message": {"chat": {"id": chat_id, "username": "u"}, "text": text}}


def test_degraded_loop_warns_on_subscription_events(fresh_db, owner_set, monkeypatch, caplog):
    bot = _load_bot()
    storage_mod.get_storage().init_sync = "timeout"
    monkeypatch.setattr(bot, "DEGRADED_RESTART_SEC", 3600)      # 이번 테스트에선 자진 종료 안 함
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([_msg_update(5, 100, "/start")]))
    monkeypatch.setattr("src.notifier.send_safe", lambda *a, **k: True)
    monkeypatch.setattr(bot, "send_safe", lambda *a, **k: True)
    with caplog.at_level("WARNING"):
        assert bot.main() == 0
    msgs = [r.getMessage() for r in caplog.records if "강등(오프라인) 상태에서" in r.getMessage()]
    assert len(msgs) == 1 and "구독/승인 1건" in msgs[0]


def test_degraded_loop_no_warning_for_plain_queries(fresh_db, owner_set, monkeypatch, caplog):
    """조회/도움말 같은 구독과 무관한 메시지만이면 경고하지 않는다."""
    bot = _load_bot()
    storage_mod.get_storage().init_sync = "timeout"
    monkeypatch.setattr(bot, "DEGRADED_RESTART_SEC", 3600)
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([_msg_update(6, 100, "안녕")]))
    monkeypatch.setattr(bot, "send_safe", lambda *a, **k: True)
    with caplog.at_level("WARNING"):
        assert bot.main() == 0
    assert not any("강등(오프라인) 상태에서" in r.getMessage() for r in caplog.records)


def test_healthy_loop_never_warns_about_degraded_events(fresh_db, owner_set, monkeypatch, caplog):
    bot = _load_bot()
    assert storage_mod.get_storage().init_sync == "local"
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([_msg_update(7, 100, "/start")]))
    monkeypatch.setattr(bot, "send_safe", lambda *a, **k: True)
    monkeypatch.setattr("src.notifier.send_safe", lambda *a, **k: True)
    with caplog.at_level("WARNING"):
        assert bot.main() == 0
    assert not any("강등(오프라인) 상태에서" in r.getMessage() for r in caplog.records)


def test_run_docstring_documents_degraded_write_limit():
    bot = _load_bot()
    assert "강등" in bot.run.__doc__ and "offline.db" in bot.run.__doc__


# ---------------- S2FIX-SURFACE S1: /ledger 평문 전송 ----------------


def _sent_parse_modes(bot, monkeypatch, texts):
    """소유자(chat 1)가 texts 를 보낸 한 배치를 돌리고 (reply, parse_mode) 목록을 반환."""
    sent: list[tuple[str, str | None]] = []
    monkeypatch.setattr(bot, "send_safe",
                        lambda text, chat_id=None, parse_mode="Markdown", reply_markup=None:
                            sent.append((text, parse_mode)) or True)
    batch = [_msg_update(10 + i, 1, t) for i, t in enumerate(texts)]
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop(batch))
    monkeypatch.setattr(bot.bot_commands, "respond",
                        lambda text, chat_id, limiter=None: f"reply:{text}")
    assert bot.main() == 0
    return sent


def test_ledger_and_news_sent_plaintext_others_markdown(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    sent = _sent_parse_modes(bot, monkeypatch,
                             ["/ledger us", "/news AAPL", "/scan us", "/stock AAPL"])
    modes = {text: pm for text, pm in sent}
    assert modes["reply:/ledger us"] is None            # 원장 본문의 snake_case 가 Markdown 에 깨지지 않게
    assert modes["reply:/news AAPL"] is None
    assert modes["reply:/scan us"] == "Markdown"
    assert modes["reply:/stock AAPL"] == "Markdown"


# ---------------- RVOPS-06: 조회 명령 전 pull (rate-limit 300초) ----------------


def _run_batches_with_clock(bot, monkeypatch, batches, clock):
    """batches: 배치 목록(각각 updates). clock: bot._now 가 차례로 돌려줄 값 (마지막 값은 반복)."""
    events: list[str] = []
    store = storage_mod.get_storage()
    monkeypatch.setattr(type(store), "sync", lambda self: events.append("sync") or "ok")
    pending = list(batches)

    def fake_get_updates(offset=None, timeout=0):
        if pending:
            return pending.pop(0)
        raise KeyboardInterrupt

    monkeypatch.setattr(bot, "get_updates", fake_get_updates)
    times = list(clock)
    monkeypatch.setattr(bot, "_now", lambda: times.pop(0) if len(times) > 1 else times[0])
    monkeypatch.setattr(bot, "send_safe", lambda *a, **k: events.append("send") or True)
    monkeypatch.setattr(bot.bot_commands, "respond",
                        lambda text, chat_id, limiter=None: events.append("respond") or "ok")
    assert bot.main() == 0
    return events


def test_query_command_pulls_before_responding_when_stale(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    # clock: 기동(last_sync=0) → 배치1 pull 판정(1000s 경과) → 배치 끝 sync 시각
    events = _run_batches_with_clock(bot, monkeypatch, [[_msg_update(1, 1, "/scan us")]],
                                     [0.0, 1000.0, 1000.0])
    assert events[0] == "sync"                         # 처리 전에 pull
    assert events.index("sync") < events.index("respond")
    assert events.count("sync") == 2                   # 사전 pull 1 + 기존 배치 끝 sync 1


def test_query_pull_is_rate_limited_to_300s(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    # 기동 0 → 100초 뒤 조회: 300초 미만이라 사전 pull 없음 (배치 끝 sync 만)
    events = _run_batches_with_clock(bot, monkeypatch, [[_msg_update(1, 1, "/stock AAPL")]],
                                     [0.0, 100.0, 100.0])
    assert events.index("respond") < events.index("sync")
    assert events.count("sync") == 1


def test_query_pull_rate_limit_spans_batches(fresh_db, owner_set, monkeypatch):
    """배치 끝 sync 도 타이머를 갱신 — 300초 안의 두 번째 조회 배치는 사전 pull 없음, 넘으면 다시."""
    bot = _load_bot()
    two = [[_msg_update(1, 1, "/scan us")], [_msg_update(2, 1, "/stock AAPL")]]
    events = _run_batches_with_clock(bot, monkeypatch, two, [0.0, 1000.0, 1000.0, 1100.0, 1100.0])
    assert events.count("sync") == 3                   # 사전 pull → 배치 끝 → (2번째: 배치 끝만)
    bot = _load_bot()
    two = [[_msg_update(1, 1, "/scan us")], [_msg_update(2, 1, "/stock AAPL")]]
    events = _run_batches_with_clock(bot, monkeypatch, two, [0.0, 1000.0, 1000.0, 1400.0, 1400.0])
    assert events.count("sync") == 4                   # 2번째 배치도 400초 경과 → 사전 pull


def test_query_pull_skipped_while_degraded_at_start(fresh_db, owner_set, monkeypatch):
    """초기 pull 타임아웃 강등(오프라인 파일)으로 떴으면 사전 pull 을 하지 않는다 — 재시작이 복구 수단."""
    bot = _load_bot()
    monkeypatch.setattr(storage_mod.get_storage(), "init_sync", "timeout")
    events = _run_batches_with_clock(bot, monkeypatch, [[_msg_update(1, 1, "/scan us")]],
                                     [0.0, 1000.0, 1000.0])
    assert events.count("sync") == 1                   # 배치 끝 sync 만
    assert events.index("respond") < events.index("sync")


def test_non_query_commands_do_not_trigger_pull(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    events = _run_batches_with_clock(bot, monkeypatch, [[_msg_update(1, 1, "/help")]],
                                     [0.0, 9999.0, 9999.0])
    assert events.count("sync") == 1                   # 배치 끝 sync 만 (사전 pull 없음)
    assert events.index("respond") < events.index("sync")


def test_ledger_command_triggers_pull(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    events = _run_batches_with_clock(bot, monkeypatch, [[_msg_update(1, 1, "/ledger us")]],
                                     [0.0, 500.0, 500.0])
    assert events[0] == "sync" and events.count("sync") == 2


def test_query_pull_failure_does_not_kill_loop(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    store = storage_mod.get_storage()
    calls: list[str] = []

    def flaky(self):
        calls.append("sync")
        if len(calls) == 1:
            raise RuntimeError("sync 장애")
        return "ok"

    monkeypatch.setattr(type(store), "sync", flaky)
    sent: list[str] = []
    monkeypatch.setattr(bot, "get_updates", _one_batch_then_stop([_msg_update(1, 1, "/scan us")]))
    monkeypatch.setattr(bot, "send_safe", lambda *a, **k: sent.append("send") or True)
    monkeypatch.setattr(bot.bot_commands, "respond", lambda text, chat_id, limiter=None: "ok")
    times = iter([0.0, 1000.0] + [1000.0] * 5)
    monkeypatch.setattr(bot, "_now", lambda: next(times))
    assert bot.main() == 0                             # 사전 pull 예외에도 루프 생존
    assert sent == ["send"]                            # 조회 응답은 그대로 나감


# ---------------- 유휴 pull (2026-10-26 /stock 4분 지연) ----------------
# 메시지가 없는 폴링 주기에도 IDLE_PULL_SEC(900초)마다 pull — 레플리카 델타를 늘 작게 유지.


def test_idle_pull_after_900s_without_messages(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    events = _run_batches_with_clock(bot, monkeypatch, [[]], [0.0, 1000.0, 1000.0])
    assert events == ["sync"]


def test_no_idle_pull_before_900s(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    events = _run_batches_with_clock(bot, monkeypatch, [[], []], [0.0, 500.0, 899.0])
    assert events == []


def test_idle_pull_skipped_while_degraded_at_start(fresh_db, owner_set, monkeypatch):
    bot = _load_bot()
    monkeypatch.setattr(storage_mod.get_storage(), "init_sync", "timeout")
    events = _run_batches_with_clock(bot, monkeypatch, [[]], [0.0, 5000.0, 5000.0])
    assert events == []


def test_idle_pull_resets_query_pull_timer(fresh_db, owner_set, monkeypatch):
    """유휴 pull 직후(100초) 조회 명령은 사전 pull 없이 바로 응답 — 배치 끝 sync 만."""
    bot = _load_bot()
    batches = [[], [_msg_update(1, 1, "/stock AAPL")]]
    events = _run_batches_with_clock(bot, monkeypatch, batches, [0.0, 1000.0, 1000.0, 1100.0, 1100.0])
    assert events == ["sync", "respond", "send", "sync"]   # 유휴 pull → 응답 → 배치 끝 sync


def test_slow_pull_logged_at_info(fresh_db, owner_set, monkeypatch, caplog):
    import logging

    bot = _load_bot()
    store = storage_mod.get_storage()
    ticks = iter([0.0, 37.5])
    monkeypatch.setattr(bot.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(type(store), "sync", lambda self: "ok")
    with caplog.at_level(logging.DEBUG):
        bot._timed_sync(store, "조회 전")
    rec = [r for r in caplog.records if "조회 전 pull" in r.getMessage()]
    assert rec and rec[0].levelno == logging.INFO and "37.5s (ok)" in rec[0].getMessage()
