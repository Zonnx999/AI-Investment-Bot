"""
scripts/ledger.py
=================
신호 원장 CLI (Stage 1b WP-LEDGER) — 발송 신호의 사후 성적표.

하위 명령:
  snapshot   유니버스 단면 저장
             --source rescore(기본) | pre_rescore(필요할 때만) | reconstructed(07-10 복원)
  backfill   07-13 소급 백필 — 기본 dry-run(쓰기 0), --confirm 일 때만 기록
  score      채점 (발송 후 ExecStartPost 용). 실패 시 exit 1 (유닛에서 '-' 로 무시)

공통: 저장소가 강등(초기 pull 타임아웃 → 빈 오프라인 파일)이면 어떤 명령도 실행하지 않고 exit 3.
  report     us | kr — 평문 성적표
  ic         최근 두 스냅샷 사이 점수-수익 순위상관 (정보용, 판정 근거 아님)

예:
    python scripts/ledger.py snapshot --source reconstructed
    python scripts/ledger.py backfill
    python scripts/ledger.py backfill --confirm
    python scripts/ledger.py score
    python scripts/ledger.py report us

롤아웃 순서(심사 §6): 배포 → snapshot reconstructed → backfill(dry-run → --confirm)
→ score → 그 다음 재점수 타이머 활성화.
"""

from __future__ import annotations

import argparse
import sys

from src import ledger
from src.exceptions import QuantBotError
from src.logger import get_logger
from src.storage import DB_ERRORS
from src.storage import StorageError

logger = get_logger(__name__)


def _print_snapshot_result(res: dict) -> None:
    for m, info in res.items():
        if info.get("written"):
            print(f"{m}: 저장 {info['as_of']} ({info['source']}, {info['rule_version']}) "
                  f"{info['n_rows']}행 {info['bytes'] / 1024:.0f}KB")
        elif "n_rows" in info:
            print(f"{m}: 쓰기 없음 {info['as_of']} ({info['source']}) {info['n_rows']}행 "
                  f"— dry-run 이거나 같은 as_of 가 이미 있음")
        else:
            mark = ("거부" if info.get("refused")
                    else "실패" if info.get("db_error") else "생략")
            print(f"{m}: {mark} — {info.get('reason')}")


def cmd_snapshot(args: argparse.Namespace) -> int:
    if args.source == "pre_rescore":
        res = ledger.ensure_pre_snapshot(markets=tuple(args.markets), dry_run=args.dry_run)
    elif args.source == "reconstructed":
        markets = tuple(m for m in args.markets if m != "CRYPTO") or ledger.MARKETS
        res = ledger.snapshot_reconstructed(as_of=args.asof or ledger.BACKFILL_ASOF,
                                            markets=markets, dry_run=args.dry_run)
    else:
        res = ledger.snapshot_universe(args.source, rule_version=args.rule_version,
                                       as_of=args.asof, markets=tuple(args.markets),
                                       dry_run=args.dry_run)
    _print_snapshot_result(res)
    if not args.dry_run:
        ledger.get_storage().sync()
    return 2 if any(v.get("refused") or v.get("db_error") for v in res.values()) else 0


def cmd_backfill(args: argparse.Namespace) -> int:
    plan, written = ledger.backfill(confirm=args.confirm)
    print(ledger.format_backfill_plan(plan, confirm=args.confirm, written=written))
    if not plan.ok:
        return 2
    if args.confirm:
        ledger.get_storage().sync()
        return 0 if written and all(written.values()) else 1
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    res = ledger.score(tuple(args.markets), dry_run=args.dry_run)
    failed = False
    for m, r in res.items():
        if not r.get("ok"):
            failed = True
            print(f"{m}: 실패 — {r.get('error')}")
            continue
        if r.get("skipped"):
            print(f"{m}: 생략 — {r['skipped']}")
            continue
        s = r["summary"]
        line = ledger.format_ledger_line(s) or "(표시 게이트 미통과 — 줄 생략)"
        stop = r["flags"].get("stop_reason")
        print(f"{m}: 새 일별행 {r['rows']} · 픽 결과 {r['outcomes']}"
              + (f" · 중단 {r['flags'].get('stopped_at')} ({stop})" if stop else ""))
        print(f"   {line}")
    return 1 if failed else 0


def cmd_report(args: argparse.Namespace) -> int:
    print(ledger.report_text(args.market))   # check_backtest [1] 과 같은 진입점
    return 0


def cmd_ic(args: argparse.Namespace) -> int:
    print(ledger.ic_text(args.markets, score_key=args.score))   # check_backtest [2] 와 같은 진입점
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="신호 원장 (발송 기록·채점·스냅샷·백필)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="유니버스 스냅샷 저장")
    sp.add_argument("--source", choices=("rescore", "pre_rescore", "reconstructed"),
                    default="rescore")
    sp.add_argument("--asof", default=None, help="as_of 날짜 (기본: MAX(updated_at) 날짜)")
    sp.add_argument("--markets", nargs="+", default=["US", "KR", "CRYPTO"],
                    type=str.upper, choices=("US", "KR", "CRYPTO"))
    sp.add_argument("--rule-version", default=None)
    sp.add_argument("--dry-run", action="store_true", help="쓰기 0 (계획만 출력)")
    sp.set_defaults(fn=cmd_snapshot)

    bp = sub.add_parser("backfill", help="07-13 소급 백필 (기본 dry-run)")
    bp.add_argument("--confirm", action="store_true", help="실제로 기록")
    bp.set_defaults(fn=cmd_backfill)

    sc = sub.add_parser("score", help="채점 (발송 후)")
    sc.add_argument("--markets", nargs="+", default=list(ledger.MARKETS),
                    type=str.upper, choices=ledger.MARKETS)
    sc.add_argument("--dry-run", action="store_true", help="계산만, 쓰기 0")
    sc.set_defaults(fn=cmd_score)

    rp = sub.add_parser("report", help="평문 성적표")
    rp.add_argument("market", type=str.lower, choices=("us", "kr"))
    rp.set_defaults(fn=cmd_report)

    ic = sub.add_parser("ic", help="스냅샷 간 순위상관 (정보용)")
    ic.add_argument("--markets", nargs="+", default=list(ledger.MARKETS),
                    type=str.upper, choices=ledger.MARKETS)
    ic.add_argument("--score", default="total", choices=("total", "value", "health"))
    ic.set_defaults(fn=cmd_ic)
    return ap


EXIT_DEGRADED = 3


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # 초기 pull 타임아웃으로 빈 오프라인 파일로 강등됐으면 실행하지 않는다 (RVOPS-02) — 그대로 가면
        # score/report 가 '원장 비어 있음 → 생략' exit 0 으로 실패를 숨긴다.
        if getattr(ledger.get_storage(), "init_sync", None) == "timeout":
            print("[중단] 저장소 강등 — Turso 초기 pull 타임아웃으로 빈 오프라인 파일이 열렸습니다. "
                  "원장 결과가 무효라 실행하지 않습니다. QUANT_BOT_SYNC_TIMEOUT=240 으로 다시 실행하세요.",
                  file=sys.stderr)
            return EXIT_DEGRADED
        return args.fn(args)
    except (StorageError, *DB_ERRORS) as e:
        logger.error("원장 DB 오류: %s", e, exc_info=True)
        return 1
    except QuantBotError as e:
        logger.error("원장 명령 실패: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
