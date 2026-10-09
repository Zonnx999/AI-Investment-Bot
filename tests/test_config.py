"""src/config.py — 환경변수 파서 헬퍼 (네트워크·DB 없음).

Settings 는 import 시점에 고정되는 frozen dataclass 라 여기서는 파서 함수만 직접 검증한다.
"""

from __future__ import annotations

import logging

import pytest

from src import config


@pytest.mark.parametrize("raw,expected", [
    ("", "on"), ("on", "on"), ("OWNER", "owner"), (" off ", "off"),
])
def test_env_choice_accepts_allowed_values_case_insensitive(monkeypatch, raw, expected):
    monkeypatch.setenv("QUANT_BOT_LEDGER_LINE", raw)
    assert config._env_choice("QUANT_BOT_LEDGER_LINE", "on", ("on", "owner", "off")) == expected


def test_env_choice_typo_falls_back_to_default_with_warning(monkeypatch, caplog):
    """D7: 오타(onwer)가 조용히 '성과 줄 없음' 이 되지 않게 — 'on' 폴백 + WARNING."""
    monkeypatch.setenv("QUANT_BOT_LEDGER_LINE", "onwer")
    with caplog.at_level(logging.WARNING, logger="src.config"):
        assert config._env_choice("QUANT_BOT_LEDGER_LINE", "on", ("on", "owner", "off")) == "on"
    assert any("QUANT_BOT_LEDGER_LINE" in r.getMessage() and "onwer" in r.getMessage()
               for r in caplog.records)


def test_settings_ledger_line_is_valid():
    assert config.settings.ledger_line in ("on", "owner", "off")


def test_env_int_and_float_fallbacks(monkeypatch):
    monkeypatch.setenv("X_INT", "abc")
    monkeypatch.setenv("X_FLOAT", "")
    assert config._env_int("X_INT", 7) == 7
    assert config._env_float("X_FLOAT", -10.0) == -10.0
    monkeypatch.setenv("X_INT", "12")
    assert config._env_int("X_INT", 7) == 12
