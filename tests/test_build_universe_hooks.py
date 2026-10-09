"""scripts/build_universe.py 호출 순서 — 재점수 후 스냅샷 전에 sync (RVOPS-05). 네트워크 없음."""

from __future__ import annotations

import pytest

import src.storage as storage_mod
from scripts import build_universe as bu
from src import ledger, universe


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "hooks.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


def test_sync_runs_before_rescore_snapshot(fresh_db, monkeypatch):
    calls: list[str] = []
    store = storage_mod.get_storage()
    monkeypatch.setattr(type(store), "require_remote", lambda self, what: None, raising=False)
    monkeypatch.setattr(type(store), "sync", lambda self: calls.append("sync"))

    ok = {"enriched": 1, "no_data": 0, "failed": 0, "aborted": False}
    monkeypatch.setattr(universe, "discover", lambda **k: {"US": 1})
    monkeypatch.setattr(universe, "enrich", lambda **k: dict(ok))
    monkeypatch.setattr(universe, "enrich_kr", lambda **k: dict(ok))
    monkeypatch.setattr(universe, "symbols_needing_enrichment", lambda *a, **k: [])
    monkeypatch.setattr(universe, "_kr_symbols_needing_enrichment", lambda *a, **k: [])
    monkeypatch.setattr(ledger, "ensure_pre_snapshot", lambda *a, **k: {})

    def snap(kind, **k):
        calls.append(f"snapshot:{kind}")
        return {}

    monkeypatch.setattr(ledger, "snapshot_universe", snap)
    monkeypatch.setattr("sys.argv", ["build_universe.py"])
    assert bu.main() == bu.EXIT_OK
    # snapshot 직전에 sync 가 있어야 하고, 마지막(push) sync 도 유지
    i = calls.index("snapshot:rescore")
    assert i > 0 and calls[i - 1] == "sync"
    assert calls[-1] == "sync"
