"""src/ledger.py — 신호 원장 (기록·채점·스냅샷·백필). tmp DB + 합성 가격, 네트워크 없음."""

from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import src.storage as storage_mod
from src import data_fetcher, ledger
from src.exceptions import DataFetchError, DataValidationError
from src.storage import StorageError

# ----------------------------------------------------------------------
# 픽스처·헬퍼
# ----------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """storage 싱글톤을 tmp DB 로 격리 (로컬 sqlite3) + 스키마 memo 리셋."""
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    monkeypatch.setattr(ledger, "_schema_ready_store", None)
    yield tmp_path / "ledger.db"
    monkeypatch.setattr(storage_mod, "_storage", None)


@dataclass
class Row:
    """ScanRow 와 같은 속성의 duck-typed 픽."""
    symbol: str
    name: str = ""
    sector: str = ""
    price: float = 10.0
    total_score: int = 80
    value_score: int = 80
    health_score: int = 80


def _table_exists(name: str) -> bool:
    r = storage_mod.get_storage().conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


def _count(sql: str, params: tuple = ()) -> int:
    return storage_mod.get_storage().conn.execute(sql, params).fetchone()[0]


def _pick(sent_date, sym, rank=1, market="US", rv="v1", source="live", yf=None, bench=None):
    yf = yf or sym
    return ledger.LedgerPick(sent_date, market, rank, sym, yf,
                             bench or ledger.bench_for(market, yf), rv, source)


def _panel(dates, **cols) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    return pd.DataFrame({k: [np.nan if v is None else float(v) for v in vals]
                         for k, vals in cols.items()}, index=idx)


D = ["2026-07-13", "2026-07-14", "2026-07-15"]


# ----------------------------------------------------------------------
# 스키마
# ----------------------------------------------------------------------


def test_schema_idempotent_and_no_alter(fresh_db):
    ledger._conn()
    ledger._schema_ready_store = None          # 강제 재초기화 — 두 번째 CREATE IF NOT EXISTS
    ledger._conn()
    for t in ("signal_ledger", "pick_outcomes", "ledger_perf", "universe_snapshots"):
        assert _table_exists(t)
    src = inspect.getsource(ledger)
    assert "ALTER TABLE" not in src.upper() and "ADD_COLUMN_IF_MISSING" not in src.upper()


def test_conn_memoized_per_store(fresh_db, monkeypatch):
    calls = []
    real = ledger._SCHEMA
    ledger._conn()
    monkeypatch.setattr(ledger, "_SCHEMA", real + "\n-- probe")
    store = storage_mod.get_storage()
    orig = store.conn.executescript
    store_conn = store.conn

    class Spy:
        def __getattr__(self, n):
            return getattr(store_conn, n)

        def executescript(self, sql):
            calls.append(sql)
            return orig(sql)

    monkeypatch.setattr(store, "_conn", Spy())
    ledger._conn()
    assert calls == []                          # 같은 Storage 인스턴스면 재실행 안 함


# ----------------------------------------------------------------------
# record_send
# ----------------------------------------------------------------------

PICKS_US = [Row("CALM", "Cal-Maine", "Consumer Defensive", 78.1, 92, 92, 92),
            Row("HG", "Hamilton", "Financial Services", 31.9, 88, 88, 88)]


@pytest.mark.parametrize("kwargs", [
    {"sent": 0}, {"sent": None}, {"sent": 3, "degraded": True}, {"sent": 3, "dry_run": True},
])
def test_record_send_gates_write_nothing(fresh_db, kwargs):
    ok = ledger.record_send("US", "2026-10-08", PICKS_US, rule_version="v1", **kwargs)
    assert ok is False
    assert not _table_exists("signal_ledger")                   # 스키마 생성조차 안 함
    assert storage_mod.get_storage().get_state("digest_runs", "US:2026-10-08") is None


def test_record_send_writes_rows_and_ignores_duplicates(fresh_db):
    assert ledger.record_send("US", "2026-10-08", PICKS_US, rule_version="v1", sent=2,
                              text="msg")
    run = storage_mod.get_storage().get_state("digest_runs", "US:2026-10-08")
    assert run["status"] == "recorded" and run["n_sent"] == 2 and run["text"] == "msg"
    assert run["list_key"] == "v1|CALM,HG"
    # 같은 발송 재기록 → PK(sent_date, market, rank) 중복 무시
    assert ledger.record_send("US", "2026-10-08", list(reversed(PICKS_US)), rule_version="v1",
                              sent=2)
    rows = ledger.load_sends("US")
    assert [(r.rank, r.symbol, r.yf_symbol, r.bench_symbol, r.source) for r in rows] == [
        (1, "CALM", "CALM", "SPY", "live"), (2, "HG", "HG", "SPY", "live")]


def test_record_send_kr_suffix_and_bench(fresh_db):
    picks = [Row("009970", "영원무역홀딩스", ""), Row("041510", "에스엠", "우량기업부")]
    assert ledger.record_send("KR", "2026-10-08", picks, rule_version="v1", sent=1)
    rows = {r.symbol: r for r in ledger.load_sends("KR")}
    assert (rows["009970"].yf_symbol, rows["009970"].bench_symbol) == ("009970.KS", "069500.KS")
    assert (rows["041510"].yf_symbol, rows["041510"].bench_symbol) == ("041510.KQ", "229200.KS")


def test_record_send_uses_tuple_params_single_statement(fresh_db, monkeypatch):
    real_store = storage_mod.get_storage()
    ledger._conn()
    calls = []

    class ConnSpy:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, params=()):
            calls.append((sql, params))
            return self._c.execute(sql, params)

        def __getattr__(self, n):
            return getattr(self._c, n)

    class StoreSpy:
        def __init__(self, s):
            self._s = s
            self.conn = ConnSpy(s.conn)

        def __getattr__(self, n):
            return getattr(self._s, n)

    spy = StoreSpy(real_store)
    monkeypatch.setattr(storage_mod, "_storage", spy)
    monkeypatch.setattr(ledger, "get_storage", lambda: spy)
    monkeypatch.setattr(ledger, "_schema_ready_store", spy)
    picks = [Row(f"S{i}") for i in range(6)]
    assert ledger.record_send("US", "2026-10-08", picks, rule_version="v1", sent=1,
                              score_asof="2026-10-07")
    inserts = [(s, p) for s, p in calls if s.startswith("INSERT OR IGNORE INTO signal_ledger")]
    assert len(inserts) == 1                                 # 멀티로우 1 statement
    sql, params = inserts[0]
    assert isinstance(params, tuple) and len(params) == 6 * 16   # flat tuple 96 파라미터
    assert all(isinstance(p, tuple) for _, p in calls)


def test_record_send_db_failure_is_best_effort(fresh_db, monkeypatch):
    def boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ledger, "_conn", boom)
    assert ledger.record_send("US", "2026-10-08", PICKS_US, rule_version="v1", sent=1) is False
    run = storage_mod.get_storage().get_state("digest_runs", "US:2026-10-08")
    assert run["status"] == "ledger_failed"

    def storage_down():
        raise StorageError("no db")

    monkeypatch.setattr(ledger, "get_storage", storage_down)
    monkeypatch.setattr(ledger, "_conn", storage_down)
    assert ledger.record_send("US", "2026-10-09", PICKS_US, rule_version="v1", sent=1) is False


def test_record_send_bad_input_does_not_raise(fresh_db):
    assert ledger.record_send("US", "10/08/2026", PICKS_US, rule_version="v1", sent=1) is False
    assert ledger.record_send("JP", "2026-10-08", PICKS_US, rule_version="v1", sent=1) is False


def test_reads_return_empty_without_tables(fresh_db):
    assert ledger.load_sends("US") == []
    assert ledger.load_perf("US") == []
    assert ledger.load_outcomes("US") == {}
    assert ledger.latest_snapshot("US") is None
    assert not _table_exists("signal_ledger")


# ----------------------------------------------------------------------
# yf_symbol / bench 규칙 (universe.yf_symbol 과 동일 규칙 고정)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("args, expected", [
    (("007700", "KR", "KOSPI"), "007700.KS"),
    (("041510", "KR", "KOSDAQ"), "041510.KQ"),
    (("009970", "KR", None, ""), "009970.KS"),
    (("041510", "KR", None, "우량기업부"), "041510.KQ"),
    (("0126Z0", "KR", None, ""), "0126Z0.KS"),
    (("BRK-B", "US"), "BRK-B"),
    (("BTC", "CRYPTO"), "BTC-USD"),
])
def test_yf_symbol_fallback_rule_table(args, expected):
    assert ledger._yf_symbol_fallback(*args) == expected


def test_bench_for():
    assert ledger.bench_for("US", "CALM") == "SPY"
    assert ledger.bench_for("KR", "009970.KS") == "069500.KS"
    assert ledger.bench_for("KR", "041510.KQ") == "229200.KS"
    with pytest.raises(ValueError):
        ledger.bench_for("CRYPTO", "BTC-USD")


# ----------------------------------------------------------------------
# pick_events
# ----------------------------------------------------------------------


def test_pick_events_new_names_and_reentry():
    sends = [_pick("2026-07-13", "A", 1), _pick("2026-07-13", "B", 2),
             _pick("2026-07-14", "A", 1), _pick("2026-07-14", "C", 2),      # C 신규
             _pick("2026-07-15", "A", 1), _pick("2026-07-15", "B", 2)]      # B 재진입
    ev = [(e.sent_date, e.symbol) for e in ledger.pick_events(sends)]
    assert ev == [("2026-07-13", "A"), ("2026-07-13", "B"), ("2026-07-14", "C"),
                  ("2026-07-15", "B")]


def test_pick_events_rule_version_change_resets():
    sends = [_pick("2026-07-13", "A", rv="v0"), _pick("2026-07-14", "A", rv="v1")]
    assert [(e.sent_date, e.rule_version) for e in ledger.pick_events(sends)] == [
        ("2026-07-13", "v0"), ("2026-07-14", "v1")]


# ----------------------------------------------------------------------
# roll_forward — 손계산
# ----------------------------------------------------------------------


def test_roll_forward_hand_calc_entry_cost_and_drift():
    closes = _panel(D, A=[100, 110, 110], B=[100, 90, 99], SPY=[100, 105, 105])
    sends = [_pick("2026-07-13", "A", 1), _pick("2026-07-13", "B", 2)]
    rows, cur, flags = ledger.roll_forward(None, sends, closes, market="US")
    assert [r["date"] for r in rows] == D
    # 진입일: 현금→리스트 턴오버 1 × 10bp, 벤치 0
    assert rows[0]["port_ret"] == pytest.approx(-0.001)
    assert rows[0]["turnover"] == pytest.approx(1.0) and rows[0]["bench_ret"] == 0.0
    # +10% / -10% 동일가중 = 0, 벤치 +5%
    assert rows[1]["port_ret"] == pytest.approx(0.0)
    assert rows[1]["bench_ret"] == pytest.approx(0.05)
    # 비중 표류: A 0.55, B 0.45 → B +10% 만 → 0.045
    assert rows[2]["port_ret"] == pytest.approx(0.045)
    assert rows[2]["n_held"] == 2 and rows[2]["n_priced"] == 2
    assert cur["last_date"] == "2026-07-15" and flags["stopped_at"] is None
    assert rows[0]["list_key"] == "v1|A,B" and rows[0]["rule_version"] == "v1"


def test_roll_forward_rebalance_on_list_change_with_cost():
    closes = _panel(D, A=[100, 110, 110], B=[100, 90, 99], C=[50, 50, 50],
                    SPY=[100, 105, 105])
    sends = [_pick("2026-07-13", "A", 1), _pick("2026-07-13", "B", 2),
             _pick("2026-07-15", "A", 1), _pick("2026-07-15", "B", 2), _pick("2026-07-15", "C", 3)]
    rows, cur, _ = ledger.roll_forward(None, sends, closes, market="US")
    a, b = 0.55 / 1.045, 0.495 / 1.045             # 07-15 종가 표류 비중
    turnover = abs(1 / 3 - a) + abs(1 / 3 - b) + 1 / 3
    assert rows[2]["turnover"] == pytest.approx(turnover)
    assert rows[2]["port_ret"] == pytest.approx(0.045 - turnover * 0.001)
    assert rows[2]["list_key"] == "v1|A,B"          # 그날 수익은 직전 리스트 보유분
    assert cur["list_key"] == "v1|A,B,C"
    assert cur["w"] == pytest.approx({"A": 1 / 3, "B": 1 / 3, "C": 1 / 3})


def test_roll_forward_resume_from_cursor_equals_full_run():
    dates = pd.bdate_range("2026-07-13", periods=8).strftime("%Y-%m-%d").tolist()
    rng = np.random.default_rng(3)
    a = 100 * np.cumprod(1 + rng.normal(0, 0.02, 8))
    b = 50 * np.cumprod(1 + rng.normal(0, 0.02, 8))
    s = 400 * np.cumprod(1 + rng.normal(0, 0.01, 8))
    closes = _panel(dates, A=a, B=b, SPY=s)
    sends = [_pick(dates[0], "A", 1), _pick(dates[0], "B", 2), _pick(dates[4], "A", 1)]
    full, cur_full, _ = ledger.roll_forward(None, sends, closes, market="US")
    part1, cur1, _ = ledger.roll_forward(None, sends, closes, market="US", asof=dates[3])
    cur1 = json.loads(json.dumps(cur1))              # state 저장 왕복
    part2, cur2, _ = ledger.roll_forward(cur1, sends, closes, market="US")
    assert [r["date"] for r in part1 + part2] == dates
    for x, y in zip(full, part1 + part2):
        assert x["port_ret"] == pytest.approx(y["port_ret"])
        assert x["bench_ret"] == pytest.approx(y["bench_ret"])
    assert cur2["w"] == pytest.approx(cur_full["w"])


def test_roll_forward_missing_latest_price_stops_without_row():
    closes = _panel(D, A=[100, 110, 120], B=[100, 90, None], SPY=[100, 105, 106])
    sends = [_pick("2026-07-13", "A", 1), _pick("2026-07-13", "B", 2)]
    rows, cur, flags = ledger.roll_forward(None, sends, closes, market="US")
    assert [r["date"] for r in rows] == D[:2]
    assert cur["last_date"] == "2026-07-14"
    assert flags["stopped_at"] == "2026-07-15" and "B" in flags["stop_reason"]


def test_roll_forward_gap_then_catch_up():
    dates = ["2026-07-13", "2026-07-14", "2026-07-15"]
    closes = _panel(dates, A=[100, 100, 100], B=[100, None, 120], SPY=[100, 100, 100])
    sends = [_pick(dates[0], "A", 1), _pick(dates[0], "B", 2)]
    rows, _, flags = ledger.roll_forward(None, sends, closes, market="US")
    assert rows[1]["port_ret"] == pytest.approx(0.0) and rows[1]["n_priced"] == 1
    assert rows[2]["port_ret"] == pytest.approx(0.5 * 0.2)     # 재등장일에 누적 반영
    assert flags["gaps"] == [("B", "2026-07-14")]


def _long_panel(n, b_missing_from):
    dates = pd.bdate_range("2026-07-13", periods=n).strftime("%Y-%m-%d").tolist()
    b = [100.0 if i < b_missing_from else None for i in range(n)]
    return dates, _panel(dates, A=[100.0 + i for i in range(n)], B=b, SPY=[100.0] * n)


def test_roll_forward_4_missing_days_wait_5_days_halt():
    dates, closes = _long_panel(7, 3)                # B 결측 4일 (03..06)
    sends = [_pick(dates[0], "A", 1), _pick(dates[0], "B", 2)]
    rows, _, flags = ledger.roll_forward(None, sends, closes, market="US")
    assert len(rows) == 3 and flags["stopped_at"] == dates[3]

    dates, closes = _long_panel(8, 3)                # B 결측 5일 → 거래정지 취급(수익 0)
    rows, cur, flags = ledger.roll_forward(None, sends, closes, market="US")
    assert len(rows) == 8 and flags["stopped_at"] is None
    assert flags["halted"] == {"B": 5} and flags["delisted"] == []
    assert rows[-1]["n_held"] == 2 and rows[-1]["n_priced"] == 1
    assert cur["px"]["B"] == 100.0                   # 마지막 가격 유지


def test_roll_forward_10_missing_days_delisted_flag():
    dates, closes = _long_panel(13, 3)               # B 결측 10일
    sends = [_pick(dates[0], "A", 1), _pick(dates[0], "B", 2)]
    rows, _, flags = ledger.roll_forward(None, sends, closes, market="US")
    assert len(rows) == 13 and flags["delisted"] == ["B"] and flags["halted"]["B"] == 10


def test_roll_forward_entry_without_price_excluded_after_5_days():
    dates = pd.bdate_range("2026-07-13", periods=6).strftime("%Y-%m-%d").tolist()
    closes = _panel(dates, A=[100.0] * 6, X=[None] * 6, SPY=[100.0] * 6)
    sends = [_pick(dates[0], "A", 1), _pick(dates[0], "X", 2)]
    rows, cur, flags = ledger.roll_forward(None, sends, closes, market="US")
    assert len(rows) == 6 and cur["w"] == {"A": 1.0} and flags["no_price"] == ["X"]
    # 3일치만 있으면 진입 대기 (행 없음)
    rows, cur, flags = ledger.roll_forward(None, sends, closes.iloc[:3], market="US")
    assert rows == [] and flags["stopped_at"] == dates[0]


def test_roll_forward_kr_uses_home_index_etfs():
    closes = _panel(D, **{"009970.KS": [100, 110, 110], "041510.KQ": [100, 100, 100],
                          "069500.KS": [100, 101, 101], "229200.KS": [100, 110, 110]})
    sends = [_pick(D[0], "009970", 1, "KR", yf="009970.KS"),
             _pick(D[0], "041510", 2, "KR", yf="041510.KQ")]
    rows, cur, _ = ledger.roll_forward(None, sends, closes, market="KR")
    # 벤치 = 0.5 × KODEX200(+1%) + 0.5 × KODEX코스닥150(+10%)
    assert rows[1]["bench_ret"] == pytest.approx(0.055)
    assert cur["bench_of"] == {"009970.KS": "069500.KS", "041510.KQ": "229200.KS"}


def test_roll_forward_does_not_mutate_input_cursor():
    closes = _panel(D, A=[100, 110, 110], SPY=[100, 105, 105])
    sends = [_pick(D[0], "A")]
    _, cur, _ = ledger.roll_forward(None, sends, closes, market="US", asof=D[1])
    snap = json.dumps(cur, sort_keys=True)
    ledger.roll_forward(cur, sends, closes, market="US")
    assert json.dumps(cur, sort_keys=True) == snap


def test_ledger_perf_rows_are_frozen(fresh_db):
    conn = ledger._conn()
    row = {"market": "US", "date": "2026-07-13", "port_ret": 0.01, "bench_ret": 0.0,
           "turnover": 1.0, "n_held": 0, "n_priced": 0, "list_key": "v1|A", "rule_version": "v1"}
    ledger._write_perf(conn, [row])
    ledger._write_perf(conn, [{**row, "port_ret": 0.99}])
    assert [r["port_ret"] for r in ledger.load_perf("US")] == [0.01]


# ----------------------------------------------------------------------
# update_outcomes (21/63 거래일, COALESCE)
# ----------------------------------------------------------------------


def _outcome_panel(n=70):
    dates = pd.bdate_range("2026-07-13", periods=n)
    a = [100.0 * (1.01 ** i) for i in range(n)]
    s = [100.0 + i * 0.1 for i in range(n)]
    return _panel(dates, A=a, SPY=s), dates


def test_update_outcomes_horizons_hand_calc():
    closes, dates = _outcome_panel(70)
    ev = [_pick("2026-07-13", "A")]
    rows = ledger.update_outcomes(ev, {}, closes, market="US")
    r = rows[0]
    assert r["entry_date"] == "2026-07-13" and r["entry_px"] == 100.0
    assert r["ret_21"] == pytest.approx((1.01 ** 21 - 1) * 100)
    assert r["xs_21"] == pytest.approx((1.01 ** 21 - 1) * 100 - (102.1 / 100 - 1) * 100)
    assert r["ret_63"] == pytest.approx((1.01 ** 63 - 1) * 100)
    assert r["status"] == "done"
    # 아직 미성숙
    rows = ledger.update_outcomes(ev, {}, closes.iloc[:30], market="US")
    assert rows[0]["ret_21"] is not None and rows[0]["ret_63"] is None
    assert rows[0]["status"] == "open"


def test_update_outcomes_sent_on_weekend_enters_next_trading_day():
    closes, _ = _outcome_panel(30)
    rows = ledger.update_outcomes([_pick("2026-07-11", "A")], {}, closes, market="US")
    assert rows[0]["entry_date"] == "2026-07-13"


def test_outcomes_upsert_coalesce_keeps_first_values(fresh_db):
    conn = ledger._conn()
    closes, _ = _outcome_panel(30)
    ev = [_pick("2026-07-13", "A")]
    first = ledger.update_outcomes(ev, {}, closes, market="US", now_iso="t1")
    ledger._upsert_outcomes(conn, first)
    tampered = [{**first[0], "entry_px": 1.0, "ret_21": 999.0, "ret_63": 5.0, "status": "done"}]
    ledger._upsert_outcomes(conn, tampered)
    got = ledger.load_outcomes("US")[("A", "2026-07-13")]
    assert got["entry_px"] == 100.0                              # 채운 값 불변
    assert got["ret_21"] == pytest.approx(first[0]["ret_21"])
    assert got["ret_63"] == 5.0                                  # 빈 칸만 채움
    assert got["status"] == "done"
    ledger._upsert_outcomes(conn, [{**first[0], "status": "open"}])
    assert ledger.load_outcomes("US")[("A", "2026-07-13")]["status"] == "done"   # done 동결
    # done 은 다음 계산에서 건너뜀
    assert ledger.update_outcomes(ev, ledger.load_outcomes("US"), closes, market="US") == []


def test_update_outcomes_delisted_uses_last_price():
    dates = pd.bdate_range("2026-07-13", periods=40)
    a = [100.0 if i < 10 else None for i in range(40)]
    a[9] = 80.0
    closes = _panel(dates, A=a, SPY=[100.0] * 40)
    rows = ledger.update_outcomes([_pick("2026-07-13", "A")], {}, closes, market="US")
    r = rows[0]
    assert r["ret_21"] == pytest.approx(-20.0)                   # 마지막 가격으로 확정
    assert r["status"] == "delisted" and r["last_px"] == 80.0


def test_update_outcomes_no_entry_price():
    dates = pd.bdate_range("2026-07-13", periods=8)
    closes = _panel(dates, A=[None] * 8, SPY=[100.0] * 8)
    rows = ledger.update_outcomes([_pick("2026-07-13", "A")], {}, closes, market="US")
    assert rows[0]["status"] == "no_price" and rows[0]["entry_px"] is None


# ----------------------------------------------------------------------
# block_stats / classify_status
# ----------------------------------------------------------------------


def _perf(port, bench, rv="v1"):
    dates = pd.bdate_range("2026-07-13", periods=len(port)).strftime("%Y-%m-%d")
    return [{"date": d, "port_ret": p, "bench_ret": b, "turnover": 0.0, "rule_version": rv}
            for d, p, b in zip(dates, port, bench)]


def test_block_stats_non_overlapping_complete_blocks_only():
    perf = _perf([0.001] * 45, [0.0] * 45)
    bs = ledger.block_stats(perf)
    assert bs["n"] == 2 and bs["days_in_partial"] == 5
    assert bs["blocks"][0] == pytest.approx((1.001 ** 20 - 1) * 100)
    assert bs["t"] is None                                       # 표준편차 0


def test_block_stats_folds_entry_cost_row_into_first_block():
    entry = {"date": "2026-07-10", "port_ret": -0.001, "bench_ret": 0.0, "turnover": 1.0,
             "n_held": 0, "rule_version": "v1"}
    perf = [entry] + [{**r, "n_held": 2} for r in _perf([0.0] * 20, [0.0] * 20)]
    bs = ledger.block_stats(perf)
    assert bs["n"] == 1 and bs["days_in_partial"] == 0          # 진입일은 거래일로 안 셈
    assert bs["blocks"][0] == pytest.approx(-0.1)                # 비용은 첫 블록에 반영


@pytest.mark.parametrize("blocks, expected", [
    ({"n": 5, "mean": -5.0, "t": -3.0}, "pending"),
    ({"n": 6, "mean": -1.0, "t": -1.0}, "demoted"),              # 경계 포함
    ({"n": 6, "mean": -0.9, "t": -1.5}, "testing"),              # 평균 > -1.0%p
    ({"n": 6, "mean": -2.0, "t": -0.9}, "testing"),              # t > -1.0
    ({"n": 6, "mean": 1.0, "t": 2.0}, "validated"),              # 경계 포함
    ({"n": 6, "mean": 1.0, "t": 1.99}, "testing"),
    ({"n": 8, "mean": 0.5, "t": None}, "testing"),
])
def test_classify_status_boundaries(blocks, expected):
    assert ledger.classify_status(blocks, None, is_monday=True) == expected


def test_classify_status_changes_only_on_monday():
    b = {"n": 6, "mean": -2.0, "t": -1.5}
    assert ledger.classify_status(b, "testing", is_monday=False) == "testing"
    assert ledger.classify_status(b, "testing", is_monday=True) == "demoted"
    assert ledger.classify_status(b, None, is_monday=False) == "pending"


def test_summarize_status_transition_monday(fresh_db):
    perf = _perf([0.0] * 120, [0.002] * 60 + [0.001] * 60)      # 6블록, 매 블록 음수
    perf[3]["port_ret"] = 0.001                                  # 분산 > 0
    sends = [_pick("2026-07-13", "A")]
    cur = {"rule_version": "v1", "list_key": "v1|A"}
    tue = datetime(2026, 12, 29, 15, tzinfo=timezone.utc)        # 화요일
    mon = datetime(2027, 1, 4, 15, tzinfo=timezone.utc)          # 월요일
    s1 = ledger.summarize("US", perf, cur, {}, sends, now=tue)
    assert s1["status"] == "pending" and not s1["status_changed"]
    s2 = ledger.summarize("US", perf, cur, {}, sends, prev_summary=s1, now=mon)
    assert s2["status"] == "demoted" and s2["status_changed"] and s2["status_changed_at"]
    s3 = ledger.summarize("US", perf, cur, {}, sends, prev_summary=s2, now=tue)
    assert s3["status"] == "demoted" and not s3["status_changed"]
    assert s3["status_changed_at"] == s2["status_changed_at"]


# ----------------------------------------------------------------------
# 표시: format_ledger_line / weekly / report
# ----------------------------------------------------------------------

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
BANNED_WORDS = ("추천", "좋", "나쁨", "우수", "부진", "통과", "성공", "실패", "유망", "매수", "매도")


def _summary(**over):
    s = {
        "market": "US", "rule_version": "v0", "since": "2026-07-13", "entry_date": "2026-07-13",
        "price_asof": "2026-10-07", "computed_at": "2026-10-08T00:00:00+00:00", "n_days": 62,
        "cum_port": -0.65, "cum_bench": 4.0, "excess_pp": -4.65, "mdd_port": -7.11,
        "mdd_bench": -3.36, "bench_label": "SPY",
        "blocks": {"n": 3, "mean": -1.5, "t": -0.62, "blocks": [-2.2, 3.0, -5.5],
                   "days_in_partial": 2},
        "status": "pending", "has_reconstructed": True, "n_no_price": 0, "halted": [],
        "events": {"n": 6, "h21": {"n_done": 6, "hits": 2, "mean_xs": -1.0},
                   "h63": {"n_done": 0, "hits": 0, "mean_xs": None}},
        "picks": [{"symbol": "CALM", "ret": -24.8, "xs": -28.8}], "prev_version": None,
    }
    s.update(over)
    return s


def test_format_ledger_line_matches_mockup_and_is_markdown_safe():
    line = ledger.format_ledger_line(_summary(), now=NOW)
    assert line == ("📒 성적: 07-13 이후 이 리스트 보유 -0.7% · SPY +4.0% · 초과 -4.7%p "
                    "(10-07 종가, 비용 포함) · 소급 포함 · ⏳검증 전 3/6")
    assert not set(line) & set("_*`[]\\")
    assert not any(w in line for w in BANNED_WORDS)


@pytest.mark.parametrize("over", [
    {"status": "testing", "blocks": {"n": 7, "mean": -1.5, "t": -0.6, "blocks": []}},
    {"status": "validated", "blocks": {"n": 8, "mean": 2.5, "t": 2.4, "blocks": []}},
    {"status": "demoted", "blocks": {"n": 6, "mean": -2.1, "t": -1.4, "blocks": []}},
    {"halted": [["BRK_B*", 7]], "n_no_price": 2},
    {"prev_version": {"rule_version": "v0", "excess_pp": -4.7}, "rule_version": "v1"},
])
def test_format_ledger_line_variants_safe(over):
    line = ledger.format_ledger_line(_summary(**over), now=NOW)
    assert line and not set(line) & set("_*`[]\\")
    assert not any(w in line for w in BANNED_WORDS)


def test_format_ledger_line_tails_and_transition():
    line = ledger.format_ledger_line(
        _summary(has_reconstructed=False, n_no_price=1, halted=[["057050", 7]]), now=NOW)
    assert "가격 미확보 1건" in line and "거래정지 의심 1건(057050, 7거래일)" in line
    assert "소급 포함" not in line
    line = ledger.format_ledger_line(_summary(
        rule_version="v1", has_reconstructed=False, blocks={"n": 0, "blocks": []},
        prev_version={"rule_version": "v0", "excess_pp": -4.7}), now=NOW)
    assert line.endswith("⏳새 규칙 v1 검증 0/6 (v0 초과 -4.7%p)")


def test_format_ledger_line_gate():
    stale_calc = _summary(computed_at="2026-10-04T00:00:00+00:00")       # 4일 전 계산
    assert ledger.format_ledger_line(stale_calc, now=NOW) is None
    ok, footer = ledger.ledger_gate(stale_calc, now=NOW)
    assert not ok and footer.startswith("성적표 4일째 미갱신(마지막 10-07)")
    old_price = _summary(price_asof="2026-10-01")                         # 5거래일 전 종가
    assert ledger.format_ledger_line(old_price, now=NOW) is None
    assert ledger.format_ledger_line(_summary(price_asof="2026-10-02"), now=NOW)  # 4거래일
    assert ledger.format_ledger_line(None, now=NOW) is None
    assert ledger.format_ledger_line(_summary(price_asof=None), now=NOW) is None


def test_format_weekly_block_hides_horizon_until_six_done():
    s = _summary(events={"n": 6, "h21": {"n_done": 5, "hits": 2, "mean_xs": -1.0},
                         "h63": {"n_done": 0, "hits": 0, "mean_xs": None}})
    blk = ledger.format_weekly_block(s, now=NOW)
    assert "판정까지 3구간" in blk and "지평 적중은 픽 6건 이상 완료 후 표시" in blk
    assert "적중 2/5" not in blk
    blk = ledger.format_weekly_block(_summary(), now=NOW)
    assert "21일 적중 2/6 (평균 -1.0%p)" in blk
    assert not set(blk) & set("_*`[]\\")


def test_format_report_plain_text():
    txt = ledger.format_report(_summary(), {}, runs={}, now=NOW)
    assert "리스트 보유 -0.65% vs SPY +4.00% (초과 -4.65%p)" in txt
    assert "CALM -24.8% (-28.8%p)" in txt and "라이브 기록 없음" in txt
    assert ledger.format_report(None).startswith("원장 성적 없음")


# ----------------------------------------------------------------------
# 시간 헬퍼
# ----------------------------------------------------------------------


def test_last_complete_session_local_cutoff():
    # 08:30 KST 발송 직후 → KR 은 전날까지, US 는 (ET 19:30) 당일까지
    t = datetime(2026, 10, 7, 23, 31, tzinfo=timezone.utc)            # 10-08 08:31 KST
    assert str(ledger.last_complete_session("KR", t)) == "2026-10-07"
    assert str(ledger.last_complete_session("US", t)) == "2026-10-07"
    t = datetime(2026, 10, 8, 13, 1, tzinfo=timezone.utc)             # 10-08 09:01 ET
    assert str(ledger.last_complete_session("US", t)) == "2026-10-07"
    assert str(ledger.last_complete_session("KR", t)) == "2026-10-08"


# ----------------------------------------------------------------------
# 스냅샷 / 백필 (screened 동결 DB 를 합성)
# ----------------------------------------------------------------------

_US_ROWS = [  # symbol, total, mcap
    ("CALM", 92, 3.7e9), ("HG", 88, 3.18e9), ("EOG", 86, 73e9), ("SM", 86, 7.4e9),
    ("MGR", 86, 5.8e9), ("AFGE", 85, 1.33e9), ("UVE", 85, 1.06e9), ("ZZZ", 99, 9e9),
]
_KR_ROWS = [
    ("009970", 92, 2.4e12, ""), ("007700", 92, 6.4e11, ""), ("284740", 92, 5.0e11, ""),
    ("041510", 90, 1.9e12, "우량기업부"), ("073240", 90, 1.37e12, ""), ("003090", 90, 1.05e12, ""),
    ("192400", 90, 9.1e11, ""),
]


def _make_screened(updated="2026-07-10T16:50:43.760204+00:00"):
    """per/pbr 없는 구버전 screened (stale 레플리카 형태 — PRAGMA 가드 검증)."""
    conn = storage_mod.get_storage().conn
    conn.executescript("""
        CREATE TABLE screened (symbol TEXT, market TEXT, name TEXT, sector TEXT, price REAL,
            market_cap REAL, health_score INTEGER, value_score INTEGER, total_score INTEGER,
            enriched INTEGER NOT NULL DEFAULT 0, updated_at TEXT, PRIMARY KEY (symbol, market));
    """)
    rows = [(s, "US", s + " Inc", "Energy", 10.0, mc, t, t, t, 0 if s == "ZZZ" else 1, updated)
            for s, t, mc in _US_ROWS]
    rows += [(s, "KR", "회사" + s, sec, 1000.0, mc, t, t, t, 1, updated)
             for s, t, mc, sec in _KR_ROWS]
    conn.executemany("INSERT INTO screened VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_snapshot_universe_and_ensure_pre_snapshot_idempotent(fresh_db):
    _make_screened()
    res = ledger.ensure_pre_snapshot(markets=("US", "KR"))
    assert res["US"]["written"] and res["US"]["as_of"] == "2026-07-10"
    assert res["US"]["source"] == "pre_rescore" and res["US"]["rule_version"] == "v0"
    again = ledger.ensure_pre_snapshot(markets=("US", "KR"))
    assert not again["US"]["written"] and "이미 있음" in again["US"]["reason"]
    assert _count("SELECT COUNT(*) FROM universe_snapshots") == 2
    snap = ledger.load_snapshot("US", "2026-07-10")
    assert len(snap["rows"]) == len(_US_ROWS)                 # enriched=0 행도 포함
    assert snap["rows"][0]["per"] is None                     # 없는 컬럼은 NULL


def test_snapshot_dry_run_writes_nothing(fresh_db):
    _make_screened()
    res = ledger.snapshot_universe("rescore", rule_version="v1", markets=("US",), dry_run=True)
    assert res["US"]["n_rows"] == len(_US_ROWS) and not res["US"]["written"]
    assert not _table_exists("universe_snapshots")


def test_snapshot_reconstructed_guard(fresh_db):
    _make_screened(updated="2026-10-05T01:00:00+00:00")       # 재점수됨
    res = ledger.snapshot_reconstructed()
    assert res["US"]["refused"] and not _count(
        "SELECT COUNT(*) FROM universe_snapshots")


def test_backfill_dry_run_default_writes_nothing(fresh_db):
    _make_screened()
    plan, written = ledger.backfill()
    assert plan.ok and written == {}
    assert plan.markets["US"].computed == ledger.BACKFILL_PICKS["US"]
    assert plan.markets["KR"].computed == ledger.BACKFILL_PICKS["KR"]
    assert not _table_exists("signal_ledger") and not _table_exists("universe_snapshots")
    txt = ledger.format_backfill_plan(plan)
    assert "DRY-RUN(쓰기 0)" in txt and "5 MGR 86 (후순위채 의심)" in txt
    assert "4 041510.KQ 회사041510 90→229200.KS" in txt
    assert "backfill --confirm" in txt


def test_backfill_confirm_writes_12_rows_once(fresh_db):
    _make_screened()
    plan, written = ledger.backfill(confirm=True)
    assert plan.ok and written == {"US": True, "KR": True}
    rows = ledger.load_sends("US") + ledger.load_sends("KR")
    assert len(rows) == 12
    assert {(r.sent_date, r.rule_version, r.source) for r in rows} == {
        ("2026-07-13", "v0", "reconstructed")}
    kr = {r.symbol: r for r in rows if r.market == "KR"}
    assert kr["041510"].bench_symbol == "229200.KS"
    assert ledger.latest_snapshot("US")["source"] == "reconstructed"
    # 재실행 — 중복 없음, digest_runs 미기록(소급은 실제 발송이 아님)
    ledger.backfill(confirm=True)
    assert _count("SELECT COUNT(*) FROM signal_ledger") == 12
    assert ledger.load_runs("US") == {}


def test_backfill_guard_rescored_db_refused(fresh_db):
    _make_screened(updated="2026-10-05T01:00:00+00:00")
    plan, written = ledger.backfill(confirm=True)
    assert not plan.ok and written == {}
    assert any("재점수됨" in r for r in plan.reasons)
    assert not _table_exists("signal_ledger") or _count("SELECT COUNT(*) FROM signal_ledger") == 0


def test_backfill_rescored_but_pre_rescore_snapshot_ok(fresh_db):
    _make_screened()
    ledger.ensure_pre_snapshot(markets=("US", "KR"))          # 07-10 pre_rescore 보존
    storage_mod.get_storage().conn.execute(
        "UPDATE screened SET updated_at='2026-10-12T00:00:00+00:00', total_score=10")
    plan, _ = ledger.backfill()
    assert plan.ok and plan.markets["US"].data_source == "snapshot:pre_rescore"


def test_backfill_guard_live_rows_refused(fresh_db):
    _make_screened()
    ledger.record_send("US", "2026-10-08", PICKS_US, rule_version="v1", sent=1)
    plan, written = ledger.backfill(confirm=True)
    assert not plan.ok and written == {}
    assert any("live" in r for r in plan.reasons)
    assert _count("SELECT COUNT(*) FROM signal_ledger WHERE source='reconstructed'") == 0


def test_backfill_guard_cross_check_mismatch_aborts_all(fresh_db):
    conn = _make_screened()
    conn.execute("UPDATE screened SET total_score=93 WHERE symbol='UVE'")   # 순위 뒤집힘
    conn.commit()
    plan, written = ledger.backfill(confirm=True)
    assert not plan.ok and written == {}
    assert plan.markets["US"].computed[0] == "UVE"
    assert _count("SELECT COUNT(*) FROM signal_ledger") == 0  # KR 도 쓰지 않음 (부분 백필 금지)
    assert "[중단]" in ledger.format_backfill_plan(plan)


def test_v0_top_tie_break_mcap():
    rows = [{"symbol": "B", "total": 90, "mcap": 1.0, "enriched": 1},
            {"symbol": "A", "total": 90, "mcap": 2.0, "enriched": 1},
            {"symbol": "C", "total": 95, "mcap": 0.5, "enriched": 0}]
    assert [r["symbol"] for r in ledger.v0_top(rows, 2)] == ["A", "B"]


def test_rank_ic_perfect_and_waiting(fresh_db):
    a = [{"symbol": f"S{i}", "total": i, "price": 100.0, "enriched": 1} for i in range(20)]
    b = [{"symbol": f"S{i}", "price": 100.0 + i} for i in range(20)]
    r = ledger.rank_ic(a, b)
    assert r["n"] == 20 and r["ic"] == pytest.approx(1.0) and r["spread"] > 0
    assert ledger.universe_ic("US")["status"] in ("waiting", "error")


# ----------------------------------------------------------------------
# score 오케스트레이터 (fetch monkeypatch)
# ----------------------------------------------------------------------


def _fake_fetch_factory(panel):
    calls = []

    def fetch(tickers, start, end=None):
        calls.append((tickers, start, end))
        cols = [t for t in tickers if t in panel.columns]
        return panel.loc[(panel.index >= pd.Timestamp(start))
                         & (panel.index < pd.Timestamp(end or "2100-01-01")), cols]

    return fetch, calls


def test_score_end_to_end_and_idempotent(fresh_db):
    dates = pd.bdate_range("2026-07-13", periods=30)
    panel = _panel(dates, A=[100 * 1.01 ** i for i in range(30)], B=[50.0] * 30,
                   SPY=[100 + i * 0.5 for i in range(30)])
    ledger.record_send("US", "2026-07-13", [Row("A"), Row("B")], rule_version="v0",
                       source="reconstructed", score_asof="2026-07-10")
    fetch, calls = _fake_fetch_factory(panel)
    now = datetime(2026, 8, 24, 23, tzinfo=timezone.utc)      # 월요일 19시 ET
    res = ledger.score(("US", "KR"), now=now, fetch=fetch)
    assert res["KR"]["skipped"] and res["US"]["ok"]
    assert res["US"]["rows"] == 30 and "SPY" in calls[0][0]
    s = ledger.load_summary("US")
    assert s["n_days"] == 29 and s["has_reconstructed"] and s["status"] == "pending"
    # 동일가중 매수후보유: 일수익 복리 = 가치비 (0.5·1.01^29 + 0.5), 진입 비용 10bp 1회
    expect_port = ((1 - 0.001) * (0.5 * 1.01 ** 29 + 0.5) - 1) * 100
    assert s["cum_port"] == pytest.approx(expect_port, rel=1e-9)
    assert s["cum_bench"] == pytest.approx((114.5 / 100 - 1) * 100)
    out = ledger.load_outcomes("US")
    assert out[("A", "2026-07-13")]["ret_21"] == pytest.approx((1.01 ** 21 - 1) * 100)
    # 재실행: 새 행 0, 수치 동일 (동결)
    res2 = ledger.score(("US",), now=now, fetch=fetch)
    assert res2["US"]["rows"] == 0
    assert ledger.load_summary("US")["cum_port"] == pytest.approx(s["cum_port"])
    assert len(ledger.load_perf("US")) == 30
    line = ledger.format_ledger_line(ledger.load_summary("US"), now=now)
    assert line.startswith("📒 성적: 07-13 이후 이 리스트 보유")


def test_score_late_send_symbols_are_fetched(fresh_db):
    dates = pd.bdate_range("2026-07-13", periods=6)
    panel = _panel(dates, A=[100.0] * 6, C=[50.0, 50, 50, 50, 55, 55], SPY=[100.0] * 6)
    ledger.record_send("US", "2026-07-13", [Row("A")], rule_version="v1",
                       source="reconstructed")
    fetch, calls = _fake_fetch_factory(panel)
    ledger.score(("US",), now=datetime(2026, 7, 15, 23, tzinfo=timezone.utc), fetch=fetch)
    # 이미 채점된 날짜(07-15)로 늦게 기록된 발송 → 다음 날 재조정 때 C 가격이 있어야 함
    ledger.record_send("US", "2026-07-15", [Row("A"), Row("C")], rule_version="v1", sent=1)
    res = ledger.score(("US",), now=datetime(2026, 7, 17, 23, tzinfo=timezone.utc), fetch=fetch)
    assert "C" in calls[-1][0]
    s = res["US"]["summary"]
    assert s["n_no_price"] == 0 and s["list_size"] == 2
    assert {p["symbol"] for p in s["picks"]} == {"A", "C"}


def test_score_dry_run_writes_nothing(fresh_db):
    dates = pd.bdate_range("2026-07-13", periods=5)
    panel = _panel(dates, A=[100.0] * 5, SPY=[100.0] * 5)
    ledger.record_send("US", "2026-07-13", [Row("A")], rule_version="v0",
                       source="reconstructed")
    fetch, _ = _fake_fetch_factory(panel)
    now = datetime(2026, 7, 20, 23, tzinfo=timezone.utc)
    res = ledger.score(("US",), now=now, fetch=fetch, dry_run=True)
    assert res["US"]["rows"] == 5
    assert ledger.load_perf("US") == [] and ledger.load_summary("US") is None


def test_score_fetch_failure_reported_not_raised(fresh_db):
    ledger.record_send("US", "2026-07-13", [Row("A")], rule_version="v0", source="reconstructed")

    def bad_fetch(*a, **k):
        raise DataFetchError("down", source="yfinance")

    res = ledger.score(("US",), fetch=bad_fetch)
    assert res["US"]["ok"] is False and "가격 조회 실패" in res["US"]["error"]


def test_cli_backfill_dry_run_and_score_exit_codes(fresh_db, capsys, monkeypatch):
    from scripts import ledger as cli

    _make_screened()
    assert cli.main(["backfill"]) == 0
    assert "DRY-RUN(쓰기 0)" in capsys.readouterr().out
    monkeypatch.setattr(ledger, "_default_fetch",
                        lambda *a, **k: (_ for _ in ()).throw(DataFetchError("x")))
    ledger.record_send("US", "2026-07-13", [Row("A")], rule_version="v0", source="reconstructed")
    assert cli.main(["score", "--markets", "US"]) == 1
    assert cli.main(["report", "us"]) == 0
    assert "원장 성적 없음" in capsys.readouterr().out


# ----------------------------------------------------------------------
# fetch_close_panel (yf.download monkeypatch — 네트워크 없음)
# ----------------------------------------------------------------------


def _yf_frame(tickers, dates, multi=True):
    fields = ["Adj Close", "Close", "Volume"]
    if multi:
        cols = pd.MultiIndex.from_product([fields, tickers], names=["Price", "Ticker"])
        data = np.arange(len(dates) * len(cols), dtype=float).reshape(len(dates), len(cols)) + 1
        return pd.DataFrame(data, index=pd.DatetimeIndex(dates, name="Date"), columns=cols)
    return pd.DataFrame({f: np.arange(len(dates), dtype=float) + 1 for f in fields},
                        index=pd.DatetimeIndex(dates, name="Date"))


def test_fetch_close_panel_multi_ticker(monkeypatch):
    dates = pd.bdate_range("2026-07-13", periods=3)
    seen = {}

    def fake_download(tickers, **kw):
        seen.update(kw, tickers=tickers)
        return _yf_frame(["SPY", "CALM"], dates)

    monkeypatch.setattr(data_fetcher.yf, "download", fake_download)
    df = data_fetcher.fetch_close_panel(("CALM", "SPY", "NOPE"), "2026-07-01")
    assert list(df.columns) == ["CALM", "SPY", "NOPE"] and df["NOPE"].isna().all()
    assert seen["auto_adjust"] is False and seen["tickers"] == ["CALM", "SPY", "NOPE"]
    assert df.index.tz is None and len(df) == 3


def test_fetch_close_panel_single_level_frame(monkeypatch):
    dates = pd.bdate_range("2026-07-13", periods=2)
    monkeypatch.setattr(data_fetcher.yf, "download",
                        lambda *a, **k: _yf_frame(["SPY"], dates, multi=False))
    df = data_fetcher.fetch_close_panel(("SPY",), "2026-07-01")
    assert list(df.columns) == ["SPY"] and df["SPY"].tolist() == [1.0, 2.0]


def test_fetch_close_panel_errors(monkeypatch):
    monkeypatch.setattr(data_fetcher.yf, "download", lambda *a, **k: pd.DataFrame())
    with pytest.raises(DataValidationError):
        data_fetcher.fetch_close_panel(("SPY",), "2026-07-01")

    def boom(*a, **k):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(data_fetcher.yf, "download", boom)
    with pytest.raises(DataFetchError):
        data_fetcher.fetch_close_panel(("SPY",), "2026-07-01")
    with pytest.raises(DataValidationError):
        data_fetcher.fetch_close_panel(tuple(f"T{i}" for i in range(201)), "2026-07-01")
    with pytest.raises(DataValidationError):
        data_fetcher.fetch_close_panel((), "2026-07-01")


def test_fetch_close_panel_not_cached():
    # @cached 래퍼는 __wrapped__ 를 남긴다 — 캐시 없음 확인 (§4.10 #2)
    assert not hasattr(data_fetcher.fetch_close_panel, "__wrapped__")
    assert hasattr(data_fetcher.fetch_wikipedia_pageviews, "__wrapped__")
