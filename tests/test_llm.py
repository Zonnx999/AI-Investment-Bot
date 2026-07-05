"""src/llm.py — LLM 요약 (오프라인: 로컬 서버 + monkeypatch, 네트워크 불필요)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import src.llm as llm_mod
from src.config import settings
from src.exceptions import (
    ApiAuthError,
    ApiAuthorizationError,
    ApiConnectionError,
    ApiHttpError,
    ApiTimeoutError,
    DataValidationError,
    MissingApiKeyError,
    RateLimitError,
)
from src.http import REDACTED, mask_secrets
from src.llm import MAX_SUMMARY_CHARS, llm_enabled, summarize, summarize_safe

FAKE_KEY = "test_fake_minimax_key_0123456789abcdef"
DIGEST = "*📊 일일 투자 신호*\n국면: 위험선호\nNVDA 종합 64"


def _set_key(value: str):
    # settings 는 frozen dataclass → object.__setattr__ 우회 (conftest 와 동일 패턴)
    object.__setattr__(settings, "minimax_api_key", value)


@pytest.fixture
def minimax_key():
    backup = settings.minimax_api_key
    _set_key(FAKE_KEY)
    yield FAKE_KEY
    _set_key(backup)


@pytest.fixture
def no_minimax_key():
    backup = settings.minimax_api_key
    _set_key("")
    yield
    _set_key(backup)


DEFAULT_CONTENT = "오늘은 위험선호 국면이 이어졌습니다. 반도체 발굴 종목이 상단에 올라왔습니다."


class _NimHandler(BaseHTTPRequestHandler):
    """OpenAI-chat 호환 응답 모사 (NVIDIA NIM)."""

    mode = "ok"       # ok | auth | forbidden | ratelimit | server_error | not_json
                      # | no_choices | empty_content
    content = DEFAULT_CONTENT
    calls = 0
    last_payload: dict = {}
    last_auth = ""

    def do_POST(self):  # noqa: N802
        cls = type(self)
        length = int(self.headers.get("Content-Length", 0))
        cls.last_payload = json.loads(self.rfile.read(length) or b"{}")
        cls.last_auth = self.headers.get("Authorization", "")
        cls.calls += 1

        code, body = 200, None
        if cls.mode == "auth":
            code, body = 401, {"error": "invalid api key"}
        elif cls.mode == "forbidden":
            code, body = 403, {"error": "no access"}
        elif cls.mode == "ratelimit":
            code, body = 429, {"error": "rate limited"}
        elif cls.mode == "server_error":
            code, body = 500, {"error": "boom"}
        elif cls.mode == "not_json":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>oops</html>")
            return
        elif cls.mode == "no_choices":
            body = {"id": "cmpl-1", "object": "chat.completion"}
        elif cls.mode == "empty_content":
            body = {"choices": [{"message": {"role": "assistant", "content": "   "}}]}
        else:
            body = {"choices": [{"message": {"role": "assistant", "content": cls.content}}]}

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):  # noqa: ANN001 — 테스트 출력 오염 방지
        pass


@pytest.fixture(scope="module")
def _llm_http_server():
    """모듈당 서버 1개 (테스트별 0.5s shutdown 대기 방지 — 상태는 llm_server 가 리셋)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NimHandler)
    threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    ).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def llm_server(_llm_http_server):
    _NimHandler.mode = "ok"
    _NimHandler.calls = 0
    _NimHandler.content = DEFAULT_CONTENT
    backup = settings.minimax_base_url
    object.__setattr__(settings, "minimax_base_url", _llm_http_server)
    yield _NimHandler
    object.__setattr__(settings, "minimax_base_url", backup)


# ---------------- 정상 경로 ----------------


def test_summarize_happy_path(minimax_key, llm_server):
    out = summarize(DIGEST)
    assert out == llm_server.content
    # 요청 형태: OpenAI chat 호환 + Bearer 헤더 + 다이제스트 원문 전달
    assert llm_server.last_auth == f"Bearer {FAKE_KEY}"
    assert llm_server.last_payload["model"] == settings.minimax_model
    assert llm_server.last_payload["messages"][1]["content"] == DIGEST
    assert llm_server.last_payload["max_tokens"] == llm_mod.LLM_MAX_TOKENS
    assert llm_server.last_payload["temperature"] == llm_mod.LLM_TEMPERATURE


def test_summarize_normalizes_whitespace(minimax_key, llm_server):
    llm_server.content = "  오늘은\n\n조용한   하루였습니다.  "
    assert summarize(DIGEST) == "오늘은 조용한 하루였습니다."


def test_summarize_truncates_long_content(minimax_key, llm_server):
    llm_server.content = "가" * (MAX_SUMMARY_CHARS + 200)
    out = summarize(DIGEST)
    assert len(out) == MAX_SUMMARY_CHARS
    assert out.endswith("…")


def test_summarize_safe_happy_path(minimax_key, llm_server):
    llm_server.content = "요약 한 줄."
    assert summarize_safe(DIGEST) == "요약 한 줄."


# ---------------- 키 미설정 / 킬스위치 (HTTP 호출 자체가 없어야 함) ----------------


def test_summarize_missing_key_raises_without_http_call(no_minimax_key, llm_server):
    with pytest.raises(MissingApiKeyError):
        summarize(DIGEST)
    assert llm_server.calls == 0


def test_summarize_safe_missing_key_returns_none_without_http_call(no_minimax_key, llm_server):
    assert summarize_safe(DIGEST) is None
    assert llm_server.calls == 0


@pytest.mark.parametrize("value", ["0", "off", "false", " OFF "])
def test_kill_switch_disables_llm(monkeypatch, minimax_key, llm_server, value):
    monkeypatch.setenv("QUANT_BOT_LLM", value)
    assert llm_enabled() is False
    assert summarize_safe(DIGEST) is None
    assert llm_server.calls == 0


def test_llm_enabled_by_default(monkeypatch):
    monkeypatch.delenv("QUANT_BOT_LLM", raising=False)
    assert llm_enabled() is True


# ---------------- HTTP 에러 → 도메인 예외 ----------------


@pytest.mark.parametrize("mode,exc", [
    ("auth", ApiAuthError),
    ("forbidden", ApiAuthorizationError),
    ("ratelimit", RateLimitError),
    ("server_error", ApiHttpError),
])
def test_summarize_http_errors_raise_domain_exceptions(minimax_key, llm_server, mode, exc):
    llm_server.mode = mode
    with pytest.raises(exc):
        summarize(DIGEST)


def test_summarize_http_error_keeps_status_code(minimax_key, llm_server):
    llm_server.mode = "server_error"
    with pytest.raises(ApiHttpError) as exc:
        summarize(DIGEST)
    assert exc.value.status_code == 500


# ---------------- 응답 스키마 방어 ----------------


@pytest.mark.parametrize("mode", ["not_json", "no_choices", "empty_content"])
def test_summarize_bad_response_shape_raises_validation(minimax_key, llm_server, mode):
    llm_server.mode = mode
    with pytest.raises(DataValidationError):
        summarize(DIGEST)


# ---------------- 네트워크 실패 (세션 monkeypatch — 서버 불필요) ----------------


class _FailingSession:
    def __init__(self, exc: Exception):
        self._exc = exc

    def post(self, *a, **kw):  # noqa: ANN001
        raise self._exc


def test_summarize_timeout_raises_domain_error(monkeypatch, minimax_key):
    monkeypatch.setattr(
        llm_mod, "get_http_session",
        lambda: _FailingSession(requests.exceptions.ReadTimeout("slow")),
    )
    with pytest.raises(ApiTimeoutError):
        summarize(DIGEST)


def test_summarize_connection_error_raises_domain_error(monkeypatch, minimax_key):
    monkeypatch.setattr(
        llm_mod, "get_http_session",
        lambda: _FailingSession(requests.exceptions.ConnectionError("refused")),
    )
    with pytest.raises(ApiConnectionError):
        summarize(DIGEST)


# ---------------- summarize_safe: 어떤 실패든 None (다이제스트 불가침) ----------------


@pytest.mark.parametrize("mode", ["auth", "ratelimit", "server_error", "not_json", "no_choices"])
def test_summarize_safe_returns_none_on_any_failure(minimax_key, llm_server, mode):
    llm_server.mode = mode
    assert summarize_safe(DIGEST) is None


def test_summarize_safe_swallows_unexpected_error(monkeypatch, minimax_key):
    monkeypatch.setattr(
        llm_mod, "get_http_session",
        lambda: _FailingSession(RuntimeError("정말 예상 밖")),
    )
    assert summarize_safe(DIGEST) is None


# ---------------- 키 마스킹 (§4.9) ----------------


def test_minimax_key_is_masked_by_http_layer(minimax_key):
    text = f"Authorization: Bearer {FAKE_KEY} 로 호출 실패"
    masked = mask_secrets(text)
    assert FAKE_KEY not in masked
    assert REDACTED in masked


def test_summarize_error_messages_never_contain_key(minimax_key, llm_server):
    llm_server.mode = "auth"
    with pytest.raises(ApiAuthError) as exc:
        summarize(DIGEST)
    assert FAKE_KEY not in str(exc.value)


# ---------------- 리뷰 회귀: reasoning 태그 / Markdown 문자 제거 ----------------


def test_summarize_strips_think_blocks(minimax_key, llm_server):
    """reasoning 모델의 <think> 블록은 사용자에게 노출되면 안 됨."""
    llm_server.content = "<think>사고 과정 blah blah</think>오늘 시장은 조용했습니다."
    assert llm_mod.summarize(DIGEST) == "오늘 시장은 조용했습니다."


def test_summarize_strips_unclosed_think_block(minimax_key, llm_server):
    """max_tokens 절단 등으로 닫는 태그가 없는 <think> — 이후 전부 버림."""
    llm_server.content = "요약 문장입니다. <think>잘린 사고 과정"
    assert llm_mod.summarize(DIGEST) == "요약 문장입니다."


def test_summarize_strips_markdown_entities(minimax_key, llm_server):
    """*·_·`·[ 가 남으면 Markdown 다이제스트 전체가 파싱 실패(평문 폴백·전송 2배)."""
    llm_server.content = "오늘 *시장* 은 _조용_ 했고 `BTC` 는 [강세]였다."
    out = llm_mod.summarize(DIGEST)
    assert not any(ch in out for ch in "*_`[")
    assert "시장" in out and "강세]" in out            # 내용 자체는 보존 (여는 [ 만 제거)


def test_summarize_reasoning_only_response_is_failure(minimax_key, llm_server):
    """<think> 블록뿐인 응답 → 정규화 후 빈 문자열 = DataValidationError → safe 는 None."""
    llm_server.content = "<think>결론을 못 냈다</think>"
    with pytest.raises(DataValidationError):
        llm_mod.summarize(DIGEST)
    assert llm_mod.summarize_safe(DIGEST) is None
