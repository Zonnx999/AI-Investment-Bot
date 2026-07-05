"""
src/backtest.py
===============
백테스트 프레임워크 (ROADMAP §2.1) — Phase 5 신호 / Phase 6 예측의 과거 성과 검증.

세 가지 도구:
  1. ``run_backtest``          — 가격 + 포지션 시리즈 → 성과 지표 (벡터화 단일 자산)
  2. ``walk_forward_topn``     — 팩터 점수 상위 N 종목 주기 리밸런싱 워크포워드
  3. ``evaluate_lead_lag_oos`` — 선행지표 예측의 아웃오브샘플 방향 적중률
     (predictors.analyze_lead_lag 가 찾은 lag 를 입력으로 받아 검증)

설계 (CLAUDE.md §4.4): 이 모듈은 **순수 함수만** — 네트워크/외부 API 호출 없음.
데이터 fetch 는 scripts/check_backtest.py 오케스트레이터가 담당.

룩어헤드 방지 규약: t 시점 종가에 결정한 포지션은 t→t+1 수익률부터 적용.
거래비용은 포지션 변화(턴오버)에 편도 bps 로 부과.

⚠️ 한계 (정직하게): 생존 편향(현재 워치리스트로 과거를 봄), 슬리피지 미모델링,
배당 미반영(Adj Close 사용 시 완화). 절대 성과가 아니라 신호의 상대적 유효성
판단용입니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from src.exceptions import InsufficientDataError
from src.logger import get_logger
from src.risk_engine import max_drawdown
from src.utils import TRADING_DAYS_PER_YEAR

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 상수 (튜닝 지점)
# ----------------------------------------------------------------------
DEFAULT_COST_BPS = 10.0          # 편도 거래비용 (bps). 10bp = 0.10%
MIN_BACKTEST_POINTS = 20         # 성과 통계가 의미를 갖는 최소 수익률 표본 수
DEFAULT_REBALANCE = "ME"         # 워크포워드 리밸런싱 주기 (월말)
MOMENTUM_LOOKBACK_D = 126        # 기본 스코어 룩백 (~6개월, signals 와 동일 사상)
MOMENTUM_SKIP_D = 21             # 스킵-먼스 (Jegadeesh-Titman)
MIN_TRAIN_MONTHS = 24            # lead-lag OOS 최소 학습 표본 (predictors 와 동일)
MIN_OOS_MONTHS = 6               # lead-lag OOS 최소 평가 표본
_EPS = 1e-12                     # 부동소수 0 판정 (턴오버/변동성)


# ----------------------------------------------------------------------
# 데이터 구조
# ----------------------------------------------------------------------


@dataclass
class BacktestResult:
    """단일 전략 백테스트 성과. 수치 필드는 전부 % 단위 (sharpe 제외)."""

    name: str
    start: pd.Timestamp
    end: pd.Timestamp
    n_periods: int                  # 수익률 표본 수 (일 단위 백테스트면 거래일 수)
    total_return_pct: float
    annualized_return_pct: float
    annualized_vol_pct: float
    sharpe: float                   # 변동성 0 이면 0.0 (§4.10 #5 — 0 나눗셈 가드)
    max_drawdown_pct: float         # 음수 (예: -23.1)
    hit_rate_pct: float             # 포지션 보유 기간 중 수익 기간 비율
    n_trades: int                   # 포지션이 변한 횟수
    cost_bps: float
    equity_curve: pd.Series = field(repr=False, compare=False, default=None)

    def to_dict(self) -> dict:
        """직렬화용 dict (equity_curve 제외 — 표/JSON 에 넣기엔 너무 큼)."""
        return {
            "name": self.name,
            "start": str(self.start.date()) if hasattr(self.start, "date") else str(self.start),
            "end": str(self.end.date()) if hasattr(self.end, "date") else str(self.end),
            "n_periods": self.n_periods,
            "total_return_pct": round(self.total_return_pct, 2),
            "annualized_return_pct": round(self.annualized_return_pct, 2),
            "annualized_vol_pct": round(self.annualized_vol_pct, 2),
            "sharpe": round(self.sharpe, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "hit_rate_pct": round(self.hit_rate_pct, 1),
            "n_trades": self.n_trades,
            "cost_bps": self.cost_bps,
        }

    def __str__(self) -> str:
        return (
            f"[{self.name}] {str(self.start)[:10]} ~ {str(self.end)[:10]}"
            f" ({self.n_periods}기간)\n"
            f"  총수익 {self.total_return_pct:+.1f}% | 연환산 {self.annualized_return_pct:+.1f}%"
            f" | 변동성 {self.annualized_vol_pct:.1f}% | 샤프 {self.sharpe:.2f}\n"
            f"  MDD {self.max_drawdown_pct:.1f}% | 적중률 {self.hit_rate_pct:.1f}%"
            f" | 매매 {self.n_trades}회 | 비용 {self.cost_bps:.0f}bp"
        )


@dataclass
class WalkForwardResult:
    """워크포워드 top-N 전략 vs 동일가중 매수후보유 벤치마크."""

    strategy: BacktestResult
    benchmark: BacktestResult
    picks: list[tuple[pd.Timestamp, tuple[str, ...]]]   # (리밸런스일, 선택 종목들)
    top_n: int
    rebalance: str

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.to_dict(),
            "benchmark": self.benchmark.to_dict(),
            "top_n": self.top_n,
            "rebalance": self.rebalance,
            "picks": [(str(d.date()), list(t)) for d, t in self.picks],
        }

    def __str__(self) -> str:
        excess = self.strategy.total_return_pct - self.benchmark.total_return_pct
        return (
            f"{self.strategy}\n{self.benchmark}\n"
            f"  초과수익 {excess:+.1f}%p (top-{self.top_n}, 리밸런스 {self.rebalance},"
            f" 리밸런스 {len(self.picks)}회)"
        )


@dataclass
class LeadLagBacktest:
    """선행지표 예측의 아웃오브샘플(확장 윈도우) 방향 적중률 검증 결과."""

    leading_name: str
    target_name: str
    lag: int                        # 검증에 사용한 선행 lag (개월)
    n_obs: int                      # 정렬된 전체 표본 수
    n_oos: int                      # 아웃오브샘플 평가 표본 수
    accuracy_pct: float             # 방향 적중률
    baseline_accuracy_pct: float    # 나이브 베이스라인 ('항상 상승' 가정) 적중률
    n_up_predictions: int           # OOS 중 상승 예측 횟수
    strategy_return_pct: float | None = None   # 예측 추종 long/flat 총수익 (target_returns 제공 시)
    buyhold_return_pct: float | None = None    # 같은 기간 매수후보유 총수익
    equity_curve: pd.Series | None = field(repr=False, compare=False, default=None)

    @property
    def beats_baseline(self) -> bool:
        return self.accuracy_pct > self.baseline_accuracy_pct

    def to_dict(self) -> dict:
        return {
            "leading_name": self.leading_name,
            "target_name": self.target_name,
            "lag": self.lag,
            "n_obs": self.n_obs,
            "n_oos": self.n_oos,
            "accuracy_pct": round(self.accuracy_pct, 1),
            "baseline_accuracy_pct": round(self.baseline_accuracy_pct, 1),
            "n_up_predictions": self.n_up_predictions,
            "strategy_return_pct": (
                round(self.strategy_return_pct, 2) if self.strategy_return_pct is not None else None
            ),
            "buyhold_return_pct": (
                round(self.buyhold_return_pct, 2) if self.buyhold_return_pct is not None else None
            ),
            "beats_baseline": self.beats_baseline,
        }

    def __str__(self) -> str:
        flag = "✅ 베이스라인 상회" if self.beats_baseline else "⚠️ 베이스라인 이하"
        lines = [
            f"[{self.leading_name} → {self.target_name}] lag {self.lag}개월,"
            f" OOS {self.n_oos}개월 (전체 {self.n_obs})",
            f"  방향 적중률 {self.accuracy_pct:.1f}% vs 항상-상승 {self.baseline_accuracy_pct:.1f}%  {flag}",
        ]
        if self.strategy_return_pct is not None and self.buyhold_return_pct is not None:
            lines.append(
                f"  예측 추종 long/flat {self.strategy_return_pct:+.1f}%"
                f" vs 매수후보유 {self.buyhold_return_pct:+.1f}%"
            )
        return "\n".join(lines)


# ----------------------------------------------------------------------
# 내부 헬퍼 — 순수 수익률 시리즈 → 성과 지표
# ----------------------------------------------------------------------


def _result_from_returns(
    name: str,
    net_returns: pd.Series,
    *,
    gross_returns: pd.Series | None = None,
    active_mask: pd.Series | None = None,
    n_trades: int = 0,
    cost_bps: float = 0.0,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    min_points: int = MIN_BACKTEST_POINTS,
) -> BacktestResult:
    """기간 수익률 시리즈(비용 차감 후) → BacktestResult.

    적중률은 gross_returns(비용 차감 전) 기준 — 방향 판단력을 재는 지표라
    비용으로 흐리지 않음. active_mask 가 없으면 전 기간을 대상으로 계산.
    """
    net = net_returns.dropna()
    if len(net) < min_points:
        raise InsufficientDataError(
            f"{name}: 수익률 표본 {len(net)}개 — 최소 {min_points}개 필요",
            n_points=len(net), required=min_points,
        )

    equity = (1.0 + net).cumprod()
    end_value = float(equity.iloc[-1])
    total_pct = (end_value - 1.0) * 100

    # 연환산 (CAGR). 전액 손실(가치 ≤ 0) 은 -100% 로 처리 — 음수 밑 거듭제곱 방지.
    if end_value <= 0:
        annualized_pct = -100.0
    else:
        annualized_pct = (end_value ** (periods_per_year / len(net)) - 1.0) * 100

    sd = float(net.std())
    vol_pct = sd * np.sqrt(periods_per_year) * 100
    # 샤프 — macro_analyzer.sharpe_ratio 와 동일 규약 (초과수익 평균/표준편차 연환산).
    # 변동성 0 (예: 무포지션/상수 수익률) 은 정의 불가 → 0.0 (§4.10 #5 가드).
    if sd < _EPS:
        sharpe = 0.0
    else:
        sharpe = float((net.mean() - risk_free_rate / periods_per_year) / sd) * np.sqrt(
            periods_per_year
        )

    # MDD 는 초기자본(1.0)을 피크 후보에 포함해야 함 — equity 가 (1+r0) 부터 시작하면
    # 표본 첫 기간부터 시작되는 하락이 러닝맥스에 안 잡혀 과소보고됨
    # (예: 100→90 후 횡보가 MDD 0% 로 나옴). 한 기간 앞에 1.0 앵커를 붙여 계산.
    if len(net) >= 2:
        anchor_ts = net.index[0] - (net.index[1] - net.index[0])
    else:
        anchor_ts = net.index[0] - pd.Timedelta(days=1)
    equity_anchored = pd.concat([pd.Series([1.0], index=[anchor_ts]), equity])
    mdd_pct = float(max_drawdown(equity_anchored).max_dd_pct)   # risk_engine 재사용

    hit_basis = gross_returns if gross_returns is not None else net
    if active_mask is not None:
        hit_basis = hit_basis[active_mask.reindex(hit_basis.index, fill_value=False)]
    hit_rate = float((hit_basis > 0).mean()) * 100 if len(hit_basis) > 0 else 0.0

    return BacktestResult(
        name=name,
        start=net.index[0],
        end=net.index[-1],
        n_periods=len(net),
        total_return_pct=float(total_pct),
        annualized_return_pct=float(annualized_pct),
        annualized_vol_pct=float(vol_pct),
        sharpe=sharpe,
        max_drawdown_pct=mdd_pct,
        hit_rate_pct=hit_rate,
        n_trades=n_trades,
        cost_bps=cost_bps,
        equity_curve=equity,
    )


# ----------------------------------------------------------------------
# 1. 벡터화 단일 자산 백테스터
# ----------------------------------------------------------------------


def run_backtest(
    prices: pd.Series,
    positions: pd.Series,
    cost_bps: float = DEFAULT_COST_BPS,
    name: str = "strategy",
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    min_points: int = MIN_BACKTEST_POINTS,
) -> BacktestResult:
    """가격 시리즈 + 포지션 시리즈 → 성과 지표.

    Parameters
    ----------
    prices : pd.Series
        종가 시리즈 (DatetimeIndex 권장 — MDD 기간 계산에 필요).
        단일 컬럼 DataFrame 이 오면 squeeze.
    positions : pd.Series
        각 시점 종가에 결정한 목표 포지션. {-1, 0, 1} 또는 비중(가중치).
        가격 인덱스에 reindex + ffill (결측 초반부는 0 = 무포지션).
    cost_bps : float
        편도 거래비용 (bps). 포지션 변화량 |Δpos| × cost 로 부과.

    룩어헤드 방지: t 종가에 결정한 포지션은 t→t+1 수익률에 적용.
    t 시점의 거래비용은 t 로 끝나는 기간 수익률에서 차감 (최초 진입 비용은
    첫 평가 기간에 합산 — 백테스트 시작일의 진입도 공짜가 아님).

    Raises
    ------
    InsufficientDataError
        수익률 표본이 min_points 미만이거나 포지션이 전부 결측일 때.
    """
    if isinstance(prices, pd.DataFrame):
        prices = prices.squeeze()
    if cost_bps < 0:
        raise ValueError(f"cost_bps 는 음수일 수 없습니다: {cost_bps}")

    p = prices.dropna()
    if positions.dropna().empty:
        raise InsufficientDataError(
            f"{name}: 포지션 시리즈가 비어있습니다.", n_points=0, required=1
        )
    pos = positions.astype(float).reindex(p.index).ffill().fillna(0.0)

    r = p.pct_change().iloc[1:]                       # 기간 수익률 (t-1 → t)
    if len(r) < min_points:
        raise InsufficientDataError(
            f"{name}: 수익률 표본 {len(r)}개 — 최소 {min_points}개 필요",
            n_points=len(r), required=min_points,
        )

    pos_held = pos.shift(1).reindex(r.index)          # r[t] 동안 보유한 포지션
    gross = pos_held * r

    turnover = pos.diff().abs()
    turnover.iloc[0] = abs(float(pos.iloc[0]))        # 최초 진입도 거래
    n_trades = int((turnover > _EPS).sum())
    # 비용을 수익률 인덱스로 정렬 — 시작일(t0) 진입 비용은 첫 평가 기간에 합산.
    turnover_r = turnover.iloc[1:].copy()
    turnover_r.iloc[0] += float(turnover.iloc[0])
    net = gross - turnover_r * (cost_bps / 1e4)

    return _result_from_returns(
        name, net,
        gross_returns=gross,
        active_mask=pos_held.abs() > _EPS,
        n_trades=n_trades,
        cost_bps=cost_bps,
        risk_free_rate=risk_free_rate,
        periods_per_year=periods_per_year,
        min_points=min_points,
    )


def buy_and_hold_positions(prices: pd.Series) -> pd.Series:
    """벤치마크용 상시 매수(1.0) 포지션 시리즈."""
    return pd.Series(1.0, index=prices.dropna().index)


def ma_crossover_positions(prices: pd.Series, window: int = 200) -> pd.Series:
    """장기 이동평균선 위 = 매수(1) / 아래 = 현금(0) — 추세추종 long/flat.

    signals.momentum_score 의 '200일선 위치' 성분을 매매 룰로 번역한 것.
    이동평균이 아직 정의되지 않는 초반 구간은 0 (무포지션).

    Raises
    ------
    InsufficientDataError
        가격이 window+1 개 미만이라 신호가 하나도 안 나올 때.
    """
    p = prices.dropna()
    if len(p) < window + 1:
        raise InsufficientDataError(
            f"이동평균({window}일) 신호에 가격 {len(p)}개 — 최소 {window + 1}개 필요",
            n_points=len(p), required=window + 1,
        )
    ma = p.rolling(window).mean()
    return (p > ma).astype(float)


# ----------------------------------------------------------------------
# 2. 워크포워드 팩터 top-N 평가
# ----------------------------------------------------------------------


def momentum_score_fn(
    prices: pd.Series,
    lookback: int = MOMENTUM_LOOKBACK_D,
    skip: int = MOMENTUM_SKIP_D,
) -> float:
    """기본 스코어 — 스킵-먼스 모멘텀 수익률 (signals.momentum_score A 성분과 동일 규약).

    p[-skip] / p[-(lookback+skip)] - 1. 높을수록 좋음.

    Raises
    ------
    InsufficientDataError
        가격이 lookback+skip 개 미만일 때 (워크포워드가 해당 종목을 스킵하는 신호).
    """
    p = prices.dropna()
    required = lookback + skip
    if len(p) < required:
        raise InsufficientDataError(
            f"모멘텀 스코어에 가격 {len(p)}개 — 최소 {required}개 필요",
            n_points=len(p), required=required,
        )
    return float(p.iloc[-skip] / p.iloc[-required] - 1.0)


def walk_forward_topn(
    prices: dict[str, pd.Series],
    score_fn: Callable[[pd.Series], float] | None = None,
    top_n: int = 3,
    rebalance: str = DEFAULT_REBALANCE,
    cost_bps: float = DEFAULT_COST_BPS,
    risk_free_rate: float = 0.0,
    min_points: int = MIN_BACKTEST_POINTS,
) -> WalkForwardResult:
    """리밸런스일마다 스코어 상위 N 종목을 동일가중 보유하는 워크포워드 평가.

    각 리밸런스일에 '그 시점까지의 데이터만' 으로 스코어 산출 (룩어헤드 없음).
    스코어 불가 종목(InsufficientDataError / NaN)은 그 회차에서 제외.
    유효 스코어가 top_n 미만인 회차는 직전 보유를 유지 (첫 유효 회차 전은 평가 제외).

    벤치마크: 첫 리밸런스일 기준 동일가중 매수후보유 (무비용 — 1회 진입이라
    비용 영향이 미미해 생략, 전략 쪽만 비용 부과로 보수적 비교).

    Parameters
    ----------
    prices : dict[str, pd.Series]
        티커 → 종가 시리즈.
    score_fn : Callable[[pd.Series], float]
        과거 가격 시리즈 → 스코어 (높을수록 좋음). 기본 momentum_score_fn.

    Raises
    ------
    InsufficientDataError
        유효한 리밸런스 회차가 한 번도 없거나 평가 표본 부족.
    """
    if top_n < 1:
        raise ValueError(f"top_n 은 1 이상이어야 합니다: {top_n}")
    if len(prices) < top_n:
        raise ValueError(f"종목 수 {len(prices)}개 < top_n {top_n}")
    score = score_fn or momentum_score_fn

    # 혼합 캘린더(주식/크립토) 정렬 — macro_analyzer.current_drawdown 과 같은 이유로 ffill.
    panel = pd.DataFrame(prices).sort_index().ffill()
    tickers = list(panel.columns)

    # 리밸런스일 = 각 주기의 마지막 거래일
    reb_dates = panel.index.to_series().resample(rebalance).last().dropna().tolist()

    weights_events: dict[pd.Timestamp, pd.Series] = {}
    picks: list[tuple[pd.Timestamp, tuple[str, ...]]] = []
    prev_w: pd.Series | None = None
    for d in reb_dates:
        scores: dict[str, float] = {}
        for t in tickers:
            hist = panel[t].loc[:d].dropna()
            try:
                s = score(hist)
            except InsufficientDataError:
                continue
            if pd.notna(s):
                scores[t] = float(s)
        if len(scores) < top_n:
            if prev_w is not None:
                weights_events[d] = prev_w      # 스코어 부족 → 직전 보유 유지
            continue
        chosen = tuple(t for t, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
        w = pd.Series(0.0, index=tickers)
        w[list(chosen)] = 1.0 / top_n
        weights_events[d] = w
        prev_w = w
        picks.append((d, chosen))

    if not picks:
        raise InsufficientDataError(
            "유효한 리밸런스 회차 0회 — 데이터 기간이 스코어 룩백보다 짧습니다.",
            n_points=0, required=1,
        )

    d0 = picks[0][0]                             # 첫 실제 포지션 시작일
    weights = pd.DataFrame(weights_events).T.reindex(panel.index).ffill().fillna(0.0)

    returns = panel.pct_change()
    gross = (weights.shift(1) * returns).sum(axis=1)              # NaN 수익률은 skipna
    turnover = weights.diff().abs().sum(axis=1)
    turnover.loc[d0] = float(weights.loc[d0].abs().sum())         # 최초 진입

    eval_idx = panel.index[panel.index > d0]
    if len(eval_idx) < min_points:
        raise InsufficientDataError(
            f"워크포워드 평가 표본 {len(eval_idx)}개 — 최소 {min_points}개 필요",
            n_points=len(eval_idx), required=min_points,
        )
    gross_eval = gross.reindex(eval_idx)
    turnover_eval = turnover.reindex(eval_idx).copy()
    turnover_eval.iloc[0] += float(turnover.loc[d0])              # 진입 비용을 첫 기간에
    net = gross_eval - turnover_eval * (cost_bps / 1e4)
    n_trades = int((turnover.loc[d0:] > _EPS).sum())

    strategy = _result_from_returns(
        f"모멘텀 top-{top_n} 워크포워드", net,
        gross_returns=gross_eval,
        n_trades=n_trades,
        cost_bps=cost_bps,
        risk_free_rate=risk_free_rate,
        min_points=min_points,
    )

    # 벤치마크 — d0 시점 가격이 있는 종목 동일가중 매수후보유
    base = panel.loc[d0]
    eligible = [t for t in tickers if pd.notna(base[t])]
    bench_equity = (panel.loc[d0:, eligible] / base[eligible]).mean(axis=1)
    bench_ret = bench_equity.pct_change().reindex(eval_idx)
    benchmark = _result_from_returns(
        f"동일가중 매수후보유 ({len(eligible)}종목)", bench_ret,
        n_trades=0,
        cost_bps=0.0,
        risk_free_rate=risk_free_rate,
        min_points=min_points,
    )

    return WalkForwardResult(
        strategy=strategy, benchmark=benchmark,
        picks=picks, top_n=top_n, rebalance=rebalance,
    )


# ----------------------------------------------------------------------
# 3. Lead-lag 예측 아웃오브샘플 평가
# ----------------------------------------------------------------------


def evaluate_lead_lag_oos(
    leading: pd.Series,
    target: pd.Series,
    lag: int,
    leading_name: str = "leading",
    target_name: str = "target",
    target_returns: pd.Series | None = None,
    min_train: int = MIN_TRAIN_MONTHS,
    min_oos: int = MIN_OOS_MONTHS,
    cost_bps: float = DEFAULT_COST_BPS,
    publication_lag_months: int = 1,
) -> LeadLagBacktest:
    """확장 윈도우 아웃오브샘플로 lead-lag 예측의 방향 적중률을 검증.

    predictors.analyze_lead_lag 는 전체 표본으로 lag·회귀를 적합 (인샘플) —
    여기서는 매 시점 t 마다 't 이전 데이터만' 으로 회귀를 다시 적합해
    target[t] 의 부호를 예측하고 실제와 비교합니다 (R² 과최적화 우려의 정량 해소).

    Parameters
    ----------
    leading, target : pd.Series
        analyze_lead_lag 에 넣었던 것과 같은 월간 시리즈 (예: YoY 증가율).
    lag : int
        검증할 선행 개월 수 (보통 analyze_lead_lag 결과의 best_lag_months).
        ⚠️ lag 자체를 전체 표본으로 골랐다면 그만큼 낙관 편향이 남음 — 해석 주의.
    target_returns : pd.Series | None
        목표 자산의 월간 단순 수익률. 주어지면 '예측 상승 시에만 보유' long/flat
        전략의 총수익과 매수후보유 총수익을 함께 계산. 없으면 적중률만.
    cost_bps : float
        long/flat 전략의 편도 거래비용 (bps).
    publication_lag_months : int
        선행지표의 발표 지연(월). FRED 월간 거시지표는 해당 월 데이터가 다음 달
        중순에야 공개되므로 기본 1. lag ≤ publication_lag 이면 t-lag 시점 값이
        t 월초에 아직 미발표 → **전략 수익 계산에서만** 신호를 그만큼 늦춰 체결해
        실거래 불가능한 선견편향을 제거. 적중률(accuracy)은 통계적 관계 측정이라
        보정하지 않음 — lag ≤ publication_lag 인 관계는 '실시간 매매용' 이 아니라
        '관계 존재 검증용' 으로 해석할 것.

    Raises
    ------
    InsufficientDataError
        정렬된 표본이 min_train + min_oos 미만일 때.
    """
    from scipy.stats import linregress

    if lag < 1:
        raise ValueError(f"lag 는 1 이상이어야 합니다 (0 은 '선행'이 아님): {lag}")
    if publication_lag_months < 0:
        raise ValueError(f"publication_lag_months 는 0 이상: {publication_lag_months}")

    joined = pd.concat([leading.shift(lag), target], axis=1, join="inner").dropna()
    n = len(joined)
    if n < min_train + min_oos:
        raise InsufficientDataError(
            f"{leading_name}→{target_name}: 정렬 표본 {n}개 — "
            f"최소 {min_train + min_oos}개 필요 (학습 {min_train} + 평가 {min_oos})",
            n_points=n, required=min_train + min_oos,
        )

    x = joined.iloc[:, 0].to_numpy()
    y = joined.iloc[:, 1].to_numpy()
    dates = joined.index

    preds: dict[pd.Timestamp, float] = {}
    for i in range(min_train, n):
        xs, ys = x[:i], y[:i]
        if np.ptp(xs) < _EPS:
            continue                    # 상수 입력 — 회귀 불가, 해당 시점 스킵
        reg = linregress(xs, ys)
        preds[dates[i]] = float(reg.slope * x[i] + reg.intercept)

    if len(preds) < min_oos:
        raise InsufficientDataError(
            f"{leading_name}→{target_name}: OOS 예측 {len(preds)}개 — 최소 {min_oos}개 필요",
            n_points=len(preds), required=min_oos,
        )

    actual = joined.iloc[:, 1].loc[list(preds.keys())]
    pred_up = pd.Series({d: v > 0 for d, v in preds.items()})
    actual_up = actual > 0
    accuracy = float((pred_up == actual_up).mean()) * 100
    baseline = float(actual_up.mean()) * 100        # '항상 상승' 예측의 적중률

    strategy_ret_pct: float | None = None
    buyhold_ret_pct: float | None = None
    equity: pd.Series | None = None
    if target_returns is not None:
        # 발표 지연 보정 — lag 개월 전 지표가 체결 시점에 이미 공개됐어야 거래 가능.
        # 부족분(extra)만큼 신호를 늦춰 '미발표 데이터로 매매' 하는 선견편향 제거.
        extra = max(0, publication_lag_months + 1 - lag)
        signal = pred_up.shift(extra).dropna().astype(bool) if extra else pred_up
        rets = target_returns.reindex(signal.index).dropna()
        if not rets.empty:
            pos = signal.reindex(rets.index).astype(float)
            turnover = pos.diff().abs()
            turnover.iloc[0] = abs(float(pos.iloc[0]))
            net = pos * rets - turnover * (cost_bps / 1e4)
            equity = (1.0 + net).cumprod()
            strategy_ret_pct = (float(equity.iloc[-1]) - 1.0) * 100
            buyhold_ret_pct = (float((1.0 + rets).prod()) - 1.0) * 100
        else:
            logger.warning(
                "%s→%s: target_returns 가 OOS 기간과 겹치지 않음 — 전략 수익 생략",
                leading_name, target_name,
            )

    return LeadLagBacktest(
        leading_name=leading_name,
        target_name=target_name,
        lag=lag,
        n_obs=n,
        n_oos=len(preds),
        accuracy_pct=accuracy,
        baseline_accuracy_pct=baseline,
        n_up_predictions=int(pred_up.sum()),
        strategy_return_pct=strategy_ret_pct,
        buyhold_return_pct=buyhold_ret_pct,
        equity_curve=equity,
    )
