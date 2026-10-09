"""scripts/notify_owner.py — 소유자 평문 운영 알림 (네트워크·systemd·DB 없음)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import scripts.notify_owner as no
from src.config import settings
from src.http import REDACTED

_ROOT = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "123456:test_fake_telegram_token_abcdefghij"
NOW = datetime(2026, 10, 8, 8, 31, tzinfo=no.KST)


@pytest.fixture
def fake_token():
    backup = settings.telegram_bot_token
    object.__setattr__(settings, "telegram_bot_token", FAKE_TOKEN)
    yield FAKE_TOKEN
    object.__setattr__(settings, "telegram_bot_token", backup)


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(no, "send_safe", lambda text, **kw: calls.append((text, kw)) or True)
    return calls


def test_format_failure_masks_secrets_and_truncates(fake_token):
    tail = "\n".join(
        [f"Traceback line {i} https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage " + "x" * 400
         for i in range(40)]
    )
    text = no.format_failure("digest-kr", "quant-digest@kr.service",
                             {"Result": "exit-code", "ExecMainStatus": "1"}, tail, NOW)
    assert FAKE_TOKEN not in text
    assert REDACTED in text
    assert len(text) <= no.MAX_TEXT
    assert text.splitlines()[0] == "[quant-bot 실패] quant-digest@kr.service (digest-kr)"
    assert "결과: exit-code (exit 1)" in text
    # 줄당 200자 절단 + 꼬리 12줄만
    log_lines = [ln for ln in text.splitlines() if ln.startswith("Traceback line")]
    assert len(log_lines) <= no.TAIL_LINES
    assert all(len(ln) <= no.MAX_LINE for ln in log_lines)


def test_format_failure_truncates_to_3800_even_with_long_lines():
    tail = "\n".join("가" * 500 for _ in range(50))
    text = no.format_failure("bot", "quant-bot.service", {}, tail, NOW)
    assert len(text) <= 3800
    assert "결과: ? (exit ?)" in text


def test_failed_unit_skips_dm_when_app_already_notified(monkeypatch, sent):
    monkeypatch.setattr(no, "unit_status",
                        lambda unit: {"Result": "exit-code", "ExecMainStatus": "20"})
    monkeypatch.setattr(no, "_run", lambda cmd: pytest.fail("journal 을 읽으면 안 됨"))
    assert no.main(["--failed-unit", "digest-kr"]) == 0
    assert sent == []


def test_failed_unit_sends_plain_dm(monkeypatch, sent):
    monkeypatch.setattr(no, "unit_status",
                        lambda unit: {"Result": "exit-code", "ExecMainStatus": "1"})
    monkeypatch.setattr(no, "_run", lambda cmd: "boom\nlast line")
    assert no.main(["--failed-unit", "universe"]) == 0
    assert len(sent) == 1
    text, kw = sent[0]
    assert kw.get("parse_mode", "unset") is None              # 평문 (§4.10 #10)
    assert "quant-universe.service" in text and "last line" in text


def test_text_mode_sends_plain_masked(sent, fake_token):
    assert no.main(["--text", f"CI 실패 _x_ *y* token={FAKE_TOKEN}"]) == 0
    text, kw = sent[0]
    assert kw.get("parse_mode", "unset") is None
    assert FAKE_TOKEN not in text and "_x_ *y*" in text          # 평문이라 원문 그대로


def test_main_always_exits_zero_on_unexpected_error(monkeypatch):
    def boom(text, **kw):
        raise RuntimeError("예상 밖")

    monkeypatch.setattr(no, "send_safe", boom)
    assert no.main(["--text", "hi"]) == 0


def test_unit_status_parses_systemctl_show(monkeypatch):
    monkeypatch.setattr(no, "_run", lambda cmd: "Result=exit-code\nExecMainStatus=20")
    assert no.unit_status("quant-bot.service") == {"Result": "exit-code", "ExecMainStatus": "20"}
    monkeypatch.setattr(no, "_run", lambda cmd: "")             # systemctl 없음
    assert no.unit_status("quant-bot.service") == {}


def test_import_does_not_load_storage_or_pandas():
    """레플리카를 건드리지 않고(§4.10 #9) 1GB 박스에서 가볍게 — 서브프로세스로 확인."""
    code = (
        "import sys, scripts.notify_owner\n"
        "print('src.storage' in sys.modules, 'pandas' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True,
                         text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": str(_ROOT)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False"


def test_format_failure_masks_key_straddling_line_cut(fake_token):
    """SEC-01 회귀: 키가 MAX_LINE(200자) 경계에 걸쳐도 접두부가 새지 않아야 한다.

    줄을 먼저 자르고 마스킹하면 잘린 키는 전체 치환에 안 걸려 앞부분이 그대로 나간다.
    """
    # 키 앞부분이 6자 이상 잘려 남는 모든 위치 (짧은 접두부는 다른 문자열과 우연히 겹칠 수 있어 제외)
    for prefix_len in range(no.MAX_LINE - len(FAKE_TOKEN) + 1, no.MAX_LINE - 5):
        line = "u" * prefix_len + FAKE_TOKEN + "&file_type=json"
        text = no.format_failure("digest-kr", "quant-digest@kr.service", {}, line, NOW)
        head = FAKE_TOKEN[: no.MAX_LINE - prefix_len]
        assert head not in text, prefix_len
