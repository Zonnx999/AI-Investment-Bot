"""
src/signals.py
==============
Phase 5 — Signal Engine. 친구 C 봇("저평가 종목 알림")의 진화 버전.

세 가지 신호를 결정론적으로 생성합니다 (LLM 없음):
  1. **팩터 점수** — momentum / value / quality 각 0~100 + 종합
  2. **스크리닝 룰** — "ROE > 10%, FCF yield 양수, P/E ≤ 동료 중간값" 필터로 종목 발굴
  3. **알림 룰** — 자산 낙폭(고정 임계표·확대·히스테리시스), 단기 변동성 급등,
     하이일드 스프레드 급등, 원/달러 급변, 국면 전환('중립 이하' 경계 통과만)
     (모두 '지난 커밋 상태 대비 변화' — storage 의 state 테이블 signals/global:v2)

설계: 점수/룰/알림 판정은 **순수 함수** (데이터를 인자로 받음 → 오프라인 테스트 가능).
데이터 fetch 는 generate_signal_report() 오케스트레이터만 담당하고, 이 함수는 **상태를
쓰지 않는다**. 다음 상태는 report.pending_state 로 돌려주고, 저장은 발송 성공 후
commit_signal_state(report) 만 한다 (dry-run·점검 스크립트가 알림을 먹어치우던 MSG-09).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.exceptions import ConfigError, DataFetchError, InsufficientDataError
from src.logger import get_logger
from src.screener import calculate_health_score, calculate_value_score, latest_fundamentals
from src.storage import StorageError, get_storage
from src.utils import TRADING_DAYS_PER_YEAR, close_series

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

# ----------------------------------------------------------------------
# 상수 (튜닝 지점)
# ----------------------------------------------------------------------
DEFAULT_SIGNAL_TICKERS = ("CPNG", "NVDA")

MOMENTUM_LOOKBACK_LONG_D = 126    # ~6개월
MOMENTUM_LOOKBACK_SHORT_D = 63    # ~3개월
MOMENTUM_SKIP_D = 21             # 스킵-먼스 (최근 1개월 제외, Jegadeesh-Titman)
MOMENTUM_12M_D = 252             # ~12개월
MA_LONG_WINDOW = 200              # 장기 추세선

# 종합 점수 가중치 — 합 1.0 (4팩터: 모멘텀/밸류/퀄리티/로우볼)
COMPOSITE_WEIGHTS = {"momentum": 0.25, "value": 0.25, "quality": 0.25, "low_vol": 0.25}

# 스크리닝 룰
SCREEN_MIN_ROE = 0.10             # ROE > 10%
SCREEN_MIN_FCF_YIELD = 0.0        # FCF yield 양수
# P/E 는 절대 임계 대신 '유효 P/E 의 중간값 이하' (업종 중간값의 워치리스트 근사)

# ---- 알림 룰 (10년 시뮬레이션 근거: 2017-01~2026-10 평일 2,546일, 전일 종가 기준) ----
# 낙폭: 자산별 고정 임계표. 값은 각 자산 장기 연변동성(σ)의 약 0.6~0.75배로 잡았다
# (예: SPY σ≈17% → -10, BTC σ≈60%+ → -25, KOSDAQ σ≈22% → -15). 변동성 정규화(k·σ1y)는
# 급락 직후 σ 가 커지며 임계가 느슨해지는 내생성 때문에 기각 (2026 KOSPI 가 -29% 에야 울림).
# 이 규칙: 🔔 연 10.0회, 14일 내 반전 6% (현행 -10%/6개월/무히스테리시스는 42줄·69%).
# 과적합 방지를 위해 표 값은 이번 라운드 고정 — 재튜닝은 원장 데이터가 쌓인 뒤에만.
DD_THRESHOLDS_PCT = {
    "S&P 500": -10, "장기국채 (20Y+)": -10, "회사채 (투자등급)": -6, "하이일드 채권": -6,
    "금": -10, "원자재": -12, "비트코인": -25, "KOSPI": -12, "KOSDAQ": -15,
}
DD_DEFAULT_PCT = -10.0
DD_PEAK_LOOKBACK_CAL_DAYS = 365   # 52주 고점 창 (자기 캘린더, 마지막 관측일 기준)
DD_MIN_OBS = 200                  # 52주 창 관측치가 이보다 적으면 평가 생략
DD_ESCALATE_MULT = 1.75           # 확대: 진입 임계의 1.75배 (에피소드당 1회 🔔)
DD_CLEAR_FRACTION = 0.5           # 해제: 임계의 절반 위로 회복
CLEAR_CONFIRM_DATES = 3           # 해제 조건이 '서로 다른 관측일' 로 3번 연속 (0.5T·1일이면 반전 10%)

# 변동성 급등: σ20/σ252 ≥ 2.0 진입, < 1.25 해제 (9개 자산 합계 연 2.5회. 1.75 면 5.3회)
VOL_SHORT_WIN = 20
VOL_LONG_WIN = 252
VOL_RATIO_ENTER = 2.0
VOL_RATIO_CLEAR = 1.25

CHG_LOOKBACK_OBS = 20             # 20관측(영업일) 변화 — 신용·환율 공통

# 신용: HY OAS 20영업일 변화 (FRED 는 3년치만 제공 — 표본 3건, 신뢰도 낮음)
CREDIT_SERIES = "BAMLH0A0HYM2"
CREDIT_D20_ENTER = 0.50           # %p — 연 1.0회
CREDIT_D20_ESCALATE = 1.00        # %p — 2025-04-04 에 걸림
CREDIT_D20_CLEAR = 0.15

# 환율: 원/달러 20관측 변화율 (|Δ| ≥ 5% 연 1.7회)
FX_LABEL = "원/달러"
FX_CHG_ENTER_PCT = 5.0
FX_CHG_CLEAR_PCT = 2.5

# 국면: 같은 후보가 서로 다른 KST 날짜 2번 관측되면 확정 (전환 21→17회), 🔔 는 '중립 이하'
# 경계 통과만 (연 1.0회)
REGIME_CONFIRM_DATES = 2

KR_ASSET_LABELS = ("KOSPI", "KOSDAQ")
NON_DRAWDOWN_LABELS = (FX_LABEL,)   # 낙폭·변동성 규칙을 적용하지 않는 패널 항목

Severity = Literal["info", "warning", "critical"]


# ----------------------------------------------------------------------
# 데이터 구조
# ----------------------------------------------------------------------


@dataclass
class FactorScores:
    """한 종목의 팩터 점수 (각 0~100)."""

    ticker: str
    momentum: int
    value: int
    quality: int
    composite: int
    notes: list[str] = field(default_factory=list)
    low_vol: int = 0   # 4번째 팩터 (Low Volatility)
    vol_pct: float | None = None   # 연환산 변동성 % — 단일 계산 지점 (low_vol 근거)


@dataclass
class Alert:
    severity: Severity
    category: str      # "regime" | "drawdown" | "volatility" | "credit" | "fx"
    message: str
    key: str = ""      # "dd:KOSPI:enter" 등 — 중복 제거·테스트용 식별자

    def __str__(self) -> str:
        icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}[self.severity]
        return f"{icon} [{self.category}] {self.message}"


@dataclass
class AssetObs:
    """한 자산의 관측 요약 (자기 캘린더 기준, asset_observations 산출)."""

    label: str
    asof: str                       # 마지막 관측일 YYYY-MM-DD
    last: float
    chg_1d_pct: float | None
    chg_1d_abs: float | None
    chg_20d_pct: float | None
    dd_52w_pct: float | None        # 365일 창 최고 종가 대비 (%)
    vs_ma200_pct: float | None
    vol_ratio: float | None         # σ(20 수익률) / σ(252 수익률)
    n_52w: int = 0                  # 52주 창 관측 수 (DD_MIN_OBS 판정용)


@dataclass
class KrContext:
    """KR 아침 메시지용 한국 시장 스냅샷 (조회 실패 항목은 None)."""

    kospi: AssetObs | None
    kosdaq: AssetObs | None
    usdkrw: AssetObs | None


@dataclass
class RegimeView:
    """국면 표시용 결과 (regime_view 산출).

    shown_label: 메시지에 보일 라벨 (불완전 시 '직전 판정 유지' 문구 포함).
    held: 지표 불완전으로 직전 판정을 유지 중인가.
    pending/pending_count: 확정 대기 중인 후보 라벨과 관측된 서로 다른 날짜 수
      (렌더 예: "· 변화 관찰 중: ⚪ 중립 1/2", 분모는 REGIME_CONFIRM_DATES).
    prev_label: 이번에 확정된 전환의 직전 라벨 (렌더 예: "(어제: 🟢 위험선호)").
    reason_line: 근거 1줄 (들여쓰기·Markdown 특수문자 없음).
    """

    shown_label: str
    score: int
    available: int
    total: int
    held: bool
    pending: str | None
    prev_label: str | None
    reason_line: str
    pending_count: int = 0


@dataclass
class SignalReport:
    # 기존 6필드 — tests/test_digest._report 처럼 위치·키워드 생성 호환 유지
    generated_at: str
    regime_label: str               # = regime.shown_label
    factors: list[FactorScores]
    candidates: list[dict]          # 스크리닝 통과 종목
    alerts: list[Alert]             # 🔔 푸시 (critical → warning 순)
    first_run: bool = False         # 비교 기준 없음(첫 실행·기준 읽기 실패) — 변화 알림 생략됨
    # 신규 (전부 기본값)
    regime: RegimeView | None = None
    kr: KrContext | None = None     # include_snapshot 일 때만
    active: list[str] = field(default_factory=list)      # '경보 중' 요약 문자열
    cleared: list[str] = field(default_factory=list)     # 해제 노트 (비푸시)
    data_issues: list[str] = field(default_factory=list)  # 조회 실패·노후 등 (Markdown 안전)
    baseline_missing: bool = False  # 상태 읽기 실패 (첫 실행과 구분)
    pending_state: dict | None = None   # commit_signal_state 가 저장할 다음 상태


# ----------------------------------------------------------------------
# 1. 팩터 — momentum (순수 함수)
# ----------------------------------------------------------------------


def momentum_score(prices: pd.Series) -> tuple[int, list[str]]:
    """가격 시리즈 → 모멘텀 점수 (0~100, 연속) + 근거.

    연속 점수 (구버전 0/33/67/100 → 세분화):
      A. 스킵-먼스 모멘텀 (70%): 12개월-1개월 수익률 (Jegadeesh-Titman 1993 —
         최근 1개월 제외가 단기 평균회귀 노이즈를 걷어내 신호를 강화).
         데이터 부족 시 3개월 수익률로 대체.
      B. 200일선 위치 (30%): 이동평균 대비 ±% 거리.
    매핑: 0% → 50점, +30%(A)/+10%(B) → ~83점 (선형, 0~100 클램프).
    """
    p = prices.dropna()
    if len(p) < MOMENTUM_LOOKBACK_SHORT_D + 1:
        raise InsufficientDataError(
            f"모멘텀 평가에 데이터 {len(p)}개 — 최소 {MOMENTUM_LOOKBACK_SHORT_D + 1}개 필요",
            n_points=len(p), required=MOMENTUM_LOOKBACK_SHORT_D + 1,
        )
    notes: list[str] = []
    scores: list[float] = []
    weights: list[float] = []
    SKIP, LONG = MOMENTUM_SKIP_D, MOMENTUM_12M_D  # 모듈 상수 (테스트 오버라이드 가능)

    if len(p) > LONG + SKIP:
        r = float(p.iloc[-SKIP] / p.iloc[-(LONG + SKIP)] - 1) * 100
        scores.append(max(0.0, min(100.0, 50.0 + r * (50.0 / 30.0))))
        weights.append(0.70)
        notes.append(f"12-1개월(skip) 수익률 {r:+.1f}% {'↑' if r > 0 else '↓'}")
    else:
        r = float(p.iloc[-1] / p.iloc[-(MOMENTUM_LOOKBACK_SHORT_D + 1)] - 1) * 100
        scores.append(max(0.0, min(100.0, 50.0 + r * (50.0 / 15.0))))
        weights.append(0.70)
        notes.append(f"3개월 수익률 {r:+.1f}% (데이터 부족, 단기 대체)")

    if len(p) >= MA_LONG_WINDOW:
        ma = float(p.rolling(MA_LONG_WINDOW).mean().iloc[-1])
        last = float(p.iloc[-1])
        pct = (last / ma - 1) * 100
        scores.append(max(0.0, min(100.0, 50.0 + pct * (50.0 / 10.0))))
        weights.append(0.30)
        notes.append(f"200일선 {'위' if pct > 0 else '아래'} {pct:+.1f}%")

    final = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
    return round(final), notes


def annualized_vol_pct(prices: pd.Series) -> float | None:
    """연환산 변동성 % — 팩터/알림이 공유하는 **단일 계산 지점**.

    252(거래일) 연환산 — 주식 기준. 크립토(365일 거래)엔 적용되지 않음
    (크립토는 calculate_crypto_scores 사용). 추후 크립토가 이 경로를 타면 365 필요.
    수익률이 비면(가격 <2개) None.
    """
    returns = prices.dropna().pct_change().dropna()
    if returns.empty:
        return None
    return float(returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100)


def low_vol_score(prices: pd.Series) -> tuple[int, list[str], float | None]:
    """Low Volatility 팩터 (0~100) — 변동성 낮을수록 높음.

    Robeco/MSCI 가 독립적 알파 원천으로 확인한 팩터. 연환산 변동성 기준
    ~15% → 100, ~30% → 50, ~60% → 25 (score = clip(30/vol × 50)).

    Returns (점수, 근거, vol_pct) — vol_pct 는 데이터 부족 시 None (중립 50점).
    호출부(factor_scores)가 vol_pct 를 FactorScores 에 실어 노출 (low_vol 점수 근거).
    변동성 급등 알림은 종목이 아니라 자산 패널의 σ20/σ252 로 따로 판정.
    """
    p = prices.dropna()
    if len(p) < MOMENTUM_LOOKBACK_SHORT_D:
        return 50, ["변동성 데이터 부족 — 중립(50)"], None
    vol = annualized_vol_pct(p)
    if vol is None:
        return 50, ["변동성 데이터 부족 — 중립(50)"], None
    score = max(0.0, min(100.0, (30.0 / max(vol, 1.0)) * 50.0))
    return round(score), [f"연환산 변동성 {vol:.1f}% → Low Vol {round(score)}"], vol


# ----------------------------------------------------------------------
# 2. 스크리닝 룰 (순수 함수)
# ----------------------------------------------------------------------


def apply_screen_rules(rows: list[dict]) -> list[dict]:
    """지표 행들에 발굴 룰 적용 → 통과 종목만 (통과 사유 포함).

    rows 의 각 dict: {"ticker", "pe", "roe", "fcf_yield"} (pe 는 None 가능).
    룰: ROE > 10% AND FCF yield > 0 AND (P/E 유효 시) P/E ≤ 유효 P/E 중간값.
    P/E 중간값 비교는 '업종 중간값' 의 워치리스트 근사 — 적자(P/E 없음) 종목은
    P/E 룰만 면제하고 나머지 룰로 평가.
    """
    valid_pes = [r["pe"] for r in rows if r.get("pe") and r["pe"] > 0]
    pe_median = statistics.median(valid_pes) if valid_pes else None

    passing: list[dict] = []
    for r in rows:
        reasons: list[str] = []
        if r.get("roe") is None or r["roe"] <= SCREEN_MIN_ROE:
            continue
        reasons.append(f"ROE {r['roe'] * 100:.1f}% > {SCREEN_MIN_ROE * 100:.0f}%")

        if r.get("fcf_yield") is None or r["fcf_yield"] <= SCREEN_MIN_FCF_YIELD:
            continue
        reasons.append(f"FCF yield {r['fcf_yield'] * 100:.1f}% 양수")

        pe = r.get("pe")
        if pe and pe > 0 and pe_median is not None:
            if pe > pe_median:
                continue
            reasons.append(f"P/E {pe:.1f} ≤ 중간값 {pe_median:.1f}")

        passing.append({**r, "reasons": reasons})

    # 정렬: 저PER 우선이되 같은 PER 대역이면 고ROE 우선 (PER만 보면 value-trap 위험).
    # (이미 ROE>10% 필터를 통과한 후보군이라 1차 가드는 있음)
    passing.sort(key=lambda r: (r.get("pe") or float("inf"), -(r.get("roe") or 0.0)))
    return passing


# ----------------------------------------------------------------------
# 3. 알림 룰 (순수 함수 — 관측값 + 이전 상태 → (푸시 알림, 해제 노트, 병합 상태))
# ----------------------------------------------------------------------
#
# 공통 규칙
# - 진입은 즉시 🔔(warning), 확대는 에피소드당 1회 🔔(critical).
# - 해제는 '서로 다른 관측일(asof)' 기준 CLEAR_CONFIRM_DATES 번 연속 조건 충족 시 —
#   같은 asof 가 다시 와도(같은 날 KR→US 런) 세지 않는다. 해제는 🔔 가 아니라 노트.
# - 상태는 이전 상태에 **병합** — 이번 런에 관측이 없는 자산은 이전 상태 그대로 (ALR-08).


def _mmdd(iso: str | None) -> str:
    return iso[5:10] if iso and len(iso) >= 10 else (iso or "?")


def _count_ok(st: dict, asof: str) -> None:
    """해제 조건을 만족한 관측 1건 — 같은 asof 는 한 번만 센다."""
    if st.get("ok_asof") != asof:
        st["ok"] = int(st.get("ok", 0)) + 1
        st["ok_asof"] = asof


def _reset_ok(st: dict) -> None:
    st["ok"] = 0
    st["ok_asof"] = None


def asset_observations(panel: pd.DataFrame) -> dict[str, AssetObs]:
    """종가 패널 → 자산별 관측 요약 (순수 함수).

    **각 자산 자기 캘린더**(컬럼별 dropna)로 계산 — BTC(주말 거래)와 ETF·지수(평일)가
    섞인 패널에서 서로의 NaN 이 끼지 않도록. 52주 고점은 마지막 관측일에서
    DD_PEAK_LOOKBACK_CAL_DAYS(365일) 안의 최고 종가 (6개월 창에서 옛 고점이 빠져나가며
    생기던 '가짜 회복' 방지). vol_ratio = σ(최근 20 수익률) / σ(최근 252 수익률).
    """
    out: dict[str, AssetObs] = {}
    if panel is None or panel.empty:
        return out
    for label in panel.columns:
        s = panel[label].dropna()
        s = s[s > 0]          # 0·음수 가격은 수익률을 망가뜨림 — 불량 틱 방어
        if s.empty:
            continue
        last_ts = pd.Timestamp(s.index[-1])
        last = float(s.iloc[-1])

        chg_1d_pct = chg_1d_abs = None
        if len(s) >= 2:
            prev = float(s.iloc[-2])
            chg_1d_abs = last - prev
            chg_1d_pct = (last / prev - 1) * 100
        chg_20d_pct = None
        if len(s) > CHG_LOOKBACK_OBS:
            chg_20d_pct = (last / float(s.iloc[-(CHG_LOOKBACK_OBS + 1)]) - 1) * 100

        window = s.loc[last_ts - pd.Timedelta(days=DD_PEAK_LOOKBACK_CAL_DAYS):]
        dd_52w = (last / float(window.max()) - 1) * 100

        vs_ma200 = None
        if len(s) >= MA_LONG_WINDOW:
            vs_ma200 = (last / float(s.iloc[-MA_LONG_WINDOW:].mean()) - 1) * 100

        vol_ratio = None
        r = s.pct_change().dropna()
        if len(r) >= VOL_LONG_WIN:
            long_sd = float(r.iloc[-VOL_LONG_WIN:].std())
            if long_sd > 0:
                vol_ratio = float(r.iloc[-VOL_SHORT_WIN:].std()) / long_sd

        out[str(label)] = AssetObs(
            label=str(label), asof=last_ts.strftime("%Y-%m-%d"), last=last,
            chg_1d_pct=chg_1d_pct, chg_1d_abs=chg_1d_abs, chg_20d_pct=chg_20d_pct,
            dd_52w_pct=dd_52w, vs_ma200_pct=vs_ma200, vol_ratio=vol_ratio,
            n_52w=len(window),
        )
    return out


def drawdown_threshold(label: str) -> float:
    """자산별 낙폭 경보 임계 (%, 음수)."""
    return float(DD_THRESHOLDS_PCT.get(label, DD_DEFAULT_PCT))


def drawdown_transitions(
    obs: dict[str, AssetObs], prev: dict | None,
) -> tuple[list[Alert], list[str], dict]:
    """52주 고점 대비 낙폭 경보 — 고정 임계표 + 확대(1.75T) + 해제(0.5T × 서로 다른 관측일 3회).

    Returns (푸시 알림, 해제 노트, 병합 상태). 상태 항목:
    {on, T(진입 시점 값으로 고정), entered, worst, esc, ok, ok_asof}.
    진입 시점에 이미 확대 구간이면 critical 1건으로 합쳐 낸다 (같은 날 2줄 방지).
    """
    alerts: list[Alert] = []
    notes: list[str] = []
    state = {k: dict(v) for k, v in (prev or {}).items() if isinstance(v, dict)}
    for label, o in obs.items():
        if label in NON_DRAWDOWN_LABELS:
            continue
        if o.dd_52w_pct is None or o.n_52w < DD_MIN_OBS:
            continue
        dd = o.dd_52w_pct
        st = state.get(label)
        if not st or not st.get("on"):
            T = drawdown_threshold(label)
            if dd > T:
                continue
            esc = dd <= DD_ESCALATE_MULT * T
            state[label] = {"on": True, "T": T, "entered": o.asof, "worst": dd,
                            "esc": esc, "ok": 0, "ok_asof": None}
            if esc:
                alerts.append(Alert(
                    "critical", "drawdown",
                    f"{label} 52주 고점 대비 {dd:+.1f}% — 경보 임계 {T:.0f}% 진입, "
                    f"낙폭 확대(임계의 {DD_ESCALATE_MULT:g}배)",
                    key=f"dd:{label}:enter",
                ))
            else:
                alerts.append(Alert(
                    "warning", "drawdown",
                    f"{label} 52주 고점 대비 {dd:+.1f}% — 경보 임계 {T:.0f}% 진입",
                    key=f"dd:{label}:enter",
                ))
            continue

        T = float(st.get("T", drawdown_threshold(label)))
        st["worst"] = min(float(st.get("worst", dd)), dd)
        if not st.get("esc") and dd <= DD_ESCALATE_MULT * T:
            st["esc"] = True
            alerts.append(Alert(
                "critical", "drawdown",
                f"{label} 52주 고점 대비 {dd:+.1f}% — 낙폭 확대(임계의 {DD_ESCALATE_MULT:g}배)",
                key=f"dd:{label}:escalate",
            ))
        if dd >= DD_CLEAR_FRACTION * T:
            _count_ok(st, o.asof)
            if st["ok"] >= CLEAR_CONFIRM_DATES:
                notes.append(f"해제: {label} {dd:+.1f}% ({_mmdd(st.get('entered'))}~{_mmdd(o.asof)})")
                state[label] = {"on": False, "cleared": o.asof}
        else:
            _reset_ok(st)
    return alerts, notes, state


def vol_ratio_transitions(
    obs: dict[str, AssetObs], prev: dict | None,
) -> tuple[list[Alert], list[str], dict]:
    """단기 변동성 급등 — σ20/σ252 ≥ 2.0 진입, < 1.25 가 서로 다른 관측일 3회면 해제(비푸시).

    같은 자산 패널(ALERT_PANEL)로 계산. 환율은 제외 (원/달러는 fx_transition 담당).
    """
    alerts: list[Alert] = []
    notes: list[str] = []
    state = {k: dict(v) for k, v in (prev or {}).items() if isinstance(v, dict)}
    for label, o in obs.items():
        if label in NON_DRAWDOWN_LABELS or o.vol_ratio is None:
            continue
        ratio = o.vol_ratio
        st = state.get(label)
        if not st or not st.get("on"):
            if ratio >= VOL_RATIO_ENTER:
                state[label] = {"on": True, "entered": o.asof, "ok": 0, "ok_asof": None}
                alerts.append(Alert(
                    "warning", "volatility",
                    f"{label} 단기 변동성 급등 — 20일 σ 가 1년 σ 의 {ratio:.1f}배",
                    key=f"vol:{label}:enter",
                ))
            continue
        if ratio < VOL_RATIO_CLEAR:
            _count_ok(st, o.asof)
            if st["ok"] >= CLEAR_CONFIRM_DATES:
                notes.append(f"해제: {label} 변동성 정상화 ({ratio:.1f}배)")
                state[label] = {"on": False, "cleared": o.asof}
        else:
            _reset_ok(st)
    return alerts, notes, state


def credit_transition(
    hy: pd.Series | None, prev: dict | None,
) -> tuple[list[Alert], list[str], dict]:
    """하이일드 스프레드(HY OAS) 20영업일 변화 — +0.50%p 진입, +1.00%p 확대(1회),
    +0.15%p 미만이 서로 다른 관측일 3회면 해제. hy=None(조회 실패)이면 상태 유지.

    표본이 3년·3건뿐이라(FRED 라이선스) 문구에 '참고용' 을 명시한다.
    """
    state = dict(prev or {})
    if hy is None:
        return [], [], state
    s = hy.dropna()
    if len(s) <= CHG_LOOKBACK_OBS:
        return [], [], state
    last = float(s.iloc[-1])
    d20 = last - float(s.iloc[-(CHG_LOOKBACK_OBS + 1)])
    asof = pd.Timestamp(s.index[-1]).strftime("%Y-%m-%d")
    alerts: list[Alert] = []
    notes: list[str] = []
    if not state.get("on"):
        if d20 >= CREDIT_D20_ENTER:
            esc = d20 >= CREDIT_D20_ESCALATE
            state = {"on": True, "entered": asof, "esc": esc, "ok": 0, "ok_asof": None}
            alerts.append(Alert(
                "critical" if esc else "warning", "credit",
                f"하이일드 스프레드 20일 {d20:+.2f}%p 급등 ({last:.2f}%) · 표본 적어 참고용",
                key="credit:enter",
            ))
        return alerts, notes, state
    if not state.get("esc") and d20 >= CREDIT_D20_ESCALATE:
        state["esc"] = True
        alerts.append(Alert(
            "critical", "credit",
            f"하이일드 스프레드 20일 {d20:+.2f}%p — 급등 확대 ({last:.2f}%)",
            key="credit:escalate",
        ))
    if d20 < CREDIT_D20_CLEAR:
        _count_ok(state, asof)
        if state["ok"] >= CLEAR_CONFIRM_DATES:
            notes.append(f"해제: 하이일드 스프레드 급등 (20일 {d20:+.2f}%p)")
            state = {"on": False, "cleared": asof}
    else:
        _reset_ok(state)
    return alerts, notes, state


def fx_transition(
    fx: AssetObs | None, prev: dict | None,
) -> tuple[list[Alert], list[str], dict]:
    """원/달러 20관측 변화율 |Δ| ≥ 5% 진입, |Δ| < 2.5% 가 서로 다른 관측일 3회면 해제.

    KRW=X 는 '1달러당 원' — 상승 = 원화 약세. 낙폭 규칙은 적용하지 않음.
    """
    state = dict(prev or {})
    if fx is None or fx.chg_20d_pct is None:
        return [], [], state
    chg = fx.chg_20d_pct
    alerts: list[Alert] = []
    notes: list[str] = []
    if not state.get("on"):
        if abs(chg) >= FX_CHG_ENTER_PCT:
            side = "원화 약세" if chg > 0 else "원화 강세"
            state = {"on": True, "entered": fx.asof, "ok": 0, "ok_asof": None}
            alerts.append(Alert(
                "warning", "fx", f"원/달러 20일 {chg:+.1f}% ({side})", key="fx:enter",
            ))
        return alerts, notes, state
    if abs(chg) < FX_CHG_CLEAR_PCT:
        _count_ok(state, fx.asof)
        if state["ok"] >= CLEAR_CONFIRM_DATES:
            notes.append(f"해제: 원/달러 급변 진정 (20일 {chg:+.1f}%)")
            state = {"on": False, "cleared": fx.asof}
    else:
        _reset_ok(state)
    return alerts, notes, state


def _regime_side(score: int | None) -> str | None:
    """'중립 이하'(score ≤ 0) 경계의 어느 쪽인가. 점수 모르면 None."""
    if score is None:
        return None
    return "down" if score <= 0 else "up"


def regime_view(
    report, prev: dict | None, today_kst: str,
) -> tuple[RegimeView, Alert | None, dict]:
    """국면 표시·확정·알림 판정 (순수 함수). Returns (표시, 알림|None, 다음 국면 상태).

    - 불완전(지표 n/3): 직전 확정 라벨 + '(직전 판정 유지 · 지표 n/3)' 를 보이고,
      알림 없음, **상태 불변** (실패가 전환으로 둔갑하지 않게).
    - 완전: 확정 라벨과 같으면 대기 후보 삭제. 다르면 후보로 쌓고, 같은 후보가
      **서로 다른 KST 날짜** 2번 관측되면 확정 (같은 날 KR→US 는 1회). 첫 실행은 즉시 확정.
    - 확정 시 '중립 이하'(score ≤ 0) 경계를 넘을 때만 Alert. 같은 쪽 안의 전환은
      prev_label 로만 노출 (렌더러가 '(어제: …)' 표시).
    상태: {"confirmed", "confirmed_score", "pending", "pending_dates": [...]}.
    """
    prev = dict(prev or {})
    confirmed = prev.get("confirmed")
    available = int(getattr(report, "available", 0))
    total = int(getattr(report, "total", 0))
    reason = report.reason_line() if hasattr(report, "reason_line") else ""
    complete = bool(getattr(report, "complete", False))

    if not complete:
        shown = (f"{confirmed} (직전 판정 유지 · 지표 {available}/{total})" if confirmed
                 else f"⚪ 판정 보류 (지표 {available}/{total})")
        pending = prev.get("pending")
        view = RegimeView(
            shown_label=shown, score=int(report.score), available=available, total=total,
            held=True, pending=pending, prev_label=None, reason_line=reason,
            pending_count=len(set(prev.get("pending_dates") or [])) if pending else 0,
        )
        return view, None, prev

    label, score = report.regime, int(report.score)

    def _view(shown: str, pending: str | None = None, count: int = 0,
              prev_label: str | None = None) -> RegimeView:
        return RegimeView(
            shown_label=shown, score=score, available=available, total=total, held=False,
            pending=pending, prev_label=prev_label, reason_line=reason, pending_count=count,
        )

    if not confirmed:   # 첫 실행 — 즉시 확정, 알림 없음
        state = {"confirmed": label, "confirmed_score": score, "pending": None,
                 "pending_dates": []}
        return _view(label), None, state

    if label == confirmed:
        state = {**prev, "confirmed_score": score, "pending": None, "pending_dates": []}
        return _view(label), None, state

    dates = list(prev.get("pending_dates") or []) if prev.get("pending") == label else []
    if today_kst not in dates:
        dates.append(today_kst)
    if len(set(dates)) < REGIME_CONFIRM_DATES:
        state = {**prev, "pending": label, "pending_dates": dates}
        return _view(confirmed, pending=label, count=len(set(dates))), None, state

    # 확정
    old_side = _regime_side(prev.get("confirmed_score"))
    new_side = _regime_side(score)
    alert = None
    if old_side is not None and old_side != new_side:
        alert = Alert(
            "warning", "regime", f"시장 국면 전환: {confirmed} → {label}",
            key=f"regime:{label}",
        )
    state = {"confirmed": label, "confirmed_score": score, "pending": None,
             "pending_dates": []}
    return _view(label, prev_label=confirmed), alert, state


def active_summaries(state: dict, obs: dict[str, AssetObs]) -> list[str]:
    """'경보 중' 요약 줄들 — 병합 상태에서 on 인 항목 (Markdown 특수문자 없음).

    예: "KOSPI -25% (07-03~, 임계 -12%)", "원/달러 급변 중 (20일 +5.9%)".
    """
    out: list[str] = []
    dd_state = state.get("dd") or {}
    order = sorted(dd_state, key=lambda a: (0 if a in KR_ASSET_LABELS else 1))
    for label in order:
        st = dd_state[label]
        if not isinstance(st, dict) or not st.get("on"):
            continue
        o = obs.get(label)
        dd = o.dd_52w_pct if (o and o.dd_52w_pct is not None) else st.get("worst")
        dd_txt = f"{dd:.0f}%" if isinstance(dd, (int, float)) else "?"
        out.append(f"{label} {dd_txt} ({_mmdd(st.get('entered'))}~, 임계 {float(st.get('T', 0)):.0f}%)")
    for label, st in (state.get("vol") or {}).items():
        if isinstance(st, dict) and st.get("on"):
            o = obs.get(label)
            ratio = f" ({o.vol_ratio:.1f}배)" if (o and o.vol_ratio is not None) else ""
            out.append(f"{label} 변동성 급등{ratio}")
    if (state.get("credit") or {}).get("on"):
        out.append(f"하이일드 스프레드 급등 ({_mmdd(state['credit'].get('entered'))}~)")
    if (state.get("fx") or {}).get("on"):
        o = obs.get(FX_LABEL)
        chg = f" (20일 {o.chg_20d_pct:+.1f}%)" if (o and o.chg_20d_pct is not None) else ""
        out.append(f"원/달러 급변{chg}")
    return out


def _alert_sort_key(a: Alert) -> tuple[int, int]:
    """critical → warning, 같은 등급 안에서는 KR 자산 → 미국 → 신용/환율 → 국면."""
    sev = {"critical": 0, "warning": 1, "info": 2}.get(a.severity, 3)
    if a.category in ("drawdown", "volatility"):
        asset = a.key.split(":")[1] if a.key.count(":") >= 2 else ""
        group = 0 if asset in KR_ASSET_LABELS else 1
    elif a.category in ("credit", "fx"):
        group = 2
    else:
        group = 3
    return sev, group


# ----------------------------------------------------------------------
# 오케스트레이션 — fetch + state 관리는 여기서만
# ----------------------------------------------------------------------

_STATE_NS = "signals"
STATE_KEY = "global:v2"      # KR·US 공유 글로벌 상태 (구 last_run / last_run:KR 은 읽지도 쓰지도 않음)
STATE_VERSION = 2


def factor_scores(ticker: str) -> FactorScores:
    """한 종목의 4팩터 점수 (모멘텀/밸류/퀄리티/로우볼). 데이터 미가용 시 0점 + note."""
    from src.data_fetcher import fetch_prices, fetch_quote

    # 2y 필수: 12-1 스킵월 모멘텀은 273 거래일 초과 필요 — 1y(~250행)면
    # 주 브랜치가 영원히 죽은 코드가 되고 3개월 폴백만 돌게 됨 (리뷰 회귀)
    closes = close_series(fetch_prices(ticker, period="2y"))
    momentum, notes = momentum_score(closes)
    low_vol, lv_notes, vol_pct = low_vol_score(closes)
    notes.extend(lv_notes)

    quote: dict = {}
    metrics: dict = {}
    try:
        quote = fetch_quote(ticker)
    except DataFetchError as e:
        notes.append(f"quote 미가용 ({type(e).__name__}) — value 점수 불완전")
    try:
        metrics = latest_fundamentals(ticker)   # key-metrics + ratios 병합
        if not metrics:
            notes.append("fundamentals 빈 응답 — value/quality 점수 불완전")
    except DataFetchError as e:
        notes.append(f"fundamentals 미가용 ({type(e).__name__}) — value/quality 점수 불완전")

    value = calculate_value_score(quote, metrics)
    quality = calculate_health_score(metrics)
    composite = round(
        momentum * COMPOSITE_WEIGHTS["momentum"]
        + value * COMPOSITE_WEIGHTS["value"]
        + quality * COMPOSITE_WEIGHTS["quality"]
        + low_vol * COMPOSITE_WEIGHTS["low_vol"]
    )
    return FactorScores(ticker, momentum, value, quality, composite, notes,
                        low_vol=low_vol, vol_pct=vol_pct)


def screen_candidates(tickers: list[str]) -> list[dict]:
    """워치리스트에서 발굴 룰 통과 종목 추출 (key-metrics 는 7일 캐시 — 쿼터 보호)."""
    from src.data_fetcher import fetch_key_metrics
    from src.utils import pick_first

    rows: list[dict] = []
    for t in tickers:
        try:
            df = fetch_key_metrics(t, limit=1)
        except DataFetchError as e:
            logger.info("스크리닝 스킵 %s — %s", t, e)
            continue
        if df.empty:
            continue
        m = df.iloc[-1]
        ey = pick_first(m, ["earningsYield"])
        # FMP stable 은 P/E 를 earningsYield 로 줌 → 역수. 음수 yield(적자)의 역수는
        # '음수 PER' 이라는 무의미한 저평가 신호가 되므로 **명시적으로 None** —
        # apply_screen_rules 가 '데이터 없음(P/E 룰 면제)' 경로로 처리 (§4.10 #5).
        rows.append({
            "ticker": t,
            "pe": (1.0 / ey) if (ey and ey > 0) else None,
            "roe": pick_first(m, ["returnOnEquity", "roe"]),
            "fcf_yield": pick_first(m, ["freeCashFlowYield"]),
        })
    return apply_screen_rules(rows)


def select_screened_tickers(n: int = 6, market: str = "US") -> list[str]:
    """스크리너가 뽑은 '오늘의 발굴 종목' 상위 N개 티커 (value+health 종합 점수순).

    매일 시장 상황에 따라 결과가 바뀜 → 고정 워치리스트 대신 다이제스트의
    팩터 표 주제로 사용. 스크리너 전체 실패 시 빈 리스트 (호출부가 폴백).
    """
    from src.screener import KR_WATCHLIST, US_WATCHLIST, screen_watchlist

    watchlist = list(US_WATCHLIST if market == "US" else KR_WATCHLIST)
    rows = screen_watchlist(watchlist, country_label=market)  # total_score 내림차순 정렬됨
    return [r["symbol"] for r in rows[:n]]


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def generate_signal_report(
    tickers: tuple[str, ...] = DEFAULT_SIGNAL_TICKERS,
    screen_tickers: list[str] | None = None,
    market: str = "US",
    *,
    include_snapshot: bool | None = None,
    today_kst: str | None = None,
) -> SignalReport:
    """일일 신호 리포트 생성 — **상태를 쓰지 않는다** (dry-run·점검 스크립트 안전).

    다음 상태는 ``report.pending_state`` 에 담아 돌려주고, 저장은 발송 성공 후
    ``commit_signal_state(report)`` 만 한다. 상태는 KR·US 두 런이 공유하는
    글로벌 하나(signals/global:v2) — US 런의 알림은 정의상 '아침 KR 커밋 이후 새로
    생긴 변화' 뿐이다.

    screen_tickers=None 이면 스크리닝 섹션 생략 (빈 리스트). tickers=() 면 팩터는 비고
    국면/알림만. include_snapshot: KR 시장 스냅샷(report.kr) 포함 여부 — None 이면
    market=="KR" 일 때만. today_kst: 국면 2일 확인용 날짜(YYYY-MM-DD, 테스트 주입용).
    """
    from src.macro_analyzer import (
        ALERT_PANEL,
        KR_PANEL,
        _fetch_resilient,
        classify_regime,
        failed_indicator_names,
        fetch_panel_with_failures,
        observation_age_issues,
    )

    market = (market or "US").upper()
    if include_snapshot is None:
        include_snapshot = market == "KR"
    today = today_kst or _today_kst()
    data_issues: list[str] = []

    # --- 비교 기준 (글로벌 공유 상태) — '없음' 과 '못 읽음' 을 구분 ---
    baseline_missing = False
    prev: dict | None = None
    try:
        raw_prev = get_storage().get_state(_STATE_NS, STATE_KEY, strict=True)
    except StorageError:
        logger.warning("신호 상태 읽기 실패 — 변화 알림 생략, 커밋 차단", exc_info=True)
        baseline_missing = True
        data_issues.append("비교 기준 읽기 실패 — 변화 알림 생략")
    else:
        if isinstance(raw_prev, dict) and raw_prev.get("v") == STATE_VERSION:
            prev = raw_prev
        elif raw_prev is not None:
            logger.warning("신호 상태 형식이 예상과 다름 — 첫 실행으로 취급")
    first_run = prev is None   # baseline_missing 도 '비교 기준 없음' → 변화 알림 생략
    base = prev or {}

    # --- 시장 수준: 국면 ---
    regime_report = classify_regime()
    if not regime_report.complete:
        failed = ", ".join(failed_indicator_names(regime_report)) or "지표"
        data_issues.append(
            f"국면 지표 {regime_report.available}/{regime_report.total} — {failed} 조회 실패"
        )
    data_issues.extend(observation_age_issues(regime_report, date.fromisoformat(today)))
    rview, regime_alert, regime_state = regime_view(regime_report, base.get("regime"), today)

    # --- 시장 수준: 자산 패널 (미국 7 + KOSPI/KOSDAQ + 원/달러) ---
    panel, failed_assets = fetch_panel_with_failures({**ALERT_PANEL, **KR_PANEL}, period="2y")
    for label in failed_assets:
        data_issues.append(f"가격 조회 실패: {label}")
    obs = asset_observations(panel)

    hy: pd.Series | None = None
    try:
        hy, _stale = _fetch_resilient(CREDIT_SERIES)
    except (ConfigError, DataFetchError) as e:
        logger.warning("신용 스프레드(%s) 조회 실패 — 신용 알림 상태 유지: %s", CREDIT_SERIES, e)
        data_issues.append("신용 스프레드 조회 실패")

    dd_alerts, dd_notes, dd_state = drawdown_transitions(obs, base.get("dd"))
    vol_alerts, vol_notes, vol_state = vol_ratio_transitions(obs, base.get("vol"))
    cr_alerts, cr_notes, cr_state = credit_transition(hy, base.get("credit"))
    fx_alerts, fx_notes, fx_state = fx_transition(obs.get(FX_LABEL), base.get("fx"))

    alerts: list[Alert] = []
    cleared: list[str] = []
    if not first_run:
        # 첫 실행(또는 기준 읽기 실패)은 비교 기준이 없으므로 변화 알림 일괄 생략 —
        # 상태만 시딩. '경보 중' 요약(active)이 현재 상황을 보여주므로 정보 손실 없음.
        alerts = dd_alerts + vol_alerts + cr_alerts + fx_alerts
        if regime_alert:
            alerts.append(regime_alert)
        alerts.sort(key=_alert_sort_key)
        cleared = dd_notes + vol_notes + cr_notes + fx_notes

    next_state = {
        "v": STATE_VERSION,
        "regime": regime_state,
        "dd": dd_state,
        "vol": vol_state,
        "credit": cr_state,
        "fx": fx_state,
        "ts": datetime.now(timezone.utc).isoformat(),
        "committed_by": market,
    }
    active = active_summaries(next_state, obs)

    kr = None
    if include_snapshot:
        kr = KrContext(kospi=obs.get("KOSPI"), kosdaq=obs.get("KOSDAQ"),
                       usdkrw=obs.get(FX_LABEL))

    # --- 종목 수준 ---
    factors: list[FactorScores] = []
    for t in tickers:
        try:
            factors.append(factor_scores(t))
        except (DataFetchError, InsufficientDataError) as e:
            logger.warning("팩터 스킵 %s — %s", t, e)

    candidates = screen_candidates(screen_tickers) if screen_tickers else []

    return SignalReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        regime_label=rview.shown_label,
        factors=factors,
        candidates=candidates,
        alerts=alerts,
        first_run=first_run,
        regime=rview,
        kr=kr,
        active=active,
        cleared=cleared,
        data_issues=data_issues,
        baseline_missing=baseline_missing,
        # 기준을 못 읽었으면 커밋 차단 — 좋은 상태를 시드로 덮어쓰지 않기 위함
        pending_state=None if baseline_missing else next_state,
    )


def commit_signal_state(report: SignalReport) -> bool:
    """report.pending_state 를 글로벌 상태로 저장 (발송 성공 후에만 호출할 것).

    pending_state 가 None(기준 읽기 실패)이면 저장하지 않고 False. 저장 실패도 False.
    클라우드 push(sync)는 호출부 책임 (send_daily_digest: 커밋 → sync).
    """
    if report.pending_state is None:
        logger.warning("신호 상태 커밋 생략 — pending_state 없음 (비교 기준 읽기 실패)")
        return False
    try:
        result = get_storage().put_state(_STATE_NS, STATE_KEY, report.pending_state)
    except StorageError:
        logger.warning("신호 상태 커밋 실패 — 저장소 사용 불가", exc_info=True)
        return False
    # put_state 는 best-effort(종전: None 반환). bool 을 돌려주는 버전이면 그대로 존중.
    return result is not False
