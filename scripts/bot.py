"""
scripts/bot.py
==============
Phase 11b — 상시 인터랙티브 봇 (폴링 워커).

상시 호스트(Oracle 등)에서 systemd 서비스로 구동. getUpdates long-poll 로 메시지를 받아
매 메시지를 라우팅:
  · 조회 명령(/stock·/scan·/help) → bot_commands.respond → 응답 전송
  · 구독 명령(/start·/stop·/approve·/deny·/pending) → subscribers.apply_events
  · callback_query(인라인 [승인][거절] 버튼) → bot_commands.handle_callback
무거운 분석은 cron 이 Turso 에 사전계산 → 이 봇은 **DB 읽기 위주(경량)**.

⚠️ 이 봇이 getUpdates 를 소유하므로, 같이 도는 다이제스트 cron 은 구독 동기화를 끄고
   발송만 해야 함 (offset 경합 방지): `python scripts/send_digest.py --no-sync`.

실행:  python scripts/bot.py            (Ctrl+C 로 종료)
설정:  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (+ TURSO_*) 필요
"""

from __future__ import annotations

import time
from typing import NoReturn

from src import bot_commands, subscribers
from src.config import settings
from src.exceptions import DataFetchError, MissingApiKeyError
from src.logger import get_logger
from src.notifier import get_updates, send_safe
from src.storage import get_storage

logger = get_logger(__name__)

LONG_POLL_SEC = 20          # http 세션 read timeout(25s)보다 작게
ERROR_BACKOFF_SEC = 3       # getUpdates 실패 시 잠깐 쉬고 재시도
RATE_MAX_CALLS = 8          # 유저별
RATE_WINDOW_SEC = 60.0


_GATE_MSG = "🔒 구독자 전용입니다. /start 로 가입 요청 후 소유자 승인을 받으면 이용할 수 있어요."


def _extract(update: dict) -> tuple[str | None, str]:
    """update → (chat_id 문자열, 텍스트). 메시지/텍스트 없으면 (None, "")."""
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    return (str(chat_id) if chat_id is not None else None), text


def _is_subscriber(chat_id: str, owner: str | None) -> bool:
    """active 구독자 또는 소유자면 True (조회 명령 접근 제어). 소유자는 승인 없이 허용.

    subscribers.subscriber_status 는 프로세스 단일 connection 재사용 (스키마 초기화 1회) —
    메시지마다 Turso 왕복을 만들지 않음.
    """
    if owner and chat_id == owner:
        return True
    return subscribers.subscriber_status(chat_id) == "active"


def run() -> NoReturn:
    """폴링 루프 — 정상 흐름에선 반환하지 않음 (종료는 예외로만: Ctrl+C 등)."""
    store = get_storage()
    subscribers.ensure_owner()                       # 소유자 항상 active
    owner = str(settings.telegram_chat_id) if settings.telegram_chat_id else None
    limiter = bot_commands.RateLimiter(max_calls=RATE_MAX_CALLS, window_sec=RATE_WINDOW_SEC)
    offset = subscribers.get_updates_offset()
    logger.info("봇 시작 (offset=%s, long-poll=%ds)", offset, LONG_POLL_SEC)

    while True:
        try:
            updates = get_updates(offset=offset, timeout=LONG_POLL_SEC)
        except DataFetchError as e:
            logger.warning("getUpdates 실패 — %ds 후 재시도: %s", ERROR_BACKOFF_SEC, e)
            time.sleep(ERROR_BACKOFF_SEC)
            continue

        if not updates:
            continue

        # 0) 버튼(reply keyboard) 라벨 → 명령으로 정규화 — 조회·구독 파싱이 모두 같은 명령을
        #    보도록 raw update 의 text 를 치환 (탭 = 명령 입력과 동일 흐름)
        for u in updates:
            msg = u.get("message") or u.get("edited_message")
            if msg and msg.get("text") in bot_commands.BUTTON_TO_COMMAND:
                msg["text"] = bot_commands.BUTTON_TO_COMMAND[msg["text"]]

        # 1) 콜백 쿼리 (인라인 [승인][거절] 버튼) — 소유자 검증·응답·편집은 handle_callback 이
        #    전담. per-update try/except 로 poison callback 이 루프를 죽이지 않게 함.
        for u in updates:
            cq = u.get("callback_query")
            if not cq:
                continue
            try:
                bot_commands.handle_callback(cq, owner)
            except Exception:  # noqa: BLE001 — poison 콜백이 봇을 크래시 루프시키지 않도록
                logger.exception("콜백 처리 실패 (update_id=%s) — 건너뜀", u.get("update_id"))

        # 2) 조회 명령 즉답 — /stock·/scan 은 active 구독자(또는 소유자)만, /help·/menu 는 공개
        for u in updates:
            chat_id, text = _extract(u)
            if not chat_id or not text:
                continue
            try:
                cmd = bot_commands.parse_command(text)
                if cmd.kind in ("stock", "scan") and not _is_subscriber(chat_id, owner):
                    if limiter.allow(chat_id):       # 게이트 안내도 rate limit (남용 방지)
                        send_safe(_GATE_MSG, chat_id)
                    continue
                reply = bot_commands.respond(text, chat_id, limiter)
                if reply:
                    # help/menu 응답에 버튼 메뉴 부착 (소유자는 관리자 버튼 포함)
                    kb = (bot_commands.main_keyboard(owner is not None and chat_id == owner)
                          if cmd.kind == "help" else None)
                    # /news 는 의도적 평문([site]·URL 포함) — Markdown 으로 보내면
                    # 매번 400 → 평문 폴백으로 전송이 2배가 되므로 처음부터 평문.
                    pm = None if cmd.kind == "news" else "Markdown"
                    send_safe(reply, chat_id, parse_mode=pm, reply_markup=kb)
            except Exception:  # noqa: BLE001 — poison 메시지가 봇을 크래시 루프시키지 않도록
                logger.exception("명령 처리 실패 (chat=%s) — 건너뜀", chat_id)

        # 3) 구독 명령 처리 (조회 명령은 parse_updates 가 "ignore" 로 흘림 —
        #    callback_query update 는 메시지가 없어 이벤트 없이 offset 만 전진)
        events, next_offset = subscribers.parse_updates(updates)
        try:
            # 상시 봇만 인라인 버튼 부착 — callback_query 를 이 루프가 처리하므로.
            subscribers.apply_events(events, interactive_buttons=True)
        except Exception:  # noqa: BLE001 — 한 배치 처리 실패가 루프를 죽이지 않도록
            logger.exception("구독 이벤트 처리 실패 — 계속 진행")

        # 4) offset 전진 + 클라우드 동기화 (처리한 메시지 재수신 방지)
        if next_offset is not None:
            offset = next_offset
            subscribers.set_updates_offset(offset)
        store.sync()


def main() -> int:
    try:
        run()                                        # NoReturn — 예외로만 빠져나옴
    except KeyboardInterrupt:
        logger.info("봇 종료 (KeyboardInterrupt)")
        return 0
    except MissingApiKeyError as e:
        logger.error("필수 설정 누락 — 봇 시작 불가: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
