"""
src/digest.py
=============
일일 다이제스트 v2 (Stage 2 WP-DIGEST) — "판정 먼저, 변화만 펼치고, 선정에 쓴 그 숫자만".

메시지 구조 (KR 아침 08:30 KST / US 저녁 09:00 ET)
  1 판정 헤더  `*🇰🇷 MM-DD(요일) HH:MM · 오늘 할 일: {판정}* — 사유`   (decide_actions 순수 함수)
  2 기준       `기준: 시세 MM-DD 종가 · 점수 MM-DD (N일 전)`           (항상)
  3 🔔 새 알림 — 이번 런에서 처음 생긴 이벤트만 (KR) / 🌐 아침 이후 변화 (US)
  4 🇰🇷 한국 시장 (KR 만) — 코스피·코스닥·원/달러 + 간밤 미국 1줄
  5 🌐 글로벌 (KR 만) — 국면 1줄 + 경보 유지 1줄
  6 📋 스크리닝 후보 — 🆕 3줄 / ➖ 1줄 / 유지 1줄 / ↔ 동점 교체, 점수는 DB total_score 하나
  7 📒 지난 후보 성과 — ledger.format_ledger_line (월요일 format_weekly_block)
  8 ⚙️ 상태 — 문제가 있을 때만, 고정 어휘 (health.HealthReport)
  9 면책 (평문)

설계 원칙
  - 렌더러는 전부 **순수 함수**(이미 계산된 객체 → 문자열, 오프라인 테스트). fetch·DB 읽기는
    `assemble_daily_digest` 만, 쓰기(state 커밋·원장·헬스·sync)는 `send_daily_digest` 가
    **발송 성공(sent ≥ 1) 이후에만**. dry-run 은 assemble 만 호출하므로 쓰기 0.
  - Markdown 엔티티는 정적 제목·판정 헤더의 `*…*` 만. 동적 문자열(회사명·사유·알림 문구)은
    `utils.md_safe` 를 통과 (CLAUDE.md §4.10 #10). 이탤릭·백틱·링크 금지.
  - 판정은 예측이 아니라 사실의 변화(후보 리스트 diff·새 알림·선정 후 하락)에서만 나온다.
  - 예측·제안 비중·LLM 요약은 검증 전이라 기본 제거 — config 플래그로만 재활성화 (코드 보존).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.config import settings
from src.exceptions import DataFetchError, InsufficientDataError, QuantBotError
from src.findings import (
    CONFIDENCE_RELIABLE,
    LABEL_CANDIDATES,
    LABEL_HEALTH,
    LABEL_VALUE,
    from_prediction,
)
from src.health import HealthReport, llm_status
from src.logger import get_logger
from src.signals import MA_LONG_WINDOW, REGIME_CONFIRM_DATES, Alert, SignalReport, annualized_vol_pct
from src.utils import MD_SAFE_NAME_LEN, close_series, md_safe

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

# ----------------------------------------------------------------------
# 상수
# ----------------------------------------------------------------------
_FLAG = {"KR": "🇰🇷", "US": "🇺🇸"}
_MARKET_KO = {"KR": "한국", "US": "미국"}
_WEEKDAY_KO = "월화수목금토일"
_SCAN_CMD = {"KR": "/scan kr", "US": "/scan us"}

DISCLAIMER = "규칙 기반 참고 자료 · 투자 자문 아님 — 투자 결정은 본인 책임"

# 길이 예산 (심사 설계 §8) — 테스트로 단언. notifier 의 3,900 절단은 최후 방어.
LEN_BUDGET_QUIET = {"KR": 1200, "US": 900}
LEN_BUDGET_CHANGE = 2000
LEN_HARD_CAP = 2500

MAX_ALERT_LINES = 5
MAX_NEW_LINES = 6
MAX_KEEP_LINES = 6
MAX_ACTIVE_SHOWN = 3
NEW_HIGH_EPS_PCT = 0.05          # 52주 고점比 이 이상이면 '52주 신고가'
# '선정 후' 수익률 절대값이 이 이상이면 비정상(분할·데이터 오류 의심) — 표시·-10% 판정 제외, 소유자 DM 상세
SINCE_ABS_MAX_PCT = 60.0
# KR 아침 🌐 '경보 유지' 목록에서 뺄 자산 — 🇰🇷 한국 시장 섹션이 이미 '⚠️ 경보 중' 으로 표시

VERDICT_NONE = "없음"
VERDICT_BUY = "매수 검토"
VERDICT_DROP = "탈락"
VERDICT_RISK = "리스크 점검"

# KR 아침 '간밤 미국' 1줄 (사용자 옵션 E 기본: 포함). yfinance 무료, fetch_prices 캐시 경유.
OVERNIGHT_US = {"S&P500": "^GSPC", "나스닥": "^IXIC", "10년물": "^TNX", "VIX": "^VIX"}

# 종료코드 (T3b §4 표) — scripts/send_digest.py · systemd 유닛(SuccessExitStatus=10) ·
# scripts/notify_owner.py(20 이면 중복 DM 생략) 가 이 값을 전제로 함.
EXIT_OK = 0
EXIT_PARTIAL_FAIL = 10
EXIT_ALL_FAIL = 11
EXIT_NO_RECIPIENTS = 12
EXIT_HOLD_NOTIFIED = 20
EXIT_HOLD_DM_FAILED = 21
EXIT_CONFIG_MISSING = 30

_HEALTH_NS = "health"


# ----------------------------------------------------------------------
# 데이터 구조
# ----------------------------------------------------------------------


@dataclass
class PriceMetrics:
    """종목 가격 지표 (yfinance 1y 종가 — 참고용, 점수 미반영)."""

    price: float                      # 마지막 Close (표시 가격)
    asof: str                         # YYYY-MM-DD
    adj_last: float | None            # 마지막 Adj Close ('선정 후' 계산 — 원장 진입가와 같은 기준)
    vs_ma200_pct: float | None
    dd_52w_pct: float | None
    mdd_1y_pct: float | None
    vol_pct: float | None
    # 같은 프레임의 Adj Close 시리즈 (tz 없는 날짜 index) — 진입 기준가를 **이 프레임에서** 다시
    # 구하기 위함. yfinance 는 분할·배당을 과거 가격에 소급 반영하므로, 채점기가 예전에 저장한
    # entry_px 와 오늘 받은 adj_last 를 섞으면 2:1 분할 뒤 '선정 후 -50%' 가 된다 (D2).
    adj_close: pd.Series | None = None


@dataclass
class PickView:
    """후보 한 종목의 렌더 입력."""

    row: Any                          # universe.ScanRow (duck-typed: symbol/name/total_score/...)
    kind: str                         # "new" | "keep" | "base"(첫 기록)
    metrics: PriceMetrics | None = None
    since_pct: float | None = None    # 선정 후 수익률 (원장 진입가 대비)
    since_date: str | None = None     # 연속 포함 시작일
    evidence: str = ""                # 🆕 근거 줄 (원값 3개 + 꼬리표)


@dataclass
class DroppedPick:
    symbol: str
    name: str
    reason: str


@dataclass
class ListDiff:
    """직전 원장 리스트 대비 변화 (diff_lists 산출)."""

    prev_date: str | None
    first_run: bool
    new: list[str] = field(default_factory=list)
    dropped: list[DroppedPick] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    swaps: list[tuple[str, str]] = field(default_factory=list)   # (탈락, 신규) 동점 교체

    @property
    def changed(self) -> bool:
        return bool(self.new or self.dropped)


@dataclass
class Verdict:
    label: str
    reasons: list[str]
    n_buy: int = 0
    n_drop: int = 0
    n_risk: int = 0

    @property
    def quiet(self) -> bool:
        return self.label == VERDICT_NONE


@dataclass
class DigestBundle:
    """assemble_daily_digest 결과 — 순수 조립, 쓰기 0."""

    text: str
    market: str
    picks: list
    score_asof: str | None
    report: SignalReport | None
    health: HealthReport
    degraded: bool
    silent: bool
    hold: bool = False                      # 브로드캐스트 보류 (강등·후보 0)
    hold_reason: str | None = None
    record_ledger: bool = True              # 후보 보류(점수 노후)면 False
    dd10_new: dict[str, str] = field(default_factory=dict)   # 이번 런에 새로 발화한 '선정 후 -10%'
    verdict: Verdict | None = None
    diff: ListDiff | None = None
    # 소유자 DM 기술 상세 — assemble 이 만든 번들에서는 health.notes 와 **같은 리스트** (D4)
    owner_extra: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 작은 순수 헬퍼
# ----------------------------------------------------------------------


def _mmdd(iso: str | None) -> str:
    return iso[5:10] if iso and len(iso) >= 10 else "—"


def _pct(v: float | None, nd: int = 0) -> str:
    return "—" if v is None or not math.isfinite(v) else f"{v:+.{nd}f}%"


def _price_txt(p: float | None, market: str) -> str:
    if p is None or not math.isfinite(p):
        return "—"
    return f"{p:,.0f}원" if market == "KR" else f"${p:,.2f}"


def _name(row: Any) -> str:
    return md_safe(getattr(row, "name", "") or "", MD_SAFE_NAME_LEN)


def _sym(row_or_str: Any) -> str:
    return md_safe(getattr(row_or_str, "symbol", row_or_str))


def _age_days(asof: str | None, now: datetime) -> int | None:
    if not asof:
        return None
    try:
        return (now.astimezone(KST).date() - date.fromisoformat(asof[:10])).days
    except ValueError:
        return None


def _bdays(start_iso: str | None, end: date) -> int | None:
    if not start_iso:
        return None
    try:
        return int(np.busday_count(date.fromisoformat(start_iso[:10]), end))
    except ValueError:
        return None


def price_metrics_from_frame(df: pd.DataFrame) -> PriceMetrics | None:
    """yfinance OHLCV(1y) → PriceMetrics (순수). 200일선은 관측 200개 이상일 때만."""
    from src.risk_engine import max_drawdown

    if df is None or df.empty:
        return None
    close = (df["Close"] if "Close" in df.columns else close_series(df)).squeeze().dropna()
    close = close[close > 0]
    if close.empty:
        return None
    last = float(close.iloc[-1])
    asof = pd.Timestamp(close.index[-1]).strftime("%Y-%m-%d")
    adj_last = None
    adj_series: pd.Series | None = None
    try:
        adj = close_series(df).dropna()
        adj = adj[adj > 0]
        if not adj.empty:
            adj_last = float(adj.iloc[-1])
            adj_series = _naive_index(adj)
    except QuantBotError:
        adj_last = None
    vs_ma200 = None
    if len(close) >= MA_LONG_WINDOW:
        vs_ma200 = (last / float(close.iloc[-MA_LONG_WINDOW:].mean()) - 1) * 100
    dd_52w = (last / float(close.max()) - 1) * 100
    try:
        mdd = float(max_drawdown(close).max_dd_pct)
    except InsufficientDataError:
        mdd = None
    return PriceMetrics(price=last, asof=asof, adj_last=adj_last, vs_ma200_pct=vs_ma200,
                        dd_52w_pct=dd_52w, mdd_1y_pct=mdd, vol_pct=annualized_vol_pct(close),
                        adj_close=adj_series)


def _naive_index(s: pd.Series) -> pd.Series:
    """DatetimeIndex 를 tz 없는 날짜로 (yfinance 가 tz-aware 로 줄 때 날짜 비교가 깨지지 않게)."""
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return pd.Series(s.to_numpy(dtype=float), index=idx.normalize())


def entry_base_from_frame(adj: pd.Series | None, entry_date: str | None,
                          first_sent_date: str | None) -> tuple[float | None, str]:
    """같은 프레임의 Adj Close 에서 진입 기준가 — (기준가, 출처). 순수.

    기준일 = ``entry_date`` (채점기가 확정한 진입 거래일), 없으면 ``first_sent_date`` 당일 또는 이후
    첫 거래일 (ledger.update_outcomes 와 같은 규칙 — 아침 발송이라 당일 종가가 진입가).
    출처: "frame"(프레임에서 구함) / "out_of_window"(진입일이 프레임 첫 관측 이전 — 저장 entry_px
    폴백 대상) / "missing"(시리즈·날짜 없음, 또는 진입일이 마지막 관측 이후).
    """
    if adj is None or len(adj) == 0:
        return None, "missing"
    ref = entry_date or first_sent_date
    if not ref:
        return None, "missing"
    try:
        ts = pd.Timestamp(str(ref)[:10])
    except ValueError:
        return None, "missing"
    s = _naive_index(adj.dropna())
    s = s[s > 0]
    if s.empty:
        return None, "missing"
    if ts < s.index[0]:
        return None, "out_of_window"
    later = s[s.index >= ts]
    if later.empty:
        return None, "missing"
    return float(later.iloc[0]), "frame"


def since_pct_for(m: PriceMetrics | None, ent: Mapping[str, Any] | None) -> tuple[float | None, str]:
    """'선정 후' 수익률 (%) — (값, 기준가 출처 "frame"|"stored"|"none"). 순수.

    1순위: 같은 프레임의 진입일 Adj Close (분할·배당 소급과 같은 기준). 프레임이 진입일을 덮지
    못하면(1년 창 밖, 또는 시리즈 없음) 채점기가 저장한 ``entry_px`` 로 폴백. 둘 다 없으면 None.
    진입일이 프레임 마지막 관측 이후면(캐시된 프레임이 낡음) None — 저장 entry_px 는 더 새 다운로드
    기준이라 오늘 프레임의 adj_last 와 섞지 않는다.
    """
    if m is None or m.adj_last is None or not ent:
        return None, "none"
    base, how = entry_base_from_frame(m.adj_close, ent.get("entry_date"), ent.get("first_sent_date"))
    src = "frame"
    if base is None:
        if how == "missing" and m.adj_close is not None and len(m.adj_close) > 0:
            return None, "none"
        stored = ent.get("entry_px")
        base = float(stored) if stored else None
        src = "stored"
    if not base or base <= 0:
        return None, "none"
    return (m.adj_last / base - 1) * 100, src


def evidence_from_detail(detail: dict | None, market: str, flags: Sequence[str] = (),
                         is_holding: bool = False) -> str:
    """screened.detail JSON → 🆕 근거 줄 (원값 3개 + 꼬리표 ≤1). 값이 없으면 빈 문자열.

    US: 이익수익률 · EV/EBITDA · ROE,  KR: PER · PBR · ROE — 선정에 쓴 그 숫자를 그대로.
    꼬리표: 금융업(fin_neutral) / 지주사(holding) / 일회성이익 가드(one_off) 중 하나.
    """
    comps: dict[str, str] = {}
    for section in ("value", "health"):
        for c in ((detail or {}).get(section) or {}).get("components") or []:
            if len(c) >= 4 and c[0] not in comps and c[3] not in (None, ""):
                comps[str(c[0])] = str(c[3])
    parts: list[str] = []
    if market == "KR":
        if "PER" in comps:
            parts.append(f"PER {comps['PER']}배")
        if "PBR" in comps:
            parts.append(f"PBR {comps['PBR']}")
    else:
        if "이익수익률" in comps:
            parts.append(f"이익수익률 {comps['이익수익률']}")
        if "EV/EBITDA" in comps:
            parts.append(f"EV/EBITDA {comps['EV/EBITDA'].replace('x', '배')}")
    if "ROE" in comps:
        parts.append(f"ROE {comps['ROE']}")
    tag = None
    fl = set(flags or ())
    if "fin_neutral" in fl:
        tag = "금융업"
    elif "holding" in fl or is_holding:
        tag = "지주사"
    elif "one_off" in fl:
        tag = "일회성이익 가드"
    if tag:
        parts.append(tag)
    return md_safe(" · ".join(parts))


# ----------------------------------------------------------------------
# 판정 (순수)
# ----------------------------------------------------------------------


REASON_SECTOR_CAP = "선정 규칙(같은 업종 최대 2개)"
REASON_HOLDING_CAP = "선정 규칙(지주사 최대 2개)"
REASON_LAGGING = "점수 갱신 누락 (재보강 실패)"


def skip_reasons(market: str, picks: Sequence[Any], dropped_rows: dict[str, Any],
                 lagging_syms: Iterable[str] = (), top_n: int = 6) -> dict[str, str]:
    """탈락 종목 중 '순위 밖' 이 아닌 사유를 아는 것 → {symbol: 사유} (순수, RVM-01·RVQ-1).

    · 재보강 누락 행(universe.lagging_rows) → REASON_LAGGING
    · pick_top 의 상한으로 건너뛴 행: 정렬 키(total_score, market_cap) 가 마지막 선정 종목보다
      앞인데(또는 선정이 top_n 을 못 채웠는데) 선정되지 않았고, US 면 같은 sector 선정이 이미 2개 /
      KR 이면 지주사 → 상한 사유. pick_top 과 같은 정렬·같은 판정 함수(universe.is_holding_row).
    제외(excluded) 행은 diff_lists 가 '필터 제외' 로 먼저 처리하므로 여기서 다루지 않는다.
    """
    from src import universe

    lag = set(lagging_syms)
    picked = {str(p.symbol) for p in picks}
    last = picks[-1] if picks else None
    out: dict[str, str] = {}
    for sym, row in dropped_rows.items():
        if row is None or getattr(row, "excluded", None) or sym in picked:
            continue
        if sym in lag:
            out[sym] = REASON_LAGGING
            continue
        try:
            key = (float(row.total_score), float(row.market_cap or 0))
            ahead = last is None or len(picks) < top_n or key > (
                float(last.total_score), float(last.market_cap or 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if not ahead:
            continue
        if market == "KR":
            if universe.is_holding_row(row):
                out[sym] = REASON_HOLDING_CAP
        elif getattr(row, "sector", None) and sum(
                1 for p in picks if getattr(p, "sector", None) == row.sector) >= 2:
            out[sym] = REASON_SECTOR_CAP
    return out


def diff_lists(prev: Sequence[Any], cur: Sequence[Any], dropped_rows: dict[str, Any] | None = None,
               prev_date: str | None = None, reasons: dict[str, str] | None = None) -> ListDiff:
    """직전 원장 리스트(prev: LedgerPick duck — symbol/total_score) vs 현재 후보(cur: ScanRow).

    탈락 사유: 유니버스에서 제외됐으면 '필터 제외' / ``reasons`` 에 있으면 그 사유(선정 규칙 상한·
    재보강 누락 — skip_reasons) / 점수가 내려갔으면 '점수 하락 92→79' / 그 외 '순위 밖'.
    **동점 교체**(탈락·신규의 total_score 가 같음)는 swaps 로 분리해 판정
    카운트에서 제외 — KR 90점 동률 6종목의 시총 tie-break churn 이 '매수 검토' 를 만들지 않게.
    prev 가 비면 first_run(기준 수립): new 는 비우고 kept 에 전부 (6종목 '매수 검토' 방지).
    """
    dropped_rows = dropped_rows or {}
    reasons = reasons or {}
    prev_list = list(prev or [])
    cur_syms = [str(getattr(r, "symbol")) for r in cur]
    if not prev_list:
        return ListDiff(prev_date=None, first_run=True, kept=cur_syms)
    prev_score = {str(p.symbol): getattr(p, "total_score", None) for p in prev_list}
    cur_score = {str(r.symbol): getattr(r, "total_score", None) for r in cur}
    new = [s for s in cur_syms if s not in prev_score]
    kept = [s for s in cur_syms if s in prev_score]
    dropped: list[DroppedPick] = []
    for p in prev_list:
        s = str(p.symbol)
        if s in cur_score:
            continue
        row = dropped_rows.get(s)
        name = md_safe(getattr(row, "name", None) or getattr(p, "name", None) or "", MD_SAFE_NAME_LEN)
        now_score = getattr(row, "total_score", None) if row is not None else None
        before = prev_score.get(s)
        if row is not None and getattr(row, "excluded", None):
            reason = "필터 제외 (보통주 아님 등)"
        elif s in reasons:
            reason = reasons[s]
        elif (now_score is not None and before is not None and now_score < before):
            reason = f"점수 하락 {before}→{now_score}"
        else:
            reason = "순위 밖"
        dropped.append(DroppedPick(s, name, reason))
    # 동점 교체: 탈락 종목과 신규 종목의 점수가 같고 사유가 '순위 밖' 이면 시총 tie-break 변동
    swaps: list[tuple[str, str]] = []
    for d in list(dropped):
        if d.reason != "순위 밖":
            continue
        for s in list(new):
            if cur_score.get(s) is not None and cur_score.get(s) == prev_score.get(d.symbol):
                swaps.append((d.symbol, s))
                new.remove(s)
                dropped.remove(d)
                break
    return ListDiff(prev_date=prev_date, first_run=False, new=new, dropped=dropped,
                    kept=kept, swaps=swaps)


def _alert_short(a: Alert) -> str:
    """판정 사유용 짧은 알림 라벨 (고정 어휘 + 자산명)."""
    asset = a.key.split(":")[1] if a.key.count(":") >= 2 else ""
    if a.category == "shock":
        return f"{asset} 급변" if asset else "시장 충격"
    if a.category == "regime":
        return "국면 전환 확정"
    if a.category == "drawdown":
        return f"{asset} 낙폭 경보" if asset else "낙폭 경보"
    if a.category == "volatility":
        return f"{asset} 변동성 급등" if asset else "변동성 급등"
    if a.category == "credit":
        return "신용 스프레드 급등"
    if a.category == "fx":
        return "원/달러 급변"
    return a.category


def decide_actions(diff: ListDiff, new_alerts: Sequence[Alert], dd10_labels: Sequence[str] = (),
                   warn_count: int = 0, first_run: bool = False,
                   held_age: int | None = None, baseline_note: str | None = None) -> Verdict:
    """'오늘 할 일' 판정 — 사실의 변화만으로 (점수·예측으로 행동을 지시하지 않음).

    매수 검토 N = 신규 후보 수(동점 교체 제외), 탈락 N = 탈락 수, 리스크 점검 N = 새 알림 수
    (국면 전환·낙폭·충격·신용·환율·종목 선정 후 하락). 전부 0 이면 '없음'.
    첫 기록(first_run)은 '기준 수립' — 6종목 전부 매수 검토가 되지 않는다.
    후보 보류일(``held_age``: 점수 노후 일수)은 후보 diff 를 판정에서 뺀다 — 매수/탈락/기준 수립 0,
    사유 '후보 보류 (점수 N일 경과)'. 원장이 갱신되지 않는 날이라 같은 diff 가 매일 반복되기 때문
    (D1). 새 알림(국면·낙폭·충격)은 보류와 무관하게 남는다.
    ``baseline_note`` 가 있으면(규칙 버전 전환일) 첫 기록처럼 기준 수립으로 보고 사유를 그 문구로 —
    원장이 그날을 새 규칙의 기준선으로 기록하므로 '매수 검토 N · 탈락 M' 을 내지 않는다.
    """
    held = held_age is not None
    baseline = (diff.first_run or first_run or baseline_note is not None) and not held
    n_buy = 0 if (baseline or held) else len(diff.new)
    n_drop = 0 if (baseline or held) else len(diff.dropped)
    n_risk = len(new_alerts) + len(dd10_labels)
    parts = []
    if n_buy:
        parts.append(f"{VERDICT_BUY} {n_buy}")
    if n_drop:
        parts.append(f"{VERDICT_DROP} {n_drop}")
    if n_risk:
        parts.append(f"{VERDICT_RISK} {n_risk}")
    label = " · ".join(parts) if parts else VERDICT_NONE

    reasons: list[str] = []
    if held:
        reasons.append(f"후보 보류 (점수 {held_age}일 경과)")
    elif baseline:
        reasons.append(md_safe(baseline_note) if baseline_note else "후보 목록 기준 수립 (첫 기록)")
    elif diff.changed:
        reasons.append("후보 변경일")
    elif diff.swaps:
        reasons.append("후보 변화 없음 (동점 교체만)")
    else:
        reasons.append("후보 변화 없음")
    if n_risk:
        labels = [_alert_short(a) for a in new_alerts] + list(dd10_labels)
        reasons.extend(md_safe(x) for x in labels[:2])
        if len(labels) > 2:
            reasons.append(f"외 {len(labels) - 2}건")
    else:
        reasons.append("새 알림 없음")
    if warn_count:
        reasons.append(f"⚠️ 데이터 점검 {warn_count}건")
    return Verdict(label=label, reasons=reasons, n_buy=n_buy, n_drop=n_drop, n_risk=n_risk)


# ----------------------------------------------------------------------
# 섹션 렌더러 (순수)
# ----------------------------------------------------------------------


def render_header(market: str, now: datetime, verdict: Verdict) -> str:
    local = now.astimezone(KST)
    stamp = f"{local:%m-%d}({_WEEKDAY_KO[local.weekday()]}) {local:%H:%M}"
    head = f"*{_FLAG.get(market, '')} {stamp} · 오늘 할 일: {verdict.label}*"
    return f"{head} — {' · '.join(verdict.reasons)}" if verdict.reasons else head


def render_basis(market: str, now: datetime, price_asof: str | None, score_asof: str | None,
                 *, stale_warn: bool) -> str:
    today = now.astimezone(KST).date()
    lag = _bdays(price_asof, today)
    px = f"시세 {_mmdd(price_asof)} 종가" if price_asof else "시세 —"
    if lag is not None and lag >= 2:
        # 2영업일 이상 전 — 휴장일 수도, 가격 갱신 지연일 수도 있어 단정하지 않음 (D6).
        # 3영업일 초과는 health.price_stale 가 ⚙️ 상태 줄로 따로 알린다.
        px += " (휴장 또는 지연)"
    age = _age_days(score_asof, now)
    if score_asof is None:
        sc = "점수 기준일 없음"
    else:
        sc = f"점수 {_mmdd(score_asof)} ({age}일 전"
        sc += ", 주간 갱신 지연)" if stale_warn else ")"
        if stale_warn:
            sc = "⚠️ " + sc
    line = f"기준: {px} · {sc}"
    if market == "US":
        line += " · 개장 전"
    return line


def _alert_icon(a: Alert) -> str:
    return {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(a.severity, "ℹ️")


def render_alert_lines(alerts: Sequence[Alert], dd10_lines: Sequence[str] = ()) -> list[str]:
    """🔔 줄들 — 시장 충격은 한 줄 병합, rank 순(충격 → 국면/낙폭 등 → 종목), 최대 5줄."""
    lines: list[str] = []
    shocks = [a for a in alerts if a.category == "shock"]
    if shocks:
        lines.append("🚨 " + " · ".join(md_safe(a.message) for a in shocks))
    for a in alerts:
        if a.category == "shock":
            continue
        lines.append(f"{_alert_icon(a)} {md_safe(a.message)}")
    lines.extend(f"⚠️ {md_safe(x)}" for x in dd10_lines)
    if len(lines) > MAX_ALERT_LINES:
        extra = len(lines) - MAX_ALERT_LINES
        lines = lines[:MAX_ALERT_LINES] + [f"외 {extra}건"]
    return lines


def render_alerts_kr(alerts: Sequence[Alert], dd10_lines: Sequence[str] = ()) -> list[str]:
    lines = render_alert_lines(alerts, dd10_lines)
    if not lines:
        return []
    return ["*🔔 새 알림*", *lines]


def render_changes_us(alerts: Sequence[Alert], dd10_lines: Sequence[str] = ()) -> list[str]:
    lines = render_alert_lines(alerts, dd10_lines)
    if not lines:
        return ["*🌐 아침 이후 변화* — 없음 (국면·자산 경보는 아침 메시지 참고)"]
    return ["*🌐 아침 이후 변화*", *lines]


def _asset_line(label: str, o: Any, alarmed: bool) -> str:
    if o is None:
        return f"{label} 조회 실패"
    s = f"{label} {o.last:,.0f} {_pct(o.chg_1d_pct, 1)} · 200일선 {_pct(o.vs_ma200_pct)} · 52주 고점比 {_pct(o.dd_52w_pct)}"
    return s + " ⚠️ 경보 중" if alarmed else s


def render_kr_market(report: SignalReport | None, overnight_line: str | None) -> list[str]:
    lines = ["*🇰🇷 한국 시장*"]
    kr = getattr(report, "kr", None)
    active = list(getattr(report, "active", []) or [])
    if kr is None:
        lines.append("코스피·코스닥 조회 실패")
    else:
        lines.append(_asset_line("코스피", kr.kospi, any(a.startswith("KOSPI ") for a in active)))
        lines.append(_asset_line("코스닥", kr.kosdaq, any(a.startswith("KOSDAQ ") for a in active)))
        fx = kr.usdkrw
        if fx is None:
            lines.append("원/달러 조회 실패")
        else:
            lines.append(f"원/달러 {fx.last:,.1f} · 1개월 {_pct(fx.chg_20d_pct, 1)} · 200일선 {_pct(fx.vs_ma200_pct)}")
    if overnight_line:
        lines.append(md_safe(overnight_line))
    return lines


def _reason_core(reason_line: str) -> str:
    """RegimeView.reason_line '근거: A · B — 3/3 지표, 10-07 기준' → 'A · B'."""
    core = reason_line or ""
    if core.startswith("근거: "):
        core = core[len("근거: "):]
    if " — " in core:
        core = core.rsplit(" — ", 1)[0]
    return md_safe(core)


def _active_short(text: str) -> str:
    """'KOSPI -25% (07-03~, 임계 -12%)' → 'KOSPI -25%'."""
    return md_safe(text.rsplit(" (", 1)[0] if " (" in text else text)


def _kr_section_shown(report: SignalReport | None) -> tuple[str, ...]:
    """🇰🇷 한국 시장 섹션이 실제로 그린 지수의 경보 접두사 — 조회 실패한 지수는 제외."""
    kr = getattr(report, "kr", None)
    if kr is None:
        return ()
    pairs = (("KOSPI ", getattr(kr, "kospi", None)), ("KOSDAQ ", getattr(kr, "kosdaq", None)))
    return tuple(prefix for prefix, obs in pairs if obs is not None)


def render_global(report: SignalReport | None, extra_new: int = 0) -> list[str]:
    """🌐 글로벌 (KR 아침 전용) — 국면 1줄 + '새 알림 N건/없음 · 경보 유지' 1줄.

    extra_new = 🔔 블록의 종목 알림 수. '경보 유지' 목록·개수에서 KOSPI·KOSDAQ 은 뺀다 —
    바로 위 🇰🇷 한국 시장 섹션이 같은 경보를 '⚠️ 경보 중' 으로 이미 표시 (D5). 단 그 섹션이
    조회 실패로 해당 지수를 못 그린 날은 빼지 않는다 (경보가 메시지에서 사라지지 않게).
    """
    lines = ["*🌐 글로벌* (아침 1회)"]
    if report is None:
        lines.append("국면 조회 실패")
        return lines
    view = report.regime
    if view is None:
        lines.append(f"국면 {md_safe(report.regime_label)}")
    else:
        if view.held:        # shown_label 이 이미 '… · 직전 판정 유지 (지표 n/3)' 형식
            label = view.shown_label
        else:                # 유지 경로와 같은 ' · ' 구분 — 괄호 연속 '(Risk-on) (지표 3/3…)' 방지
            label = f"{view.shown_label} · 지표 {view.available}/{view.total}, 현황 설명용"
        if view.prev_label:
            label += f" (어제: {view.prev_label})"
        if view.pending:
            label += f" · 변화 관찰 중: {view.pending} {view.pending_count}/{REGIME_CONFIRM_DATES}"
        core = _reason_core(view.reason_line)
        lines.append(f"국면 {md_safe(label)}" + (f" — {core}" if core else ""))
    n_new = len(report.alerts) + int(extra_new)
    head = f"새 알림 {n_new}건 (위)" if n_new else "새 알림 없음"
    skip = _kr_section_shown(report)
    active = [_active_short(a) for a in report.active if not str(a).startswith(skip)]
    if active:
        shown = " · ".join(active[:MAX_ACTIVE_SHOWN])
        if len(active) > MAX_ACTIVE_SHOWN:
            shown += f" · 외 {len(active) - MAX_ACTIVE_SHOWN}"
        lines.append(f"{head} · 경보 유지 {len(active)}: {shown}")
    else:
        lines.append(f"{head} · 경보 없음")
    return lines


def _keep_line(v: PickView, market: str) -> str:
    r = v.row
    head = f"{_sym(r)} {_name(r)} {r.total_score}"
    m = v.metrics
    if m is None:
        return f"{head} · 가격 조회 실패"
    tail = (f"선정 후 {_pct(v.since_pct, 1)}" if v.since_pct is not None
            else f"52주 고점比 {_pct(m.dd_52w_pct)}")
    return f"{head} · {_price_txt(m.price, market)} · 200일선 {_pct(m.vs_ma200_pct)} · {tail}"


def _new_lines(v: PickView, market: str) -> list[str]:
    r = v.row
    out = [f"🆕 {_sym(r)} {_name(r)} — {r.total_score} ({LABEL_VALUE} {r.value_score} · {LABEL_HEALTH} {r.health_score})"]
    m = v.metrics
    if m is None:
        out.append("   가격 조회 실패")
    else:
        pos = ("52주 신고가" if (m.dd_52w_pct is not None and m.dd_52w_pct >= -NEW_HIGH_EPS_PCT)
               else f"52주 고점比 {_pct(m.dd_52w_pct)}")
        vol = f"{m.vol_pct:.0f}%" if m.vol_pct is not None else "—"
        out.append(f"   {_price_txt(m.price, market)} · {pos} · 200일선 {_pct(m.vs_ma200_pct)} · "
                   f"1년 최대낙폭 {_pct(m.mdd_1y_pct)} · 변동성 {vol}")
    if v.evidence:
        out.append(f"   근거: {v.evidence}")
    return out


def _legend(market: str) -> str:
    if market == "KR":
        return f"점수 = 재무점수({LABEL_VALUE}+{LABEL_HEALTH}, DART 연간, 가격요인 없음) · 가격 줄은 참고용 · 상세 /stock 코드"
    return f"점수 = 재무점수({LABEL_VALUE}+{LABEL_HEALTH}, FMP 최근 4분기) · 가격 줄은 참고용, 점수 미반영 · 상세 /stock 티커"


def render_candidates(market: str, views: Sequence[PickView], diff: ListDiff, *,
                      hold_age: int | None = None, list_since: str | None = None,
                      collapse_keep: bool = False, compact_new: bool = False,
                      weights_line: str | None = None, rule_note: str | None = None) -> list[str]:
    """📋 후보 섹션. hold_age 가 있으면 '후보 보류' 1줄로 대체 (점수 >14일).

    rule_note: 규칙 버전 전환 당일 1회 꼬리 줄 ('규칙 v1 기준으로 성과 집계 재시작', D3).
    """
    title = f"*📋 {_MARKET_KO.get(market, market)} {LABEL_CANDIDATES} {len(views)}*"
    if hold_age is not None:
        return [f"*📋 {_MARKET_KO.get(market, market)} {LABEL_CANDIDATES}* — 보류",
                f"후보 보류 — 점수 {hold_age}일 경과, 재점수 필요"]
    if not views:
        return [f"*📋 {_MARKET_KO.get(market, market)} {LABEL_CANDIDATES}*", "후보 없음 — 유니버스 비어 있음"]
    by_sym = {str(v.row.symbol): v for v in views}
    new_views = [by_sym[s] for s in diff.new if s in by_sym]
    keep_syms = [str(v.row.symbol) for v in views if str(v.row.symbol) not in set(diff.new)]
    keep_views = [by_sym[s] for s in keep_syms]

    if diff.first_run:
        head = f"{title} — 기준 수립 (첫 기록)"
    elif diff.changed or diff.swaps:
        bits = []
        if diff.new:
            bits.append(f"신규 {len(diff.new)}")
        if diff.dropped:
            bits.append(f"탈락 {len(diff.dropped)}")
        bits.append(f"유지 {len(keep_views)}")
        if diff.swaps:
            bits.append(f"동점 교체 {len(diff.swaps)}")
        head = f"{title} — " + " · ".join(bits)
    else:
        head = f"{title} — 변화 없음" + (f" ({_mmdd(list_since)} 이후 동일)" if list_since else "")
    lines = [head]

    for v in new_views[:MAX_NEW_LINES]:
        if compact_new:
            lines.append("🆕 " + _keep_line(v, market))
        else:
            lines.extend(_new_lines(v, market))
    if len(new_views) > MAX_NEW_LINES:
        lines.append(f"외 {len(new_views) - MAX_NEW_LINES} — {_SCAN_CMD.get(market, '/scan')}")
    for old, new in diff.swaps:
        lines.append(f"↔ 동점 교체: {md_safe(old)} → {md_safe(new)}")
    if diff.dropped:
        by_reason: dict[str, list[str]] = {}
        for d in diff.dropped:
            by_reason.setdefault(d.reason, []).append(md_safe(d.symbol))
        if len(by_reason) == 1:
            reason, syms = next(iter(by_reason.items()))
            lines.append(f"➖ {' · '.join(syms)} 탈락 — {md_safe(reason)}")
        else:
            lines.append("➖ " + " · ".join(f"{' '.join(s)} 탈락 — {md_safe(r)}" for r, s in by_reason.items()))
    if keep_views:
        if collapse_keep:
            lines.append(f"유지 {len(keep_views)} — {_SCAN_CMD.get(market, '/scan')}")
        else:
            if diff.changed or diff.swaps:
                lines.append(f"유지 {len(keep_views)}")
            for v in keep_views[:MAX_KEEP_LINES]:
                lines.append(_keep_line(v, market))
            if len(keep_views) > MAX_KEEP_LINES:
                lines.append(f"외 {len(keep_views) - MAX_KEEP_LINES} — {_SCAN_CMD.get(market, '/scan')}")
    if weights_line:
        lines.append(weights_line)
    if rule_note:
        lines.append(md_safe(rule_note))
    lines.append(_legend(market))
    return lines


def render_ledger(summary: dict | None, *, now: datetime, has_history: bool,
                  weekly: bool = False, ledger_mod=None, held: bool = False) -> tuple[list[str], str | None]:
    """📒 섹션 — (줄들, 상태 푸터). 게이트 미통과면 섹션 생략 + 푸터 반환.

    held(후보 보류일): 요약이 없을 때 '첫 발송 — 성과 기록 시작'/'발송 후 자동 채점' 대신
    '후보 보류 중 — 원장 미기록' — 보류일은 원장에 기록되지 않아 채점도 시작되지 않는다 (D1).
    """
    from src import ledger as _ledger
    lg = ledger_mod or _ledger
    title = "*📒 지난 후보 성과*"
    if summary is None:
        if held:
            return [title, "후보 보류 중 — 원장 미기록"], None
        if not has_history:
            return [title, "첫 발송 — 성과 기록 시작"], None
        return [title, "채점 전 — 발송 후 자동 채점"], None
    line = lg.format_ledger_line(summary, now=now)
    if line is None:
        _ok, footer = lg.ledger_gate(summary, now=now)
        return [], footer
    # 문구는 WP-LEDGER 출력 그대로 — 제목에 이미 📒 가 있어 선두 이모지만 뗀다
    lines = [title, md_safe(line[2:] if line.startswith("📒 ") else line)]
    if weekly:
        block = lg.format_weekly_block(summary, now=now)
        if block:
            lines.extend(md_safe(x) for x in block.splitlines())
    return lines, None


def render_status(health: HealthReport, now: datetime, extra: Sequence[str] = ()) -> str | None:
    parts = [p.user_text for p in health.problems(now) if p.user_text]
    parts.extend(x for x in extra if x)
    if not parts:
        return None
    return "⚙️ 상태: " + " · ".join(md_safe(p) for p in parts)


def render_predictions(predictions: Sequence[Any]) -> list[str]:
    """선행지표 예측 (플래그 QUANT_BOT_DIGEST_PREDICTIONS 로만 — 검증 미통과·참고용)."""
    if not predictions:
        return []
    findings = [from_prediction(p) for p in predictions]
    reliable = [f for f in findings if f.confidence == CONFIDENCE_RELIABLE]
    lines = ["*🔮 선행지표 예측* (검증 미통과 · 참고용)"]
    if reliable:
        lines.extend(f"  {md_safe(f.summary)}" for f in sorted(reliable, key=lambda x: x.score or 0, reverse=True))
    else:
        lines.append("  R² 0.3 이상 관계 없음")
    weak = len(findings) - len(reliable)
    if weak:
        lines.append(f"  그 외 {weak}개 약함 (참고 제외)")
    return lines


def with_summary(text: str, summary: str | None) -> str:
    """LLM 요약을 맨 위에 붙임 (순수). 없으면 원문 그대로 — 기본 OFF (QUANT_BOT_LLM=1 로만)."""
    if not summary:
        return text
    return f"🧠 {md_safe(summary)}\n\n{text}"


# ----------------------------------------------------------------------
# 조립 (순수 — 이미 계산된 입력 → 전체 텍스트, 길이 예산 적용)
# ----------------------------------------------------------------------


@dataclass
class DigestInputs:
    """render_digest 의 입력 묶음 (assemble_daily_digest 가 채움, 테스트가 직접 만들 수 있음)."""

    market: str
    now: datetime
    verdict: Verdict
    diff: ListDiff
    views: list[PickView]
    report: SignalReport | None
    health: HealthReport
    price_asof: str | None
    score_asof: str | None
    stale_warn: bool
    hold_age: int | None = None
    list_since: str | None = None
    dd10_lines: list[str] = field(default_factory=list)
    overnight_line: str | None = None
    ledger_summary: dict | None = None
    ledger_has_history: bool = False
    weekly: bool = False
    show_ledger: bool = True
    predictions: list = field(default_factory=list)
    weights_line: str | None = None
    rule_note: str | None = None            # 규칙 버전 전환 당일 1회 (D3)


def render_digest(inp: DigestInputs, *, collapse_keep: bool = False, compact_new: bool = False,
                  ledger_mod=None) -> str:
    m = inp.market
    alerts = list(inp.report.alerts) if inp.report is not None else []
    out: list[str] = [render_header(m, inp.now, inp.verdict),
                      render_basis(m, inp.now, inp.price_asof, inp.score_asof,
                                   stale_warn=inp.stale_warn)]
    if m == "KR":
        block = render_alerts_kr(alerts, inp.dd10_lines)
        if block:
            out += ["", *block]
        out += ["", *render_kr_market(inp.report, inp.overnight_line)]
        out += ["", *render_global(inp.report, extra_new=len(inp.dd10_lines))]
    else:
        out += ["", *render_changes_us(alerts, inp.dd10_lines)]
    out += ["", *render_candidates(m, inp.views, inp.diff, hold_age=inp.hold_age,
                                   list_since=inp.list_since, collapse_keep=collapse_keep,
                                   compact_new=compact_new, weights_line=inp.weights_line,
                                   rule_note=inp.rule_note)]
    footer = None
    if inp.show_ledger:
        lines, footer = render_ledger(inp.ledger_summary, now=inp.now,
                                      has_history=inp.ledger_has_history, weekly=inp.weekly,
                                      ledger_mod=ledger_mod, held=inp.hold_age is not None)
        if lines:
            out += ["", *lines]
    pred = render_predictions(inp.predictions)
    if pred:
        out += ["", *pred]
    status = render_status(inp.health, inp.now, [footer] if footer else [])
    out.append("")
    if status:
        out.append(status)
    out.append(DISCLAIMER)
    return "\n".join(out)


def render_with_budget(inp: DigestInputs, *, ledger_mod=None) -> str:
    """길이 예산 — 하드캡(2,500) 초과 시 유지 종목 줄부터 축약 (줄 중간 자르기 금지)."""
    text = render_digest(inp, ledger_mod=ledger_mod)
    if len(text) <= LEN_HARD_CAP:
        return text
    for kw in ({"collapse_keep": True}, {"collapse_keep": True, "compact_new": True}):
        text = render_digest(inp, ledger_mod=ledger_mod, **kw)
        if len(text) <= LEN_HARD_CAP:
            return text
    logger.warning("다이제스트 길이 %d > 하드캡 %d — notifier 절단에 의존", len(text), LEN_HARD_CAP)
    return text


# ----------------------------------------------------------------------
# 오케스트레이터 — fetch + DB 읽기 (쓰기 0)
# ----------------------------------------------------------------------


def _price_metrics(symbol: str, market: str, exchange: str | None, sector: str) -> PriceMetrics | None:
    """yfinance 1y (캐시 경유). KR 은 .KS/.KQ 실패 시 반대 접미사 1회 재시도. 실패 None."""
    from src import universe
    from src.data_fetcher import fetch_prices

    candidates = [universe.yf_symbol(symbol, market, exchange, sector)]
    candidates += universe.yf_symbol_alternates(symbol, market, exchange, sector)
    for ys in candidates:
        try:
            df = fetch_prices(ys, period="1y")
        except DataFetchError as e:
            logger.info("가격 지표 조회 실패 %s (%s) — %s", symbol, ys, e)
            continue
        try:
            return price_metrics_from_frame(df)
        except QuantBotError as e:
            logger.info("가격 지표 계산 실패 %s — %s", symbol, e)
            return None
    return None


def _bench_price_asof(market: str) -> str | None:
    """후보 가격이 없을 때(보류·조회 실패) 기준 줄의 시세 날짜 — 벤치(SPY) 마지막 관측일.

    신호 엔진이 같은 (ticker, period) 로 방금 받은 패널이 캐시에 있어 추가 API 호출 0.
    """
    from src.data_fetcher import fetch_prices

    ticker = "SPY" if market == "US" else "^KS11"
    try:
        df = fetch_prices(ticker, period="2y")
    except DataFetchError as e:
        logger.info("기준 시세 날짜 조회 실패 %s — %s", ticker, e)
        return None
    close = close_series(df).dropna()
    return pd.Timestamp(close.index[-1]).strftime("%Y-%m-%d") if not close.empty else None


def _overnight_us_line() -> str | None:
    """KR 아침 '간밤 미국' 1줄 — 전부 best-effort. 하나도 못 받으면 None."""
    from src.macro_analyzer import fetch_panel_with_failures

    try:
        panel, _failed = fetch_panel_with_failures(OVERNIGHT_US, period="1mo")
    except QuantBotError as e:
        logger.warning("간밤 미국 시세 조회 실패 — %s", e)
        return None
    parts: list[str] = []
    for label in OVERNIGHT_US:
        if label not in panel.columns:
            continue
        s = panel[label].dropna()
        if s.empty:
            continue
        last = float(s.iloc[-1])
        if label == "10년물":
            parts.append(f"{label} {last:.2f}%")
        elif label == "VIX":
            parts.append(f"{label} {last:.1f}")
        else:
            chg = (last / float(s.iloc[-2]) - 1) * 100 if len(s) >= 2 else None
            parts.append(f"{label} {last:,.0f} {_pct(chg, 1)}")
    return ("간밤 미국 " + " · ".join(parts)) if parts else None


def _collect_predictions() -> list:
    """선행지표 예측 — 플래그 켜졌을 때만 호출. 개별 실패는 스킵(로그)."""
    from src.predictors import PREDICTORS

    out = []
    for name, predict in PREDICTORS.items():
        try:
            out.append(predict())
        except QuantBotError as e:
            logger.warning("예측 스킵 %s — %s", name, e)
    return out


def _propose_weights(tickers: Sequence[str]) -> dict[str, float] | None:
    """제안 비중 (Phase 13c, 플래그 QUANT_BOT_DIGEST_WEIGHTS 로만) — best-effort, 실패 None."""
    uniq = list(dict.fromkeys(tickers))
    if len(uniq) < 2:
        return None
    from src import portfolio
    from src.data_fetcher import fetch_prices

    prices = {}
    for t in uniq:
        try:
            prices[t] = close_series(fetch_prices(t))
        except QuantBotError as e:
            logger.debug("제안 비중용 가격 스킵 %s — %s", t, e)
    try:
        proposal = portfolio.propose(list(prices), prices)
    except QuantBotError as e:
        logger.warning("제안 비중 생략 — %s", e)
        return None
    return proposal.weights


def _weights_line(weights: dict[str, float] | None) -> str | None:
    if not weights:
        return None
    parts = [f"{md_safe(t)} {w * 100:.0f}%" for t, w in weights.items()]
    cash = max(0.0, 1.0 - sum(weights.values()))
    if cash >= 0.005:
        parts.append(f"현금 {cash * 100:.0f}%")
    return "제안 비중(참고, 검증 전): " + " · ".join(parts)


def _pick_tags(row: Any) -> tuple[Sequence[str], bool]:
    from src import universe

    flags = tuple(getattr(row, "flags", ()) or ())
    is_holding = False
    if getattr(row, "market", "") == "KR":
        try:
            is_holding = universe.kr_is_holding(getattr(row, "name", None))
        except (AttributeError, TypeError):
            is_holding = False
    return flags, is_holding


def assemble_daily_digest(market: str = "us", *, now: datetime | None = None,
                          top_n: int = 6) -> DigestBundle:
    """fetch + DB 읽기 → DigestBundle (쓰기 0). dry-run 과 send 가 같은 함수를 쓴다."""
    from src import ledger, universe
    from src.signals import generate_signal_report
    from src.storage import DB_ERRORS, StorageError, get_storage

    market = (market or "us").upper()
    now = (now or datetime.now(KST)).astimezone(KST)
    store = get_storage()
    health = HealthReport(market=market, started_at=now,
                          storage=getattr(store, "init_sync", "local"), universe_as_of=None)
    degraded = bool(getattr(store, "degraded", False))
    # 소유자 DM 상세 줄 — health.notes 와 같은 리스트 (signature 제외, 평문 DM 에만). D4
    owner_extra: list[str] = health.notes

    # --- 후보 (DB 재무점수 하나) ---
    picks: list = []
    lagging: list = []
    try:
        picks = list(universe.pick_top(market, top_n))
        fresh = universe.freshness(market)
        # 재보강 누락 행(시장 배치보다 1주 이상 옛 점수)은 pick_top 이 이미 뺐다 (RVQ-1) — 그중
        # 후보권 점수였던 행만 소유자에게 알린다. 낡은 1행이 min(기준일) 로 시장 전체를
        # '후보 보류' 로 끌고 가지 않게 하는 쪽은 universe.scan 의 컷오프가 담당.
        floor = min((int(p.total_score) for p in picks), default=0)
        lagging = list(universe.lagging_rows(market, min_total=floor, limit=10))
    except (StorageError, *DB_ERRORS):
        logger.warning("유니버스 읽기 실패 (%s)", market, exc_info=True)
        fresh = {"fund_asof": None, "price_asof": None, "age_days": None}
        health.optional_failures.append("유니버스 읽기")
    if lagging:
        # 고정 키 — 처음 생긴 날 signature 가 바뀌어 소유자 DM 1회, 같은 상태가 이어지면 억제
        health.optional_failures.append("재보강 누락 종목")
        shown = ", ".join(f"{r.symbol} {r.total_score} ({str(r.fund_asof or '?')[5:]})" for r in lagging[:5])
        more = f" 외 {len(lagging) - 5}" if len(lagging) > 5 else ""
        owner_extra.append(f"재보강 누락으로 후보 제외 {len(lagging)}종목 (그 종목 보강 실패·상폐 의심, "
                           f"build_universe 로그 확인): {shown}{more}")
    asofs = [p.fund_asof for p in picks if getattr(p, "fund_asof", None)]
    score_asof = min(asofs) if asofs else fresh.get("fund_asof")
    health.universe_as_of = score_asof
    age = _age_days(score_asof, now)
    stale_warn = age is not None and age >= settings.universe_warn_days
    hold_age = age if (age is not None and age >= settings.universe_hold_days) else None

    # --- 시장 수준 신호 (상태 쓰기 0 — 커밋은 send 에서) ---
    report: SignalReport | None = None
    try:
        report = generate_signal_report(tickers=(), screen_tickers=None, market=market)
    except QuantBotError as e:
        logger.warning("신호 리포트 실패 — 시장 섹션 생략: %s", e, exc_info=True)
        health.optional_failures.append("신호 리포트")
    if report is not None:
        if report.regime is not None:
            health.regime_ok, health.regime_total = report.regime.available, report.regime.total
            health.regime_failures = [x for x in report.data_issues if "국면 지표" in x]
        owner_extra.extend(f"데이터: {x}" for x in report.data_issues)

    # --- 원장 (diff 의 유일한 출처) ---
    prev_date, prev_list = ledger.last_sent_list(market)
    cur_syms = [str(p.symbol) for p in picks]
    starts = ledger.inclusion_start(market, cur_syms) if prev_list else {}
    # 진입 정보는 (symbol, 연속 포함 시작일) 정확 일치만 — 옛 회차(v0 백필) 진입가 혼용 금지 (D3)
    entries = ledger.entry_prices(market, starts) if starts else {}
    rule_note = None
    baseline_note = None
    prev_rv = next((getattr(p, "rule_version", None) for p in prev_list), None)
    if prev_rv and prev_rv != universe.RULE_VERSION:
        # 규칙 버전 전환 당일 (직전 리스트가 옛 규칙): 원장 판정 단위 = 규칙 버전이므로 성과를
        # 재시작한다 — 옛 회차의 시작일·진입가로 '선정 후'·-10% 알림을 만들지 않고 1회 표기만.
        starts, entries = {}, {}
        rule_note = f"규칙 {universe.RULE_VERSION} 기준으로 성과 집계 재시작"
        baseline_note = f"규칙 {universe.RULE_VERSION} 로 후보 재구성"
        owner_extra.append(f"원장: 규칙 버전 전환 {prev_rv} → {universe.RULE_VERSION} (성과 재시작)")
    summary = ledger.load_summary(market)
    dropped_rows: dict[str, Any] = {}
    for p in prev_list:
        if str(p.symbol) in cur_syms:
            continue
        try:
            dropped_rows[str(p.symbol)] = universe.lookup(str(p.symbol), market)
        except (StorageError, *DB_ERRORS):
            dropped_rows[str(p.symbol)] = None
    reasons = skip_reasons(market, picks, dropped_rows, (str(r.symbol) for r in lagging), top_n)
    diff = diff_lists(prev_list, picks, dropped_rows, prev_date=prev_date, reasons=reasons)
    if baseline_note and diff.changed:
        baseline_note += f" (편입 {len(diff.new)} · 탈락 {len(diff.dropped)})"
    list_since = max((starts.get(s) for s in cur_syms if starts.get(s)), default=None)

    # --- 가격 지표 (yfinance 캐시, 참고용) + 선정 후 + -10% 1회 알림 ---
    views: list[PickView] = []
    dd10_new: dict[str, str] = {}
    dd10_lines: list[str] = []
    dd10_labels: list[str] = []
    price_fail: list[str] = []
    anomalies: list[str] = []
    price_dates: list[str] = []
    dd_state = {}
    if report is not None and report.pending_state is not None:
        dd_state = (report.pending_state.get("pick_dd10") or {}).get(market) or {}
    for p in picks:
        if hold_age is not None:
            views.append(PickView(row=p, kind="keep"))
            continue
        sym = str(p.symbol)
        m = _price_metrics(sym, market, getattr(p, "exchange", None), getattr(p, "sector", "") or "")
        if m is None:
            price_fail.append(sym)
        else:
            price_dates.append(m.asof)
        kind = "new" if sym in diff.new else ("base" if diff.first_run else "keep")
        since_pct = None
        anomaly = False
        if kind != "new":
            since_pct, src = since_pct_for(m, entries.get(sym))
            if since_pct is not None and abs(since_pct) > SINCE_ABS_MAX_PCT:
                # 분할 미반영·데이터 오류 의심 — 표시는 52주 위치로, -10% 판정 제외, 소유자에게만 (D2)
                anomaly = True
                anomalies.append(sym)
                owner_extra.append(f"선정 후 수익률 이상치 {sym} ({since_pct:+.1f}%, 진입가 출처 {src})")
                logger.warning("선정 후 수익률 이상치 %s %s: %+.1f%% (진입가 출처 %s) — 표시 제외",
                               market, sym, since_pct, src)
                since_pct = None
        evidence = ""
        if kind == "new":
            try:
                detail = universe.lookup_detail(sym, market)
            except (StorageError, *DB_ERRORS):
                detail = None
            flags, is_h = _pick_tags(p)
            evidence = evidence_from_detail(detail, market, flags, is_h)
        views.append(PickView(row=p, kind=kind, metrics=m, since_pct=since_pct,
                              since_date=starts.get(sym), evidence=evidence))
        thr = settings.pick_dd_alert_pct
        if (thr and not anomaly and since_pct is not None and since_pct <= thr and starts.get(sym)
                and dd_state.get(sym) != starts.get(sym)):
            dd10_new[sym] = starts[sym]
            dd10_lines.append(f"{_name(p) or sym} 선정 후 {since_pct:+.1f}% — 보유 시 점검 (1회 알림)")
            dd10_labels.append(f"{_name(p) or sym} {since_pct:+.0f}%")
    if dd10_new and report is not None and report.pending_state is not None:
        report.pending_state.setdefault("pick_dd10", {}).setdefault(market, {}).update(dd10_new)
    if anomalies:
        # 고정 키 — 처음 나타난 날 signature 가 바뀌어 소유자 DM 1회, 같은 상태가 이어지면 억제
        health.optional_failures.append("선정 후 수익률 이상치")
    if price_fail:
        # 고정 키만 (signature 안정) — 개수·종목은 소유자 상세 줄 (D4)
        health.optional_failures.append("가격 조회 실패")
        owner_extra.append(f"가격 조회 실패 {len(price_fail)}종목: {', '.join(price_fail)}")

    # --- 기준일 ---
    kr = getattr(report, "kr", None)
    if market == "KR" and kr is not None and kr.kospi is not None:
        price_asof = kr.kospi.asof
    elif price_dates:
        price_asof = max(price_dates)
    else:
        price_asof = _bench_price_asof(market)
    health.price_as_of = price_asof

    # --- 선택 섹션 (플래그) ---
    overnight = _overnight_us_line() if market == "KR" else None
    predictions = _collect_predictions() if settings.digest_predictions else []
    weights_line = None
    if settings.digest_weights and market == "US" and picks:
        weights_line = _weights_line(_propose_weights([str(p.symbol) for p in picks]))

    # --- 판정 ---
    warn_count = sum(1 for pr in health.problems(now) if pr.user_text)
    alerts = list(report.alerts) if report is not None else []
    verdict = decide_actions(diff, alerts, dd10_labels, warn_count=warn_count, held_age=hold_age,
                             baseline_note=baseline_note)
    weekly = ledger.market_today(market, now).weekday() == 0
    inp = DigestInputs(
        market=market, now=now, verdict=verdict, diff=diff, views=views, report=report,
        health=health, price_asof=price_asof, score_asof=score_asof, stale_warn=stale_warn,
        hold_age=hold_age, list_since=list_since, dd10_lines=dd10_lines, overnight_line=overnight,
        ledger_summary=summary, ledger_has_history=bool(prev_list), weekly=weekly,
        show_ledger=(settings.ledger_line == "on"), predictions=predictions,
        weights_line=weights_line, rule_note=rule_note,
    )
    text = render_with_budget(inp)
    if settings.ledger_line == "owner":
        # 성과 줄을 구독자 대신 소유자 DM 에 — send_daily_digest 가 owner 모드면 매 런 DM 을 보낸다 (D7)
        line, _ = render_ledger(summary, now=now, has_history=bool(prev_list), weekly=weekly,
                                held=hold_age is not None)
        owner_extra.extend(f"성과: {x}" for x in line[1:])

    hold = health.is_hold(len(picks))
    hold_reason = None
    if hold:
        hold_reason = "강등(오프라인 파일)" if degraded else f"{market} 후보 0개"
    silent = bool(settings.silent_quiet and verdict.quiet and warn_count == 0)
    return DigestBundle(
        text=text, market=market, picks=picks, score_asof=score_asof, report=report,
        health=health, degraded=degraded, silent=silent, hold=hold, hold_reason=hold_reason,
        record_ledger=(hold_age is None and bool(picks) and not hold), dd10_new=dd10_new,
        verdict=verdict, diff=diff, owner_extra=owner_extra,
    )


def build_daily_digest(
    market: str = "us",
    top_n: int = 6,
    tickers: tuple[str, ...] | None = None,
    suggest_weights: bool = True,
) -> str:
    """기존 계약 유지 — 시장별 다이제스트 문자열 (쓰기 0). tickers 는 v2 에서 쓰지 않음
    (후보 = universe.pick_top). suggest_weights 는 config 플래그가 대신함."""
    if tickers:
        logger.info("build_daily_digest: tickers 인자는 v2 에서 무시됨 (후보 = universe.pick_top)")
    return assemble_daily_digest(market, top_n=top_n).text


# ----------------------------------------------------------------------
# 발송 — 조립 → (보류) → 브로드캐스트 → sent ≥ 1 이면 커밋·원장·헬스·sync → 소유자 DM
# ----------------------------------------------------------------------


def _owner_dm(text: str) -> bool:
    """소유자 평문 DM (parse_mode=None — §4.10 #10). 실패해도 False 만."""
    from src.notifier import send_safe

    return send_safe(text, parse_mode=None)


def send_daily_digest(
    market: str = "us",
    top_n: int = 6,
    tickers: tuple[str, ...] | None = None,
    use_llm: bool = True,
    suggest_weights: bool = True,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """시장별 다이제스트 1회 조립 후 active 구독자 전원에게 브로드캐스트.

    순서: 조립 → 강등/후보 0 이면 소유자 보류 DM(종료코드 20/21, 쓰기 0) → 브로드캐스트 →
    Markdown 평문 폴백 횟수 확정(헬스 반영) → sent ≥ 1 이면 commit_signal_state →
    ledger.record_send(후보 보류일은 미기록) → put_state('health') → sync →
    헬스 signature 변화·경고·실패가 있으면 소유자 DM 1통(평문).
    폴백 횟수·원장 결과는 **put_state 전에** 헬스에 반영한다 — 저장 signature 와 DM 판정
    signature 가 같아야 중복 억제가 맞는다 (D4).
    ``QUANT_BOT_LEDGER_LINE=owner`` 면 성과 줄이 구독자 메시지 대신 소유자 DM 에 실리므로
    signature 변화와 무관하게 **매 런** 소유자 DM 을 보낸다 (D7).
    Returns {"sent", "failed", "recipients", "exit_code"} (기존 3키 + exit_code).
    """
    from src import ledger, llm, subscribers, universe
    from src.notifier import fallback_count, reset_fallback_count, send_safe
    from src.signals import commit_signal_state
    from src.storage import get_storage

    subscribers.ensure_owner()
    recipients = subscribers.active_subscribers()
    if not recipients:
        logger.warning("active 구독자 없음 — 다이제스트 조립 생략")
        return {"sent": 0, "failed": 0, "recipients": 0, "exit_code": EXIT_NO_RECIPIENTS}

    bundle = assemble_daily_digest(market, now=now, top_n=top_n)
    health = bundle.health
    run_now = health.started_at
    mk = bundle.market

    if bundle.hold:
        logger.warning("발송 보류 (%s) — %s", mk, bundle.hold_reason)
        ok = _owner_dm(health.owner_text(run_now, held=True))
        return {"sent": 0, "failed": 0, "recipients": len(recipients),
                "exit_code": EXIT_HOLD_NOTIFIED if ok else EXIT_HOLD_DM_FAILED}

    text = bundle.text
    if use_llm and llm.llm_enabled():
        try:
            text = with_summary(text, llm.summarize(bundle.text))
            health.llm = llm_status(None)
        except QuantBotError as e:
            logger.warning("LLM 요약 실패 — 원문 그대로 발송: %s", e)
            health.llm = llm_status(e)

    reset_fallback_count()
    sent = failed = 0
    for chat_id, _name_ in recipients:
        if send_safe(text, chat_id, disable_notification=bundle.silent):
            sent += 1
        else:
            failed += 1
    health.sent, health.failed, health.recipients = sent, failed, len(recipients)
    logger.info("브로드캐스트 %s: 전송 %d / 실패 %d (대상 %d)", mk, sent, failed, len(recipients))
    # 브로드캐스트 직후 확정 — put_state 앞에서 헬스에 반영 (고정 키 + 상세는 notes)
    fb = fallback_count()
    if fb:
        health.optional_failures.append("Markdown 평문 폴백")
        health.notes.append(f"Markdown 평문 폴백 {fb}회")
    if bundle.verdict is not None:
        health.notes.insert(0, f"판정: {bundle.verdict.label}")

    store = get_storage()
    prev_health = None
    if sent >= 1:
        if bundle.report is not None:
            health.state_write_ok = commit_signal_state(bundle.report) and health.state_write_ok
        if bundle.record_ledger:
            sent_date = ledger.market_today(mk, run_now).isoformat()
            ok = ledger.record_send(
                mk, sent_date, bundle.picks, rule_version=universe.RULE_VERSION, sent=sent,
                failed=failed, degraded=bundle.degraded, score_asof=bundle.score_asof,
                text=text, yf_symbol_fn=universe.yf_symbol,
            )
            if not ok:
                health.optional_failures.append("원장 기록")
            health.notes.append(f"원장: {'기록 완료' if ok else '기록 실패'} ({sent_date}, {len(bundle.picks)}종목)")
        else:
            health.notes.append("원장: 미기록 (후보 보류 또는 후보 없음)")
        prev_health = store.get_state(_HEALTH_NS, f"digest:{mk}")
        if not store.put_state(_HEALTH_NS, f"digest:{mk}", health.to_state()):
            health.state_write_ok = False
        store.sync()

    # bundle.owner_extra 는 health.notes 의 별칭(assemble) — 테스트가 별도 리스트를 넘긴 경우만 병합
    if bundle.owner_extra is not health.notes:
        health.notes.extend(x for x in bundle.owner_extra if x)
    owner_mode = settings.ledger_line == "owner"
    if owner_mode or health.needs_owner_dm(run_now, prev_health):
        _owner_dm(health.owner_text(run_now, held=False))

    exit_code = EXIT_OK if failed == 0 else (EXIT_ALL_FAIL if sent == 0 else EXIT_PARTIAL_FAIL)
    return {"sent": sent, "failed": failed, "recipients": len(recipients), "exit_code": exit_code}
