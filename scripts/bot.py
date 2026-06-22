"""
scripts/bot.py
==============
Phase 11b — 상시 인터랙티브 봇 (폴링 워커).

상시 호스트(Oracle 등)에서 systemd 서비스로 구동. getUpdates long-poll 로 메시지를 받아
매 메시지를 라우팅:
  · 조회 명령(/stock·/scan·/help) → bot_commands.respond → 응답 전송
  · 구독 명령(/start·/stop·/approve·/deny·/pending) → subscribers.apply_events
무거운 분석은 cron 이 Turso 에 사전계산 → 이 봇은 **DB 읽기 위주(경량)**.

⚠️ 이 봇이 getUpdates 를 소유하므로, 같이 도는 다이제스트 cron 은 구독 동기화를 끄고
   발송만 해야 함 (offset 경합 방지): `python scripts/send_digest.py --no-sync`.

실행:  python scripts/bot.py            (Ctrl+C 로 종료)
설정:  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (+ TURSO_*) 필요
"""

from __future__ import annotations

import time

from src import bot_commands, subscribers
from src.exceptions import DataFetchError, MissingApiKeyError
from src.logger import get_logger
from src.notifier import get_updates, send_safe
from src.storage import get_storage

logger = get_logger(__name__)

LONG_POLL_SEC = 20          # http 세션 read timeout(25s)보다 작게
ERROR_BACKOFF_SEC = 3       # getUpdates 실패 시 잠깐 쉬고 재시도
RATE_MAX_CALLS = 8          # 유저별
RATE_WINDOW_SEC = 60.0


def _extract(update: dict) -> tuple[str | None, str]:
    """update → (chat_id 문자열, 텍스트). 메시지/텍스트 없으면 (None, "")."""
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    return (str(chat_id) if chat_id is not None else None), text


def run() -> int:
    store = get_storage()
    subscribers.ensure_owner()                       # 소유자 항상 active
    limiter = bot_commands.RateLimiter(max_calls=RATE_MAX_CALLS, window_sec=RATE_WINDOW_SEC)
    offset = store.get_state(subscribers._OFFSET_NS, subscribers._OFFSET_KEY)
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

        # 1) 조회 명령 즉답 (rate limit 적용)
        for u in updates:
            chat_id, text = _extract(u)
            if not chat_id or not text:
                continue
            try:
                reply = bot_commands.respond(text, chat_id, limiter)
                if reply:
                    send_safe(reply, chat_id)
            except Exception:  # noqa: BLE001 — poison 메시지가 봇을 크래시 루프시키지 않도록
                logger.exception("명령 처리 실패 (chat=%s) — 건너뜀", chat_id)

        # 2) 구독 명령 처리 (조회 명령은 parse_updates 가 "ignore" 로 흘림)
        events, next_offset = subscribers.parse_updates(updates)
        try:
            subscribers.apply_events(events)
        except Exception:  # noqa: BLE001 — 한 배치 처리 실패가 루프를 죽이지 않도록
            logger.exception("구독 이벤트 처리 실패 — 계속 진행")

        # 3) offset 전진 + 클라우드 동기화 (처리한 메시지 재수신 방지)
        if next_offset is not None:
            offset = next_offset
            store.put_state(subscribers._OFFSET_NS, subscribers._OFFSET_KEY, offset)
        store.sync()


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        logger.info("봇 종료 (KeyboardInterrupt)")
        return 0
    except MissingApiKeyError as e:
        logger.error("필수 설정 누락 — 봇 시작 불가: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
