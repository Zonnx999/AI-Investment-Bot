"""
scripts/build_universe.py
=========================
전 종목 유니버스 DB 구축/갱신 (Phase 8) — 주 1회 배치.

  --discover    : company-screener(US/KR) + CoinGecko(crypto) 로 유니버스 발굴
                  (US 는 보통주 분류기로 상장채·우선주·OTC·저유동 라인을 excluded 표시)
  --enrich      : 보강 안 됐거나 오래된 주식만 key-metrics/DART 로 점수화 (재개 가능)
  (둘 다 없으면 둘 다 실행)
  --reclassify  : DB 의 US 행을 현재 분류 규칙으로 재분류만 (오프라인, API 0콜)
  --kr-refresh  : KRX 일별(2콜)로 KR 가격·시총 갱신 + DART 호출 없이 오프라인 재점수

  --markets US,KR,CRYPTO : 발굴 대상 시장 (기본 전부)
  --limit N     : 이번 실행에서 보강할 종목 수 상한 (쿼터/시간 조절)
  --max-age D   : 며칠 지난 보강을 다시 할지 (기본 7)
  --force       : 신선도 무시하고 전 종목 재점수

종료코드: 0 정상 / 2 원격 DB(Turso) 필요한데 연결 불가 / 5 보강 중단(aborted) 또는 실패율 > 20%.

예:
    python scripts/build_universe.py                        # 발굴 + 전체 보강
    python scripts/build_universe.py --enrich --limit 500   # 보강 500개만 (이어하기)
    python scripts/build_universe.py --reclassify           # 규칙/WHITELIST 변경 후 재분류
    python scripts/build_universe.py --kr-refresh           # KR 일간 시총 갱신 (DART 0콜)

운영 Turso 를 대상으로는 사용자 절차로만 실행 (CLAUDE.md §4.10 #1, #9 — 전용 레플리카 파일).
"""

from __future__ import annotations

import argparse
import time
from datetime import timedelta

from src import universe
from src.logger import get_logger

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_REMOTE_UNAVAILABLE = 2
EXIT_PARTIAL = 5
MAX_FAILURE_RATE = 0.20          # 보강 실패율이 이보다 크면 비정상 종료 (부분 완료 안내)


def _make_progress_bar():
    """보강 진행바 콜백 (\\r 갱신, 새 의존성 없음). rate/ETA 포함."""
    start = time.monotonic()
    bar_len = 28

    def cb(i: int, total: int, stats: dict) -> None:
        elapsed = time.monotonic() - start
        rate = i / elapsed if elapsed > 0 else 0
        eta_min = (total - i) / rate / 60 if rate > 0 else 0
        pct = i / total if total else 1.0
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(
            f"\r    [{bar}] {i}/{total} {pct*100:3.0f}%  "
            f"보강{stats['enriched']} 빈값{stats['no_data']} 실패{stats['failed']}  "
            f"{rate:.1f}/s  ETA {eta_min:.1f}분 ",
            end="", flush=True,
        )

    return cb


def _require_remote(what: str) -> bool:
    """WP-OPS-CORE 계약 Storage.require_remote(what) 소비 — 병합 전엔 항상 True (getattr 가드)."""
    from src.storage import get_storage

    fn = getattr(get_storage(), "require_remote", None)
    if fn is None:
        return True
    return bool(fn(what))


def _enrich_outcome(stats: dict) -> bool:
    """보강 결과가 '비정상'(중단 또는 실패율 초과)인지."""
    if stats.get("aborted"):
        return True
    attempted = stats.get("enriched", 0) + stats.get("no_data", 0) + stats.get("failed", 0)
    return attempted > 0 and stats.get("failed", 0) / attempted > MAX_FAILURE_RATE


def _print_enrich_result(stats: dict) -> None:
    print()
    print(f"    완료: 보강 {stats['enriched']} / 데이터없음 {stats['no_data']} / 실패 {stats['failed']}")
    if stats.get("aborted"):
        print("    !! 중단됨 — 호출 한도(429 연속) 또는 키/플랜 오류. 남은 종목은 다음 실행에서 이어집니다.")


def _print_exclusions() -> None:
    """시장별 제외 사유 집계 (오프라인 SELECT)."""
    conn = universe._conn()
    if "excluded" not in universe._cols(conn):
        return
    rows = conn.execute(
        "SELECT market, excluded, COUNT(*) FROM screened WHERE excluded IS NOT NULL "
        "GROUP BY market, excluded ORDER BY market, COUNT(*) DESC"
    ).fetchall()
    if not rows:
        return
    print("\n[제외 사유]")
    for mkt, reason, n in rows:
        print(f"    {mkt:6s} {reason:12s} {n}")


def _parse_markets(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ("US", "KR", "CRYPTO")
    out = tuple(m.strip().upper() for m in raw.split(",") if m.strip())
    bad = [m for m in out if m not in ("US", "KR", "CRYPTO")]
    if bad:
        raise SystemExit(f"--markets 에 알 수 없는 시장: {bad} (US, KR, CRYPTO 만)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="유니버스 DB 구축/갱신")
    parser.add_argument("--discover", action="store_true", help="유니버스 발굴만")
    parser.add_argument("--enrich", action="store_true", help="펀더멘털 보강만")
    parser.add_argument("--reclassify", action="store_true",
                        help="DB 의 US 행을 현재 보통주 규칙으로 재분류만 (API 0콜)")
    parser.add_argument("--kr-refresh", action="store_true",
                        help="KRX 일별로 KR 가격·시총 갱신 + 오프라인 재점수 (DART 0콜)")
    parser.add_argument("--markets", default=None, help="발굴 시장 (예: US,KR). 기본 전부")
    parser.add_argument("--limit", type=int, default=None, help="이번에 보강할 종목 수 상한")
    parser.add_argument("--max-age", type=int, default=7, help="재보강 주기(일, 기본 7)")
    parser.add_argument("--force", action="store_true",
                        help="신선도 무시하고 전 종목 재점수 (점수 공식 변경 후). "
                             "원본 데이터는 캐시라 API 호출 거의 없이 빠름")
    args = parser.parse_args()

    from src.storage import get_storage

    print("=" * 70)
    print(" 유니버스 DB 빌드")
    print("=" * 70)

    # 원격 DB 가 설정돼 있는데 연결이 안 되면 로컬 레플리카에만 쓰고 끝나는 사고 방지
    if not _require_remote("build_universe"):
        print("\n  !! 원격 DB(Turso) 연결 불가 — 쓰기 작업을 시작하지 않습니다 (종료코드 2)")
        return EXIT_REMOTE_UNAVAILABLE

    # 단독 모드: --reclassify / --kr-refresh (다른 단계 실행 안 함)
    if args.reclassify or args.kr_refresh:
        if args.reclassify:
            print("\n[재분류] US 보통주 규칙 재적용 (오프라인)...")
            out = universe.reclassify_us()
            print(f"    변경 {out['changed']}행 / 제외 {out['excluded']} {out['by_reason']}")
        if args.kr_refresh:
            print("\n[KR 갱신] KRX 일별 가격·시총 + 오프라인 재점수 (DART 0콜)...")
            out = universe.refresh_kr_prices()
            print(f"    발굴 {out.get('discovered', 0)} / 재점수 {out.get('rescored', 0)} "
                  f"/ 건너뜀 {out.get('skipped', 0)}")
        get_storage().sync()
        _print_exclusions()
        print("=" * 70)
        return EXIT_OK

    do_discover = args.discover or not args.enrich
    do_enrich = args.enrich or not args.discover
    markets = _parse_markets(args.markets)

    # --force: max_age=0 → updated_at 이 항상 cutoff(=now) 이전이라 전 종목 재보강
    max_age = timedelta(0) if args.force else timedelta(days=args.max_age)
    abnormal = False

    if do_discover:
        print(f"\n[1] 발굴 ({', '.join(markets)}: company-screener / KRX / CoinGecko)...")
        counts = universe.discover(markets=markets)
        for mkt, n in counts.items():
            print(f"    {mkt}: {n}종목 발굴")

    if do_enrich:
        if args.force:
            print("\n(--force) 신선도 무시 — 전 종목 재점수 (캐시 데이터로 빠름)")
        print("\n[2] 미국 보강 (FMP key-metrics/ratios TTM → 점수)...")
        pending = len(universe.symbols_needing_enrichment(max_age))
        print(f"    보강 대상: {pending}종목" + (f" (이번 {args.limit}개)" if args.limit else ""))
        stats = universe.enrich(
            max_age=max_age, limit=args.limit,
            on_progress=_make_progress_bar(),
        )
        _print_enrich_result(stats)
        abnormal |= _enrich_outcome(stats)

        print("\n[3] 한국 보강 (DART 지배주주 귀속 → ROE/PER/PBR)...")
        kr_pending = len(universe._kr_symbols_needing_enrichment(max_age))
        print(f"    보강 대상: {kr_pending}종목" + (f" (이번 {args.limit}개)" if args.limit else ""))
        kr_stats = universe.enrich_kr(
            max_age=max_age, limit=args.limit,
            on_progress=_make_progress_bar(),
        )
        _print_enrich_result(kr_stats)
        abnormal |= _enrich_outcome(kr_stats)

    # 호스팅 DB(Turso)면 클라우드로 push (로컬 sqlite3 면 no-op)
    get_storage().sync()

    print("\n[현황]")
    for k, v in universe.stats().items():
        print(f"    {k}: {v}")
    _print_exclusions()

    # 남음 카운트는 실제 신선도 기준으로 — --force(max_age=0)를 그대로 쓰면
    # 방금 보강한 종목까지 전부 '남음'으로 집계돼 오해를 부름 (2026-07-11 스모크에서 발견)
    remaining = len(universe.symbols_needing_enrichment(universe.ENRICH_MAX_AGE))
    if remaining:
        print(f"\n  ⏳ 보강 남음 {remaining}종목 — 이어하려면: python scripts/build_universe.py --enrich")
    print("=" * 70)
    if abnormal:
        logger.warning("유니버스 빌드 부분 완료 (중단 또는 실패율 > %.0f%%) — 종료코드 %d",
                       MAX_FAILURE_RATE * 100, EXIT_PARTIAL)
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
