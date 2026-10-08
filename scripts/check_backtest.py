"""
scripts/check_backtest.py
=========================
검증 리포트 (ROADMAP §2.1) — 신호·예측이 실제로 쓸모 있었는지 '입증 가능한 것만' 보여준다.

5개 섹션:
  [1] 발송 원장 성과      — 실제로 발송한 종목 리스트의 사후 성과 (src.ledger, 없으면 '원장 없음')
  [2] 유니버스 순위 IC    — 점수 순위와 이후 수익률의 순위상관 (스냅샷 2개 미만이면 '관측 대기')
  [3] 선행지표 7개        — 무선견 워크포워드 · 베이스라인 2종 · 짝지은 부호검정 · Holm · 전/후반 안정성
  [4] 운영 모멘텀 top-N   — 운영 signals.momentum_score 로 월 리밸런스 vs 같은 비용 동일가중 (진단용)
  [5] SPY 200일선 (참고)  — 기간 창 선택에 따라 결과가 크게 바뀌므로 max 창 기준 참고만

실행:
    python scripts/check_backtest.py
    python scripts/check_backtest.py -v                      # 선행지표 상세
    python scripts/check_backtest.py --tickers AAPL MSFT NVDA JPM XOM --top-n 2
    python scripts/check_backtest.py --save                  # 결과를 state('validation', ...) 에 저장

종료 코드: 데이터 수집/계산 인프라 실패만 1. '통과 0/7' 같은 검증 결과는 실패가 아님.
⚠️ 생존 편향(현재 워치리스트로 과거를 봄)·슬리피지 미모델링 — 절대 성과가 아니라
   '신호가 입증 가능한 수준으로 유효했는가' 판단용입니다.
"""

from __future__ import annotations

import argparse
import importlib
import unicodedata
from datetime import datetime, timezone

from src.backtest import (
    DEFAULT_COST_BPS,
    LL_ALPHA,
    LL_MIN_DISAGREE,
    LL_MIN_EDGE_PP,
    LL_MIN_OOS,
    LeadLagWF,
    buy_and_hold_positions,
    excess_significance,
    judge_lead_lag,
    ma_crossover_positions,
    returns_from_equity,
    run_backtest,
    summarize_lead_lag,
    walk_forward_lead_lag,
    walk_forward_topn,
)
from src.exceptions import QuantBotError
from src.logger import get_logger
from src.predictors import PREDICTOR_SPECS, PredictorSpec

logger = get_logger(__name__)

# 워크포워드 기본 유니버스 — screener.US_WATCHLIST 의 축소판 (yfinance 호출 절약,
# 섹터 분산 유지). --tickers 로 교체 가능.
DEFAULT_WF_TICKERS = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "JPM", "UNH", "COST", "XOM", "CAT",
)

_WIDE = 78


# ----------------------------------------------------------------------
# 표 출력 헬퍼 — 한글(전각) 폭 정렬
# ----------------------------------------------------------------------


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = max(0, width - _width(text))
    return (" " * gap + text) if right else (text + " " * gap)


def _short_name(spec: PredictorSpec) -> str:
    lead = spec.leading_label
    for tail in (" 비율 증가율", " 관심 증가율", " 증가율"):
        lead = lead.replace(tail, "")
    tgt = spec.target_label.replace(" 수익률", "")
    label = f"{lead.replace(' ', '')} -> {tgt}"
    return f"{label} [연구]" if spec.research_only else label


def _fmt_p(p: float) -> str:
    return f"{p:.3f}"


# ----------------------------------------------------------------------
# [1] [2] 원장 — src.ledger 는 지연 import (없거나 DB 오류면 '원장 없음')
# ----------------------------------------------------------------------


def _db_errors() -> tuple[type[BaseException], ...]:
    """storage 의 DB 오류 튜플 (공개 별칭 DB_ERRORS 우선, 없으면 내부 _DB_ERRORS)."""
    from src import storage

    return tuple(getattr(storage, "DB_ERRORS", None) or getattr(storage, "_DB_ERRORS", ()))


# 원장 모듈 연동 계약 (병합 시 확정 — scripts/ledger.py 의 report/ic 명령과 같은 진입점):
#   [1] src.ledger.report_text(market: "us" | "kr") -> str
#   [2] src.ledger.ic_text() -> str   (유니버스 순위 IC 평문)
_LEDGER_REPORT_FUNCS = ("report_text",)
_LEDGER_IC_FUNCS = ("ic_text",)

_LOGGED_ONCE: set[str] = set()   # 같은 로그가 섹션마다 중복 출력되지 않게


def _log_once(level: str, msg: str, *args) -> None:
    key = msg % args if args else msg
    if key in _LOGGED_ONCE:
        return
    _LOGGED_ONCE.add(key)
    getattr(logger, level)(msg, *args)


def _ledger_call(candidates: tuple[str, ...], *args) -> tuple[str | None, bool]:
    """src.ledger 의 첫 번째로 존재하는 함수를 호출해 평문을 받는다. (text, integration_error) 반환.

    - 원장 모듈 없음 / DB 오류 / 빈 결과 -> (None, False): '원장 없음' 으로 취급 (정상 폴백).
    - 모듈은 있는데 함수 이름이 없거나 시그니처 불일치(TypeError) -> (None, True): '연동 오류' (WARNING).
    원장은 검증의 '보조 정보' 라 실패가 리포트 전체를 막지 않는다 (종료코드에도 영향 없음).
    """
    try:
        # 지연 import — 원장 모듈이 없는 환경에서도 리포트가 돈다. `from src import ledger` 는
        # 이미 import 된 패키지 속성을 그대로 가져와 sys.modules 차단을 무시하므로 importlib 사용.
        ledger = importlib.import_module("src.ledger")
    except ImportError as e:
        _log_once("info", "원장 모듈 없음 — %s", e)
        return None, False
    fn = next((getattr(ledger, n) for n in candidates if callable(getattr(ledger, n, None))), None)
    if fn is None:
        _log_once("warning", "원장 연동 오류 — src.ledger 에 %s 함수 없음", "/".join(candidates))
        return None, True
    try:
        out = fn(*args)
    except (TypeError, AttributeError) as e:
        _log_once("warning", "원장 연동 오류 — %s 시그니처 불일치: %s", fn.__name__, e)
        return None, True
    except (QuantBotError, ValueError, *_db_errors()) as e:
        logger.warning("원장 조회 실패 (%s) — %s", fn.__name__, e)
        return None, False
    return (str(out).rstrip() if out else None), False


_LEDGER_INTEGRATION_MSG = "  ⚠️ 원장 연동 오류 — src.ledger 함수 이름/시그니처가 맞지 않습니다 (로그 확인)."


def _section_ledger() -> None:
    """[1] 발송 원장 성과."""
    printed = False
    integration_error = False
    for market in ("us", "kr"):
        text, bad = _ledger_call(_LEDGER_REPORT_FUNCS, market)
        integration_error = integration_error or bad
        if text:
            print(text)
            print()
            printed = True
    if integration_error:
        print(_LEDGER_INTEGRATION_MSG)
    elif not printed:
        print("  원장 없음 — 아직 기록된 발송이 없거나 원장 모듈/DB 를 읽을 수 없습니다.")


def _section_universe_ic() -> None:
    """[2] 유니버스 순위 IC."""
    text, bad = _ledger_call(_LEDGER_IC_FUNCS)
    if text:
        print(text)
    elif bad:
        print(_LEDGER_INTEGRATION_MSG)
    else:
        print("  관측 대기 — 유니버스 스냅샷이 2개 이상 쌓이면 점수 순위 vs 이후 수익률의 순위상관(IC)을 계산합니다.")


# ----------------------------------------------------------------------
# [3] 선행지표 7개 — 무선견 워크포워드
# ----------------------------------------------------------------------


def _run_lead_lag() -> tuple[list[LeadLagWF], int]:
    """PREDICTOR_SPECS 전수 검증 → (판정 완료 결과들, 인프라 실패 수)."""
    results: list[LeadLagWF] = []
    failures = 0
    for spec in PREDICTOR_SPECS:
        label = _short_name(spec)
        try:
            lead_yoy, target = spec.build()
            df = walk_forward_lead_lag(
                lead_yoy, target, pub_lag=spec.pub_lag_months, cost_bps=DEFAULT_COST_BPS
            )
            results.append(
                summarize_lead_lag(df, label, spec.pub_lag_months, research_only=spec.research_only)
            )
        except QuantBotError as e:
            logger.warning("선행지표 검증 실패: %s — %s", label, e)
            print(f"  ❌ [{label}] {type(e).__name__}: {e}")
            failures += 1
    # 보정 강도는 '시도한 전체 가설 수' 기준 — 일부가 실패해도 느슨해지지 않게
    return judge_lead_lag(results, n_tests=len(PREDICTOR_SPECS)), failures


def _print_lead_lag_table(results: list[LeadLagWF], verbose: bool) -> None:
    cols = (("관계", 20), ("pub", 5), ("OOS", 5), ("적중%", 7), ("항상상승", 10), ("12M모멘텀", 11),
            ("우위%p", 8), ("p(상승)", 9), ("p(모멘텀)", 11))
    header = "  " + _pad(cols[0][0], cols[0][1]) + "".join(_pad(n, w, right=True) for n, w in cols[1:]) + "  판정"
    print(header)
    for r in results:
        sufficiency = [x for x in r.reasons if x.startswith(("OOS", "다른 달"))]
        note = f" ({'; '.join(sufficiency)})" if sufficiency else ""
        print(
            "  " + _pad(r.name, cols[0][1])
            + _pad(f"{r.pub_lag}m", cols[1][1], right=True)
            + _pad(str(r.n_oos), cols[2][1], right=True)
            + _pad(f"{r.acc:.1f}", cols[3][1], right=True)
            + _pad(f"{r.base_up:.1f}", cols[4][1], right=True)
            + _pad(f"{r.base_mom:.1f}", cols[5][1], right=True)
            + _pad(f"{r.edge_pp:+.1f}", cols[6][1], right=True)
            + _pad(_fmt_p(r.p_up), cols[7][1], right=True)
            + _pad(_fmt_p(r.p_mom), cols[8][1], right=True)
            + f"  {r.verdict}{note}"
        )
    n_pass = sum(1 for r in results if r.passed)
    print(
        f"  통과 기준: Holm p<{LL_ALPHA:g}(두 베이스라인) & 우위 >= +{LL_MIN_EDGE_PP:g}%p"
        f" & 다른 달 >= {LL_MIN_DISAGREE} & 전/후반 우위 > 0 & OOS >= {LL_MIN_OOS}."
        f" 결과 {n_pass}/{len(PREDICTOR_SPECS)}."
    )
    print("  '미입증' = 이 표본으로 입증 못함 (예측력 없음 아님)")
    if verbose:
        print()
        for r in results:
            print(f"  [{r.name}] pub={r.pub_lag}m  OOS {r.n_oos}개월 ({r.start[:7]}~{r.end[:7]})"
                  f"  주도 lag {r.lag_mode}  롱비중 {r.pct_long:.0f}%")
            print(f"     부호검정 p: vs항상상승 {_fmt_p(r.p_up)} (다른 달 {r.n_diff_up}),"
                  f" vs모멘텀 {_fmt_p(r.p_mom)} (다른 달 {r.n_diff_mom}) | Holm 보정 p {_fmt_p(r.p_adj)}"
                  f" | 전/후반 우위 {r.half_edges[0]:+.1f}/{r.half_edges[1]:+.1f}%p")
            print(f"     long/flat {r.strat_pct:+.0f}% vs 보유 {r.bh_pct:+.0f}% | 샤프 {r.sharpe_s:.2f} vs {r.sharpe_b:.2f}"
                  f" | 월 초과 평균 {r.exc_mean_pct:+.2f}% [{r.exc_ci_pct[0]:+.2f}, {r.exc_ci_pct[1]:+.2f}]")
            if r.reasons:
                print(f"     미충족: {'; '.join(r.reasons)}")


# ----------------------------------------------------------------------
# [4] 운영 모멘텀 top-N  /  [5] SPY 200일선
# ----------------------------------------------------------------------


def _section_walk_forward(tickers: tuple[str, ...], period: str, top_n: int):
    """[4] 운영 모멘텀 top-N 워크포워드 (진단 — 합격 판정 없음). WalkForwardResult 반환."""
    from src.data_fetcher import fetch_prices
    from src.exceptions import DataFetchError
    from src.utils import close_series

    prices = {}
    for t in tickers:
        try:
            prices[t] = close_series(fetch_prices(t, period=period))
        except DataFetchError as e:
            logger.warning("워크포워드 유니버스에서 %s 스킵 — %s", t, e)
    if len(prices) < top_n:
        raise QuantBotError(f"가격 확보 종목 {len(prices)}개 < top_n {top_n} — 섹션 생략")

    result = walk_forward_topn(prices, top_n=top_n)
    print(result)
    print("\n  최근 리밸런스 선택:")
    for d, chosen in result.picks[-3:]:
        print(f"    {str(d)[:10]}  {', '.join(chosen)}")
    print("  ※ 생존 편향(현재 종목으로 과거를 봄)·소수 종목 — 합격/불합격 판정은 하지 않음 (진단용).")
    return result


def _section_trend(period: str):
    """[5] SPY 200일선 long/flat vs 매수후보유 (참고). (strat, bench, stats) 반환."""
    from src.data_fetcher import fetch_prices
    from src.utils import close_series

    closes = close_series(fetch_prices("SPY", period=period))
    strat = run_backtest(
        closes, ma_crossover_positions(closes, window=200),
        cost_bps=DEFAULT_COST_BPS, name=f"SPY 200일선 long/flat ({period})",
    )
    bench = run_backtest(
        closes, buy_and_hold_positions(closes), cost_bps=0.0, name=f"SPY 매수후보유 ({period})",
    )
    print(strat)
    print(bench)
    excess = strat.total_return_pct - bench.total_return_pct
    stats = excess_significance(
        (returns_from_equity(strat.equity_curve) - returns_from_equity(bench.equity_curve)).dropna()
    )
    print(f"  초과수익 {excess:+.1f}%p | MDD {strat.max_drawdown_pct:.1f}% vs {bench.max_drawdown_pct:.1f}%")
    print(f"  {stats.summary_line()}")
    print("  ※ 참고용 — 짧은 기간 창(예: 5y)의 큰 격차는 창 선택 때문일 수 있어, 긴 창(max) 기준으로만 봅니다.")
    return strat, bench, stats


# ----------------------------------------------------------------------
# 저장 (--save 명시 시에만)
# ----------------------------------------------------------------------


def _save_state(results: list[LeadLagWF], wf_result) -> None:
    from src.storage import get_storage

    store = get_storage()
    asof = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.put_state("validation", "lead_lag", {
        "asof": asof,
        "n_tests": len(PREDICTOR_SPECS),
        "n_passed": sum(1 for r in results if r.passed),
        "results": [r.to_dict() for r in results],
    })
    if wf_result is not None:
        store.put_state("validation", "walk_forward", {"asof": asof, **wf_result.to_dict()})
    logger.info("검증 결과 저장 완료 (state 'validation')")


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------


def _header(title: str) -> None:
    print(title)
    print("-" * _WIDE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검증 리포트 (ROADMAP §2.1)")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help=f"운영 모멘텀 워크포워드 유니버스 (기본 {' '.join(DEFAULT_WF_TICKERS)})")
    parser.add_argument("--period", default="7y", help="섹션 4 가격 조회 기간 (기본 7y)")
    parser.add_argument("--period-trend", default="max", help="섹션 5 SPY 조회 기간 (기본 max)")
    parser.add_argument("--top-n", type=int, default=3, help="워크포워드 보유 종목 수 (기본 3)")
    parser.add_argument("-v", "--verbose", action="store_true", help="선행지표 상세 출력")
    parser.add_argument("--save", action="store_true", help="결과를 state('validation', ...) 에 저장")
    args = parser.parse_args(argv)
    tickers = tuple(t.upper() for t in args.tickers) if args.tickers else DEFAULT_WF_TICKERS

    print("=" * _WIDE)
    print(" 검증 리포트 (ROADMAP §2.1 — 신호·예측이 입증 가능한 수준으로 유효했는가)")
    print("=" * _WIDE)
    print("  ⚠️ 생존 편향·슬리피지 미모델링 — 절대 성과가 아니라 상대적 유효성 판단용.\n")

    failures = 0

    _header("[1] 발송 원장 성과 — 실제로 보낸 종목 리스트의 사후 성과")
    _section_ledger()
    print()

    _header("[2] 유니버스 순위 IC — 점수가 높을수록 이후 수익률도 높았나")
    _section_universe_ic()
    print()

    _header("[3] 선행지표 7개 — 무선견 워크포워드 (다음 1개월 방향, 비용 10bp)")
    results, ll_failures = _run_lead_lag()
    failures += ll_failures
    if results:
        _print_lead_lag_table(results, args.verbose)
    print()

    _header(f"[4] 운영 모멘텀 top-{args.top_n} 진단 — {len(tickers)}종목, 월 리밸런스, 같은 비용 동일가중 대비")
    wf_result = None
    try:
        wf_result = _section_walk_forward(tickers, args.period, args.top_n)
    except QuantBotError as e:
        logger.warning("섹션 4 실패 — %s", e)
        print(f"  ❌ {type(e).__name__}: {e}")
        failures += 1
    print()

    _header("[5] SPY 200일선 long/flat (참고)")
    try:
        _section_trend(args.period_trend)
    except QuantBotError as e:
        logger.warning("섹션 5 실패 — %s", e)
        print(f"  ❌ {type(e).__name__}: {e}")
        failures += 1
    print()

    print("=" * _WIDE)
    print("해석:")
    print("  · '미입증' = 이 표본으로 입증 못함. 예측력이 없다는 뜻은 아님")
    print("  · 운영 모멘텀: 일 초과 |t|<2 이거나 신뢰구간이 0을 포함하면 '판정 불가'")
    print("  · 유니버스 종목 점수의 실제 성과는 [1] 원장이 쌓이는 만큼만 알 수 있음")
    print("=" * _WIDE)

    if args.save:
        try:
            _save_state(results, wf_result)
        except (QuantBotError, *_db_errors()) as e:
            logger.warning("검증 결과 저장 실패 — %s", e)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
