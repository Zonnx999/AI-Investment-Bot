"""
scripts/warm_replica.py
=======================
레플리카 미리 당기기 (RVOPS-01) — QUANT_BOT_DB_PATH 의 libsql 레플리카를 열어 초기 pull 만 수행.

왜: 토요일 주간 재점수(quant-universe)가 쓴 델타를 주말에 pull 하는 주체가 없어, 월요일 08:30 KR
다이제스트의 초기 pull 프로브(QUANT_BOT_SYNC_TIMEOUT=180)가 한 주치 델타를 한 번에 받아야 했다.
넘기면 오프라인 파일로 강등 → 발송 보류(exit 20). quant-universe.service 의 ExecStartPost 가 재점수
직후 digest_replica.db 로 이 스크립트를 돌려 델타를 미리 받아 둔다 (다이제스트와 같은 flock).

출력: 'init_sync=<상태> elapsed=<초>s' 1줄 (journal 에 남음 — 첫 재점수 때 pull 소요 실측용).
종료코드: 0 = ok/local/offline, 1 = timeout/error/저장소 오류 (유닛에서는 '-' 로 무시).
"""

from __future__ import annotations

import time

from src.logger import get_logger
from src.storage import DB_ERRORS, StorageError, get_storage

logger = get_logger(__name__)

OK_STATES = frozenset({"ok", "local", "offline"})


def main() -> int:
    t0 = time.monotonic()
    try:
        store = get_storage()
    except (StorageError, *DB_ERRORS) as e:
        logger.error("레플리카 warm 실패 (저장소 오류): %s", e)
        return 1
    state = getattr(store, "init_sync", "local")
    elapsed = time.monotonic() - t0
    msg = f"init_sync={state} elapsed={elapsed:.1f}s db={store.db_path}"
    if state in OK_STATES:
        logger.info("레플리카 warm 완료 — %s", msg)
        print(msg)
        return 0
    logger.warning("레플리카 warm 미완 — %s (다음 다이제스트 프로브가 이어받음)", msg)
    print(msg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
