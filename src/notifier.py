"""
src/notifier.py
===============
Phase 7 — 알림 전송 (텔레그램).

단방향 push 만 필요하므로 무거운 `python-telegram-bot` 대신 표준 HTTP 세션
(src/http.py) 으로 Telegram Bot API 에 직접 POST. 새 의존성 0.

- `send_telegram(text)` — 설정된 채팅으로 메시지 전송 (Markdown)
- `get_updates()` — chat_id 발견용 (scripts/telegram_setup.py 에서 사용)
- `answer_callback_query` / `edit_message_text` — 인라인 버튼(callback_query) 플로우
  (Phase 11b 가입 승인 버튼). `*_safe` 변형은 예외를 삼키는 best-effort (폴링 루프용).

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
    except requests.exceptions.RequestException as e:
        # 그 외 requests 예외(ChunkedEncodingError 등)도 도메인 예외로 — 안 잡으면
        # send_safe 의 DataFetchError 캐치를 뚫고 브로드캐스트/봇 루프가 크래시함
        raise ApiConnectionError(f"텔레그램 {method} 요청 실패", source="Telegram") from e

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
    reply_markup: dict | None = None,
) -> dict:
    """설정된 채팅으로 메시지 전송.

    chat_id 미지정 시 settings.telegram_chat_id 사용.
    4096자 초과 시 안전 길이로 자름 (텔레그램 제한).
    reply_markup: 버튼(ReplyKeyboardMarkup/InlineKeyboardMarkup) dict — 있으면 그대로 전송.
    """
    cid = chat_id or settings.require("telegram_chat_id")
    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN] + "\n…(생략)"

    def _post(pm: str | None) -> dict:
        payload: dict = {"chat_id": cid, "text": text,
                         "disable_web_page_preview": disable_preview}
        if pm:
            payload["parse_mode"] = pm
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return _telegram_post("sendMessage", payload)

    try:
        result = _post(parse_mode)
    except ApiHttpError as e:
        # Markdown 파싱 실패(불균형 엔티티 등) → 평문으로 재전송해 메시지 유실 방지.
        # legacy Markdown 은 백슬래시 이스케이프를 제대로 지원하지 않으므로, 동적 콘텐츠가
        # 서식을 깨뜨릴 때 가장 견고한 대응은 서식을 포기하고 평문으로 보내는 것.
        # 판정은 HTTP 400 기준 — 에러 문구("can't parse entities") 매칭은 텔레그램이
        # 문구를 바꾸면 조용히 깨지는 취약한 방식이라 제거. parse_mode 가 있는 메시지의
        # 400 은 대부분 파싱 실패이고, 아니어도(chat not found 등) 평문 1회 재시도 후
        # 같은 예외로 실패하므로 동작 변화 없음. 400 외(403/429/5xx)는 재시도 없이 전파.
        if parse_mode and e.status_code == 400:
            logger.warning("sendMessage 400 (Markdown 파싱 실패 추정) — 평문으로 재전송 "
                           "(chat=%s): %s", cid, e)
            result = _post(None)
        else:
            raise
    logger.info("텔레그램 전송 완료 (chat=%s, %d자)", cid, len(text))
    return result


def answer_callback_query(callback_query_id: str, text: str | None = None,
                          show_alert: bool = False) -> bool:
    """인라인 버튼 탭(callback_query)에 응답 — 텔레그램은 **모든** 콜백에 응답을 요구.

    text 는 토스트(작은 팝업)로 표시. 텔레그램 제한(200자)에 맞춰 자름.
    실패는 도메인 예외(send_telegram 과 동일 계열) — 삼키려면 answer_callback_safe 사용.
    """
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]
    if show_alert:
        payload["show_alert"] = True
    _telegram_post("answerCallbackQuery", payload)   # result 는 boolean true
    return True


def edit_message_text(chat_id: str, message_id: int, text: str,
                      parse_mode: str | None = None,
                      reply_markup: dict | None = None) -> dict:
    """기존 메시지 본문 교체 (editMessageText). 인라인 버튼 제거/결과 표기에 사용.

    reply_markup 미지정 시 기존 인라인 키보드가 **제거**됨 (텔레그램 동작 — 버튼을
    없애며 결과를 남기는 승인/거절 플로우에 그대로 부합).
    parse_mode 지정 시 400 응답이면 평문으로 1회 재시도 (send_telegram 과 동일 정책).
    """
    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN] + "\n…(생략)"

    def _post(pm: str | None) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if pm:
            payload["parse_mode"] = pm
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return _telegram_post("editMessageText", payload)

    try:
        return _post(parse_mode)
    except ApiHttpError as e:
        if parse_mode and e.status_code == 400:
            logger.warning("editMessageText 400 (Markdown 파싱 실패 추정) — 평문으로 재시도 "
                           "(chat=%s): %s", chat_id, e)
            return _post(None)
        raise


def answer_callback_safe(callback_query_id: str, text: str | None = None,
                         show_alert: bool = False) -> bool:
    """예외를 삼키는 best-effort 콜백 응답 (폴링 루프용 — 실패해도 루프를 죽이지 않음)."""
    try:
        return answer_callback_query(callback_query_id, text=text, show_alert=show_alert)
    except MissingApiKeyError as e:
        logger.warning("텔레그램 미설정 — 콜백 응답 생략 (%s)", e)
        return False
    except DataFetchError:
        logger.exception("answerCallbackQuery 실패")
        return False


def edit_message_safe(chat_id: str, message_id: int, text: str,
                      parse_mode: str | None = None,
                      reply_markup: dict | None = None) -> bool:
    """예외를 삼키는 best-effort 메시지 편집 (버튼 제거 실패가 처리 자체를 막지 않도록)."""
    try:
        edit_message_text(chat_id, message_id, text,
                          parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except MissingApiKeyError as e:
        logger.warning("텔레그램 미설정 — 메시지 편집 생략 (%s)", e)
        return False
    except DataFetchError:
        logger.exception("editMessageText 실패 (chat=%s, message_id=%s)", chat_id, message_id)
        return False


def get_updates(offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
    """봇이 받은 최근 메시지들 (chat_id 발견 / 구독 명령 수거용).

    토큰만 있으면 호출 가능 (chat_id 불필요). 봇에게 메시지를 한 번 보낸 뒤
    호출하면 그 메시지의 chat.id 를 여기서 찾을 수 있음.

    offset: 이 update_id 이상만 반환 — 이전 것은 텔레그램 서버에서 확인 처리되어
            다음 호출부터 사라짐. cron 폴링(Phase 11a)이 '마지막 처리 update_id + 1'
            을 넘겨 같은 메시지를 두 번 처리하지 않도록 함.
    timeout: long-poll 대기 초 (0=즉시 반환). 상시 봇(Phase 11b)은 ~20s 로 long-poll 해
            유휴 시 요청 수를 줄임. ⚠️ http 세션 read timeout(25s)보다 작게 둘 것.
    """
    payload: dict[str, Any] = {}
    if offset is not None:
        payload["offset"] = offset
    if timeout:
        payload["timeout"] = timeout
    return _telegram_post("getUpdates", payload)


def send_safe(text: str, chat_id: str | None = None,
              parse_mode: str | None = "Markdown", reply_markup: dict | None = None) -> bool:
    """예외를 삼키는 best-effort 전송 (배치/cron/브로드캐스트 용).

    알림 실패가 데이터 파이프라인을 죽이지 않도록 — 실패 시 로그만 남기고 False.
    chat_id 미지정 시 settings.telegram_chat_id (소유자) 로 전송.
    parse_mode=None 이면 평문 (안내·관리 메시지처럼 서식이 불필요/위험할 때).
    reply_markup: 버튼 dict (선택).
    """
    try:
        send_telegram(text, chat_id=chat_id, parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except MissingApiKeyError as e:
        logger.warning("텔레그램 미설정 — 전송 생략 (%s)", e)
        return False
    except DataFetchError:
        logger.exception("텔레그램 전송 실패")
        return False
