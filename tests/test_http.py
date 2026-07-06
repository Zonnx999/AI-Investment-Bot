"""src/http.py — 마스킹 / 타임아웃 판별 / retry (로컬 서버, 네트워크 불필요)."""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from urllib3.exceptions import MaxRetryError, ReadTimeoutError

from src.http import REDACTED, SecretMaskingFilter, build_session, is_timeout, mask_secrets


# ---------- mask_secrets / SecretMaskingFilter ----------


def test_mask_secrets_redacts_known_key(fake_fmp_key):
    text = f"GET /quote?symbol=AAPL&apikey={fake_fmp_key} failed"
    masked = mask_secrets(text)
    assert fake_fmp_key not in masked
    assert REDACTED in masked


def test_mask_secrets_leaves_clean_text_alone(fake_fmp_key):
    assert mask_secrets("no secrets here") == "no secrets here"


def test_mask_secrets_redacts_minimax_key():
    """MiniMax(LLM) 키도 로그/traceback 에서 마스킹되어야 함 (#3)."""
    from src.config import settings

    fake = "test_fake_minimax_key_0123456789abcdef"
    backup = settings.minimax_api_key
    object.__setattr__(settings, "minimax_api_key", fake)
    try:
        masked = mask_secrets(f"calling NIM with key={fake}")
        assert fake not in masked and REDACTED in masked
    finally:
        object.__setattr__(settings, "minimax_api_key", backup)


def test_masking_filter_redacts_message_and_traceback(fake_fmp_key):
    f = SecretMaskingFilter()

    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1,
        f"url with key {fake_fmp_key}", args=(), exc_info=None,
    )
    assert f.filter(record) is True
    assert fake_fmp_key not in record.getMessage()

    try:
        raise ValueError(f"boom {fake_fmp_key}")
    except ValueError:
        import sys
        record2 = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "with exc", args=(), exc_info=sys.exc_info(),
        )
    f.filter(record2)
    assert record2.exc_text is not None
    assert fake_fmp_key not in record2.exc_text
    assert REDACTED in record2.exc_text


# ---------- is_timeout ----------


def test_is_timeout_true_for_plain_timeout():
    assert is_timeout(requests.exceptions.ReadTimeout("slow"))


def test_is_timeout_true_for_retry_wrapped_read_timeout():
    # Retry 개입 시 실제로 발생하는 모양 재현:
    # ConnectionError(MaxRetryError(reason=ReadTimeoutError))
    inner = ReadTimeoutError(None, "/x", "Read timed out")
    wrapped = requests.exceptions.ConnectionError(MaxRetryError(None, "/x", reason=inner))
    assert is_timeout(wrapped)


def test_is_timeout_false_for_plain_connection_error():
    assert not is_timeout(requests.exceptions.ConnectionError("refused"))


# ---------- retry / timeout 동작 (로컬 서버) ----------


class _Handler(BaseHTTPRequestHandler):
    flaky_hits = 0

    def do_GET(self):  # noqa: N802
        cls = type(self)
        if self.path == "/flaky":
            cls.flaky_hits += 1
            self.send_response(500 if cls.flaky_hits < 3 else 200)
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(1.0)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # noqa: ANN001 — 테스트 출력 오염 방지
        pass


@pytest.fixture
def local_server():
    _Handler.flaky_hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_session_retries_5xx_until_success(local_server):
    session = build_session(backoff_factor=0)  # 테스트 속도 위해 대기 없음
    resp = session.get(f"{local_server}/flaky")
    assert resp.status_code == 200
    assert _Handler.flaky_hits == 3


def test_session_enforces_default_timeout(local_server):
    session = build_session(timeout=(1.0, 0.2), retry_total=0)
    with pytest.raises((requests.exceptions.Timeout, requests.exceptions.ConnectionError)) as exc_info:
        session.get(f"{local_server}/slow")  # timeout 인자 없이 호출 — 세션이 강제해야 함
    assert is_timeout(exc_info.value)
