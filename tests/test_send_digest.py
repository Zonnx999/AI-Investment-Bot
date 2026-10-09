"""send_daily_digest / scripts/send_digest.py — 커밋 순서·종료코드·소유자 DM·dry-run 무쓰기.

외부 경계(텔레그램·yfinance·FRED)는 전부 스텁, DB 는 tmp sqlite3. 네트워크 없음.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import pytest

import scripts.send_digest as send_script
import src.storage as storage_mod
from src import digest as dg
from src.config import settings
from src.health import HealthReport
from src.signals import SignalReport
from tests import test_digest as T

NOW = datetime(2026, 10, 8, 8, 30, tzinfo=dg.KST)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    import src.ledger as ledger_mod

    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "send.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    monkeypatch.setattr(ledger_mod, "_schema_ready_store", None)
    yield tmp_path / "send.db"
    monkeypatch.setattr(storage_mod, "_storage", None)


@pytest.fixture
def owner(monkeypatch):
    backup = (settings.telegram_chat_id, settings.telegram_bot_token)
    object.__setattr__(settings, "telegram_chat_id", "1")
    object.__setattr__(settings, "telegram_bot_token", "123:fake")
    yield "1"
    object.__setattr__(settings, "telegram_chat_id", backup[0])
    object.__setattr__(settings, "telegram_bot_token", backup[1])


def _bundle(market="KR", *, hold=False, record_ledger=True, picks=None, report=None, silent=False):
    rep = report if report is not None else SignalReport(
        generated_at="x", regime_label="🟢", factors=[], candidates=[], alerts=[],
        pending_state={"v": 2, "regime": {}, "dd": {}, "vol": {}, "credit": {}, "fx": {}, "shock": {},
                       "pick_dd10": {}})
    health = HealthReport(market=market, started_at=NOW, storage="ok", universe_as_of="2026-10-05",
                          regime_ok=3, regime_total=3)
    return dg.DigestBundle(
        text="*🇰🇷 본문*", market=market, picks=list(T.KR_ROWS if picks is None else picks),
        score_asof="2026-10-05", report=rep, health=health, degraded=False, silent=silent,
        hold=hold, hold_reason="KR 후보 0개" if hold else None, record_ledger=record_ledger,
        verdict=dg.Verdict("없음", ["후보 변화 없음"]), diff=dg.ListDiff(None, True),
    )


class _Calls:
    def __init__(self):
        self.order: list[str] = []
        self.sent: list[tuple[str, dict]] = []
        self.owner_dm: list[str] = []


def _wire_send(monkeypatch, bundle, *, send_ok=lambda chat_id: True, commit_ok=True,
               ledger_ok=True, recipients=(("1", "owner"), ("2", "friend"))):
    import src.ledger as ledger_mod
    import src.notifier as notifier_mod
    import src.signals as sig
    import src.subscribers as subs

    calls = _Calls()
    monkeypatch.setattr(dg, "assemble_daily_digest", lambda market, now=None, top_n=6: bundle)
    monkeypatch.setattr(subs, "ensure_owner", lambda: None)
    monkeypatch.setattr(subs, "active_subscribers", lambda: list(recipients))

    def fake_send(text, chat_id=None, parse_mode="Markdown", reply_markup=None,
                  disable_notification=False):
        if parse_mode is None and chat_id is None:
            calls.order.append("owner_dm")
            calls.owner_dm.append(text)
            return True
        calls.order.append(f"send:{chat_id}")
        calls.sent.append((text, {"chat_id": chat_id, "parse_mode": parse_mode,
                                  "disable_notification": disable_notification}))
        return send_ok(chat_id)

    monkeypatch.setattr(notifier_mod, "send_safe", fake_send)

    def fake_commit(report):
        calls.order.append("commit")
        return commit_ok

    monkeypatch.setattr(sig, "commit_signal_state", fake_commit)

    def fake_record(market, sent_date, picks, **kw):
        calls.order.append("ledger")
        calls.record_kw = dict(kw, market=market, sent_date=sent_date, n=len(picks))
        return ledger_ok

    monkeypatch.setattr(ledger_mod, "record_send", fake_record)
    store = storage_mod.get_storage()
    orig_put, orig_sync = store.put_state, store.sync

    def spy_put(ns, key, obj):
        calls.order.append(f"put:{ns}/{key}")
        return orig_put(ns, key, obj)

    def spy_sync():
        calls.order.append("sync")
        return orig_sync()

    monkeypatch.setattr(store, "put_state", spy_put)
    monkeypatch.setattr(store, "sync", spy_sync)
    return calls


# ----------------------------------------------------------------------
# 커밋 순서
# ----------------------------------------------------------------------


def test_send_order_broadcast_then_commit_ledger_health_sync(fresh_db, owner, monkeypatch):
    calls = _wire_send(monkeypatch, _bundle())
    result = dg.send_daily_digest("kr", now=NOW)
    assert result == {"sent": 2, "failed": 0, "recipients": 2, "exit_code": dg.EXIT_OK}
    # 정상 런: 헬스 signature([]) 가 직전(없음=[]) 과 같고 실패 0 → 소유자 DM 없음 (다이제스트가 하트비트)
    assert calls.order == ["send:1", "send:2", "commit", "ledger", "put:health/digest:KR", "sync"]
    assert calls.owner_dm == []
    assert calls.record_kw["sent"] == 2 and calls.record_kw["sent_date"] == "2026-10-08"
    assert calls.record_kw["rule_version"] and calls.record_kw["n"] == 6
    assert all(kw["disable_notification"] is False for _, kw in calls.sent)
    saved = storage_mod.get_storage().get_state("health", "digest:KR")
    assert saved["sent"] == 2 and saved["signature"] == []


def test_send_zero_success_skips_commit_ledger_health(fresh_db, owner, monkeypatch):
    calls = _wire_send(monkeypatch, _bundle(), send_ok=lambda cid: False)
    result = dg.send_daily_digest("kr", now=NOW)
    assert result["exit_code"] == dg.EXIT_ALL_FAIL and result["sent"] == 0
    assert "commit" not in calls.order and "ledger" not in calls.order
    assert not any(o.startswith("put:") for o in calls.order) and "sync" not in calls.order
    assert calls.owner_dm and "실패 2" in calls.owner_dm[0]
    assert storage_mod.get_storage().get_state("health", "digest:KR") is None


def test_send_partial_failure_exit_10_and_commits(fresh_db, owner, monkeypatch):
    calls = _wire_send(monkeypatch, _bundle(), send_ok=lambda cid: cid == "1")
    result = dg.send_daily_digest("kr", now=NOW)
    assert result == {"sent": 1, "failed": 1, "recipients": 2, "exit_code": dg.EXIT_PARTIAL_FAIL}
    assert "commit" in calls.order and "ledger" in calls.order and calls.owner_dm


def test_send_no_recipients_exit_12_without_assembling(fresh_db, owner, monkeypatch):
    calls = _wire_send(monkeypatch, _bundle(), recipients=())
    monkeypatch.setattr(dg, "assemble_daily_digest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("조립 호출됨")))
    assert dg.send_daily_digest("kr", now=NOW) == {"sent": 0, "failed": 0, "recipients": 0,
                                                   "exit_code": dg.EXIT_NO_RECIPIENTS}
    assert calls.order == []


def test_send_hold_sends_owner_dm_only_exit_20_then_21(fresh_db, owner, monkeypatch):
    calls = _wire_send(monkeypatch, _bundle(hold=True))
    result = dg.send_daily_digest("kr", now=NOW)
    assert result["exit_code"] == dg.EXIT_HOLD_NOTIFIED and result["sent"] == 0
    assert calls.order == ["owner_dm"] and calls.owner_dm[0].startswith("[quant-bot 발송 보류] KR")
    # 소유자 DM 도 실패하면 21
    monkeypatch.setattr(dg, "_owner_dm", lambda text: False)
    assert dg.send_daily_digest("kr", now=NOW)["exit_code"] == dg.EXIT_HOLD_DM_FAILED


def test_send_ledger_not_recorded_on_hold_candidates(fresh_db, owner, monkeypatch):
    b = _bundle(record_ledger=False)
    b.health.universe_as_of = "2026-09-20"          # 점수 노후(보류) → 헬스 문제 → 소유자 DM
    calls = _wire_send(monkeypatch, b)
    dg.send_daily_digest("kr", now=NOW)
    assert "ledger" not in calls.order and "commit" in calls.order
    assert any("원장: 미기록" in t for t in calls.owner_dm)


def test_send_ledger_failure_and_md_fallback_go_to_owner_dm(fresh_db, owner, monkeypatch):
    import src.notifier as notifier_mod

    calls = _wire_send(monkeypatch, _bundle(), ledger_ok=False)
    monkeypatch.setattr(notifier_mod, "fallback_count", lambda: 2)
    dg.send_daily_digest("kr", now=NOW)
    dm = calls.owner_dm[-1]
    assert "선택 기능 실패: 원장 기록" in dm and "Markdown 평문 폴백 2회" in dm
    assert "원장: 기록 실패" in dm and "판정: 없음" in dm
    # D4: 폴백은 put_state 전에 고정 키로 반영 — 저장 signature == DM 판정 signature
    saved = storage_mod.get_storage().get_state("health", "digest:KR")
    assert saved["signature"] == ["optional:Markdown 평문 폴백", "optional:원장 기록"]
    assert "Markdown 평문 폴백 2회" in saved["notes"] and "선택 기능 실패: Markdown 평문 폴백" in dm


def test_send_no_fallback_line_when_zero(fresh_db, owner, monkeypatch):
    """D4(a): fb=0 이면 '평문 폴백' 문구가 DM·signature 어디에도 없다."""
    calls = _wire_send(monkeypatch, _bundle(), ledger_ok=False)      # 원장 실패 → DM 발생
    dg.send_daily_digest("kr", now=NOW)
    assert calls.owner_dm and "평문 폴백" not in calls.owner_dm[-1]
    assert storage_mod.get_storage().get_state("health", "digest:KR")["signature"] == ["optional:원장 기록"]


def test_send_owner_ledger_mode_dms_every_run(fresh_db, owner, monkeypatch):
    """D7: QUANT_BOT_LEDGER_LINE=owner — signature 가 같아도 매 런 소유자 DM (성과 줄 전달 경로)."""
    object.__setattr__(settings, "ledger_line", "owner")
    try:
        b = _bundle()
        b.health.notes.append("성과: 성적: 07-13 이후 이 리스트 보유 -2.4%")
        calls = _wire_send(monkeypatch, b)
        dg.send_daily_digest("kr", now=NOW)
        assert len(calls.owner_dm) == 1 and "성과: 성적: 07-13 이후" in calls.owner_dm[0]
        calls2 = _wire_send(monkeypatch, _bundle())
        dg.send_daily_digest("kr", now=NOW)
        assert len(calls2.owner_dm) == 1                 # 정상·변화 없음이어도 보낸다
    finally:
        object.__setattr__(settings, "ledger_line", "on")
    calls3 = _wire_send(monkeypatch, _bundle())
    dg.send_daily_digest("kr", now=NOW)
    assert calls3.owner_dm == []                         # on 모드: 하트비트는 다이제스트 자체


def test_send_owner_dm_once_per_signature_change(fresh_db, owner, monkeypatch):
    stale = _bundle()
    stale.health.universe_as_of = "2026-09-20"
    calls = _wire_send(monkeypatch, stale)
    dg.send_daily_digest("kr", now=NOW)
    assert len(calls.owner_dm) == 1 and "[quant-bot 상태] KR" in calls.owner_dm[0]
    stale2 = _bundle()
    stale2.health.universe_as_of = "2026-09-20"
    calls2 = _wire_send(monkeypatch, stale2)
    dg.send_daily_digest("kr", now=NOW)
    assert calls2.owner_dm == []                 # 같은 signature · 실패 0 → 중복 DM 억제
    calls3 = _wire_send(monkeypatch, _bundle())  # 문제 해소 → signature 변화 → DM 1통
    dg.send_daily_digest("kr", now=NOW)
    assert len(calls3.owner_dm) == 1


def test_send_commit_false_marks_state_write_problem(fresh_db, owner, monkeypatch):
    calls = _wire_send(monkeypatch, _bundle(), commit_ok=False)
    dg.send_daily_digest("kr", now=NOW)
    assert any("상태 저장: 실패" in t for t in calls.owner_dm)


def test_send_silent_bundle_sets_disable_notification(fresh_db, owner, monkeypatch):
    calls = _wire_send(monkeypatch, _bundle(silent=True))
    dg.send_daily_digest("kr", now=NOW)
    assert all(kw["disable_notification"] is True for _, kw in calls.sent)


def test_send_llm_only_when_enabled(fresh_db, owner, monkeypatch):
    import src.llm as llm_mod

    called = []
    monkeypatch.setattr(llm_mod, "summarize", lambda text: called.append(text) or "요약")
    monkeypatch.delenv("QUANT_BOT_LLM", raising=False)
    calls = _wire_send(monkeypatch, _bundle())
    dg.send_daily_digest("kr", now=NOW)
    assert called == [] and calls.sent[0][0] == "*🇰🇷 본문*"
    monkeypatch.setenv("QUANT_BOT_LLM", "1")
    calls = _wire_send(monkeypatch, _bundle())
    dg.send_daily_digest("kr", now=NOW)
    assert called and calls.sent[0][0].startswith("🧠 요약\n\n")


# ----------------------------------------------------------------------
# scripts/send_digest.py — 종료코드 매핑 + dry-run 무쓰기
# ----------------------------------------------------------------------


@pytest.mark.parametrize("code", [dg.EXIT_OK, dg.EXIT_PARTIAL_FAIL, dg.EXIT_ALL_FAIL,
                                  dg.EXIT_NO_RECIPIENTS, dg.EXIT_HOLD_NOTIFIED, dg.EXIT_HOLD_DM_FAILED])
def test_script_returns_exit_code_from_result(monkeypatch, capsys, code):
    monkeypatch.setattr(send_script, "send_daily_digest",
                        lambda **k: {"sent": 1, "failed": 0, "recipients": 1, "exit_code": code})
    assert send_script.main(["--market", "kr", "--no-sync"]) == code
    assert capsys.readouterr().out.strip()


def test_script_config_error_exit_30(monkeypatch, capsys):
    from src.exceptions import MissingApiKeyError

    def boom(**k):
        raise MissingApiKeyError(key_name="TELEGRAM_BOT_TOKEN")

    monkeypatch.setattr(send_script, "send_daily_digest", boom)
    assert send_script.main(["--market", "us", "--no-sync"]) == dg.EXIT_CONFIG_MISSING
    assert "설정 누락" in capsys.readouterr().out


def test_script_rejects_removed_flags():
    with pytest.raises(SystemExit) as exc:
        send_script.main(["--no-llm"])
    assert exc.value.code == 2


def _db_fingerprint(path) -> str:
    """state + 원장 3테이블의 내용 해시 (테이블이 없으면 그 사실도 포함)."""
    con = sqlite3.connect(path)
    h = hashlib.sha256()
    try:
        for table in ("state", "signal_ledger", "ledger_perf", "pick_outcomes"):
            try:
                rows = con.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                h.update(f"{table}:{rows!r}".encode())
            except sqlite3.OperationalError:
                h.update(f"{table}:missing".encode())
    finally:
        con.close()
    return h.hexdigest()


def test_script_dry_run_writes_nothing(fresh_db, monkeypatch, capsys):
    """--dry-run 은 assemble 만 호출 — state·원장 해시 불변, 텔레그램·커밋·원장 호출 0."""
    import src.ledger as ledger_mod
    import src.notifier as notifier_mod
    import src.signals as sig

    store = storage_mod.get_storage()
    store.put_state("signals", "global:v2", {"v": 2, "regime": {"confirmed": "🟢"}, "dd": {}})
    before = _db_fingerprint(fresh_db)
    T._wire(monkeypatch, picks=T.KR_ROWS, prev=T._prev(*[(r.symbol, r.total_score) for r in T.KR_ROWS]),
            starts={r.symbol: "2026-07-13" for r in T.KR_ROWS}, summary=T.SUMMARY_KR)
    monkeypatch.setattr(notifier_mod, "send_safe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run 에서 전송")))
    monkeypatch.setattr(sig, "commit_signal_state",
                        lambda r: (_ for _ in ()).throw(AssertionError("dry-run 에서 커밋")))
    monkeypatch.setattr(ledger_mod, "record_send",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run 에서 원장")))
    assert send_script.main(["--market", "kr", "--dry-run"]) == dg.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("*🇰🇷 ") and "오늘 할 일:" in out and dg.DISCLAIMER in out
    assert _db_fingerprint(fresh_db) == before
    # 두 번 돌려도 동일
    assert send_script.main(["--market", "kr", "--dry-run"]) == dg.EXIT_OK
    assert _db_fingerprint(fresh_db) == before
