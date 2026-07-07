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
    mode = "ok"               # "ok" | "error" | "parse_fail" | "forbidden"
    last_payload: dict = {}
    last_path = ""            # 호출된 메서드 확인용 (…/answerCallbackQuery 등)
    calls = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).last_payload = payload
        type(self).last_path = self.path
        type(self).calls += 1
        mode = type(self).mode
        if mode == "parse_fail" and "parse_mode" in payload:
            # Markdown 일 때만 실패 (텔레그램 parse-entities 오류 모사), 평문 재시도는 성공
            body = {"ok": False,
                    "description": "Bad Request: can't parse entities: Can't find end of the entity"}
            code = 400
        elif mode == "error":
            body = {"ok": False, "description": "Bad Request: chat not found"}
            code = 400
        elif mode == "forbidden":
            body = {"ok": False, "description": "Forbidden: bot was blocked by the user"}
            code = 403
        else:
            body = {"ok": True, "result": {"message_id": 1}}
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):  # noqa: ANN001
        pass


@pytest.fixture
def telegram_server(monkeypatch):
    _TgHandler.mode = "ok"
    _TgHandler.calls = 0
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


# ---------------- Markdown 파싱 실패 → 평문 폴백 ----------------


def test_markdown_parse_failure_falls_back_to_plain(telegram_creds, telegram_server):
    telegram_server.mode = "parse_fail"
    # username 의 '_' 처럼 동적 콘텐츠가 Markdown 을 깨뜨리는 상황
    result = send_telegram("가입 요청: john_doe", parse_mode="Markdown")
    assert result == {"message_id": 1}                       # 평문 재전송 성공
    assert telegram_server.calls == 2                        # md 시도 → 평문 재시도
    assert "parse_mode" not in telegram_server.last_payload  # 재시도는 평문


def test_any_400_retries_plain_once_then_raises(telegram_creds, telegram_server):
    # 판정이 status_code==400 기반 — 파싱 무관 400(chat not found)도 평문 1회 재시도 후
    # 같은 400 이라 결국 실패 전파 (동작 결과는 이전과 동일: 예외).
    telegram_server.mode = "error"
    with pytest.raises(ApiHttpError):
        send_telegram("hi", parse_mode="Markdown")
    assert telegram_server.calls == 2                        # md 시도 → 평문 재시도 → 실패


def test_non_400_error_does_not_retry(telegram_creds, telegram_server):
    telegram_server.mode = "forbidden"                       # 403 — 파싱 실패 아님
    with pytest.raises(ApiHttpError):
        send_telegram("hi", parse_mode="Markdown")
    assert telegram_server.calls == 1                        # 폴백 재시도 없음


def test_plain_message_400_does_not_retry(telegram_creds, telegram_server):
    telegram_server.mode = "error"                           # 400 이지만 평문 전송
    with pytest.raises(ApiHttpError):
        send_telegram("hi", parse_mode=None)
    assert telegram_server.calls == 1                        # parse_mode 없음 → 재시도 없음


def test_plain_mode_omits_parse_mode(telegram_creds, telegram_server):
    assert send_safe("john_doe 가입 (chat_id=5)", chat_id="100", parse_mode=None) is True
    assert telegram_server.calls == 1                        # 평문은 한 번에 성공
    assert "parse_mode" not in telegram_server.last_payload


def test_reply_markup_included_in_payload(telegram_creds, telegram_server):
    kb = {"keyboard": [["🇺🇸 미국 추천"]], "resize_keyboard": True}
    send_telegram("메뉴", reply_markup=kb)
    assert telegram_server.last_payload["reply_markup"] == kb


def test_inline_keyboard_reply_markup_in_payload(telegram_creds, telegram_server):
    """가입 승인 알림용 인라인 키보드가 sendMessage payload 에 그대로 실리는지."""
    kb = {"inline_keyboard": [[{"text": "✅ 승인", "callback_data": "approve:100"},
                               {"text": "❌ 거절", "callback_data": "deny:100"}]]}
    send_telegram("🔔 가입 요청", reply_markup=kb, parse_mode=None)
    assert telegram_server.last_payload["reply_markup"] == kb
    assert telegram_server.last_path.endswith("/sendMessage")


# ---------------- answerCallbackQuery / editMessageText (인라인 버튼 플로우) ----------------


def test_answer_callback_query_payload(telegram_creds, telegram_server):
    assert notifier_mod.answer_callback_query("cbq_1", text="승인 완료") is True
    assert telegram_server.last_path.endswith("/answerCallbackQuery")
    assert telegram_server.last_payload["callback_query_id"] == "cbq_1"
    assert telegram_server.last_payload["text"] == "승인 완료"
    assert "show_alert" not in telegram_server.last_payload      # 기본은 토스트


def test_answer_callback_query_truncates_toast_to_200(telegram_creds, telegram_server):
    notifier_mod.answer_callback_query("cbq_1", text="x" * 300)
    assert len(telegram_server.last_payload["text"]) == 200      # 텔레그램 토스트 제한


def test_answer_callback_query_error_raises(telegram_creds, telegram_server):
    telegram_server.mode = "error"
    with pytest.raises(ApiHttpError):
        notifier_mod.answer_callback_query("cbq_1", text="hi")


def test_answer_callback_safe_swallows_error(telegram_creds, telegram_server):
    telegram_server.mode = "error"
    assert notifier_mod.answer_callback_safe("cbq_1", "hi") is False   # 예외 없이 False


def test_answer_callback_safe_without_token(no_telegram):
    assert notifier_mod.answer_callback_safe("cbq_1") is False


def test_edit_message_text_payload(telegram_creds, telegram_server):
    notifier_mod.edit_message_text("999", 42, "🔔 가입 요청\n\n→ ✅ 승인됨")
    assert telegram_server.last_path.endswith("/editMessageText")
    p = telegram_server.last_payload
    assert p["chat_id"] == "999" and p["message_id"] == 42
    assert p["text"].endswith("→ ✅ 승인됨")
    assert "parse_mode" not in p                    # 기본 평문
    assert "reply_markup" not in p                  # 생략 = 인라인 키보드 제거 (텔레그램 동작)


def test_edit_message_markdown_400_falls_back_to_plain(telegram_creds, telegram_server):
    telegram_server.mode = "parse_fail"
    result = notifier_mod.edit_message_text("999", 42, "john_doe 처리", parse_mode="Markdown")
    assert result == {"message_id": 1}              # 평문 재시도 성공
    assert telegram_server.calls == 2
    assert "parse_mode" not in telegram_server.last_payload


def test_edit_message_safe_swallows_error(telegram_creds, telegram_server):
    telegram_server.mode = "error"
    assert notifier_mod.edit_message_safe("999", 42, "x") is False


def test_generic_request_exception_becomes_domain_error(monkeypatch):
    """Timeout/ConnectionError 외 requests 예외(ChunkedEncodingError 등)도
    도메인 예외로 변환 — send_safe 를 뚫고 루프를 죽이던 회귀."""
    import requests

    import src.notifier as notifier_mod
    from src.exceptions import ApiConnectionError

    class _FakeSession:
        def post(self, *a, **k):
            raise requests.exceptions.ChunkedEncodingError("연결 끊김")

    monkeypatch.setattr(notifier_mod, "get_http_session", lambda: _FakeSession())
    backup = (settings.telegram_bot_token, settings.telegram_chat_id)
    _set("123:fake", "1")
    try:
        with pytest.raises(ApiConnectionError):
            notifier_mod._telegram_post("sendMessage", {"chat_id": "1", "text": "x"})
        # send_safe 는 이제 이 실패를 삼키고 False (크래시 없음)
        assert notifier_mod.send_safe("x", "1") is False
    finally:
        _set(*backup)
