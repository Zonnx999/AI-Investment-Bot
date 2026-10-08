"""
scripts/send_digest.py
======================
일일 다이제스트(v2)를 텔레그램으로 전송 — systemd 타이머(quant-digest@kr / @us)가 호출.

실행:
    python scripts/send_digest.py --market kr            # 구독 동기화 → 전 구독자 브로드캐스트
    python scripts/send_digest.py --market us --dry-run  # 전송·쓰기 없이 터미널에만 출력
    python scripts/send_digest.py --no-sync              # 구독 동기화(getUpdates) 건너뛰고 발송만
                                                         #   (봇 프로세스가 돌고 있으면 필수 — offset 경합)

종료코드 (T3b §4 — deploy/systemd/quant-digest@.service 의 SuccessExitStatus=10,
scripts/notify_owner.py 의 '20 이면 중복 DM 생략' 과 짝):
    0  전원 발송 / dry-run 성공          10 부분 전송 실패        11 전원 전송 실패
    12 수신자 0                          20 강등 보류 + 소유자 DM 완료
    21 강등 보류 + 소유자 DM 실패        30 필수 설정 누락(ConfigError)
    1  예기치 못한 크래시(파이썬 기본)    2 argparse 사용법 오류(기본)

LLM 요약·예측·제안 비중은 환경변수 플래그(QUANT_BOT_LLM / QUANT_BOT_DIGEST_PREDICTIONS /
QUANT_BOT_DIGEST_WEIGHTS, 기본 0)로만 켠다 — 예전 --no-llm/--no-weights 옵션은 제거.

가입(소유자 승인제): 친구가 봇에게 /start → 소유자가 /approve <chat_id> → 다음 실행부터 수신.
"""

from __future__ import annotations

import argparse

from src.digest import (
    EXIT_ALL_FAIL,
    EXIT_CONFIG_MISSING,
    EXIT_HOLD_DM_FAILED,
    EXIT_HOLD_NOTIFIED,
    EXIT_NO_RECIPIENTS,
    EXIT_OK,
    EXIT_PARTIAL_FAIL,
    assemble_daily_digest,
    send_daily_digest,
)
from src.exceptions import ConfigError
from src.logger import get_logger

logger = get_logger(__name__)

_EXIT_MESSAGES = {
    EXIT_PARTIAL_FAIL: "⚠️ 일부 전송 실패 — logs/quant_bot.log 확인",
    EXIT_ALL_FAIL: "❌ 전원 전송 실패 — 토큰/네트워크 확인",
    EXIT_NO_RECIPIENTS: "⚠️ 구독자 없음 — TELEGRAM_CHAT_ID(소유자) 또는 /start 가입 확인",
    EXIT_HOLD_NOTIFIED: "⏸ 발송 보류 (강등/후보 0) — 소유자 DM 완료",
    EXIT_HOLD_DM_FAILED: "⏸ 발송 보류 (강등/후보 0) — 소유자 DM 도 실패",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="일일 다이제스트 전송 (멀티유저 브로드캐스트)")
    parser.add_argument("--market", choices=["us", "kr"], default="us",
                        help="다이제스트 시장 (us=미국 저녁, kr=한국 아침)")
    parser.add_argument("--dry-run", action="store_true",
                        help="전송·쓰기 없이 미리보기만 (state·원장·헬스 모두 불변)")
    parser.add_argument("--top", type=int, default=6, help="스크리닝 후보 상위 N개 (기본 6)")
    parser.add_argument("--no-sync", action="store_true",
                        help="구독 동기화(getUpdates) 건너뛰고 발송만")
    args = parser.parse_args(argv)

    try:
        if args.dry_run:
            bundle = assemble_daily_digest(args.market, top_n=args.top)
            print(bundle.text)  # 미리보기 — stdout deliverable
            if bundle.hold:
                print(f"\n(dry-run) 실발송이었다면 보류: {bundle.hold_reason}")
            return EXIT_OK

        # 1) 명령 수거 → 요청/승인/거절/해지 처리 (best-effort)
        if not args.no_sync:
            from src.subscribers import sync_subscribers
            sub = sync_subscribers()
            if any(sub.values()):
                print(f"구독 동기화: 요청 {sub['requests']} / 승인 {sub['approved']} "
                      f"/ 거절 {sub['denied']} / 해지 {sub['unsubscribed']}")

        # 2) 전 구독자에게 브로드캐스트 (시장별)
        result = send_daily_digest(market=args.market, top_n=args.top)
    except ConfigError as e:
        # TELEGRAM_* 등 필수 설정 누락 — 크래시(1)와 구분되는 종료코드 (SRV-01)
        logger.error("필수 설정 누락 — %s", e)
        print(f"❌ 설정 누락: {e}")
        return EXIT_CONFIG_MISSING

    code = int(result.get("exit_code", EXIT_OK))
    if code == EXIT_OK:
        print(f"✅ [{args.market}] 브로드캐스트 완료: {result['sent']}명 전송")
    else:
        print(f"{_EXIT_MESSAGES.get(code, '⚠️ 비정상 종료')} "
              f"(전송 {result['sent']} / 실패 {result['failed']} / 대상 {result['recipients']}, exit {code})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
