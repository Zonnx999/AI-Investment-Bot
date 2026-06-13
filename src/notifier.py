"""
src/notifier.py
===============
Phase 7 — 알림 전송 (텔레그램).

단방향 push 만 필요하므로 무거운 `python-telegram-bot` 대신 표준 HTTP 세션
(src/http.py) 으로 Telegram Bot API 에 직접 POST. 새 의존성 0.

- `send_telegram(text)` — 설정된 채팅으로 메시지 전송 (Markdown)
- `get_updates()` — chat_id 발견용 (scripts/telegram_setup.py 에서 사용)

봇 토큰은 URL 경로에 들어가므로 http.py 의 SecretMaskingFilter 마스킹 대상에
등록돼 있음 (로그/traceback 노출 차단).
"""

from __future__ import annotations

from typing import Any

from src.config import settings
from src.exceptions import (
    ApiConnectionError,
    ApiHttpError,
    ApiTimeoutError,
    DataFetchError,
    MissingApiKeyError,
)
from src.http import RETRY_TOTAL, get_http_session, is_timeout
from src.logger import get_logger

logger = get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
# 텔레그램 메시지 최대 길이 (4096) — 여유 두고 자름
MAX_MESSAGE_LEN = 3900


def _telegram_post(method: str, payload: dict) -> dict:
    """Telegram Bot API POST 공통. 토큰 검증 + 도메인 예외 변환.

    Raises
    ------
    MissingApiKeyError   TELEGRAM_BOT_TOKEN 미설정
    ApiHttpError         텔레그램이 4xx/5xx (ok=false 포함)
    ApiTimeoutError / ApiConnectionError   네트워크 실패
    """
    import requests

    token = settings.require("telegram_bot_token")  # raises MissingApiKeyError
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"

    try:
        response = get_http_session().post(url, json=payload)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if is_timeout(e):
            raise ApiTimeoutError(
                f"텔레그램 {method} 타임아웃 (재시도 {RETRY_TOTAL}회 포함)", source="Telegram"
            ) from e
        raise ApiConnectionError(f"텔레그램 {method} 연결 실패", source="Telegram") from e

    # 텔레그램은 에러도 200 이 아닌 코드 + JSON {ok:false, description} 로 줌
    try:
        body = response.json()
    except ValueError as e:
        raise ApiHttpError(
            f"텔레그램 {method}: 비정상 응답 (HTTP {response.status_code})",
            status_code=response.status_code, source="Telegram",
        ) from e

    if not body.get("ok", False):
        # description 에 토큰은 없지만 방어적으로 마스킹은 logger 가 처리
        raise ApiHttpError(
            f"텔레그램 {method} 실패: {body.get('description', '알 수 없음')}",
            status_code=response.status_code, source="Telegram",
        )
    return body["result"]


def send_telegram(
    text: str,
    chat_id: str | None = None,
    parse_mode: str = "Markdown",
    disable_preview: bool = True,
) -> dict:
    """설정된 채팅으로 메시지 전송.

    chat_id 미지정 시 settings.telegram_chat_id 사용.
    4096자 초과 시 안전 길이로 자름 (텔레그램 제한).
    """
    cid = chat_id or settings.require("telegram_chat_id")
    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN] + "\n…(생략)"
    result = _telegram_post("sendMessage", {
        "chat_id": cid,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    })
    logger.info("텔레그램 전송 완료 (chat=%s, %d자)", cid, len(text))
    return result


def get_updates() -> list[dict[str, Any]]:
    """봇이 받은 최근 메시지들 (chat_id 발견용).

    토큰만 있으면 호출 가능 (chat_id 불필요). 봇에게 메시지를 한 번 보낸 뒤
    호출하면 그 메시지의 chat.id 를 여기서 찾을 수 있음.
    """
    return _telegram_post("getUpdates", {})


def send_safe(text: str) -> bool:
    """예외를 삼키는 best-effort 전송 (배치/cron 용).

    알림 실패가 데이터 파이프라인을 죽이지 않도록 — 실패 시 로그만 남기고 False.
    """
    try:
        send_telegram(text)
        return True
    except MissingApiKeyError as e:
        logger.warning("텔레그램 미설정 — 전송 생략 (%s)", e)
        return False
    except DataFetchError:
        logger.exception("텔레그램 전송 실패")
        return False
