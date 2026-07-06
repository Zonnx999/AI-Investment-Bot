"""
src/portfolio.py
================
포지션 사이징 엔진 (ROADMAP §1.1 13b) + 사이징 룰 검증 백테스터 (13d).

지금까지의 모듈이 "이 종목 살만한가?"(signals/screener)에 답했다면, 여기는
"**얼마나** 살 것인가 — 이미 들고 있는 것과의 상관·리스크를 감안해"에 답합니다.

파이프라인 (``propose``):
  1. ``inverse_vol_weights``   — 변동성 역가중 (risk parity 유사) + 종목별 상한
  2. ``correlation_penalty``   — 기존 보유와 상관 높은 후보 감액 (분산 효과 보호)
  3. ``kelly_cap``             — Kelly fraction 소프트 상한 (소프: 파산 회피 사이징)

검증 (``weighted_backtest``):
  임의의 weight_fn 을 월 리밸런스로 리플레이 — 각 리밸런스일에 '그 시점까지의
  데이터만' 전달 (룩어헤드 없음). 성과 지표 산식은 ``src.backtest._result_from_returns``
  를 그대로 재사용 (초기자본 앵커 MDD 등 — 지표 산식의 단일 출처 유지).

설계 (CLAUDE.md §4.4): 이 모듈은 **순수 함수만** — 네트워크/외부 API 호출 없음.
데이터 fetch 는 scripts/check_portfolio.py 오케스트레이터가 담당.

§4.10 #5 가드 요약 (음수·0·결측이 유리하게 새지 않도록):
- 변동성 ~0 / 표본 부족 자산은 역가중에서 **제외** (무한 가중 방지, 사유 로깅)
- Kelly f* ≤ 0 (음의 엣지) / 분산 ~0 / 표본 부족 → 비중 **0** (모르는 엣지에 베팅 금지)
- 상관 판정은 겹치는 일별 수익률 ``MIN_CORR_OVERLAP`` 개 미만이면 **보류** (페널티 미적용)
- 상한(cap) 산술이 합 1.0 을 만들 수 없는 자산 수면 ``InsufficientDataError``

⚠️ 한계 (정직하게): Kelly f* 는 일별 수익률 표본평균/표본분산 추정치 — 평균 추정의
표준오차가 커서 (σ/√n) f* 자체가 매우 noisy 합니다. 그래서 full-Kelly 가 아니라
half-Kelly(기본 cap_fraction=0.5) '상한' 으로만 쓰고, 목표 비중은 역변동성이 정합니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from src.backtest import (
    DEFAULT_COST_BPS,
    MIN_BACKTEST_POINTS,
    BacktestResult,
    _result_from_returns,
)
from src.exceptions import InsufficientDataError
from src.logger import get_logger
from src.utils import TRADING_DAYS_PER_YEAR

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 상수 (튜닝 지점)
# ----------------------------------------------------------------------
DEFAULT_VOL_LOOKBACK_D = 63       # 역변동성 추정 룩백 (~3개월)
DEFAULT_MAX_WEIGHT = 0.25         # 종목당 비중 상한 (집중 위험 방지)
DEFAULT_CORR_THRESHOLD = 0.7      # 보유와 이 이상 상관이면 '중복 자산' 판정
DEFAULT_CORR_PENALTY = 0.5        # 중복 자산 비중 감액 배율
DEFAULT_KELLY_FRACTION = 0.5      # Kelly 상한 배율 (0.5 = half-Kelly)
DEFAULT_KELLY_LOOKBACK_D = 252    # Kelly 엣지 추정 룩백 (~1년)
DEFAULT_REBALANCE = "ME"          # 검증 백테스트 리밸런스 주기 (월말)
MIN_CORR_OVERLAP = 40             # 상관 판정에 필요한 최소 겹침 수익률 표본
MIN_KELLY_POINTS = 60             # Kelly 추정에 필요한 최소 수익률 표본
MIN_PORTFOLIO_ASSETS = 2          # 포트폴리오라 부를 최소 자산 수
_EPS = 1e-12                      # 부동소수 0 판정
_MIN_DAILY_VOL = 1e-8             # 일별 변동성 '~0' 판정 (무한 가중 방지)


# ----------------------------------------------------------------------
# 공용 내부 헬퍼
# ----------------------------------------------------------------------


def _daily_returns(prices: pd.Series) -> pd.Series:
    """가격 시리즈 → 일별 단순 수익률 (결측 제거)."""
    return prices.dropna().pct_change().dropna()


def _note(notes: list[str], msg: str) -> None:
    """자산별 판정 사유를 노트에 쌓고 동시에 로깅 (§4.10 #5 — 조용한 제외 금지)."""
    notes.append(msg)
    logger.info("portfolio: %s", msg)


# ----------------------------------------------------------------------
# 1. 변동성 역가중 (risk parity 유사)
# ----------------------------------------------------------------------


def _inverse_vol(
    prices: dict[str, pd.Series], lookback: int, max_weight: float
) -> tuple[dict[str, float], list[str]]:
    """inverse_vol_weights 본체 — (비중, 사유 노트) 를 함께 반환 (propose 용)."""
    if lookback < 2:
        raise ValueError(f"lookback 은 2 이상이어야 합니다: {lookback}")
    if not (0.0 < max_weight <= 1.0):
        raise ValueError(f"max_weight 는 (0, 1] 구간이어야 합니다: {max_weight}")

    notes: list[str] = []
    inv: dict[str, float] = {}
    for sym, series in prices.items():
        r = _daily_returns(series)
        if len(r) < lookback:
            _note(notes, f"{sym}: 수익률 표본 {len(r)}개 < 룩백 {lookback} — 제외")
            continue
        sd = float(r.iloc[-lookback:].std())
        if not np.isfinite(sd) or sd < _MIN_DAILY_VOL:
            _note(notes, f"{sym}: 변동성 ~0 — 제외 (역가중 무한대 방지)")
            continue
        inv[sym] = 1.0 / (sd * math.sqrt(TRADING_DAYS_PER_YEAR))

    # cap × 자산 수 < 1 이면 어떤 재배분도 합 1.0 을 못 만든다 → 자산 수 부족으로 취급.
    required = max(MIN_PORTFOLIO_ASSETS, math.ceil(1.0 / max_weight - _EPS))
    if len(inv) < required:
        raise InsufficientDataError(
            f"역변동성 가중에 사용 가능 자산 {len(inv)}개 — 최소 {required}개 필요"
            f" (상한 {max_weight:.0%} 로 합 1.0 달성 조건 포함)",
            n_points=len(inv), required=required,
        )

    total_inv = sum(inv.values())
    weights = {k: v / total_inv for k, v in inv.items()}

    # 상한 초과분을 미달 자산에 역변동성 비례로 재배분 (반복 — 재배분이 새 초과를
    # 만들 수 있음). required 가드 덕분에 free 가 비기 전에 반드시 수렴.
    fixed: dict[str, float] = {}
    free = dict(weights)
    while True:
        over = [k for k, v in free.items() if v > max_weight + _EPS]
        if not over:
            break
        for k in over:
            _note(notes, f"{k}: 비중 상한 {max_weight:.0%} 적용 (역변동성 원비중 {weights[k]:.1%})")
            fixed[k] = max_weight
            free.pop(k)
        remaining = 1.0 - max_weight * len(fixed)
        inv_free = sum(inv[k] for k in free)
        free = {k: inv[k] / inv_free * remaining for k in free}

    merged = {**free, **fixed}
    return {k: merged[k] for k in prices if k in merged}, notes


def inverse_vol_weights(
    prices: dict[str, pd.Series],
    lookback: int = DEFAULT_VOL_LOOKBACK_D,
    max_weight: float = DEFAULT_MAX_WEIGHT,
) -> dict[str, float]:
    """변동성 역가중 목표 비중 (risk parity 유사) — 합 1.0 으로 정규화.

    각 자산의 최근 ``lookback`` 개 일별 수익률로 연환산 변동성을 추정하고,
    1/변동성에 비례해 배분합니다. 종목당 ``max_weight`` 상한을 두고 초과분은
    미달 자산에 역변동성 비례로 재배분.

    가드 (§4.10 #5):
    - 수익률 표본 < lookback 또는 변동성 ~0 인 자산은 사유를 로깅하고 **제외**
      (0 변동성이 무한 가중을 받는 사고 방지).
    - 사용 가능 자산이 ``max(2, ceil(1/max_weight))`` 미만이면
      ``InsufficientDataError`` — 상한 산술상 합 1.0 이 불가능한 경우 포함.

    Raises
    ------
    ValueError
        lookback < 2 이거나 max_weight ∉ (0, 1].
    InsufficientDataError
        가드 통과 자산 수 부족.
    """
    weights, _ = _inverse_vol(prices, lookback, max_weight)
    return weights


# ----------------------------------------------------------------------
# 2. 상관 페널티 (기존 보유와의 중복 감액)
# ----------------------------------------------------------------------


def _correlation_penalty(
    weights: dict[str, float],
    prices: dict[str, pd.Series],
    held: dict[str, pd.Series],
    threshold: float,
    penalty: float,
) -> tuple[dict[str, float], list[str]]:
    """correlation_penalty 본체 — (비중, 사유 노트) 를 함께 반환 (propose 용)."""
    if not (0.0 < threshold < 1.0):
        raise ValueError(f"threshold 는 (0, 1) 구간이어야 합니다: {threshold}")
    if not (0.0 <= penalty <= 1.0):
        raise ValueError(f"penalty 는 [0, 1] 구간이어야 합니다: {penalty}")

    notes: list[str] = []
    if not held:
        return dict(weights), notes

    held_returns = {name: _daily_returns(s) for name, s in held.items()}
    out: dict[str, float] = {}
    for sym, w in weights.items():
        series = prices.get(sym)
        if series is None:
            _note(notes, f"{sym}: 가격 시계열 없음 — 상관 판정 보류 (페널티 미적용)")
            out[sym] = w
            continue
        rc = _daily_returns(series)
        worst_name: str | None = None
        worst_corr = -np.inf
        for hname, rh in held_returns.items():
            joined = pd.concat({"cand": rc, "held": rh}, axis=1, join="inner").dropna()
            if len(joined) < MIN_CORR_OVERLAP:
                logger.debug(
                    "correlation_penalty: %s↔%s 겹침 %d개 < %d — 판정 보류",
                    sym, hname, len(joined), MIN_CORR_OVERLAP,
                )
                continue
            c = float(joined["cand"].corr(joined["held"]))
            if not np.isfinite(c):        # 상수 시리즈 등 — 상관 정의 불가
                continue
            if c > worst_corr:
                worst_corr, worst_name = c, hname
        if worst_name is not None and worst_corr > threshold:
            out[sym] = w * penalty
            _note(
                notes,
                f"{sym}: 보유 '{worst_name}' 와 상관 {worst_corr:+.2f} > {threshold:.2f}"
                f" — 비중 ×{penalty:g}",
            )
        else:
            out[sym] = w

    total = sum(out.values())
    if total < _EPS:                      # penalty=0 + 전 후보 중복 → 신규 배분 없음
        _note(notes, "전 후보가 보유와 중복 (페널티 후 합 0) — 신규 배분 없음 (현금 100%)")
        return {k: 0.0 for k in out}, notes
    return {k: v / total for k, v in out.items()}, notes


def correlation_penalty(
    weights: dict[str, float],
    prices: dict[str, pd.Series],
    held: dict[str, pd.Series],
    threshold: float = DEFAULT_CORR_THRESHOLD,
    penalty: float = DEFAULT_CORR_PENALTY,
) -> dict[str, float]:
    """기존 보유와 상관 높은 후보의 비중을 감액 후 재정규화.

    각 후보에 대해 **모든** 보유 자산과의 상관을 보고, 하나라도 ``threshold`` 를
    초과하면 비중에 ``penalty`` 를 곱한 뒤 전체를 다시 합 1.0 으로 정규화합니다.

    상관 산식: 두 시계열의 **겹치는 날짜의 일별 수익률** (inner join) 피어슨 상관.
    겹침이 ``MIN_CORR_OVERLAP`` 개 미만인 (후보, 보유) 쌍은 통계적으로 판정할 수
    없어 **보류** — 그 쌍으로는 페널티를 주지 않습니다 (§4.10 #5: 결측을 벌점의
    근거로 쓰지 않음). 상수 시리즈처럼 상관이 정의되지 않는(NaN) 쌍도 보류.

    주의:
    - 후보가 보유 목록에 그대로 있으면 자기 자신과 상관 ≈ 1.0 → 페널티 대상
      (이미 들고 있는 것을 또 사는 셈이므로 의도된 동작).
    - '감액 후 재정규화' 구조라 **모든** 후보가 똑같이 감액되면 상대 비중이
      복원됩니다 — 페널티는 후보 간 상대 배분을 바꾸는 장치입니다.
    - held 가 비어 있으면 입력 비중 그대로 (복사본) 반환.

    Raises
    ------
    ValueError
        threshold ∉ (0, 1) 또는 penalty ∉ [0, 1].
    """
    out, _ = _correlation_penalty(weights, prices, held, threshold, penalty)
    return out


# ----------------------------------------------------------------------
# 3. Kelly fraction 소프트 상한 (소프: 파산 회피)
# ----------------------------------------------------------------------


def _kelly_cap(
    weights: dict[str, float],
    prices: dict[str, pd.Series],
    cap_fraction: float,
    lookback: int,
    risk_free_rate: float,
) -> tuple[dict[str, float], list[str]]:
    """kelly_cap 본체 — (비중, 사유 노트) 를 함께 반환 (propose 용)."""
    if cap_fraction <= 0.0:
        raise ValueError(f"cap_fraction 은 양수여야 합니다: {cap_fraction}")
    if lookback < 2:
        raise ValueError(f"lookback 은 2 이상이어야 합니다: {lookback}")

    notes: list[str] = []
    rf_daily = risk_free_rate / TRADING_DAYS_PER_YEAR
    out: dict[str, float] = {}
    for sym, w in weights.items():
        series = prices.get(sym)
        r = _daily_returns(series) if series is not None else pd.Series(dtype=float)
        r = r.iloc[-lookback:]
        if len(r) < MIN_KELLY_POINTS:
            out[sym] = 0.0
            _note(
                notes,
                f"{sym}: Kelly 추정 표본 {len(r)}개 < {MIN_KELLY_POINTS}"
                " — 엣지 미상, 비중 0 (모르는 엣지에 베팅 금지)",
            )
            continue
        var = float(r.var())
        if not np.isfinite(var) or var < _EPS:
            out[sym] = 0.0
            _note(notes, f"{sym}: 수익률 분산 ~0 — Kelly 정의 불가, 비중 0")
            continue
        f_star = (float(r.mean()) - rf_daily) / var
        if f_star <= _EPS:                # 음(0)의 엣지 — 베팅 근거 없음
            out[sym] = 0.0
            _note(notes, f"{sym}: 추정 엣지 음수/0 (f*={f_star:+.2f}) — 비중 0")
            continue
        cap = cap_fraction * f_star
        if w > cap + _EPS:
            out[sym] = cap
            _note(
                notes,
                f"{sym}: Kelly 상한 — {w:.1%} → {cap:.1%}"
                f" ({cap_fraction:g}×f*, f*={f_star:.2f})",
            )
        else:
            out[sym] = w

    total = sum(out.values())
    if total > 1.0 + _EPS:                # 입력 합 > 1 이었던 경우만 발생 가능
        out = {k: v / total for k, v in out.items()}
        _note(notes, f"비중 합 {total:.2f} > 1 — 합 1.0 으로 정규화")
    return out, notes


def kelly_cap(
    weights: dict[str, float],
    prices: dict[str, pd.Series],
    cap_fraction: float = DEFAULT_KELLY_FRACTION,
    lookback: int = DEFAULT_KELLY_LOOKBACK_D,
    risk_free_rate: float = 0.0,
) -> dict[str, float]:
    """자산별 Kelly fraction 상한 적용 — 이것은 **상한이지 목표가 아님**.

    최근 ``lookback`` 개 일별 수익률로 full-Kelly f* = 평균초과수익 / 분산을
    추정하고, 각 비중을 ``cap_fraction × f*`` (기본 half-Kelly) 로 자릅니다.

    현금 처리: 상한 적용으로 비중 합이 1 미만이 되면 **그대로 둡니다** — 남는
    몫은 현금입니다 (소프: 확신 없는 만큼은 베팅하지 않는다). 합이 1 을 넘는
    입력이 들어온 경우에만 합 1.0 으로 정규화.

    가드 (§4.10 #5):
    - f* ≤ 0 (음의 엣지) → 비중 **0** (음수 Kelly 는 '팔라' 는 뜻 — long-only
      사이징에서는 배분 근거 없음).
    - 분산 ~0 → f* 정의 불가(무한대) → 비중 0 (퇴화 데이터가 무제한 베팅을
      정당화하지 않도록).
    - 수익률 표본 < ``MIN_KELLY_POINTS`` → 비중 0 (엣지 추정 불가).

    ⚠️ 추정 한계: 일별 평균수익률의 표준오차(σ/√n)가 평균 자체보다 큰 경우가
    흔해 f* 는 매우 noisy — 그래서 half-Kelly 기본값이며, 상한으로만 사용.

    Raises
    ------
    ValueError
        cap_fraction ≤ 0 또는 lookback < 2.
    """
    out, _ = _kelly_cap(weights, prices, cap_fraction, lookback, risk_free_rate)
    return out


# ----------------------------------------------------------------------
# 4. 파이프라인 — 제안 비중
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioProposal:
    """포지션 사이징 파이프라인 결과 (정보 제공용 — 주문 집행 없음)."""

    weights: dict[str, float]        # 후보별 목표 비중 (합 ≤ 1.0)
    cash_weight: float               # 1 − Σweights (Kelly 상한이 남긴 몫)
    notes: tuple[str, ...] = ()      # 자산별 제외/감액/상한 사유 (한국어)

    def to_dict(self) -> dict:
        return {
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "cash_weight": round(self.cash_weight, 4),
            "notes": list(self.notes),
        }

    def __str__(self) -> str:
        invested = sum(self.weights.values())
        lines = [f"제안 비중 — 투자 {invested:.1%} + 현금 {self.cash_weight:.1%}"]
        for sym, w in sorted(self.weights.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {sym:<12} {w:7.1%}")
        if self.cash_weight > 1e-6:
            lines.append(f"  {'현금':<12} {self.cash_weight:7.1%}")
        if self.notes:
            lines.append("메모:")
            lines.extend(f"  · {n}" for n in self.notes)
        return "\n".join(lines)


def propose(
    candidates: Iterable[str],
    prices: dict[str, pd.Series],
    held: dict[str, pd.Series] | None = None,
    *,
    lookback: int = DEFAULT_VOL_LOOKBACK_D,
    max_weight: float = DEFAULT_MAX_WEIGHT,
    corr_threshold: float = DEFAULT_CORR_THRESHOLD,
    corr_penalty: float = DEFAULT_CORR_PENALTY,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    kelly_lookback: int = DEFAULT_KELLY_LOOKBACK_D,
) -> PortfolioProposal:
    """사이징 파이프라인: 역변동성 → 상관 페널티(보유 있을 때) → Kelly 상한.

    Parameters
    ----------
    candidates : Iterable[str]
        배분 대상 후보 심볼들 (선별·점수화는 signals/screener 상류에서 완료 가정).
        중복은 제거, prices 에 없는 심볼은 사유 노트와 함께 제외.
    prices : dict[str, pd.Series]
        심볼 → 종가 시리즈 (후보를 포함해야 함).
    held : dict[str, pd.Series] | None
        현재 보유 자산의 이름 → 종가 시리즈. 주어지면 상관 페널티 적용.

    Returns
    -------
    PortfolioProposal
        weights 합 ≤ 1.0, 나머지는 cash_weight. notes 에 자산별
        제외/감액/상한 사유 (한국어) — 파이프라인 순서대로.

    Raises
    ------
    InsufficientDataError
        가드 통과 후보 수 부족 (inverse_vol_weights 가드 참조).
    ValueError
        파라미터 구간 위반.
    """
    notes: list[str] = []
    unique: list[str] = []
    for c in candidates:
        if c in unique:
            continue
        unique.append(c)
        if c not in prices:
            _note(notes, f"{c}: 가격 시계열 없음 — 제외")
    sub = {c: prices[c] for c in unique if c in prices}

    weights, n1 = _inverse_vol(sub, lookback, max_weight)
    notes.extend(n1)
    if held:
        weights, n2 = _correlation_penalty(weights, sub, held, corr_threshold, corr_penalty)
        notes.extend(n2)
    weights, n3 = _kelly_cap(weights, sub, kelly_fraction, kelly_lookback, 0.0)
    notes.extend(n3)

    invested = sum(weights.values())
    cash = min(1.0, max(0.0, 1.0 - invested))
    return PortfolioProposal(weights=weights, cash_weight=cash, notes=tuple(notes))


# ----------------------------------------------------------------------
# 5. 사이징 룰 검증 백테스터 (13d — All Weather 리플레이 포함 범용)
# ----------------------------------------------------------------------


def equal_weights(prices: dict[str, pd.Series]) -> dict[str, float]:
    """동일가중 벤치마크용 weight_fn — 데이터 있는 자산에 1/n 씩.

    수익률이 하나라도 있는(가격 2개 이상) 자산만 대상. 전무하면
    InsufficientDataError.
    """
    usable = [sym for sym, s in prices.items() if len(s.dropna()) >= 2]
    if len(usable) < MIN_PORTFOLIO_ASSETS:
        raise InsufficientDataError(
            f"동일가중에 사용 가능 자산 {len(usable)}개 — 최소 {MIN_PORTFOLIO_ASSETS}개 필요",
            n_points=len(usable), required=MIN_PORTFOLIO_ASSETS,
        )
    return {sym: 1.0 / len(usable) for sym in usable}


def weighted_backtest(
    prices: dict[str, pd.Series],
    weight_fn: Callable[[dict[str, pd.Series]], dict[str, float]],
    rebalance: str = DEFAULT_REBALANCE,
    cost_bps: float = DEFAULT_COST_BPS,
    *,
    name: str = "가중 리밸런스 포트폴리오",
    risk_free_rate: float = 0.0,
    min_points: int = MIN_BACKTEST_POINTS,
) -> BacktestResult:
    """사이징 룰(weight_fn)을 주기 리밸런스로 리플레이 — 룰 '검증' 용 백테스트.

    각 리밸런스일 d 에 **d 까지의 가격만** weight_fn 에 전달해 목표 비중을 얻고
    (룩어헤드 없음), 다음 리밸런스까지 보유합니다. d 종가에 정한 비중은 d→d+1
    수익률부터 적용 (backtest.py 규약 동일). 거래비용은 |Δ비중| 합(턴오버)에
    편도 bps — 최초 진입 비용 포함.

    현금 처리: weight_fn 이 합 < 1 인 비중을 돌려주면 (예: kelly_cap 의 현금
    잔여) 나머지는 수익률 0 인 현금으로 취급됩니다.

    weight_fn 계약:
    - 입력: 심볼 → '해당 시점까지의' 종가 시리즈 (결측 제거됨)
    - 출력: 심볼 → 비중. **음수 금지, 합 ≤ 1** (위반 시 ValueError — 설정 오류).
    - 데이터 부족 시 InsufficientDataError 를 raise 하면 그 회차는 직전 비중 유지
      (직전이 없으면 스킵 — 평가 기간은 첫 성공 리밸런스부터).

    성과 지표는 ``src.backtest._result_from_returns`` 재사용 — 초기자본 앵커 MDD,
    변동성 0 샤프 가드 등 산식을 backtest.py 와 단일 출처로 유지.

    Raises
    ------
    ValueError
        cost_bps < 0, 또는 weight_fn 출력 계약 위반.
    InsufficientDataError
        성공한 리밸런스가 0회이거나 평가 표본 < min_points.
    """
    if cost_bps < 0:
        raise ValueError(f"cost_bps 는 음수일 수 없습니다: {cost_bps}")
    if not prices:
        raise InsufficientDataError("가격 유니버스가 비어 있습니다.", n_points=0, required=1)

    # 혼합 캘린더(주식/크립토) 정렬 — backtest.walk_forward_topn 과 동일 규약.
    panel = pd.DataFrame(prices).sort_index().ffill()
    if panel.dropna(how="all").empty:
        raise InsufficientDataError("가격 패널이 비어 있습니다.", n_points=0, required=1)
    tickers = list(panel.columns)

    reb_dates = panel.index.to_series().resample(rebalance).last().dropna().tolist()

    weights_events: dict[pd.Timestamp, pd.Series] = {}
    prev_w: pd.Series | None = None
    n_computed = 0
    for d in reb_dates:
        hist = {t: panel[t].loc[:d].dropna() for t in tickers}
        hist = {t: s for t, s in hist.items() if not s.empty}
        try:
            raw = weight_fn(hist)
        except InsufficientDataError as e:
            logger.debug("weighted_backtest: %s 리밸런스 스킵 — %s", d.date(), e)
            if prev_w is not None:
                weights_events[d] = prev_w        # 직전 비중 유지
            continue
        unknown = set(raw) - set(tickers)
        if unknown:
            raise ValueError(f"weight_fn 이 유니버스 밖 심볼을 반환: {sorted(unknown)}")
        w = pd.Series({t: float(raw.get(t, 0.0)) for t in tickers})
        if (w < -_EPS).any():
            raise ValueError(f"weight_fn 이 음수 비중을 반환: {w[w < -_EPS].to_dict()}")
        if float(w.sum()) > 1.0 + 1e-6:
            raise ValueError(f"weight_fn 비중 합 {float(w.sum()):.4f} > 1")
        weights_events[d] = w
        prev_w = w
        n_computed += 1

    if n_computed == 0:
        raise InsufficientDataError(
            "성공한 리밸런스 0회 — 데이터 기간이 weight_fn 요구 룩백보다 짧습니다.",
            n_points=0, required=1,
        )

    nonzero_dates = [d for d, w in weights_events.items() if w.abs().sum() > _EPS]
    d0 = min(nonzero_dates) if nonzero_dates else min(weights_events)
    weights = pd.DataFrame(weights_events).T.reindex(panel.index).ffill().fillna(0.0)

    returns = panel.pct_change()
    gross = (weights.shift(1) * returns).sum(axis=1)              # 현금 몫은 수익률 0
    turnover = weights.diff().abs().sum(axis=1)
    turnover.loc[d0] = float(weights.loc[d0].abs().sum())         # 최초 진입도 거래

    eval_idx = panel.index[panel.index > d0]
    if len(eval_idx) < min_points:
        raise InsufficientDataError(
            f"{name}: 평가 표본 {len(eval_idx)}개 — 최소 {min_points}개 필요",
            n_points=len(eval_idx), required=min_points,
        )
    gross_eval = gross.reindex(eval_idx)
    turnover_eval = turnover.reindex(eval_idx).copy()
    turnover_eval.iloc[0] += float(turnover.loc[d0])              # 진입 비용을 첫 기간에
    net = gross_eval - turnover_eval * (cost_bps / 1e4)
    n_trades = int((turnover.loc[d0:] > _EPS).sum())

    return _result_from_returns(
        name, net,
        gross_returns=gross_eval,
        n_trades=n_trades,
        cost_bps=cost_bps,
        risk_free_rate=risk_free_rate,
        min_points=min_points,
    )
