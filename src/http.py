"""
src/http.py
===========
프로젝트 표준 HTTP 레이어 (3단계 리팩토링).

제공하는 것
-----------
1. ``get_http_session()`` — 프로젝트 표준 ``requests.Session`` 싱글톤.
   - **Retry/backoff**: 429·5xx 같은 transient 에러에 exponential backoff
     (즉시 → 2s → 4s, 최대 3회 — urllib3 2.x 는 첫 재시도 backoff 가 0).
     ``Retry-After`` 헤더 존중.
   - **타임아웃 강제**: 호출부가 timeout 을 안 줘도 connect 5s / read 25s 적용.
   - **연결 풀링**: 같은 host 재호출 시 keep-alive 재사용.
2. ``mask_secrets(text)`` — 텍스트 안의 알려진 API 키를 ``***REDACTED***`` 로 치환.
3. ``SecretMaskingFilter`` — 로그 메시지·traceback 에서 API 키 노출 차단.
   이 모듈을 import 하는 순간 루트 로거의 모든 핸들러에 자동 장착됩니다
   (의도된 side-effect — logger.py 의 import 시 자동 설정과 같은 정책).

사용 예::

    from src.http import get_http_session

    session = get_http_session()
    resp = session.get(url, params={...})   # timeout/retry 자동 적용

주의
----
- Retry 는 ``raise_on_status=False`` 로 설정되어 있어, 재시도가 소진되면
  예외 대신 **마지막 response 를 그대로 반환**합니다. 상태 코드 → 도메인 예외
  변환은 호출부(예: ``data_fetcher._fmp_get``)의 책임입니다.
- 재시도는 GET/HEAD 에만 적용 (멱등성 보장).
- fredapi 는 내부적으로 ``urllib.request.urlopen`` 을 써서 이 세션을 주입할 수
  없습니다 (fredapi 0.5.2 기준). FRED 호출은 도메인 예외 wrapping 만 적용.
"""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, ReadTimeoutError
from urllib3.util.retry import Retry

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

# (connect, read) 초 — 모든 HTTP 호출에 강제 적용되는 기본값
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 25.0)

# Retry 정책: 즉시 → 2s → 4s
# (urllib3 2.x 공식: backoff_factor * 2^(연속실패-1), 단 첫 재시도는 0)
RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 1.0
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)

REDACTED = "***REDACTED***"

# settings 에서 마스킹 대상으로 삼을 키 속성들
_SECRET_SETTINGS_ATTRS = (
    "fmp_api_key",
    "fred_api_key",
    "anthropic_api_key",
    "minimax_api_key",     # NVIDIA NIM(MiniMax) LLM 키
    "news_api_key",
    "telegram_bot_token",  # URL 경로에 들어가므로 마스킹 필수
    "krx_api_key",         # AUTH_KEY 헤더로 전송 — 로그 노출 방지
    "turso_auth_token",    # Turso DB 토큰
    "dart_api_key",        # DART 전자공시 키
)

# 오탐 방지: 이 길이 미만의 값은 마스킹 대상에서 제외
# (실수로 .env 에 "test" 같은 값이 들어있을 때 로그 전체가 망가지는 것 방지)
_MIN_SECRET_LEN = 8


def _secret_values() -> tuple[str, ...]:
    """settings 에 실제로 설정된 (충분히 긴) 키 값들만 추림."""
    values = []
    for attr in _SECRET_SETTINGS_ATTRS:
        value = getattr(settings, attr, "")
        if value and len(value) >= _MIN_SECRET_LEN:
            values.append(value)
    return tuple(values)


def mask_secrets(text: str) -> str:
    """텍스트 안에 등장하는 알려진 API 키를 전부 ***REDACTED*** 로 치환."""
    for value in _secret_values():
        text = text.replace(value, REDACTED)
    return text


class SecretMaskingFilter(logging.Filter):
    """로그 record 의 메시지와 traceback 에서 API 키를 가리는 필터.

    핸들러에 부착해서 사용합니다 (로거가 아니라 핸들러에 붙여야
    어떤 로거에서 발생한 record 든 출력 직전에 전부 거쳐감).

    traceback 마스킹 원리: Formatter 는 ``record.exc_text`` 가 이미 채워져
    있으면 ``exc_info`` 를 다시 포맷하지 않음 (CPython logging 표준 동작).
    그래서 여기서 미리 포맷 + 마스킹해 ``exc_text`` 에 넣어둡니다.
    """

    _exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = ()

        if record.exc_text:
            record.exc_text = mask_secrets(record.exc_text)
        elif record.exc_info and record.exc_info != (None, None, None):
            record.exc_text = mask_secrets(
                self._exc_formatter.formatException(record.exc_info)
            )
        return True


def install_secret_masking() -> None:
    """루트 로거의 모든 핸들러에 SecretMaskingFilter 부착 (idempotent)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, SecretMaskingFilter) for f in handler.filters):
            handler.addFilter(SecretMaskingFilter())


class _TimeoutHTTPAdapter(HTTPAdapter):
    """호출부가 timeout 을 명시하지 않으면 기본 timeout 을 강제하는 어댑터."""

    def __init__(self, *args, timeout: tuple[float, float] = DEFAULT_TIMEOUT, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):  # noqa: ANN001 — requests 시그니처 그대로
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


def build_session(
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retry_total: int = RETRY_TOTAL,
    backoff_factor: float = RETRY_BACKOFF_FACTOR,
) -> requests.Session:
    """retry + 타임아웃 정책이 적용된 새 Session 생성.

    일반 코드는 ``get_http_session()`` 싱글톤을 쓰세요.
    이 함수는 테스트/데모에서 다른 정책의 세션이 필요할 때를 위해 분리.
    """
    retry = Retry(
        total=retry_total,
        backoff_factor=backoff_factor,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("GET", "HEAD"),  # 멱등 메서드만 재시도
        respect_retry_after_header=True,
        raise_on_status=False,  # 소진 시 마지막 response 반환 → 호출부가 도메인 예외로 변환
    )
    adapter = _TimeoutHTTPAdapter(max_retries=retry, timeout=timeout)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def is_timeout(exc: BaseException) -> bool:
    """requests 예외가 본질적으로 '타임아웃'인지 판별.

    Retry 가 개입하면 read timeout 이 urllib3 의 ``MaxRetryError`` 로 감싸여
    ``requests.exceptions.ConnectionError`` 로 표면화됩니다 (Timeout 아님!).
    그래서 타입 검사만으로는 타임아웃/접속실패를 구분할 수 없고,
    원인 체인의 reason 까지 까봐야 합니다. 도메인 예외 변환부에서 사용.
    """
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    cause = exc.args[0] if exc.args else None
    if isinstance(cause, MaxRetryError):
        return isinstance(cause.reason, (ReadTimeoutError, ConnectTimeoutError))
    return False


_session: requests.Session | None = None


def get_http_session() -> requests.Session:
    """프로젝트 표준 HTTP 세션 (싱글톤, 연결 풀 공유)."""
    global _session
    if _session is None:
        _session = build_session()
        logger.debug(
            "표준 HTTP 세션 생성: timeout=%s retry_total=%d backoff=%s status_forcelist=%s",
            DEFAULT_TIMEOUT,
            RETRY_TOTAL,
            RETRY_BACKOFF_FACTOR,
            RETRY_STATUS_FORCELIST,
        )
    return _session


# import 즉시 로그 마스킹 활성화 — 이 모듈을 쓰는 모든 코드가 보호받도록.
install_secret_masking()
