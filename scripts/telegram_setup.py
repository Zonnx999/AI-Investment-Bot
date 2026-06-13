"""
scripts/telegram_setup.py
=========================
텔레그램 chat_id 찾기 도우미 (Phase 7 셋업).

사용 순서:
  1) .env 에 TELEGRAM_BOT_TOKEN 을 먼저 채운다 (@BotFather 에서 발급)
  2) 텔레그램 앱에서 방금 만든 봇을 찾아 아무 메시지나 보낸다 (예: "hi")
  3) python scripts/telegram_setup.py  실행
  4) 출력된 chat_id 를 .env 의 TELEGRAM_CHAT_ID 에 붙여넣는다
  5) python scripts/telegram_setup.py --test  로 실제 전송 확인

토큰만 있으면 동작 (chat_id 불필요).
"""

from __future__ import annotations

import argparse

from src.config import settings
from src.exceptions import MissingApiKeyError, QuantBotError
from src.logger import get_logger
from src.notifier import get_updates, send_telegram

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="텔레그램 chat_id 셋업 도우미")
    parser.add_argument("--test", action="store_true",
                        help=".env 의 chat_id 로 테스트 메시지 전송")
    args = parser.parse_args()

    try:
        settings.require("telegram_bot_token")
    except MissingApiKeyError:
        print("❌ .env 에 TELEGRAM_BOT_TOKEN 이 없습니다.")
        print("   1) 텔레그램에서 @BotFather → /newbot → 토큰 복사")
        print("   2) .env 의 TELEGRAM_BOT_TOKEN= 뒤에 붙여넣기")
        return 1

    if args.test:
        try:
            send_telegram("✅ AI-Investment-Bot 연결 테스트 성공!\n매일 아침 신호를 여기로 보냅니다.")
            print("✅ 테스트 메시지 전송 완료 — 텔레그램을 확인하세요.")
            return 0
        except QuantBotError as e:
            print(f"❌ 전송 실패: {e}")
            print("   TELEGRAM_CHAT_ID 가 올바른지 확인하세요.")
            return 1

    # chat_id 발견 모드
    try:
        updates = get_updates()
    except QuantBotError as e:
        print(f"❌ getUpdates 실패: {e}")
        return 1

    if not updates:
        print("⚠️ 받은 메시지가 없습니다.")
        print("   텔레그램 앱에서 봇에게 아무 메시지나 먼저 보낸 뒤 다시 실행하세요.")
        return 1

    # 가장 최근 메시지의 chat 정보 추출
    seen: dict[str, str] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat", {})
        if "id" in chat:
            name = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            seen[str(chat["id"])] = name

    print("발견된 chat_id:")
    print("-" * 50)
    for cid, name in seen.items():
        print(f"  {cid}   ({name})")
    print("-" * 50)
    print("위 숫자를 .env 의 TELEGRAM_CHAT_ID= 에 붙여넣으세요.")
    print("그 다음:  python scripts/telegram_setup.py --test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
