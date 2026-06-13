"""src/storage.py — state 테이블 (TTL 없는 영속 상태)."""

from __future__ import annotations

import pytest

from src.storage import Storage


@pytest.fixture
def store(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "state.db")
    yield s
    s.close()


def test_state_roundtrip(store):
    store.put_state("signals", "last_run", {"regime": "🟢", "vols": {"NVDA": 50.0}})
    assert store.get_state("signals", "last_run") == {"regime": "🟢", "vols": {"NVDA": 50.0}}


def test_state_missing_returns_none(store):
    assert store.get_state("signals", "nope") is None


def test_state_overwrite(store):
    store.put_state("ns", "k", {"v": 1})
    store.put_state("ns", "k", {"v": 2})
    assert store.get_state("ns", "k") == {"v": 2}


def test_state_has_no_ttl_unlike_cache(store):
    # state 는 만료 개념이 없음 — get_state 는 max_age 인자조차 받지 않음
    store.put_state("ns", "k", {"v": 1})
    assert store.get_state("ns", "k") == {"v": 1}


def test_local_backend_when_no_turso(store):
    # Turso 미설정(conftest 가 비움) → 로컬 sqlite3, sync() 는 no-op (예외 없이 통과)
    assert store.is_turso is False
    store.sync()  # no-op, 예외 안 남


def test_injected_db_path_never_uses_turso(tmp_path, monkeypatch):
    # db_path 를 명시 주입하면 .env 에 TURSO 가 있어도 로컬 (테스트 안전장치)
    from src.config import settings
    from src.storage import Storage

    object.__setattr__(settings, "turso_database_url", "libsql://fake.turso.io")
    s = Storage(db_path=tmp_path / "x.db")
    assert s.is_turso is False
    s.close()
    object.__setattr__(settings, "turso_database_url", "")
