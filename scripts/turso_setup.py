"""
scripts/turso_setup.py
======================
Turso(libSQL) 호스팅 DB 연결 점검 (Phase 10).

사전 준비 (당신이 할 일):
  1) https://turso.tech 가입 (GitHub 로그인)
  2) Turso CLI 설치:  curl -sSfL https://get.tur.so/install.sh | bash
  3) turso auth login
  4) turso db create quant-bot
  5) turso db show quant-bot --url        → TURSO_DATABASE_URL 로 .env 에
  6) turso db tokens create quant-bot     → TURSO_AUTH_TOKEN 로 .env 에
  7) pip install -e ".[hosting]"

그 다음:
    python scripts/turso_setup.py          # 연결 + 동기화 점검
    python scripts/turso_setup.py --push   # 로컬 DB 를 클라우드로 한번 올리기
"""

from __future__ import annotations

import argparse

from src.config import settings
from src.logger import get_logger
from src.storage import get_storage

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Turso 호스팅 DB 점검")
    parser.add_argument("--push", action="store_true", help="로컬 → 클라우드 동기화 1회")
    args = parser.parse_args()

    if not settings.turso_database_url:
        print("❌ .env 에 TURSO_DATABASE_URL 이 없습니다.")
        print("   turso db show <db> --url  결과를 .env 에 넣으세요 (상단 주석 참고).")
        return 1

    try:
        store = get_storage()
    except Exception as e:  # noqa: BLE001 — 셋업 진단 스크립트
        print(f"❌ 연결 실패: {e}")
        print("   libsql 미설치면:  pip install -e \".[hosting]\"")
        return 1

    if not store.is_turso:
        print("⚠️ 로컬 sqlite3 로 연결됨 (Turso 비활성). TURSO_DATABASE_URL 확인 필요.")
        return 1

    print(f"✅ Turso 임베디드 레플리카 연결 OK")
    print(f"   로컬 레플리카: {store.db_path}")

    # 마커를 써서 라운드트립 확인
    store.put_state("turso_setup", "marker", {"ok": True})
    store.sync()
    print("✅ 동기화(push) 완료 — 클라우드에 반영됨")

    cache_rows = store.stats()
    print(f"   현재 캐시 현황: {cache_rows}")
    print("\n이제 다른 기기/GitHub Actions 에서도 같은 DB 를 공유합니다.")
    print("유니버스 빌드:  python scripts/build_universe.py  (끝나면 자동 sync)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
