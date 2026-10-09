"""scripts/warm_replica.py + quant-universe 유닛 배선 (RVOPS-01, 오프라인)."""

from __future__ import annotations

from pathlib import Path

import scripts.warm_replica as wr
import src.storage as storage_mod

_ROOT = Path(__file__).resolve().parents[1]


def test_warm_replica_local_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "warm.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    try:
        assert wr.main() == 0
    finally:
        monkeypatch.setattr(storage_mod, "_storage", None)
    out = capsys.readouterr().out
    assert out.startswith("init_sync=") and "elapsed=" in out


def test_warm_replica_timeout_is_nonzero(monkeypatch, capsys):
    class _S:
        init_sync = "timeout"
        db_path = "x.offline.db"
    monkeypatch.setattr(wr, "get_storage", lambda: _S())
    assert wr.main() == 1
    assert "init_sync=timeout" in capsys.readouterr().out


def test_universe_unit_warms_digest_replica_under_digest_lock():
    """재점수 유닛이 digest_replica.db 를 다이제스트와 같은 flock 으로, 실패 무시('-')로 미리 pull."""
    unit = (_ROOT / "deploy" / "systemd" / "quant-universe.service").read_text(encoding="utf-8")
    post = [ln for ln in unit.splitlines() if ln.startswith("ExecStartPost=")]
    assert len(post) == 1
    line = post[0]
    assert line.startswith("ExecStartPost=-/usr/bin/flock -w 900 /home/ubuntu/AI-Investment-Bot/data/.digest.lock ")
    assert "QUANT_BOT_DB_PATH=/home/ubuntu/AI-Investment-Bot/data/digest_replica.db" in line
    assert "QUANT_BOT_SYNC_TIMEOUT=" in line and line.endswith("scripts/warm_replica.py")
    digest = (_ROOT / "deploy" / "systemd" / "quant-digest@.service").read_text(encoding="utf-8")
    assert "/home/ubuntu/AI-Investment-Bot/data/.digest.lock" in digest
