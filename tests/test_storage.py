"""src/storage.py — SQLite 캐시 라운드트립 / TTL / 데코레이터 (tmp DB, 오프라인)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

import src.storage as storage_mod
from src.storage import Storage, cached

TTL = timedelta(hours=1)


@pytest.fixture
def store(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def cache_on(tmp_path, monkeypatch):
    """데코레이터 테스트용: 캐시 켜고 싱글톤을 tmp DB 로 격리."""
    monkeypatch.setenv("QUANT_BOT_CACHE", "on")
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "deco.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)  # 싱글톤 리셋
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


# ---------------- 라운드트립 ----------------


# 참고: 캐시는 datetime 을 항상 ns 단위로 복원 (storage._normalize_datetimes).
# pandas 3.x 의 date_range 기본 단위는 us 라서, 라운드트립 비교용 픽스처는
# ns 로 명시해 "캐시 출력 = ns" 라는 계약을 그대로 검증한다.


def test_dataframe_roundtrip_preserves_dtypes_and_index(store, ohlcv_frame):
    df = ohlcv_frame.copy()
    df.index = pd.RangeIndex(len(df))  # 정수 인덱스 라운드트립
    store.put_dataframe("ns", "k", df)
    back = store.get_dataframe("ns", "k", TTL)
    pd.testing.assert_frame_equal(back, df)


def test_dataframe_roundtrip_datetime_index_restored_as_ns(store, ohlcv_frame):
    df = ohlcv_frame.copy()
    df.index = pd.date_range("2024-01-01", periods=len(df)).as_unit("ns")
    store.put_dataframe("ns", "k", df)
    back = store.get_dataframe("ns", "k", TTL)
    pd.testing.assert_frame_equal(back, df, check_freq=False)


def test_series_roundtrip_preserves_name(store):
    idx = pd.date_range("2024-01-01", periods=2).as_unit("ns")
    s = pd.Series([1.0, 2.0], index=idx, name="T10Y2Y")
    store.put_series("ns", "k", s)
    back = store.get_series("ns", "k", TTL)
    pd.testing.assert_series_equal(back, s, check_freq=False)
    assert back.name == "T10Y2Y"


def test_json_roundtrip(store):
    obj = {"symbol": "CPNG", "price": 17.25, "한글": True}
    store.put_json("ns", "k", obj)
    assert store.get_json("ns", "k", TTL) == obj


def test_crypto_like_normalized_datetime_index_roundtrip(store):
    # fetch_crypto 의 인덱스 형태 (자정 정규화 DatetimeIndex) 가 무손실인지
    idx = pd.to_datetime(["2024-01-01", "2024-01-02"]).normalize().as_unit("ns")
    df = pd.DataFrame({"price": [1.0, 2.0]}, index=idx)
    df.index.name = "date"
    store.put_dataframe("crypto", "btc", df)
    back = store.get_dataframe("crypto", "btc", TTL)
    pd.testing.assert_frame_equal(back, df, check_freq=False)


# ---------------- miss / TTL / 격리 ----------------


def test_miss_returns_none(store):
    assert store.get_dataframe("ns", "nope", TTL) is None


def test_expired_entry_is_miss(store, ohlcv_frame):
    store.put_dataframe("ns", "k", ohlcv_frame)
    assert store.get_dataframe("ns", "k", timedelta(seconds=-1)) is None


def test_namespaces_are_isolated(store):
    store.put_json("a", "k", {"v": 1})
    assert store.get_json("b", "k", TTL) is None


def test_corrupt_payload_is_miss_not_crash(store):
    store._put_payload("ns", "k", "dataframe", "{이건 JSON 아님")
    assert store.get_dataframe("ns", "k", TTL) is None


def test_purge_and_stats(store):
    store.put_json("a", "k1", {})
    store.put_json("b", "k2", {})
    assert store.stats() == {"a": 1, "b": 1}
    assert store.purge() == 2
    assert store.stats() == {}


# ---------------- cached 데코레이터 ----------------


def test_cached_decorator_hits_after_first_call(cache_on):
    calls = {"n": 0}

    @cached("test_ns", TTL, "dataframe")
    def fetch(ticker: str, period: str = "1y") -> pd.DataFrame:
        calls["n"] += 1
        idx = pd.date_range("2024-01-01", periods=1).as_unit("ns")
        return pd.DataFrame({"x": [1.0]}, index=idx)

    a = fetch("CPNG")
    b = fetch("CPNG", period="1y")  # positional/keyword 달라도 같은 키여야 함
    assert calls["n"] == 1
    pd.testing.assert_frame_equal(a, b, check_freq=False)

    fetch("NVDA")  # 다른 인자 → 다른 키 → 실호출
    assert calls["n"] == 2


def test_cached_decorator_off_mode_bypasses(cache_on, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_CACHE", "off")
    calls = {"n": 0}

    @cached("test_ns", TTL, "json")
    def fetch() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    assert fetch() == {"v": 1}
    assert fetch() == {"v": 2}  # off — 매번 실호출


def test_cached_decorator_refresh_mode_rewrites(cache_on, monkeypatch):
    calls = {"n": 0}

    @cached("test_ns", TTL, "json")
    def fetch() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    assert fetch() == {"v": 1}
    monkeypatch.setenv("QUANT_BOT_CACHE", "refresh")
    assert fetch() == {"v": 2}          # 읽기 무시, 새로 받아 캐시 갱신
    monkeypatch.setenv("QUANT_BOT_CACHE", "on")
    assert fetch() == {"v": 2}          # 갱신된 캐시 적중


def test_cached_decorator_does_not_cache_empty(cache_on):
    calls = {"n": 0}

    @cached("test_ns", TTL, "dataframe")
    def fetch() -> pd.DataFrame:
        calls["n"] += 1
        return pd.DataFrame()

    fetch()
    fetch()
    assert calls["n"] == 2  # 빈 결과는 캐시 안 함 → 매번 재시도


def test_add_column_if_missing_adds_then_idempotent():
    import sqlite3

    from src.storage import add_column_if_missing

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a INTEGER)")
    add_column_if_missing(conn, "t", "b", "TEXT")
    assert {r[1] for r in conn.execute("PRAGMA table_info(t)")} == {"a", "b"}
    add_column_if_missing(conn, "t", "b", "TEXT")   # 이미 존재 → no-op, 예외 없음
    assert {r[1] for r in conn.execute("PRAGMA table_info(t)")} == {"a", "b"}


# ---------------- Turso sync 행 방지 (감시 타임아웃 + 오프라인 강등) ----------------
# 실사고 (2026-07-06): 블랙홀 네트워크에서 libsql sync() 가 예외 없이 무한 대기
# → @cached/get_state 를 타는 모든 진입점(다이제스트·fetch·봇)이 통째로 멈춤.


class _FakeSyncConn:
    """sync() 동작을 주입할 수 있는 가짜 libsql conn."""

    def __init__(self, behavior: str, release: "threading.Event | None" = None):
        self.behavior = behavior
        self.release = release

    def sync(self):
        if self.behavior == "hang":
            self.release.wait(30)          # 테스트 teardown 이 set() 으로 해제
        elif self.behavior == "raise":
            raise RuntimeError("sync 실패")

    # 강등 경로에서 __init__ 이후 코드가 부르지 않지만, 혹시 몰라 no-op 제공
    def executescript(self, *_a):  # pragma: no cover
        return None

    def commit(self):  # pragma: no cover
        return None


import threading  # noqa: E402


def test_safe_sync_watchdog_tristate(store, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_SYNC_TIMEOUT", "1")
    assert store._safe_sync(_FakeSyncConn("ok"), "t") == "ok"
    assert store._safe_sync(_FakeSyncConn("raise"), "t") == "error"
    ev = threading.Event()
    try:
        assert store._safe_sync(_FakeSyncConn("hang", ev), "t") == "timeout"
    finally:
        ev.set()                           # 유기 스레드 해제 (테스트 위생)


def test_sync_timeout_env_parsing(monkeypatch):
    monkeypatch.setenv("QUANT_BOT_SYNC_TIMEOUT", "abc")
    assert storage_mod._sync_timeout() == 20.0
    monkeypatch.setenv("QUANT_BOT_SYNC_TIMEOUT", "0.2")
    assert storage_mod._sync_timeout() == 1.0          # 하한 1s
    monkeypatch.setenv("QUANT_BOT_SYNC_TIMEOUT", "7")
    assert storage_mod._sync_timeout() == 7.0


def test_offline_env_bypasses_turso(tmp_path, monkeypatch):
    """QUANT_BOT_OFFLINE=1 — TURSO_* 설정돼 있어도 libsql 을 아예 안 탐 (즉시 로컬)."""
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "replica.db"))
    monkeypatch.setenv("QUANT_BOT_OFFLINE", "1")
    prev = storage_mod.settings.turso_database_url
    object.__setattr__(storage_mod.settings, "turso_database_url", "libsql://x.turso.io")
    s = Storage()                                       # 주입 없음 → env 경로
    try:
        assert s.is_turso is False
        assert s.db_path.name == "replica.offline.db"   # 레플리카 파일 미접촉
        s.put_json("ns", "k", {"a": 1})                 # 로컬 캐시 정상 동작
        assert s.get_json("ns", "k", TTL) == {"a": 1}
        assert not (tmp_path / "replica.db").exists()
    finally:
        s.close()
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


def test_initial_sync_timeout_degrades_to_offline_file(tmp_path, monkeypatch):
    """초기 pull 타임아웃 → libsql conn 유기 + 별도 오프라인 파일로 강등 (행 없음)."""
    import sys
    import types

    ev = threading.Event()
    fake_libsql = types.ModuleType("libsql_experimental")
    fake_libsql.connect = lambda *a, **k: _FakeSyncConn("hang", ev)
    monkeypatch.setitem(sys.modules, "libsql_experimental", fake_libsql)
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "replica.db"))
    monkeypatch.setenv("QUANT_BOT_SYNC_TIMEOUT", "1")
    prev = storage_mod.settings.turso_database_url
    object.__setattr__(storage_mod.settings, "turso_database_url", "libsql://x.turso.io")
    try:
        s = Storage()                                   # 1s 내 강등되어 반환돼야 함
        try:
            assert s.is_turso is False
            assert s.db_path.name == "replica.offline.db"
            s.put_state("ns", "k", {"ok": True})        # sqlite3 로 정상 동작
            assert s.get_state("ns", "k") == {"ok": True}
        finally:
            s.close()
    finally:
        ev.set()
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


def test_push_sync_timeout_marks_degraded_and_skips(store, monkeypatch):
    """push 타임아웃 후 같은 conn 에 sync 를 겹쳐 돌리지 않음 (1회 유기로 끝)."""
    calls = []
    store.is_turso = True                               # 로컬 conn 이지만 경로만 검증
    monkeypatch.setattr(store, "_safe_sync",
                        lambda conn, what: calls.append(what) or "timeout")
    store.sync()
    assert calls == ["push"] and store._sync_degraded is True
    store.sync()                                        # 강등 상태 — 추가 호출 없음
    assert calls == ["push"]
