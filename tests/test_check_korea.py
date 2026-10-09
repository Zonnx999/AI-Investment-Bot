"""scripts/check_korea.py — 출력 표 (합성 데이터, 네트워크 없음)."""

from __future__ import annotations

import numpy as np
import pandas as pd

import scripts.check_korea as ck


def _trade_frame(n: int = 14) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="MS")
    return pd.DataFrame({
        "수출(금액, USD)": np.linspace(5.0e10, 6.0e10, n),
        "수입(금액, USD)": np.linspace(4.5e10, 5.0e10, n),
    }, index=idx)


def test_check_korea_table_has_no_trade_balance_column(monkeypatch, capsys):
    monkeypatch.setattr(ck, "fetch_korea_trade", lambda: _trade_frame())
    ck.main()
    out = capsys.readouterr().out
    assert "무역수지" not in out                      # 시리즈가 제거돼 항상 '—' 이던 열
    header = next(line for line in out.splitlines() if line.startswith("월"))
    assert "수출" in header and "수입" in header
    rows = [line for line in out.splitlines() if line.startswith(("2025-", "2026-"))]
    assert len(rows) == 12 and all("—" not in line for line in rows)   # 모든 행이 값으로 채워짐
    assert "전년동월대비" in out and "수출(금액, USD)" in out


def test_check_korea_empty_frame_returns_quietly(monkeypatch, capsys):
    monkeypatch.setattr(ck, "fetch_korea_trade", lambda: pd.DataFrame())
    ck.main()                                        # 예외 없이 종료
    assert "최근 12개월" not in capsys.readouterr().out
