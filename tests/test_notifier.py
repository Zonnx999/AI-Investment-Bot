"""src/notifier.py — 토큰 검증 + 길이 제한 + best-effort (오프라인, 로컬 서버)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import src.notifier as notifier_mod
from src.config import settings
from src.exceptions import ApiHttpError, MissingApiKeyError
from src.notifier import MAX_MESSAGE_LEN, send_safe, send_telegram


def _set(token: str, chat: str):
    # settings 는 frozen dataclass → object.__setattr__ 우회 (conftest 와 동일 패턴)
    object.__setattr__(settings, "telegram_bot_token", token)
    object.__setattr__(settings, "telegram_chat_id", chat)


@pytest.fixture
def telegram_creds():
    backup = (settings.telegram_bot_token, settings.telegram_chat_id)
    _set("fake_token_123456", "999")
    yield
    _set(*backup)


@pytest.fixture
def no_telegram():
    backup = (settings.telegram_bot_token, settings.telegram_chat_id)
    _set("", "")
    yield
    _set(*backup)


class _TgHandler(BaseHTTPRequestHandler):
    mode = "ok"               # "ok" | "error"
    last_payload: dict = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).last_payload = json.loads(self.rfile.read(length) or b"{}")
        if type(self).mode == "ok":
            body = {"ok": True, "result": {"message_id": 1}}
            code = 200
        else:
            body = {"ok": False, "description": "Bad Request: chat not found"}
            code = 400
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):  # noqa: ANN001
        pass


@pytest.fixture
def telegram_server(monkeypatch):
    _TgHandler.mode = "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TgHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(notifier_mod, "TELEGRAM_API_BASE", base)
    yield _TgHandler
    server.shutdown()


# ---------------- 토큰/설정 검증 ----------------


def test_send_requires_token(no_telegram):
    with pytest.raises(MissingApiKeyError):
        send_telegram("hi")


def test_send_safe_returns_false_without_token(no_telegram):
    assert send_safe("hi") is False    # 예외 안 던지고 False


# ---------------- 정상 전송 ----------------


def test_send_telegram_posts_message(telegram_creds, telegram_server):
    send_telegram("안녕 *봇*")
    assert telegram_server.last_payload["chat_id"] == "999"
    assert telegram_server.last_payload["text"] == "안녕 *봇*"
    assert telegram_server.last_payload["parse_mode"] == "Markdown"


def test_long_message_is_truncated(telegram_creds, telegram_server):
    send_telegram("x" * (MAX_MESSAGE_LEN + 500))
    sent = telegram_server.last_payload["text"]
    assert len(sent) <= MAX_MESSAGE_LEN + len("\n…(생략)")
    assert sent.endswith("(생략)")


# ---------------- 에러 처리 ----------------


def test_api_error_raises(telegram_creds, telegram_server):
    telegram_server.mode = "error"
    with pytest.raises(ApiHttpError) as exc:
        send_telegram("hi")
    assert "chat not found" in str(exc.value)


def test_send_safe_swallows_api_error(telegram_creds, telegram_server):
    telegram_server.mode = "error"
    assert send_safe("hi") is False
