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


def _turso_env(tmp_path, monkeypatch):
    """Turso 경로 테스트 공통 셋업 — env DB 경로 + settings url (복원값 반환)."""
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "replica.db"))
    prev = storage_mod.settings.turso_database_url
    object.__setattr__(storage_mod.settings, "turso_database_url", "libsql://x.turso.io")
    return prev


def test_probe_timeout_degrades_to_offline_file(tmp_path, monkeypatch):
    """초기 pull 프로브 타임아웃 → libsql 을 아예 안 열고 오프라인 파일로 강등.

    실사고 회귀 (2026-07-06): libsql 은 GIL 을 쥔 채 블로킹하므로 스레드
    워치독이 무력 → 자식 프로세스 프로브가 유일하게 확실한 타임아웃 수단.
    """
    import sys
    import types

    fake_libsql = types.ModuleType("libsql_experimental")
    fake_libsql.connect = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("프로브 타임아웃이면 부모는 libsql 을 열면 안 됨"))
    monkeypatch.setitem(sys.modules, "libsql_experimental", fake_libsql)
    monkeypatch.setattr(Storage, "_probe_initial_sync",
                        staticmethod(lambda *a, **k: "timeout"))
    prev = _turso_env(tmp_path, monkeypatch)
    try:
        s = Storage()
        try:
            assert s.is_turso is False
            assert s.db_path.name == "replica.offline.db"
            s.put_state("ns", "k", {"ok": True})        # sqlite3 로 정상 동작
            assert s.get_state("ns", "k") == {"ok": True}
        finally:
            s.close()
    finally:
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


def test_probe_ok_or_error_connects_without_parent_sync(tmp_path, monkeypatch):
    """프로브 ok/error → 부모는 libsql 로 열되 초기 sync 를 다시 하지 않음
    (자식이 방금 pull 했거나, 빠른 실패라 stale 레플리카로 진행 — 종전 의미론)."""
    import sys
    import types

    sync_calls = []

    class _NoSyncConn:
        def sync(self):
            sync_calls.append(1)

        def executescript(self, *_a):
            return None

        def commit(self):
            return None

    fake_libsql = types.ModuleType("libsql_experimental")
    fake_libsql.connect = lambda *a, **k: _NoSyncConn()
    monkeypatch.setitem(sys.modules, "libsql_experimental", fake_libsql)
    prev = _turso_env(tmp_path, monkeypatch)
    try:
        for status in ("ok", "error"):
            monkeypatch.setattr(Storage, "_probe_initial_sync",
                                staticmethod(lambda *a, s=status, **k: s))
            st = Storage()
            assert st.is_turso is True
            assert st.db_path.name == "replica.db"
            assert sync_calls == []                     # 부모 초기 sync 없음
    finally:
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


def test_probe_subprocess_states(tmp_path, monkeypatch):
    """_probe_initial_sync 자체 — subprocess 결과 → 3상태 매핑."""
    import subprocess

    probe = storage_mod._probe_initial_sync

    class _Rc:
        def __init__(self, rc):
            self.returncode = rc
            self.stderr = "boom"

    monkeypatch.setattr(storage_mod.subprocess if hasattr(storage_mod, "subprocess")
                        else subprocess, "run",
                        lambda *a, **k: _Rc(0))
    assert probe(tmp_path / "r.db", "libsql://x", "t") == "ok"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Rc(1))
    assert probe(tmp_path / "r.db", "libsql://x", "t") == "error"

    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="python", timeout=1)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    assert probe(tmp_path / "r.db", "libsql://x", "t") == "timeout"


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


# ---------------- libsql ValueError 쓰기 가드 회귀 ----------------
# 실사고 (2026-07-11 smoke): build_universe --enrich 192/2190 에서
# libsql_experimental 이 "stream not found" 를 plain ValueError 로 올림.
# _put_payload / put_state 가 sqlite3.Error 만 잡던 시절엔 파이프라인이 크래시.
# 픽스: except _DB_ERRORS (ValueError 포함). 이 테스트가 회귀를 막는다.


class _StubConnRaisesValueError:
    """execute() 호출 시 libsql 스타일 ValueError 를 던지는 가짜 conn."""

    HRANA_MSG = "Hrana: api error: status=404 Not Found (stream not found)"

    def execute(self, *_a, **_kw):
        raise ValueError(self.HRANA_MSG)

    def commit(self):
        pass  # put_payload 분기에서 commit 은 execute 예외로 도달 안 하지만 안전하게 no-op

    def close(self):
        pass  # store fixture teardown(s.close() → conn.close()) 을 위한 no-op


@pytest.mark.skipif(
    ValueError not in storage_mod._DB_ERRORS,
    reason="libsql_experimental 없는 환경 — ValueError 가 _DB_ERRORS 에 없어 스킵",
)
def test_put_payload_and_put_state_swallow_libsql_valueerror(store):
    """_put_payload / put_state 가 libsql ValueError 를 삼켜 외부로 전파하지 않음.

    실사고 회귀: except sqlite3.Error 만 잡았을 때 Hrana ValueError 가 누출되어
    build_universe 전체가 192번째 심볼에서 크래시한 사건.
    """
    # 정상 conn 을 libsql 스타일 ValueError 를 던지는 stub 으로 교체
    store._conn = _StubConnRaisesValueError()

    # _put_payload (put_json 경유) — 예외가 밖으로 새지 않아야 함
    store.put_json("ns", "smoke_key", {"v": 1})         # 예외 없으면 통과

    # put_state — 동일 보장
    store.put_state("ns", "smoke_state", {"v": 2})      # 예외 없으면 통과


@pytest.mark.skipif(
    ValueError not in storage_mod._DB_ERRORS,
    reason="libsql_experimental 없는 환경 — ValueError 가 _DB_ERRORS 에 없어 스킵",
)
def test_get_payload_and_get_state_swallow_libsql_valueerror(store):
    """읽기 가드(_get_payload / get_state)도 libsql ValueError 를 삼킴 — 쓰기와 대칭.

    _get_payload 는 이미 _DB_ERRORS 를 쓰고 있었으나, 대칭 계약을 명시적으로
    검증해 나중에 가드가 좁아지는 회귀를 막는다.
    """
    store._conn = _StubConnRaisesValueError()

    # 읽기 경로 — None 반환이어야 하고 예외가 새면 안 됨
    result_df = store.get_dataframe("ns", "k", TTL)
    assert result_df is None

    result_state = store.get_state("ns", "k")
    assert result_state is None


# ---------------- 연결 상태 API (WP-OPS-CORE): init_sync / degraded / require_remote ----------------
# 호출부(다이제스트 강등 보류·export 가드·build_universe)가 '원격과 맞춰진 연결인가' 를
# 구분하는 계약. 실 libsql 동작은 서버 스모크로 확인 (여기선 경로별 값만).


def _fake_libsql(monkeypatch):
    import sys
    import types

    class _Conn:
        def sync(self):
            return None

        def executescript(self, *_a):
            return None

        def commit(self):
            return None

        def close(self):
            return None

    mod = types.ModuleType("libsql_experimental")
    mod.connect = lambda *a, **k: _Conn()
    monkeypatch.setitem(sys.modules, "libsql_experimental", mod)


def test_init_sync_local_when_turso_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "plain.db"))
    s = Storage()
    try:
        assert s.init_sync == "local" and s.degraded is False
        s.require_remote("t")                            # Turso 미설정 → 통과
    finally:
        s.close()


def test_init_sync_local_when_path_injected_even_with_url(tmp_path, monkeypatch):
    prev = _turso_env(tmp_path, monkeypatch)
    try:
        s = Storage(db_path=tmp_path / "injected.db")     # 테스트 주입 경로는 항상 로컬
        try:
            assert s.init_sync == "local"
            s.require_remote("t")                        # 주입 경로 → 원격 요구 안 함
        finally:
            s.close()
    finally:
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


def test_init_sync_offline_killswitch(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_OFFLINE", "1")
    prev = _turso_env(tmp_path, monkeypatch)
    try:
        s = Storage()
        try:
            assert s.init_sync == "offline"
            assert s.degraded is False                   # 의도된 우회 — 강등 아님
            with pytest.raises(storage_mod.StorageError):
                s.require_remote("build_universe")
        finally:
            s.close()
    finally:
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


def test_init_sync_timeout_is_degraded_and_requires_remote_fails(tmp_path, monkeypatch):
    _fake_libsql(monkeypatch)                            # import 확인만 통과 (프로브가 타임아웃)
    monkeypatch.setattr(Storage, "_probe_initial_sync", staticmethod(lambda *a, **k: "timeout"))
    prev = _turso_env(tmp_path, monkeypatch)
    try:
        s = Storage()
        try:
            assert s.init_sync == "timeout"
            assert s.degraded is True
            assert s.db_path.name == "replica.offline.db"
            with pytest.raises(storage_mod.StorageError, match="timeout"):
                s.require_remote("export_dashboard")
        finally:
            s.close()
    finally:
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


@pytest.mark.parametrize("probe,ok", [("ok", True), ("error", False)])
def test_init_sync_ok_or_error_with_libsql(tmp_path, monkeypatch, probe, ok):
    _fake_libsql(monkeypatch)
    monkeypatch.setattr(Storage, "_probe_initial_sync", staticmethod(lambda *a, **k: probe))
    prev = _turso_env(tmp_path, monkeypatch)
    try:
        s = Storage()
        assert s.is_turso is True
        assert s.init_sync == probe
        assert s.degraded is False                       # error = stale 레플리카, 강등 아님
        if ok:
            s.require_remote("t")
        else:
            with pytest.raises(storage_mod.StorageError, match="error"):
                s.require_remote("t")
    finally:
        object.__setattr__(storage_mod.settings, "turso_database_url", prev)


def test_put_state_returns_bool(store):
    import sqlite3

    assert store.put_state("ns", "k", {"v": 1}) is True

    class _Broken:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self):
            return None

        def close(self):
            return None

    store._conn = _Broken()
    assert store.put_state("ns", "k", {"v": 2}) is False      # 예외 대신 False


@pytest.mark.skipif(
    ValueError not in storage_mod._DB_ERRORS,
    reason="libsql_experimental 없는 환경 — ValueError 가 _DB_ERRORS 에 없어 스킵",
)
def test_put_state_returns_false_on_libsql_valueerror(store):
    store._conn = _StubConnRaisesValueError()
    assert store.put_state("ns", "k", {"v": 1}) is False


def test_sync_returns_status_strings(store, monkeypatch):
    assert store.sync() == "local"                        # 로컬 sqlite3 → no-op

    store.is_turso = True
    object.__setattr__(storage_mod.settings, "turso_database_url", "libsql://x.turso.io")
    monkeypatch.setattr(storage_mod, "_remote_reachable", lambda url, timeout=5.0: False)
    assert store.sync() == "unreachable"

    monkeypatch.setattr(storage_mod, "_remote_reachable", lambda url, timeout=5.0: True)
    monkeypatch.setattr(store, "_safe_sync", lambda conn, what: "ok")
    assert store.sync() == "ok"
    monkeypatch.setattr(store, "_safe_sync", lambda conn, what: "error")
    assert store.sync() == "error"
    monkeypatch.setattr(store, "_safe_sync", lambda conn, what: "timeout")
    assert store.sync() == "timeout"
    assert store.sync() == "skipped-degraded"             # 타임아웃 뒤엔 push 스킵


def test_sync_timeout_default_stays_20(monkeypatch):
    """기본 SYNC_TIMEOUT 20s 는 유지 — 유닛별 값은 systemd/CI env 로 명시."""
    monkeypatch.delenv("QUANT_BOT_SYNC_TIMEOUT", raising=False)
    assert storage_mod._sync_timeout() == 20.0


# ---------------- stale-if-error (cached.stale) · get_state(strict) — WP-SIGNAL ----------------


class _StubConnRaisesSqliteError:
    """execute() 가 sqlite3.OperationalError 를 던지는 가짜 conn (DB 장애 모사)."""

    def execute(self, *_a, **_kw):
        import sqlite3

        raise sqlite3.OperationalError("database is locked")

    def close(self):
        pass


def _age_cache_by(monkeypatch, delta):
    from datetime import datetime, timezone

    future = datetime.now(timezone.utc) + delta
    monkeypatch.setattr(storage_mod, "_utcnow", lambda: future)


def test_cached_stale_returns_expired_value_with_fetched_at(cache_on, monkeypatch):
    calls = {"n": 0}

    @cached("stale_ns", TTL, "series")
    def fetch(series_id: str, start: str | None = None) -> pd.Series:
        calls["n"] += 1
        idx = pd.date_range("2026-10-01", periods=2).as_unit("ns")
        return pd.Series([1.0, 2.0], index=idx, name=series_id)

    fetch("T10Y2Y")                                   # 정규 호출로 캐시 채움
    _age_cache_by(monkeypatch, timedelta(hours=5))    # TTL(1h) 경과 → 정규 조회는 miss
    hit = fetch.stale("T10Y2Y", max_age=timedelta(days=7))
    assert hit is not None
    value, fetched_at = hit
    assert list(value) == [1.0, 2.0] and value.name == "T10Y2Y"
    assert fetched_at.tzinfo is not None
    # 키는 정규 호출과 동일 — positional/keyword 혼용 무관
    assert fetch.stale(series_id="T10Y2Y", start=None, max_age=timedelta(days=7)) is not None
    assert fetch.stale("NOPE", max_age=timedelta(days=7)) is None
    assert calls["n"] == 1                            # stale 은 원본을 부르지 않음


def test_cached_stale_respects_max_age_and_cache_off(cache_on, monkeypatch):
    @cached("stale_ns2", TTL, "json")
    def fetch(x: str) -> dict:
        return {"x": x}

    fetch("a")
    _age_cache_by(monkeypatch, timedelta(days=8))
    assert fetch.stale("a", max_age=timedelta(days=7)) is None     # 7일 초과
    hit = fetch.stale("a", max_age=timedelta(days=9))
    assert hit is not None and hit[0] == {"x": "a"}
    monkeypatch.setenv("QUANT_BOT_CACHE", "off")
    assert fetch.stale("a", max_age=timedelta(days=30)) is None    # 캐시 off → None


def test_any_age_getters_on_db_error_return_none(store):
    store._conn = _StubConnRaisesSqliteError()
    assert store.get_series_any_age("ns", "k") is None
    assert store.get_dataframe_any_age("ns", "k") is None
    assert store.get_json_any_age("ns", "k") is None


def test_get_state_strict_distinguishes_missing_from_unreadable(store):
    assert store.get_state("signals", "global:v2", strict=True) is None   # 없음 = 첫 실행
    store.put_state("signals", "global:v2", {"v": 2})
    assert store.get_state("signals", "global:v2", strict=True) == {"v": 2}

    store._conn = _StubConnRaisesSqliteError()
    assert store.get_state("signals", "global:v2") is None                # 기본: 종전 동작
    with pytest.raises(storage_mod.StorageError):
        store.get_state("signals", "global:v2", strict=True)


def test_get_state_strict_raises_on_corrupt_payload(store):
    store._conn.execute(
        "INSERT INTO state(namespace, key, payload, updated_at) VALUES (?, ?, ?, ?)",
        ("signals", "bad", "{not json", "2026-10-08T00:00:00+00:00"),
    )
    assert store.get_state("signals", "bad") is None
    with pytest.raises(storage_mod.StorageError):
        store.get_state("signals", "bad", strict=True)
