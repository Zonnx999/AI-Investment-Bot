"""
src/ledger.py
=============
신호 원장 (Stage 1b WP-LEDGER) — "보낸 신호의 사후 성적표".

문제: 매일 텔레그램으로 '발굴 종목'을 보내지만, 그 종목들이 이후 실제로 어땠는지
기록·채점하는 장치가 없었다(백테스트 대상도 아님). 재점수가 돌면 과거 리스트도 사라진다.

해결 (심사 확정 설계 judge_validation §1~§7):
1. **원장(signal_ledger)** — 실제 발송된 리스트를 발송 직후 append-only 로 기록.
2. **채점기(roll_forward)** — '리스트 그대로 보유' 포트폴리오를 일별로 굴려
   ledger_perf 에 **한 번 쓰면 동결**되는 일별 수익을 쌓는다 (상장폐지로 가격 이력이
   사라져도 과거 성과가 바뀌지 않음). 벤치는 종목별 홈지수 ETF(SPY / 069500.KS /
   229200.KS) — 종목·벤치 모두 Adj Close(배당 포함)라 대칭.
3. **픽 결과(pick_outcomes)** — 새로 등장한 종목(픽 이벤트)별 21/63 거래일 수익.
   채운 값은 COALESCE 로 불변.
4. **유니버스 스냅샷(universe_snapshots)** — 재점수 전/후 단면을 시장당 JSON 1행으로 보관.
5. **판정(classify_status)** — 비중첩 20거래일 블록 초과수익으로 pending/testing/
   validated/demoted. 요동 방지를 위해 **현지 월요일 실행에서만** 상태 전환.

모듈 경계
---------
- 순수 함수(오프라인 테스트 대상): ``pick_events``, ``roll_forward``, ``update_outcomes``,
  ``block_stats``, ``classify_status``, ``summarize``, ``format_*``, ``plan_backfill``,
  ``rank_ic``, ``build_ledger_rows``.
- DB 함수: ``record_send``, ``load_*``, ``snapshot_universe``, ``ensure_pre_snapshot``,
  ``backfill``. 가격 fetch 는 오케스트레이터 ``score`` 만(``data_fetcher.fetch_close_panel``,
  지연 import).
- ``universe``/``digest`` 를 import 하지 않는다 — picks 는 ScanRow 와 같은 속성을 가진
  임의 객체(duck typing). yfinance 심볼 규칙은 ``_yf_symbol_fallback`` (universe.yf_symbol
  과 동일 규칙, 테스트로 고정).

규칙 (RULES)
-----------
- SQL 파라미터는 항상 tuple. 멀티로우 INSERT 도 flat tuple 1 statement(≤900 파라미터).
- 스키마는 CREATE TABLE IF NOT EXISTS 만 — ALTER 0건. 새 필드는 detail JSON 으로.
- 원장·스냅샷·ledger_perf 는 append/동결(INSERT OR IGNORE). pick_outcomes 는
  COALESCE 로 빈 칸만 채운다. 삭제는 사용자 수동뿐.
- 원장 기록 실패는 best-effort(로그 + False) — 발송을 절대 막지 않는다.
- 메시지 줄은 숫자만, 평가어 금지, Markdown 위험 문자(_ * ` [ ]) 금지.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.exceptions import DataFetchError
from src.logger import get_logger
from src.storage import _DB_ERRORS as DB_ERRORS
from src.storage import StorageError, get_storage

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 상수
# ----------------------------------------------------------------------
LEDGER_COST_BPS = 10.0          # 편도 거래비용 bp — backtest.DEFAULT_COST_BPS 와 동일
HORIZONS = (21, 63)             # 픽 결과 지평 (거래일) — 1M/3M, backtest 관례와 통일
BLOCK_D = 20                    # 판정 블록 길이 (거래일, 비중첩)
MIN_BLOCKS = 6                  # 판정 최소 블록 수 (≈ 6개월)
VALIDATE_T = 2.0                # validated: t ≥ +2.0
DEMOTE_T = -1.0                 # demoted: t ≤ -1.0 그리고 평균 ≤ -1.0%p
DEMOTE_MEAN_PP = -1.0
SUSPEND_DAYS = 5                # 연속 결측 5거래일(벤치는 있음) → 거래정지 취급(수익 0)
DELIST_DAYS = 10                # 연속 결측 10거래일 → 상장폐지 취급(마지막 가격 확정)
GATE_COMPUTED_DAYS = 3          # 표시 게이트: summary 계산 후 3일 이내
GATE_PRICE_BDAYS = 4            # 표시 게이트: 마지막 종가가 4거래일 이내
MAX_PICKS = 50                  # 16컬럼 × 50 = 800 파라미터 (< 900)
SESSION_DONE_HOUR = 18          # 현지 18시 이후면 당일 종가를 '확정'으로 취급

MARKETS = ("US", "KR")
MARKET_TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}
PRIMARY_BENCH = {"US": "SPY", "KR": "069500.KS"}
KOSDAQ_BENCH = "229200.KS"      # KODEX 코스닥150 (Adj Close 존재 실조회)
BENCH_LABEL = {"US": "SPY", "KR": "홈지수"}
MARKET_FLAG = {"US": "🇺🇸", "KR": "🇰🇷"}

# 07-13 소급 백필 (심사 §6) — 명시 리스트(D5 필터·scan 정렬 변경과 무관하게 고정)
BACKFILL_DATE = "2026-07-13"
BACKFILL_ASOF = "2026-07-10"
BACKFILL_RULE_VERSION = "v0"
BACKFILL_MAX_UPDATED = datetime(2026, 7, 11, tzinfo=timezone.utc)
BACKFILL_PICKS: dict[str, tuple[str, ...]] = {
    "US": ("CALM", "HG", "EOG", "SM", "MGR", "AFGE"),
    "KR": ("009970", "007700", "284740", "041510", "073240", "003090"),
}
BACKFILL_NOTES = {"MGR": "후순위채 의심", "AFGE": "후순위채 의심"}

# 텔레그램 legacy Markdown 위험 문자 (§4.10 #10) — 원장 줄에 절대 넣지 않음
_MD_UNSAFE = set("_*`[]\\")

# ----------------------------------------------------------------------
# 스키마 (CREATE IF NOT EXISTS 만 — ALTER 0건, §4.10 #9)
# ----------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_ledger (
    sent_date     TEXT NOT NULL,      -- 발송일 (시장 현지 날짜) YYYY-MM-DD
    market        TEXT NOT NULL,      -- US | KR
    rank          INTEGER NOT NULL,   -- 메시지 내 순서 1..N
    symbol        TEXT NOT NULL,
    yf_symbol     TEXT NOT NULL,
    bench_symbol  TEXT NOT NULL,
    name          TEXT,
    total_score   INTEGER,
    value_score   INTEGER,
    health_score  INTEGER,
    score_asof    TEXT,               -- 점수 기준일
    ref_price     REAL,               -- 메시지에 표시한 가격 (감사용, 진입가 아님)
    rule_version  TEXT NOT NULL,      -- universe.RULE_VERSION ('v0'=07-10 동결 규칙)
    source        TEXT NOT NULL,      -- live | reconstructed
    detail        TEXT,               -- JSON (향후 확장 — ALTER 회피)
    sent_at       TEXT NOT NULL,      -- UTC ISO
    PRIMARY KEY (sent_date, market, rank)
);
CREATE INDEX IF NOT EXISTS idx_ledger_sym ON signal_ledger (market, symbol, sent_date);

CREATE TABLE IF NOT EXISTS pick_outcomes (
    market          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    first_sent_date TEXT NOT NULL,
    yf_symbol       TEXT NOT NULL,
    bench_symbol    TEXT NOT NULL,
    rule_version    TEXT NOT NULL,
    source          TEXT NOT NULL,
    entry_date      TEXT,
    entry_px        REAL,
    bench_entry_px  REAL,
    ret_21          REAL,             -- %
    xs_21           REAL,             -- 홈지수 대비 %p
    ret_63          REAL,
    xs_63           REAL,
    last_px         REAL,
    last_px_date    TEXT,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | done | no_price | delisted
    updated_at      TEXT,
    PRIMARY KEY (market, symbol, first_sent_date)
);

CREATE TABLE IF NOT EXISTS ledger_perf (
    market        TEXT NOT NULL,
    date          TEXT NOT NULL,
    port_ret      REAL NOT NULL,      -- 비용 차감 후 일수익 (소수, 0.01 = 1%)
    bench_ret     REAL NOT NULL,
    turnover      REAL NOT NULL DEFAULT 0,
    n_held        INTEGER NOT NULL,
    n_priced      INTEGER NOT NULL,
    list_key      TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    PRIMARY KEY (market, date)
);

CREATE TABLE IF NOT EXISTS universe_snapshots (
    as_of         TEXT NOT NULL,
    market        TEXT NOT NULL,
    source        TEXT NOT NULL,      -- rescore | pre_rescore | reconstructed
    rule_version  TEXT NOT NULL,
    n_rows        INTEGER NOT NULL,
    payload       TEXT NOT NULL,      -- {"cols":[...],"rows":[[...],...]}
    created_at    TEXT NOT NULL,
    PRIMARY KEY (as_of, market)
);
"""

_LEDGER_COLS = (
    "sent_date", "market", "rank", "symbol", "yf_symbol", "bench_symbol", "name",
    "total_score", "value_score", "health_score", "score_asof", "ref_price",
    "rule_version", "source", "detail", "sent_at",
)
_PERF_COLS = (
    "market", "date", "port_ret", "bench_ret", "turnover", "n_held", "n_priced",
    "list_key", "rule_version",
)
_OUTCOME_COLS = (
    "market", "symbol", "first_sent_date", "yf_symbol", "bench_symbol", "rule_version",
    "source", "entry_date", "entry_px", "bench_entry_px", "ret_21", "xs_21", "ret_63",
    "xs_63", "last_px", "last_px_date", "status", "updated_at",
)
SNAPSHOT_COLS = (
    "symbol", "name", "sector", "total", "value", "health", "price", "mcap", "per", "pbr",
    "enriched", "updated_at",
)
# screened 컬럼명 ↔ 스냅샷 컬럼명 (per/pbr 은 9b 마이그레이션 컬럼 — stale 레플리카 대비 PRAGMA 가드)
_SCREENED_FOR_SNAPSHOT = (
    "symbol", "name", "sector", "total_score", "value_score", "health_score", "price",
    "market_cap", "per", "pbr", "enriched", "updated_at",
)
_FINAL_OUTCOME = "done"


# ----------------------------------------------------------------------
# DB 연결 (프로세스당 스키마 1회 — subscribers._conn 패턴)
# ----------------------------------------------------------------------
# 매 호출 executescript 는 Turso 레플리카에서 원격 왕복 → Storage 인스턴스로 memoize.
# 테스트의 싱글톤 리셋(tmp DB 교체) 시 인스턴스가 바뀌어 자동 재초기화.
_schema_ready_store = None


def _conn():
    """스키마가 보장된 connection. Turso 면 첫 초기화 직후 sync 1회.

    새 테이블 생성 직후 로컬 레플리카 읽기가 'no such table' 을 내지 않도록
    (T3b §8 규칙) 초기화 직후 한 번만 동기화한다.
    """
    global _schema_ready_store
    store = get_storage()
    conn = store.conn
    if _schema_ready_store is not store:
        conn.executescript(_SCHEMA)
        _schema_ready_store = store
        if getattr(store, "is_turso", False):
            store.sync()
    return conn


def _rconn():
    """읽기 전용 경로용 connection — 스키마를 만들지 않는다 (dry-run 쓰기 0 보장).

    테이블이 아직 없으면 읽기가 'no such table' 로 실패 → 각 읽기 함수가 DB_ERRORS 를
    잡아 빈 결과로 처리한다.
    """
    return get_storage().conn


# ----------------------------------------------------------------------
# 심볼·벤치·날짜 헬퍼 (순수)
# ----------------------------------------------------------------------


def _yf_symbol_fallback(symbol: str, market: str, exchange: str | None = None,
                        sector: str | None = "") -> str:
    """yfinance 심볼 — **universe.yf_symbol 과 동일 규칙** (병합 후 하나로 합칠 예정).

    KR: exchange 가 있으면 KOSDAQ→.KQ / 그 외 .KS. exchange 가 None 이면 sector(KRX 소속부)가
    비어있지 않으면 KOSDAQ(.KQ), 빈 문자열이면 KOSPI(.KS) — DB 507/507 일치 실측 규칙.
    US: 그대로 (BRK-B). CRYPTO: SYM-USD.
    """
    m = (market or "").upper()
    if m == "KR":
        if exchange is not None:
            kosdaq = str(exchange).upper() == "KOSDAQ"
        else:
            kosdaq = bool(sector)
        return f"{symbol}.{'KQ' if kosdaq else 'KS'}"
    if m == "CRYPTO":
        return f"{symbol}-USD"
    return symbol


def bench_for(market: str, yf_symbol: str) -> str:
    """종목의 홈지수 ETF (Adj Close). US=SPY, KR .KQ→229200.KS, 그 외 KR→069500.KS."""
    m = (market or "").upper()
    if m == "US":
        return PRIMARY_BENCH["US"]
    if m == "KR":
        return KOSDAQ_BENCH if yf_symbol.endswith(".KQ") else PRIMARY_BENCH["KR"]
    raise ValueError(f"원장 대상 시장 아님: {market!r}")


def market_today(market: str, now: datetime | None = None) -> date:
    """시장 현지 날짜 (KR=KST, US=ET — DST 자동)."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(MARKET_TZ[market.upper()]).date()


def last_complete_session(market: str, now: datetime | None = None) -> date:
    """종가가 확정된 마지막 날짜(상한). 현지 18시 전이면 어제까지만 채점.

    휴장일은 가격 패널에 행이 없으므로 상한만 정하면 된다. 장중 부분 봉(yfinance 가
    당일 진행 중 값을 줄 때)을 '종가'로 동결하는 사고 방지.
    """
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MARKET_TZ[market.upper()])
    d = local.date()
    return d if local.hour >= SESSION_DONE_HOUR else d - timedelta(days=1)


def _utcnow_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _num(v: Any) -> float | None:
    """유한 실수만 float, 그 외(None/NaN/inf/문자) None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(round(f)) if f is not None else None


def _md_safe(text: str) -> str:
    """Markdown 위험 문자 제거 (동적 문자열용)."""
    return "".join(ch for ch in str(text) if ch not in _MD_UNSAFE)


def _mmdd(iso_date: str | None) -> str:
    return f"{iso_date[5:7]}-{iso_date[8:10]}" if iso_date and len(iso_date) >= 10 else "?"


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# 1. 원장 기록 (발송 직후 — best-effort)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerPick:
    """signal_ledger 한 행 (채점기 입력)."""
    sent_date: str
    market: str
    rank: int
    symbol: str
    yf_symbol: str
    bench_symbol: str
    rule_version: str
    source: str
    name: str | None = None
    total_score: int | None = None


def _pick_yf_symbol(p: Any, market: str,
                    yf_symbol_fn: Callable[..., str] | None) -> str:
    explicit = getattr(p, "yf_symbol", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    sym = str(getattr(p, "symbol"))
    exchange = getattr(p, "exchange", None)
    sector = getattr(p, "sector", "") or ""
    if yf_symbol_fn is not None:
        return yf_symbol_fn(sym, market, exchange, sector)
    return _yf_symbol_fallback(sym, market, exchange, sector)


def list_key_of(symbols: Iterable[str], rule_version: str) -> str:
    """리스트 구성 식별자 — 순서 무관(동일가중), 규칙 버전 포함."""
    return f"{rule_version}|{','.join(sorted(symbols))}"


def build_ledger_rows(
    market: str,
    sent_date: str,
    picks: Sequence[Any],
    *,
    rule_version: str,
    source: str,
    sent_at: str,
    bench_symbol: str | None = None,
    score_asof: str | None = None,
    yf_symbol_fn: Callable[..., str] | None = None,
    detail: dict | None = None,
) -> list[tuple]:
    """picks(ScanRow duck-typed) → signal_ledger 16컬럼 tuple 리스트 (순수).

    picks 는 symbol 필수, name/sector/price/total_score/value_score/health_score/
    exchange/fund_asof/updated_at/flags 는 있으면 사용.
    """
    m = market.upper()
    if m not in MARKETS:
        raise ValueError(f"원장 대상 시장 아님: {market!r}")
    date.fromisoformat(sent_date)          # 형식 검증 (ValueError)
    rows: list[tuple] = []
    for rank, p in enumerate(picks, 1):
        sym = str(getattr(p, "symbol"))
        ys = _pick_yf_symbol(p, m, yf_symbol_fn)
        bench = bench_symbol or bench_for(m, ys)
        asof = (score_asof or getattr(p, "fund_asof", None)
                or (str(getattr(p, "updated_at", "") or "")[:10] or None))
        extra: dict[str, Any] = {}
        for attr in ("sector", "exchange"):
            v = getattr(p, attr, None)
            if v:
                extra[attr] = v
        flags = getattr(p, "flags", None)
        if flags:
            extra["flags"] = list(flags)
        if detail:
            extra.update(detail)
        rows.append((
            sent_date, m, rank, sym, ys, bench, getattr(p, "name", None),
            _int(getattr(p, "total_score", None)), _int(getattr(p, "value_score", None)),
            _int(getattr(p, "health_score", None)), asof,
            _num(getattr(p, "price", None)), rule_version, source,
            json.dumps(extra, ensure_ascii=False) if extra else None, sent_at,
        ))
    return rows


def _screened_max_updated(conn, market: str) -> str | None:
    """screened 의 시장별 MAX(updated_at) — 테이블이 없거나 DB 오류면 None."""
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) FROM screened WHERE market=?", (market,)
        ).fetchone()
    except DB_ERRORS:
        return None
    return row[0] if row and row[0] else None


def record_send(
    market: str,
    sent_date: str,
    picks: Sequence[Any],
    *,
    rule_version: str,
    source: str = "live",
    sent_at: str | None = None,
    bench_symbol: str | None = None,
    score_asof: str | None = None,
    sent: int | None = None,
    failed: int = 0,
    degraded: bool = False,
    dry_run: bool = False,
    text: str | None = None,
    yf_symbol_fn: Callable[..., str] | None = None,
    detail: dict | None = None,
) -> bool:
    """발송된 리스트를 원장에 기록. 반환: 원장 쓰기 성공 여부. **예외를 올리지 않음.**

    조건부 쓰기 (어긋나면 쓰기 0 + False):
      - ``dry_run`` → 쓰기 0
      - ``source == "live"`` 이면 ``sent >= 1`` 이고 ``not degraded`` 일 때만 (sent 미지정 = 0 취급)
      - ``source == "reconstructed"`` (백필) 은 발송 카운트 무관
    쓰기 = 멀티로우 ``INSERT OR IGNORE`` 1 statement (flat tuple) — PK(sent_date, market, rank)
    중복은 무시(재실행 멱등). live 면 ``state('digest_runs', f'{market}:{sent_date}')`` 1건 추가.
    실패(DB/스토리지 오류)는 경고 로그 후 False — 발송은 이미 끝났으므로 절대 막지 않는다.
    """
    m = (market or "").upper()
    if dry_run:
        logger.info("원장 기록 생략 (dry-run) — %s %s", m, sent_date)
        return False
    if source == "live":
        if degraded:
            logger.info("원장 기록 생략 (강등 런) — %s %s", m, sent_date)
            return False
        if sent is None or sent < 1:
            logger.info("원장 기록 생략 (발송 성공 0건) — %s %s", m, sent_date)
            return False
    picks = list(picks or [])
    if not picks:
        logger.info("원장 기록 생략 (종목 없음) — %s %s", m, sent_date)
        return False
    if len(picks) > MAX_PICKS:
        logger.warning("원장 기록: 종목 %d개 > %d — 앞 %d개만 기록", len(picks), MAX_PICKS, MAX_PICKS)
        picks = picks[:MAX_PICKS]
    sent_at = sent_at or _utcnow_iso()

    ok = False
    list_key = None
    try:
        conn = _conn()
        if score_asof is None and not any(
                getattr(p, "fund_asof", None) or getattr(p, "updated_at", None) for p in picks):
            mx = _screened_max_updated(conn, m)
            score_asof = mx[:10] if mx else None
        rows = build_ledger_rows(
            m, sent_date, picks, rule_version=rule_version, source=source, sent_at=sent_at,
            bench_symbol=bench_symbol, score_asof=score_asof, yf_symbol_fn=yf_symbol_fn,
            detail=detail,
        )
        list_key = list_key_of((r[3] for r in rows), rule_version)
        ph = "(" + ",".join("?" * len(_LEDGER_COLS)) + ")"
        sql = (f"INSERT OR IGNORE INTO signal_ledger ({', '.join(_LEDGER_COLS)}) VALUES "
               + ",".join([ph] * len(rows)))
        conn.execute(sql, tuple(v for row in rows for v in row))
        conn.commit()
        ok = True
        logger.info("원장 기록 %s %s: %d종목 (%s, %s)", m, sent_date, len(rows), source, rule_version)
    except (StorageError, *DB_ERRORS):
        logger.warning("원장 기록 실패 (%s %s) — 발송에는 영향 없음", m, sent_date, exc_info=True)
    except (ValueError, TypeError, AttributeError):
        # 입력 형태 오류 (잘못된 날짜·시장·picks) — 발송 경로를 죽이지 않음
        logger.warning("원장 기록 입력 오류 (%s %s) — 기록 생략", m, sent_date, exc_info=True)

    if source == "live":
        _put_run(m, sent_date, {
            "n_sent": sent, "n_failed": failed, "list_key": list_key,
            "status": "recorded" if ok else "ledger_failed", "rule_version": rule_version,
            "text": text, "recorded_at": sent_at,
        })
    return ok


def _put_run(market: str, sent_date: str, payload: dict) -> None:
    """digest_runs 상태 1건 (best-effort — put_state 가 DB 오류를 자체 흡수)."""
    try:
        get_storage().put_state("digest_runs", f"{market}:{sent_date}", payload)
    except StorageError:
        logger.warning("digest_runs 기록 실패 (%s %s)", market, sent_date, exc_info=True)


# ----------------------------------------------------------------------
# 읽기 (DB 오류 → 빈 결과)
# ----------------------------------------------------------------------


def load_sends(market: str) -> list[LedgerPick]:
    """시장의 원장 전체 (sent_date, rank 순). DB 오류면 빈 리스트."""
    try:
        rows = _rconn().execute(
            "SELECT sent_date, market, rank, symbol, yf_symbol, bench_symbol, rule_version, "
            "source, name, total_score FROM signal_ledger WHERE market=? "
            "ORDER BY sent_date, rank", (market.upper(),),
        ).fetchall()
    except (StorageError, *DB_ERRORS):
        logger.warning("원장 읽기 실패 (%s) — 빈 결과", market, exc_info=True)
        return []
    return [LedgerPick(*r) for r in rows]


def load_perf(market: str) -> list[dict]:
    """ledger_perf (date 순). DB 오류면 빈 리스트."""
    try:
        rows = _rconn().execute(
            f"SELECT {', '.join(_PERF_COLS)} FROM ledger_perf WHERE market=? ORDER BY date",
            (market.upper(),),
        ).fetchall()
    except (StorageError, *DB_ERRORS):
        logger.warning("원장 성과 읽기 실패 (%s) — 빈 결과", market, exc_info=True)
        return []
    return [dict(zip(_PERF_COLS, r)) for r in rows]


def load_outcomes(market: str) -> dict[tuple[str, str], dict]:
    """pick_outcomes → {(symbol, first_sent_date): row}. DB 오류면 빈 dict."""
    try:
        rows = _rconn().execute(
            f"SELECT {', '.join(_OUTCOME_COLS)} FROM pick_outcomes WHERE market=? "
            "ORDER BY first_sent_date, symbol", (market.upper(),),
        ).fetchall()
    except (StorageError, *DB_ERRORS):
        logger.warning("픽 결과 읽기 실패 (%s) — 빈 결과", market, exc_info=True)
        return {}
    out = {}
    for r in rows:
        d = dict(zip(_OUTCOME_COLS, r))
        out[(d["symbol"], d["first_sent_date"])] = d
    return out


def load_summary(market: str) -> dict | None:
    """채점기가 저장한 요약 (state 'ledger' / 'summary:{market}'). 없으면 None."""
    try:
        return get_storage().get_state("ledger", f"summary:{market.upper()}")
    except StorageError:
        return None


def load_runs(market: str) -> dict[str, dict]:
    """digest_runs 상태 → {sent_date: payload}. DB 오류면 빈 dict."""
    m = market.upper()
    try:
        rows = get_storage().conn.execute(
            "SELECT key, payload FROM state WHERE namespace=? AND key LIKE ?",
            ("digest_runs", f"{m}:%"),
        ).fetchall()
    except (StorageError, *DB_ERRORS):
        return {}
    out = {}
    for key, payload in rows:
        try:
            out[str(key).split(":", 1)[1]] = json.loads(payload)
        except (json.JSONDecodeError, IndexError):
            continue
    return out


# ----------------------------------------------------------------------
# 2. 채점기 — 순수 함수
# ----------------------------------------------------------------------


def _lists_by_date(sends: Sequence[LedgerPick]) -> dict[str, list[LedgerPick]]:
    out: dict[str, list[LedgerPick]] = {}
    for p in sorted(sends, key=lambda x: (x.sent_date, x.rank)):
        out.setdefault(p.sent_date, []).append(p)
    return out


def pick_events(sends: Sequence[LedgerPick]) -> list[LedgerPick]:
    """픽 이벤트 = 직전 발송 리스트에 없던 종목 (유지 종목은 이벤트 아님).

    리스트에서 빠졌다가 다시 들어오면 새 이벤트(재진입). 규칙 버전이 바뀌면
    직전 리스트를 비운 것으로 보고 새 버전의 첫 리스트 전 종목이 이벤트가 된다.
    """
    out: list[LedgerPick] = []
    prev: set[str] = set()
    prev_rv: str | None = None
    for _, plist in sorted(_lists_by_date(sends).items()):
        rv = plist[0].rule_version
        if rv != prev_rv:
            prev = set()
        out.extend(p for p in plist if p.symbol not in prev)
        prev = {p.symbol for p in plist}
        prev_rv = rv
    return out


def _empty_cursor() -> dict:
    return {
        "version": 1, "last_date": None, "list_key": None, "list_sent_date": None,
        "list_entry_date": None, "rule_version": None, "source": None,
        "w": {}, "wb": {}, "bench_of": {}, "sym_of": {}, "px": {}, "bpx": {},
        "entry_px": {}, "bench_entry_px": {}, "miss": {}, "bmiss": {}, "no_price": [],
    }


def _normalize_cursor(cursor: dict | None) -> dict:
    cur = _empty_cursor()
    if isinstance(cursor, dict):
        for k, v in cursor.items():
            if k in cur:
                cur[k] = json.loads(json.dumps(v))   # 깊은 복사 (입력 불변)
    return cur


def _calendar(closes: pd.DataFrame, market: str, asof: date | str | None) -> pd.DatetimeIndex:
    primary = PRIMARY_BENCH[market]
    if closes is None or closes.empty or primary not in closes.columns:
        return pd.DatetimeIndex([])
    cal = closes.index[closes[primary].notna()]
    if asof is not None:
        cal = cal[cal <= pd.Timestamp(asof)]
    return cal


def _px(closes: pd.DataFrame, d: pd.Timestamp, sym: str) -> float | None:
    if sym not in closes.columns:
        return None
    v = _num(closes.at[d, sym])
    return v if v is not None and v > 0 else None


def roll_forward(
    cursor: dict | None,
    sends: Sequence[LedgerPick],
    closes: pd.DataFrame,
    *,
    market: str,
    asof: date | str | None = None,
    cost_bps: float = LEDGER_COST_BPS,
) -> tuple[list[dict], dict, dict]:
    """'리스트 그대로 보유' 일별 수익을 커서 다음 날부터 굴린다 (순수).

    규약 (심사 §3):
    - 달력 = 시장 대표 벤치(SPY / 069500.KS)에 종가가 있는 날, ``asof`` 이하.
    - d 일 수익 = d-1 종가 보유분의 d 종가 수익(그 사이 비중 표류). 그날 유효한 리스트
      (= sent_date ≤ d 인 마지막 발송)의 구성이 바뀌면 **d 종가에 동일가중 재조정**하고
      턴오버 Σ|Δw| × 편도 ``cost_bps`` 를 port_ret 에서 차감 (벤치 무비용). 첫 진입은
      현금→리스트라 턴오버 1.
    - 벤치 = 같은 비중으로 각 종목의 홈지수 ETF 를 든 그림자 포트폴리오.
    - 결측: 보유 종목(또는 벤치) 가격이 d 에 없고 이후에도 없으면 → 연속 결측이
      ``SUSPEND_DAYS`` 미만이면 **d 에서 멈춤**(행 미기록, 다음 실행 재시도). 이상이면
      거래정지 취급(수익 0, 마지막 가격 유지). ``DELIST_DAYS`` 이상은 delisted 플래그.
      이후에 가격이 다시 있으면(구멍) 그날 수익 0 → 재등장일에 누적 반영.
    - 새 리스트 종목의 진입가가 없으면 같은 규칙으로 대기 또는 제외(가격 미확보 집계).

    반환: (perf_rows, new_cursor, flags). perf_rows 는 ledger_perf 행 dict 리스트.
    """
    m = market.upper()
    cur = _normalize_cursor(cursor)
    flags: dict[str, Any] = {"stopped_at": None, "stop_reason": None, "gaps": [],
                             "halted": {}, "delisted": [], "no_price": list(cur["no_price"])}
    cal = _calendar(closes, m, asof)
    if not len(cal):
        flags["stop_reason"] = "no_bench"
        return [], cur, flags
    closes = closes.sort_index()
    lists = _lists_by_date(sends)
    sent_dates = sorted(lists)
    last = cur["last_date"]
    todo = cal[cal > pd.Timestamp(last)] if last else cal
    last_valid = {c: closes[c].last_valid_index() for c in closes.columns}
    rows: list[dict] = []

    for i, d in enumerate(todo):
        ds = d.strftime("%Y-%m-%d")
        remaining = len(todo) - i          # d 포함 남은 달력일 (모두 결측일 때 연속 결측 길이)

        def kind(sym: str, prior: int, d: pd.Timestamp = d, remaining: int = remaining) -> str:
            """결측 분류: gap(이후 가격 있음) / halt(연속 결측 ≥ SUSPEND_DAYS) / wait(재시도)."""
            lv = last_valid.get(sym)
            if lv is not None and lv > d:
                return "gap"
            return "halt" if prior + remaining >= SUSPEND_DAYS else "wait"

        # ---- 1) 점검 (상태 변경 없이) ----
        wait: str | None = None
        stock_px: dict[str, float] = {}
        stock_missing: dict[str, str] = {}
        for y in cur["w"]:
            p = _px(closes, d, y)
            if p is not None:
                stock_px[y] = p
                continue
            k = kind(y, int(cur["miss"].get(y, 0)))
            if k == "wait":
                wait = y
                break
            stock_missing[y] = k
        bench_px: dict[str, float] = {}
        if wait is None:
            for b in sorted(set(cur["bench_of"].values())):
                p = _px(closes, d, b)
                if p is not None:
                    bench_px[b] = p
                    continue
                k = kind(b, int(cur["bmiss"].get(b, 0)))
                if k == "wait":
                    wait = b
                    break

        in_force = None
        for sd in sent_dates:
            if sd <= ds:
                in_force = sd
            else:
                break
        new_list: list[LedgerPick] | None = None
        entry_px: dict[str, float] = {}
        entry_bench: dict[str, str] = {}
        excluded: list[LedgerPick] = []
        if wait is None and in_force is not None:
            plist = lists[in_force]
            key = list_key_of((p.symbol for p in plist), plist[0].rule_version)
            if key != cur["list_key"]:
                new_list = plist
                for p in plist:
                    v = _px(closes, d, p.yf_symbol)
                    if v is None:
                        k = kind(p.yf_symbol, 0)
                        if k == "wait":
                            wait = p.yf_symbol
                            break
                        excluded.append(p)
                        continue
                    b = p.bench_symbol if _px(closes, d, p.bench_symbol) is not None else PRIMARY_BENCH[m]
                    entry_px[p.yf_symbol] = v
                    entry_bench[p.yf_symbol] = b

        if wait is not None:
            flags["stopped_at"] = ds
            flags["stop_reason"] = f"가격 대기: {wait}"
            break

        # ---- 2) 보유분 수익 ----
        held = cur["w"]
        n_held = len(held)
        n_priced = len(stock_px)
        port = bret = 0.0
        if held:
            r = {y: (stock_px[y] / cur["px"][y] - 1.0) if y in stock_px else 0.0 for y in held}
            tot = sum(held.values())
            port = sum(held[y] * r[y] for y in held) / tot if tot > 0 else 0.0
            cur["w"] = {y: held[y] * (1.0 + r[y]) for y in held}
            for y, p in stock_px.items():
                cur["px"][y] = p
                cur["miss"][y] = 0
            for y, k in stock_missing.items():
                cur["miss"][y] = int(cur["miss"].get(y, 0)) + 1
                if k == "gap":
                    flags["gaps"].append((cur["sym_of"].get(y, y), ds))
            rb = {b: (bench_px[b] / cur["bpx"][b] - 1.0) if b in bench_px else 0.0
                  for b in set(cur["bench_of"].values())}
            for b in rb:
                if b in bench_px:
                    cur["bpx"][b] = bench_px[b]
                    cur["bmiss"][b] = 0
                else:
                    cur["bmiss"][b] = int(cur["bmiss"].get(b, 0)) + 1
            wb = cur["wb"]
            totb = sum(wb.values())
            bret = (sum(wb[y] * rb[cur["bench_of"][y]] for y in wb) / totb) if totb > 0 else 0.0
            cur["wb"] = {y: wb[y] * (1.0 + rb[cur["bench_of"][y]]) for y in wb}

        row_version = cur["rule_version"]
        row_key = cur["list_key"]

        # ---- 3) 재조정 (d 종가) ----
        turnover = 0.0
        if new_list is not None:
            tot = sum(cur["w"].values())
            drifted = {y: w / tot for y, w in cur["w"].items()} if tot > 0 else {}
            priced = [p for p in new_list if p.yf_symbol in entry_px]
            target = {p.yf_symbol: 1.0 / len(priced) for p in priced} if priced else {}
            turnover = sum(abs(target.get(y, 0.0) - drifted.get(y, 0.0))
                           for y in set(target) | set(drifted))
            port -= turnover * cost_bps / 1e4
            rv = new_list[0].rule_version
            cur.update({
                "list_key": list_key_of((p.symbol for p in new_list), rv),
                "list_sent_date": in_force, "list_entry_date": ds, "rule_version": rv,
                "source": ("reconstructed" if any(p.source == "reconstructed" for p in new_list)
                           else "live"),
                "w": dict(target), "wb": dict(target),
                "bench_of": {y: entry_bench[y] for y in target},
                "sym_of": {p.yf_symbol: p.symbol for p in new_list},
                "px": {y: entry_px[y] for y in target},
                "entry_px": {y: entry_px[y] for y in target},
                "miss": {}, "bmiss": {},
                "no_price": [p.symbol for p in excluded],
            })
            cur["bpx"] = {b: _px(closes, d, b) for b in set(cur["bench_of"].values())}
            cur["bench_entry_px"] = {y: cur["bpx"][b] for y, b in cur["bench_of"].items()}
            flags["no_price"] = list(cur["no_price"])
            if row_version is None:
                row_version, row_key = rv, cur["list_key"]

        if row_version is not None:
            rows.append({
                "market": m, "date": ds, "port_ret": float(port), "bench_ret": float(bret),
                "turnover": float(turnover), "n_held": int(n_held), "n_priced": int(n_priced),
                "list_key": row_key, "rule_version": row_version,
            })
        cur["last_date"] = ds

    for y, n in cur["miss"].items():
        if n >= SUSPEND_DAYS:
            flags["halted"][cur["sym_of"].get(y, y)] = int(n)
        if n >= DELIST_DAYS:
            flags["delisted"].append(cur["sym_of"].get(y, y))
    return rows, cur, flags


def _ffill_px(closes: pd.DataFrame, sym: str, upto: pd.Timestamp) -> tuple[float | None, str | None]:
    """upto 이하 마지막 유효 가격과 그 날짜."""
    if sym not in closes.columns:
        return None, None
    s = closes.loc[closes.index <= upto, sym].dropna()
    s = s[s > 0]
    if s.empty:
        return None, None
    return float(s.iloc[-1]), s.index[-1].strftime("%Y-%m-%d")


def update_outcomes(
    events: Sequence[LedgerPick],
    outcomes: dict[tuple[str, str], dict],
    closes: pd.DataFrame,
    *,
    market: str,
    asof: date | str | None = None,
    now_iso: str | None = None,
) -> list[dict]:
    """픽 이벤트별 21/63 거래일 결과 행 (순수). 이미 채워진 값은 그대로 둔다.

    - 진입 = 발송일 당일 또는 이후 첫 거래일 종가(Adj Close).
    - 지평 h 의 가격이 없고 최근 SUSPEND_DAYS 거래일 내내 가격이 없으면(정지·폐지 의심)
      마지막 가격으로 확정(제외 = 낙관 편향이라 금지). 최근 DELIST_DAYS 내내 없으면 'delisted'.
    - 진입가가 SUSPEND_DAYS 거래일이 지나도록 없으면 'no_price' (집계에서 제외, 건수 표시).
    반환 행은 upsert 용 — DB 쪽은 COALESCE 로 빈 칸만 채운다.
    """
    m = market.upper()
    now_iso = now_iso or _utcnow_iso()
    cal = _calendar(closes, m, asof)
    out: list[dict] = []
    for ev in events:
        ex = outcomes.get((ev.symbol, ev.sent_date)) or {}
        if ex.get("status") == _FINAL_OUTCOME or (
                ex.get("status") == "delisted" and ex.get("ret_63") is not None):
            continue
        row = {c: ex.get(c) for c in _OUTCOME_COLS}
        row.update({"market": m, "symbol": ev.symbol, "first_sent_date": ev.sent_date,
                    "yf_symbol": ex.get("yf_symbol") or ev.yf_symbol,
                    "bench_symbol": ex.get("bench_symbol") or ev.bench_symbol,
                    "rule_version": ex.get("rule_version") or ev.rule_version,
                    "source": ex.get("source") or ev.source,
                    "status": "open", "updated_at": now_iso})
        ys, bs = row["yf_symbol"], row["bench_symbol"]
        if not len(cal):
            out.append(row)
            continue
        if row["entry_date"] is None:
            later = cal[cal >= pd.Timestamp(ev.sent_date)]
            if not len(later):
                out.append(row)
                continue
            row["entry_date"] = later[0].strftime("%Y-%m-%d")
        e = pd.Timestamp(row["entry_date"])
        if e not in cal:
            out.append(row)               # 패널이 진입일을 덮지 못함 — 다음 실행
            continue
        i0 = cal.get_loc(e)
        if row["entry_px"] is None:
            row["entry_px"] = _px(closes, e, ys)
        if row["bench_entry_px"] is None:
            row["bench_entry_px"] = _px(closes, e, bs)
        if row["entry_px"] is None or row["bench_entry_px"] is None:
            row["status"] = "no_price" if len(cal) - i0 >= SUSPEND_DAYS else "open"
            out.append(row)
            continue

        last_cal = cal[-1]
        lp, lpd = _ffill_px(closes, ys, last_cal)
        if lp is not None and (row["last_px_date"] is None or lpd >= row["last_px_date"]):
            row["last_px"], row["last_px_date"] = lp, lpd
        recent = cal[-SUSPEND_DAYS:]
        stalled = all(_px(closes, t, ys) is None for t in recent) and len(cal) - i0 > SUSPEND_DAYS
        recent_l = cal[-DELIST_DAYS:]
        delisted = (all(_px(closes, t, ys) is None for t in recent_l)
                    and len(cal) - i0 > DELIST_DAYS)

        for h in HORIZONS:
            if row[f"ret_{h}"] is not None or i0 + h >= len(cal):
                continue
            dh = cal[i0 + h]
            p = _px(closes, dh, ys)
            if p is None and stalled:
                p, _ = _ffill_px(closes, ys, dh)
                if p is None:
                    p = _num(row["last_px"])
            b = _px(closes, dh, bs)
            if p is None or b is None:
                continue
            ret = (p / row["entry_px"] - 1.0) * 100.0
            bret = (b / row["bench_entry_px"] - 1.0) * 100.0
            row[f"ret_{h}"], row[f"xs_{h}"] = ret, ret - bret

        if all(row[f"ret_{h}"] is not None for h in HORIZONS):
            row["status"] = "delisted" if delisted else _FINAL_OUTCOME
        elif delisted:
            row["status"] = "delisted"
        out.append(row)
    return out


def _merge_outcomes(existing: dict[tuple[str, str], dict], new_rows: Sequence[dict]) -> dict:
    """DB upsert(COALESCE) 와 같은 의미로 메모리 병합 — 레플리카 read-after-write 회피."""
    merged = {k: dict(v) for k, v in existing.items()}
    for r in new_rows:
        k = (r["symbol"], r["first_sent_date"])
        old = merged.get(k)
        if old is None:
            merged[k] = dict(r)
            continue
        for c in ("entry_date", "entry_px", "bench_entry_px", "ret_21", "xs_21", "ret_63", "xs_63"):
            if old.get(c) is None:
                old[c] = r.get(c)
        if old.get("status") != _FINAL_OUTCOME:
            old["status"] = r["status"]
            if r.get("last_px") is not None:
                old["last_px"], old["last_px_date"] = r["last_px"], r["last_px_date"]
        old["updated_at"] = r.get("updated_at")
    return merged


def block_stats(perf: Sequence[dict], *, block: int = BLOCK_D) -> dict:
    """비중첩 ``block`` 거래일 블록 초과수익(복리, %p) → 개수·평균·t. 미완 블록 제외.

    보유 0종목인 행(첫 진입일 — 비용만 있고 시장 노출 없음)은 거래일로 세지 않고
    다음 날 수익에 합산한다 → 블록 = '실제 보유한 20거래일', 진입 비용은 첫 블록에 반영.
    """
    rows = sorted(perf, key=lambda r: r["date"])
    factors: list[tuple[float, float]] = []
    carry_p = carry_b = 1.0
    for r in rows:
        fp, fb = 1.0 + float(r["port_ret"]), 1.0 + float(r["bench_ret"])
        if int(r.get("n_held", 1) or 0) == 0:
            carry_p, carry_b = carry_p * fp, carry_b * fb
            continue
        factors.append((fp * carry_p, fb * carry_b))
        carry_p = carry_b = 1.0
    xs: list[float] = []
    for k in range(len(factors) // block):
        seg = factors[k * block:(k + 1) * block]
        p = float(np.prod([f[0] for f in seg]))
        b = float(np.prod([f[1] for f in seg]))
        xs.append((p - b) * 100.0)
    out: dict[str, Any] = {"n": len(xs), "mean": None, "t": None, "blocks": xs,
                           "days_in_partial": len(factors) % block}
    if xs:
        mean = float(np.mean(xs))
        out["mean"] = mean
        if len(xs) >= 2:
            sd = float(np.std(xs, ddof=1))
            out["t"] = mean / (sd / math.sqrt(len(xs))) if sd > 0 else None
    return out


def classify_raw(blocks: dict) -> str:
    """블록 통계 → 상태 (요동 방지 없이). IC 는 판정 근거 아님(이번 라운드)."""
    n, mean, t = blocks.get("n", 0), blocks.get("mean"), blocks.get("t")
    if n < MIN_BLOCKS or mean is None:
        return "pending"
    if t is not None and mean < 0 and t <= DEMOTE_T and mean <= DEMOTE_MEAN_PP:
        return "demoted"
    if t is not None and t >= VALIDATE_T:
        return "validated"
    return "testing"


def classify_status(blocks: dict, prev_status: str | None = None, *, is_monday: bool) -> str:
    """판정 상태. **현지 월요일 실행에서만 전환** — 그 외 요일은 직전 상태 유지.

    직전 상태가 없으면(첫 실행·새 규칙 버전) 'pending' 에서 시작한다.
    """
    prev = prev_status or "pending"
    if not is_monday:
        return prev
    return classify_raw(blocks)


def _compound_pct(rets: Iterable[float]) -> float:
    return (float(np.prod([1.0 + r for r in rets])) - 1.0) * 100.0 if rets else 0.0


def _mdd_pct(rets: Sequence[float]) -> float:
    if not rets:
        return 0.0
    cum = np.cumprod([1.0] + [1.0 + r for r in rets])
    peak = np.maximum.accumulate(cum)
    return float(np.min(cum / peak - 1.0)) * 100.0


def _hstats(outs: Sequence[dict], h: int) -> dict:
    done = [o for o in outs if o.get(f"xs_{h}") is not None]
    xs = [float(o[f"xs_{h}"]) for o in done]
    return {"n_done": len(done), "hits": sum(1 for x in xs if x > 0),
            "mean_xs": float(np.mean(xs)) if xs else None}


def summarize(
    market: str,
    perf: Sequence[dict],
    cursor: dict | None,
    outcomes: dict[tuple[str, str], dict],
    sends: Sequence[LedgerPick],
    *,
    flags: dict | None = None,
    prev_summary: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """현재 규칙 버전의 성적 요약 (순수). state('ledger', 'summary:{market}') 에 저장되는 형태.

    상태 전환은 현지 월요일 실행에서만 (classify_status). 버전이 바뀌면 상태는 pending 부터.
    """
    m = market.upper()
    now = now or datetime.now(timezone.utc)
    cur = _normalize_cursor(cursor)
    flags = flags or {}
    rows = sorted(perf, key=lambda r: r["date"])
    rv = cur["rule_version"] or (rows[-1]["rule_version"] if rows else None)
    vrows = [r for r in rows if r["rule_version"] == rv]
    vsends = [s for s in sends if s.rule_version == rv]
    pr = [float(r["port_ret"]) for r in vrows]
    br = [float(r["bench_ret"]) for r in vrows]
    blocks = block_stats(vrows)

    prev = prev_summary if isinstance(prev_summary, dict) else {}
    prev_status = prev.get("status") if prev.get("rule_version") == rv else None
    is_monday = market_today(m, now).weekday() == 0
    status = classify_status(blocks, prev_status, is_monday=is_monday)
    changed = prev_status is not None and status != prev_status
    status_changed_at = (_utcnow_iso(now) if changed
                         else (prev.get("status_changed_at") if prev_status else None))

    prev_version = None
    older = [r for r in rows if r["rule_version"] != rv]
    if older:
        ov = older[-1]["rule_version"]
        orows = [r for r in older if r["rule_version"] == ov]
        prev_version = {
            "rule_version": ov,
            "excess_pp": (_compound_pct([r["port_ret"] for r in orows])
                          - _compound_pct([r["bench_ret"] for r in orows])),
        }

    v_outs = [o for o in outcomes.values() if o.get("rule_version") == rv]
    picks_now = []
    for y, sym in cur["sym_of"].items():
        e, p = _num(cur["entry_px"].get(y)), _num(cur["px"].get(y))
        b = cur["bench_of"].get(y)
        be, bp = _num(cur["bench_entry_px"].get(y)), _num(cur["bpx"].get(b)) if b else None
        if e and p and be and bp:
            ret = (p / e - 1.0) * 100.0
            picks_now.append({"symbol": sym, "ret": ret, "xs": ret - (bp / be - 1.0) * 100.0})

    cum_p, cum_b = _compound_pct(pr), _compound_pct(br)
    return {
        "market": m,
        "rule_version": rv,
        "since": min((s.sent_date for s in vsends), default=None),
        "entry_date": vrows[0]["date"] if vrows else None,
        "price_asof": vrows[-1]["date"] if vrows else None,
        "computed_at": _utcnow_iso(now),
        "n_days": sum(1 for r in vrows if int(r.get("n_held", 1) or 0) > 0),   # 보유 거래일
        "cum_port": cum_p, "cum_bench": cum_b, "excess_pp": cum_p - cum_b,
        "mdd_port": _mdd_pct(pr), "mdd_bench": _mdd_pct(br),
        "turnover_total": float(sum(float(r["turnover"]) for r in vrows)),
        "bench_label": BENCH_LABEL[m],
        "blocks": blocks, "min_blocks": MIN_BLOCKS,
        "status": status, "prev_status": prev_status, "status_changed": changed,
        "status_changed_at": status_changed_at,
        "has_reconstructed": any(s.source == "reconstructed" for s in vsends),
        "list_size": len(cur["w"]),
        "n_no_price": len(cur["no_price"]),
        "no_price": list(cur["no_price"]),
        "halted": sorted([[s, int(n)] for s, n in (flags.get("halted") or {}).items()]),
        "delisted": sorted(set(flags.get("delisted") or [])),
        "stopped_at": flags.get("stopped_at"),
        "stop_reason": flags.get("stop_reason"),
        "events": {"n": len(v_outs), **{f"h{h}": _hstats(v_outs, h) for h in HORIZONS}},
        "picks": picks_now,
        "prev_version": prev_version,
        "cost_bps": LEDGER_COST_BPS,
    }


# ----------------------------------------------------------------------
# 3. 표시 (숫자만·평가어 금지·Markdown 위험 문자 금지)
# ----------------------------------------------------------------------


def _bdays_between(start_iso: str, end: date) -> int:
    return int(np.busday_count(date.fromisoformat(start_iso[:10]), end))


def ledger_gate(summary: dict | None, *, now: datetime | None = None) -> tuple[bool, str | None]:
    """표시 게이트 — (보여도 되나, 아니면 상태 푸터 문구).

    computed_at ≤ 3일 **그리고** price_asof ≤ 4거래일이어야 줄을 보인다.
    """
    if not summary or not summary.get("price_asof"):
        return False, None
    now = now or datetime.now(timezone.utc)
    comp = _parse_iso(summary.get("computed_at"))
    today = market_today(summary.get("market", "US"), now)
    lag = _bdays_between(summary["price_asof"], today)
    stale_days = (now - comp).days if comp else None
    if comp is not None and stale_days <= GATE_COMPUTED_DAYS and lag <= GATE_PRICE_BDAYS:
        return True, None
    days = max(stale_days or 0, lag)
    return False, f"성적표 {days}일째 미갱신(마지막 {_mmdd(summary['price_asof'])})"


def _status_text(s: dict) -> str:
    b = s.get("blocks") or {}
    n = int(b.get("n") or 0)
    status = s.get("status") or "pending"
    if status == "pending":
        pv = s.get("prev_version")
        if pv and pv.get("rule_version") != s.get("rule_version"):
            return (f"⏳새 규칙 {s.get('rule_version')} 검증 {n}/{MIN_BLOCKS} "
                    f"({pv['rule_version']} 초과 {pv['excess_pp']:+.1f}%p)")
        return f"⏳검증 전 {n}/{MIN_BLOCKS}"
    mean, t = b.get("mean"), b.get("t")
    core = f"4주 구간 {n}개 평균 {mean:+.1f}%p" if mean is not None else f"4주 구간 {n}개"
    if t is not None:
        core += f", t {t:+.1f}"
    return ("🔻 " + core) if status == "demoted" else core


def _tails(s: dict) -> list[str]:
    tails = []
    if s.get("has_reconstructed"):
        tails.append("소급 포함")
    if s.get("n_no_price"):
        tails.append(f"가격 미확보 {int(s['n_no_price'])}건")
    halted = s.get("halted") or []
    if halted:
        first = halted[0]
        tails.append(f"거래정지 의심 {len(halted)}건({first[0]}, {int(first[1])}거래일)")
    return tails


def format_ledger_line(summary: dict | None, *, now: datetime | None = None) -> str | None:
    """메시지 1줄. 게이트를 못 넘으면 None (호출부는 ``ledger_gate`` 의 푸터를 표시).

    예: ``📒 성적: 07-13 이후 이 리스트 보유 -0.7% · SPY +4.0% · 초과 -4.7%p
    (10-07 종가, 비용 포함) · 소급 포함 · ⏳검증 전 3/6``
    """
    ok, _ = ledger_gate(summary, now=now)
    if not ok:
        return None
    s = summary
    parts = [
        f"📒 성적: {_mmdd(s.get('since') or s.get('entry_date'))} 이후 이 리스트 보유 "
        f"{s['cum_port']:+.1f}%",
        f"{s.get('bench_label', BENCH_LABEL.get(s.get('market'), '벤치'))} {s['cum_bench']:+.1f}%",
        f"초과 {s['excess_pp']:+.1f}%p ({_mmdd(s['price_asof'])} 종가, 비용 포함)",
    ]
    parts += _tails(s)
    parts.append(_status_text(s))
    return _md_safe(" · ".join(parts))


def format_weekly_block(summary: dict | None, *, now: datetime | None = None) -> str | None:
    """월요일(현지) 확장 2~3줄. 게이트 실패면 None.

    지평 적중은 픽 이벤트 ≥ 6 **그리고** 완료 지평만 (소표본 통계 노출 금지).
    """
    ok, _ = ledger_gate(summary, now=now)
    if not ok:
        return None
    s = summary
    m = s.get("market", "US")
    bl = s.get("bench_label", BENCH_LABEL.get(m, "벤치"))
    lines = [
        f"📒 주간 성적표 · {MARKET_FLAG.get(m, '')} {_mmdd(s.get('since') or s.get('entry_date'))}"
        f"~{_mmdd(s['price_asof'])} (배당 포함, 비용 {LEDGER_COST_BPS:.0f}bp)",
        f"  리스트 보유 {s['cum_port']:+.1f}% / {bl} {s['cum_bench']:+.1f}% / "
        f"초과 {s['excess_pp']:+.1f}%p · 최대낙폭 {s['mdd_port']:+.1f}% (지수 {s['mdd_bench']:+.1f}%)",
    ]
    b = s.get("blocks") or {}
    n = int(b.get("n") or 0)
    third = []
    if n == 0:
        third.append(f"4주 구간 아직 없음({int(b.get('days_in_partial') or 0)}/{BLOCK_D}거래일)")
    elif n < MIN_BLOCKS:
        xs = ", ".join(f"{x:+.1f}" for x in b.get("blocks", []))
        third.append(f"4주 구간 초과: {xs} → 판정까지 {MIN_BLOCKS - n}구간")
    else:
        third.append(_status_text({**s, "status": "testing"}))
    ev = s.get("events") or {}
    shown = []
    for h in HORIZONS:
        hs = ev.get(f"h{h}") or {}
        if int(ev.get("n") or 0) >= MIN_BLOCKS and int(hs.get("n_done") or 0) >= MIN_BLOCKS:
            shown.append(f"{h}일 적중 {hs['hits']}/{hs['n_done']} (평균 {hs['mean_xs']:+.1f}%p)")
    third.append(" · ".join(shown) if shown else "지평 적중은 픽 6건 이상 완료 후 표시")
    lines.append("  " + " · ".join(third))
    return _md_safe("\n".join(lines))


def format_report(
    summary: dict | None,
    outcomes: dict[tuple[str, str], dict] | None = None,
    *,
    runs: dict[str, dict] | None = None,
    snapshot: dict | None = None,
    now: datetime | None = None,
) -> str:
    """CLI·소유자용 평문 리포트 (scripts/ledger.py report)."""
    if not summary or not summary.get("price_asof"):
        return "원장 성적 없음 (채점 전이거나 원장 비어 있음)"
    s = summary
    m = s["market"]
    bl = s.get("bench_label", BENCH_LABEL[m])
    src = "복원" if s.get("has_reconstructed") else "라이브"
    lines = [
        f"== 발송 원장 · {MARKET_FLAG.get(m, m)} ({s.get('rule_version')} {src}, "
        f"{_mmdd(s.get('since') or s.get('entry_date'))}~{_mmdd(s['price_asof'])} 종가, "
        f"비용 {LEDGER_COST_BPS:.0f}bp) ==",
        f"리스트 보유 {s['cum_port']:+.2f}% vs {bl} {s['cum_bench']:+.2f}% "
        f"(초과 {s['excess_pp']:+.2f}%p) · MDD {s['mdd_port']:+.2f}% vs {s['mdd_bench']:+.2f}%",
    ]
    b = s.get("blocks") or {}
    xs = ", ".join(f"{x:+.1f}" for x in b.get("blocks", [])) or "없음"
    stat = f"평균 {b['mean']:+.1f}%p" if b.get("mean") is not None else "평균 -"
    if b.get("t") is not None:
        stat += f", t {b['t']:+.2f}"
    label = {"pending": "⏳검증 전", "testing": "판정 불가", "validated": "기준 충족(t ≥ 2)",
             "demoted": "🔻 기준 미달"}.get(s.get("status"), s.get("status"))
    lines.append(f"4주 구간 {int(b.get('n') or 0)}/{MIN_BLOCKS}: {xs} ({stat}) → {label}")
    ev = s.get("events") or {}
    hparts = [f"픽 {int(ev.get('n') or 0)}건"]
    for h in HORIZONS:
        hs = ev.get(f"h{h}") or {}
        if hs.get("n_done"):
            hparts.append(f"{h}일 적중 {hs['hits']}/{hs['n_done']} (평균 {hs['mean_xs']:+.1f}%p)")
        else:
            hparts.append(f"{h}일 미성숙({min(int(s.get('n_days') or 0), h)}/{h})")
    lines.append(" · ".join(hparts))
    if s.get("picks"):
        lines.append(f"진입 후 ({bl} 대비): " + " · ".join(
            f"{p['symbol']} {p['ret']:+.1f}% ({p['xs']:+.1f}%p)" for p in s["picks"]))
    tails = _tails(s)
    if s.get("stopped_at"):
        tails.append(f"채점 중단 {s['stopped_at']} ({s.get('stop_reason')})")
    if tails:
        lines.append("꼬리표: " + " · ".join(tails))
    if outcomes:
        recent = sorted(outcomes.values(), key=lambda o: (o["first_sent_date"], o["symbol"]))[-10:]
        lines.append("최근 픽 결과: " + " · ".join(
            f"{o['symbol']} {o['first_sent_date'][5:]} {o['status']}"
            + (f" 21일 {o['xs_21']:+.1f}%p" if o.get("xs_21") is not None else "")
            for o in recent))
    if runs is not None:
        lines.append(_runs_line(m, runs, now=now))
    if snapshot:
        lines.append(f"스냅샷 최신 {_mmdd(snapshot.get('as_of'))} ({snapshot.get('source')})")
    ok, footer = ledger_gate(s, now=now)
    if not ok and footer:
        lines.append(f"⚠️ {footer}")
    lines.append(f"다음 판단: {BLOCK_D}거래일 블록 {MIN_BLOCKS}개(약 6개월) 모이면 "
                 "월요일 실행에서 상태 확정")
    return "\n".join(lines)


def _runs_line(market: str, runs: dict[str, dict], *, now: datetime | None = None) -> str:
    """최근 4주 기록 성공률 — 평일 기준(휴장일은 '누락'으로 보일 수 있음)."""
    today = market_today(market, now)
    days = [today - timedelta(days=i) for i in range(1, 29)]
    live = sorted(d for d in runs)
    if not live:
        return "기록 성공률: 라이브 기록 없음"
    first = date.fromisoformat(live[0])
    wd = [d for d in days if d.weekday() < 5 and d >= first]
    ok = [d for d in wd if (runs.get(d.isoformat()) or {}).get("status") == "recorded"]
    missing = [d for d in wd if d not in ok]
    line = f"기록 성공률(최근 4주): {len(ok)}/{len(wd)}"
    if missing:
        line += " (누락 " + ", ".join(f"{d:%m/%d}" for d in sorted(missing)[-5:]) + ")"
    return line


# ----------------------------------------------------------------------
# 4. 채점 오케스트레이터 (fetch + DB) — scripts/ledger.py score
# ----------------------------------------------------------------------


def _write_perf(conn, rows: Sequence[dict]) -> None:
    """ledger_perf INSERT OR IGNORE (동결). 100행/statement (9컬럼 → 900 파라미터)."""
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        ph = "(" + ",".join("?" * len(_PERF_COLS)) + ")"
        conn.execute(
            f"INSERT OR IGNORE INTO ledger_perf ({', '.join(_PERF_COLS)}) VALUES "
            + ",".join([ph] * len(chunk)),
            tuple(r[c] for r in chunk for c in _PERF_COLS),
        )
    conn.commit()


def _upsert_outcomes(conn, rows: Sequence[dict]) -> None:
    """pick_outcomes upsert — 채운 값 불변(COALESCE), done 은 동결. 45행/statement."""
    coalesce = ("entry_date", "entry_px", "bench_entry_px", "ret_21", "xs_21", "ret_63", "xs_63")
    sets = [f"{c}=COALESCE(pick_outcomes.{c}, excluded.{c})" for c in coalesce]
    sets += [
        "last_px=CASE WHEN pick_outcomes.status='done' THEN pick_outcomes.last_px "
        "ELSE COALESCE(excluded.last_px, pick_outcomes.last_px) END",
        "last_px_date=CASE WHEN pick_outcomes.status='done' THEN pick_outcomes.last_px_date "
        "ELSE COALESCE(excluded.last_px_date, pick_outcomes.last_px_date) END",
        "status=CASE WHEN pick_outcomes.status='done' THEN 'done' ELSE excluded.status END",
        "updated_at=excluded.updated_at",
    ]
    for i in range(0, len(rows), 45):
        chunk = rows[i:i + 45]
        ph = "(" + ",".join("?" * len(_OUTCOME_COLS)) + ")"
        conn.execute(
            f"INSERT INTO pick_outcomes ({', '.join(_OUTCOME_COLS)}) VALUES "
            + ",".join([ph] * len(chunk))
            + " ON CONFLICT(market, symbol, first_sent_date) DO UPDATE SET " + ", ".join(sets),
            tuple(r[c] for r in chunk for c in _OUTCOME_COLS),
        )
    conn.commit()


def _default_fetch(tickers: tuple, start: str, end: str | None = None) -> pd.DataFrame:
    from src.data_fetcher import fetch_close_panel  # 지연 import (오프라인 테스트 격리)

    return fetch_close_panel(tickers, start, end)


def _score_market(market: str, *, now: datetime, dry_run: bool,
                  fetch: Callable[..., pd.DataFrame]) -> dict:
    m = market.upper()
    sends = load_sends(m)
    if not sends:
        return {"ok": True, "skipped": "원장 비어 있음"}
    store = get_storage()
    cursor = store.get_state("ledger", f"cursor:{m}")
    prev_summary = store.get_state("ledger", f"summary:{m}")
    outcomes = load_outcomes(m)
    existing_perf = load_perf(m)
    asof = last_complete_session(m, now)
    cur = _normalize_cursor(cursor)

    events = pick_events(sends)
    open_events = [e for e in events
                   if (outcomes.get((e.symbol, e.sent_date)) or {}).get("status") != _FINAL_OUTCOME]
    last = cur["last_date"]
    tickers = {PRIMARY_BENCH[m]} | set(cur["w"]) | set(cur["bench_of"].values())
    # 현재 보유 리스트의 발송일 이후 발송 전부 (늦게 기록된 발송이 다음 날 재조정될 때도 가격 확보)
    since = cur["list_sent_date"]
    for s in sends:
        if since is None or s.sent_date >= since:
            tickers |= {s.yf_symbol, s.bench_symbol}
    for e in open_events:
        tickers |= {e.yf_symbol, e.bench_symbol}
    starts = [last or min(s.sent_date for s in sends)]
    starts += [e.sent_date for e in open_events]
    start = (date.fromisoformat(min(starts)) - timedelta(days=10)).isoformat()
    end = (asof + timedelta(days=1)).isoformat()        # yfinance end 는 배타
    tick = tuple(sorted(tickers))
    logger.info("원장 채점 %s: 티커 %d개, %s~%s", m, len(tick), start, asof)
    closes = fetch(tick, start, end)

    rows, new_cursor, flags = roll_forward(cursor, sends, closes, market=m, asof=asof)
    now_iso = _utcnow_iso(now)
    out_rows = update_outcomes(open_events, outcomes, closes, market=m, asof=asof, now_iso=now_iso)

    if not dry_run:
        conn = _conn()
        if rows:
            _write_perf(conn, rows)
        store.put_state("ledger", f"cursor:{m}", new_cursor)
        if out_rows:
            _upsert_outcomes(conn, out_rows)

    have = {r["date"] for r in existing_perf}
    merged_perf = list(existing_perf) + [r for r in rows if r["date"] not in have]
    merged_out = _merge_outcomes(outcomes, out_rows)
    summary = summarize(m, merged_perf, new_cursor, merged_out, sends, flags=flags,
                        prev_summary=prev_summary, now=now)
    if summary.get("status_changed"):
        logger.warning("원장 상태 전환 %s: %s → %s (소유자 알림 대상)",
                       m, summary.get("prev_status"), summary.get("status"))
    if not dry_run:
        store.put_state("ledger", f"summary:{m}", summary)
    return {"ok": True, "rows": len(rows), "outcomes": len(out_rows), "flags": flags,
            "summary": summary}


def score(markets: Sequence[str] = MARKETS, *, now: datetime | None = None,
          dry_run: bool = False, fetch: Callable[..., pd.DataFrame] | None = None) -> dict[str, dict]:
    """두 시장 채점 (발송 후 ExecStartPost). 시장별 실패는 결과에 ok=False 로 담고 계속."""
    now = now or datetime.now(timezone.utc)
    fetch = fetch or _default_fetch
    results: dict[str, dict] = {}
    for mk in markets:
        try:
            results[mk.upper()] = _score_market(mk, now=now, dry_run=dry_run, fetch=fetch)
        except DataFetchError as e:
            logger.warning("원장 채점 %s: 가격 조회 실패 — %s", mk, e)
            results[mk.upper()] = {"ok": False, "error": f"가격 조회 실패 ({type(e).__name__})"}
        except (StorageError, *DB_ERRORS):
            logger.warning("원장 채점 %s: DB 오류", mk, exc_info=True)
            results[mk.upper()] = {"ok": False, "error": "DB 오류"}
    if not dry_run:
        try:
            get_storage().sync()
        except StorageError:
            logger.warning("원장 채점 후 sync 실패", exc_info=True)
    return results


# ----------------------------------------------------------------------
# 5. 유니버스 스냅샷 (재점수 전/후 단면, 시장당 JSON 1행)
# ----------------------------------------------------------------------


def _clean(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        f = float(v)
        return f if math.isfinite(f) else None
    return v


def _read_screened(conn, market: str) -> list[list]:
    """screened 전 행(enriched=0 포함 — 생존편향 보정용) → SNAPSHOT_COLS 순서 리스트.

    per/pbr 등 마이그레이션 컬럼이 stale 레플리카에 없을 수 있어 PRAGMA 로 존재 확인.
    """
    try:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(screened)").fetchall()}
    except DB_ERRORS:
        return []
    if not existing:
        return []
    sel = ", ".join(c if c in existing else "NULL" for c in _SCREENED_FOR_SNAPSHOT)
    rows = conn.execute(
        f"SELECT {sel} FROM screened WHERE market=? ORDER BY symbol", (market,)
    ).fetchall()
    return [[_clean(v) for v in r] for r in rows]


def _current_rule_version() -> str | None:
    """이미 로드된 universe 모듈의 RULE_VERSION (import 하지 않음 — duck typing)."""
    mod = sys.modules.get("src.universe")
    rv = getattr(mod, "RULE_VERSION", None) if mod is not None else None
    return rv if isinstance(rv, str) and rv else None


def latest_snapshot(market: str, conn=None) -> dict | None:
    """시장의 최신 스냅샷 메타 (payload 제외). 없거나 DB 오류면 None."""
    try:
        conn = conn or _rconn()
        r = conn.execute(
            "SELECT as_of, market, source, rule_version, n_rows, created_at "
            "FROM universe_snapshots WHERE market=? ORDER BY as_of DESC LIMIT 1",
            (market.upper(),),
        ).fetchone()
    except (StorageError, *DB_ERRORS):
        return None
    if not r:
        return None
    return dict(zip(("as_of", "market", "source", "rule_version", "n_rows", "created_at"), r))


def load_snapshot(market: str, as_of: str | None = None, conn=None) -> dict | None:
    """스냅샷 1건 → {meta..., "rows": [dict,...]}. as_of None 이면 최신."""
    try:
        conn = conn or _rconn()
        if as_of:
            r = conn.execute(
                "SELECT as_of, source, rule_version, payload FROM universe_snapshots "
                "WHERE market=? AND as_of=?", (market.upper(), as_of)).fetchone()
        else:
            r = conn.execute(
                "SELECT as_of, source, rule_version, payload FROM universe_snapshots "
                "WHERE market=? ORDER BY as_of DESC LIMIT 1", (market.upper(),)).fetchone()
    except (StorageError, *DB_ERRORS):
        return None
    if not r:
        return None
    try:
        payload = json.loads(r[3])
    except (json.JSONDecodeError, TypeError):
        logger.warning("스냅샷 payload 손상 (%s %s)", market, r[0])
        return None
    cols = payload.get("cols") or list(SNAPSHOT_COLS)
    rows = [dict(zip(cols, row)) for row in payload.get("rows", [])]
    return {"as_of": r[0], "market": market.upper(), "source": r[1], "rule_version": r[2],
            "rows": rows}


def snapshot_universe(
    source: str = "rescore",
    *,
    rule_version: str | None = None,
    as_of: str | None = None,
    markets: Sequence[str] = ("US", "KR", "CRYPTO"),
    dry_run: bool = False,
) -> dict[str, dict]:
    """screened 단면을 시장당 JSON 1행으로 보관 (INSERT OR IGNORE — 같은 as_of 는 첫 것 유지).

    as_of 기본 = 그 시장 MAX(updated_at) 날짜. rule_version 기본 = 로드된
    universe.RULE_VERSION, 없으면 'unknown'. dry_run 이면 쓰기 0 (계획만 반환).
    """
    rv = rule_version or _current_rule_version() or "unknown"
    conn = _rconn() if dry_run else _conn()
    now = _utcnow_iso()
    out: dict[str, dict] = {}
    for mk in markets:
        m = mk.upper()
        rows = _read_screened(conn, m)
        if not rows:
            out[m] = {"written": False, "reason": "screened 행 없음"}
            continue
        upd = [r[-1] for r in rows if r[-1]]
        snap_asof = as_of or (max(upd)[:10] if upd else None)
        if not snap_asof:
            out[m] = {"written": False, "reason": "updated_at 없음"}
            continue
        payload = json.dumps({"cols": list(SNAPSHOT_COLS), "rows": rows},
                             ensure_ascii=False, separators=(",", ":"))
        info = {"as_of": snap_asof, "source": source, "rule_version": rv, "n_rows": len(rows),
                "bytes": len(payload.encode("utf-8")), "written": False}
        if not dry_run:
            cur = conn.execute(
                "INSERT OR IGNORE INTO universe_snapshots "
                "(as_of, market, source, rule_version, n_rows, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snap_asof, m, source, rv, len(rows), payload, now),
            )
            conn.commit()
            info["written"] = bool(getattr(cur, "rowcount", 1))
            logger.info("유니버스 스냅샷 %s %s (%s, %s): %d행 %.0fKB", m, snap_asof, source, rv,
                        len(rows), info["bytes"] / 1024)
        out[m] = info
    return out


def ensure_pre_snapshot(*, markets: Sequence[str] = ("US", "KR", "CRYPTO"),
                        dry_run: bool = False) -> dict[str, dict]:
    """재점수 시작 전 호출 — 최신 스냅샷 as_of ≠ screened MAX(updated_at) 날짜면
    덮어쓰기 전 상태를 source='pre_rescore' 로 저장 (멱등: 같으면 아무것도 안 함).

    규칙 버전은 직전 스냅샷의 것(없으면 'v0' — 원장 이전 데이터는 v0 규칙 산출물).
    """
    conn = _rconn() if dry_run else _conn()
    out: dict[str, dict] = {}
    for mk in markets:
        m = mk.upper()
        mx = _screened_max_updated(conn, m)
        if not mx:
            out[m] = {"written": False, "reason": "screened 행 없음"}
            continue
        latest = latest_snapshot(m, conn)
        if latest and latest["as_of"] == mx[:10]:
            out[m] = {"written": False, "reason": f"이미 있음 ({latest['as_of']}, {latest['source']})"}
            continue
        rv = (latest or {}).get("rule_version") or BACKFILL_RULE_VERSION
        out[m] = snapshot_universe("pre_rescore", rule_version=rv, as_of=mx[:10], markets=(m,),
                                   dry_run=dry_run)[m]
    return out


def snapshot_reconstructed(*, as_of: str = BACKFILL_ASOF, markets: Sequence[str] = MARKETS,
                           dry_run: bool = False) -> dict[str, dict]:
    """07-10 동결 DB 를 'reconstructed' 스냅샷으로 저장. 가드:
    screened MAX(updated_at) ≤ 2026-07-11T00:00Z 이거나, 이미 as_of 스냅샷(pre_rescore 등)이 있어야 함.
    """
    conn = _rconn() if dry_run else _conn()
    out: dict[str, dict] = {}
    for mk in markets:
        m = mk.upper()
        existing = load_snapshot(m, as_of, conn)
        if existing:
            out[m] = {"written": False, "reason": f"{as_of} 스냅샷 이미 있음 ({existing['source']})"}
            continue
        mx = _parse_iso(_screened_max_updated(conn, m))
        if mx is None or mx > BACKFILL_MAX_UPDATED:
            out[m] = {"written": False, "refused": True,
                      "reason": f"재점수됨 (MAX(updated_at)={mx}) — {as_of} 복원 불가"}
            continue
        out[m] = snapshot_universe("reconstructed", rule_version=BACKFILL_RULE_VERSION,
                                   as_of=as_of, markets=(m,), dry_run=dry_run)[m]
    return out


def rank_ic(rows_a: Sequence[dict], rows_b: Sequence[dict], *, score_key: str = "total") -> dict:
    """스냅샷 A 점수 vs A→B 가격 변화의 순위 상관(스피어만) + 5분위 (순수).

    A 의 보강 완료(enriched=1) 종목 중 두 스냅샷 모두 가격이 있는 것만. 극단 수익
    (-90% 이하·+300% 이상)은 액면분할·데이터 오류 의심이라 제외.
    """
    pb = {r["symbol"]: _num(r.get("price")) for r in rows_b}
    pairs = []
    for r in rows_a:
        if not r.get("enriched"):
            continue
        s, p0, p1 = _num(r.get(score_key)), _num(r.get("price")), pb.get(r["symbol"])
        if s is None or not p0 or not p1 or p0 <= 0:
            continue
        fwd = p1 / p0 - 1.0
        if -0.9 < fwd < 3.0:
            pairs.append((s, fwd))
    if len(pairs) < 10:
        return {"n": len(pairs), "ic": None}
    df = pd.DataFrame(pairs, columns=["s", "r"])
    ic = float(df["s"].rank().corr(df["r"].rank()))
    q = pd.qcut(df["s"].rank(method="first"), 5, labels=False)
    qm = df["r"].groupby(q).mean() * 100.0
    return {"n": len(df), "ic": ic, "q1": float(qm.iloc[0]), "q5": float(qm.iloc[-1]),
            "spread": float(qm.iloc[-1] - qm.iloc[0])}


def universe_ic(market: str, *, score_key: str = "total") -> dict:
    """최근 두 스냅샷 사이 IC. 스냅샷 < 2 면 status='waiting' ('관측 대기').

    주의: 스냅샷 price 는 discover(가격 갱신)가 돈 재점수에서만 바뀐다 — enrich 단독이면
    가격이 그대로라 IC 가 무의미. 이번 라운드는 정보 표시만(판정 근거 아님).
    """
    m = market.upper()
    try:
        metas = _rconn().execute(
            "SELECT as_of FROM universe_snapshots WHERE market=? ORDER BY as_of DESC LIMIT 2",
            (m,),
        ).fetchall()
    except (StorageError, *DB_ERRORS):
        return {"status": "error", "market": m}
    if len(metas) < 2:
        return {"status": "waiting", "market": m, "n_snapshots": len(metas)}
    b, a = load_snapshot(m, metas[0][0]), load_snapshot(m, metas[1][0])
    if not a or not b:
        return {"status": "error", "market": m}
    res = rank_ic(a["rows"], b["rows"], score_key=score_key)
    return {"status": "ok", "market": m, "from": a["as_of"], "to": b["as_of"], **res}


# ----------------------------------------------------------------------
# 6. 07-13 소급 백필 (기본 dry-run)
# ----------------------------------------------------------------------


@dataclass
class _SnapRow:
    """백필용 duck-typed ScanRow (스냅샷 행 → record_send 입력)."""
    symbol: str
    name: str | None
    sector: str | None
    price: float | None
    total_score: int | None
    value_score: int | None
    health_score: int | None
    updated_at: str | None = None
    market_cap: float | None = None


@dataclass
class BackfillMarket:
    market: str
    data_source: str | None = None          # snapshot:<source> | screened
    screened_max: str | None = None
    live_rows: int = 0
    existing_rows: int = 0
    explicit: tuple[str, ...] = ()
    computed: tuple[str, ...] = ()
    picks: list[_SnapRow] = field(default_factory=list)
    refused: str | None = None


@dataclass
class BackfillPlan:
    markets: dict[str, BackfillMarket]
    ok: bool
    reasons: list[str]


def v0_top(rows: Sequence[dict], n: int = 6) -> list[dict]:
    """v0 정렬(보강 완료, total desc, mcap desc, symbol asc) 상위 n — 07-10 scan() 재현."""
    cand = [r for r in rows if r.get("enriched") and _num(r.get("total")) is not None]
    cand.sort(key=lambda r: (-_num(r["total"]), -(_num(r.get("mcap")) or 0.0), r["symbol"]))
    return cand[:n]


def plan_backfill(
    snap_rows: dict[str, list[dict] | None],
    data_source: dict[str, str | None],
    screened_max: dict[str, str | None],
    live_counts: dict[str, int],
    existing_counts: dict[str, int] | None = None,
    *,
    explicit: dict[str, tuple[str, ...]] = BACKFILL_PICKS,
) -> BackfillPlan:
    """백필 계획 (순수). 가드 3종:
    (1) 07-10 데이터 출처 없음(재점수됨 + 07-10 스냅샷 없음) → 거부
    (2) 해당 시장에 live 원장 행 있음 → 거부
    (3) 명시 리스트 ≠ 스냅샷 v0 정렬 상위 6 → 중단
    한 시장이라도 걸리면 전체 중단 (부분 백필 금지).
    """
    markets: dict[str, BackfillMarket] = {}
    reasons: list[str] = []
    for m, syms in explicit.items():
        bm = BackfillMarket(market=m, data_source=data_source.get(m),
                            screened_max=screened_max.get(m),
                            live_rows=int(live_counts.get(m, 0)),
                            existing_rows=int((existing_counts or {}).get(m, 0)),
                            explicit=tuple(syms))
        rows = snap_rows.get(m)
        if rows is None:
            bm.refused = f"{m}: 재점수됨 — {BACKFILL_ASOF} 스냅샷 없음 (복원 불가)"
        elif bm.live_rows > 0:
            bm.refused = f"{m}: live 원장 행 {bm.live_rows}개 존재 — 백필 거부"
        else:
            top = v0_top(rows, len(syms))
            bm.computed = tuple(r["symbol"] for r in top)
            if bm.computed != bm.explicit:
                bm.refused = (f"{m}: 교차검증 불일치 — 명시 {list(bm.explicit)} vs "
                              f"스냅샷 {list(bm.computed)}")
            else:
                bm.picks = [_SnapRow(r["symbol"], r.get("name"), r.get("sector"),
                                     _num(r.get("price")), _int(r.get("total")),
                                     _int(r.get("value")), _int(r.get("health")),
                                     r.get("updated_at"), _num(r.get("mcap"))) for r in top]
        if bm.refused:
            reasons.append(bm.refused)
        markets[m] = bm
    return BackfillPlan(markets=markets, ok=not reasons, reasons=reasons)


def _gather_backfill_inputs(conn) -> tuple[dict, dict, dict, dict, dict]:
    snap_rows: dict[str, list[dict] | None] = {}
    source: dict[str, str | None] = {}
    smax: dict[str, str | None] = {}
    live: dict[str, int] = {}
    existing: dict[str, int] = {}
    for m in BACKFILL_PICKS:
        smax[m] = _screened_max_updated(conn, m)
        snap = load_snapshot(m, BACKFILL_ASOF, conn)
        if snap and snap["source"] in ("reconstructed", "pre_rescore"):
            snap_rows[m], source[m] = snap["rows"], f"snapshot:{snap['source']}"
        else:
            mx = _parse_iso(smax[m])
            if mx is not None and mx <= BACKFILL_MAX_UPDATED:
                rows = _read_screened(conn, m)
                snap_rows[m] = [dict(zip(SNAPSHOT_COLS, r)) for r in rows]
                source[m] = "screened"
            else:
                snap_rows[m], source[m] = None, None
        live[m] = _count(conn, "SELECT COUNT(*) FROM signal_ledger WHERE market=? AND source='live'",
                         (m,))
        existing[m] = _count(conn, "SELECT COUNT(*) FROM signal_ledger WHERE market=? AND sent_date=?",
                             (m, BACKFILL_DATE))
    return snap_rows, source, smax, live, existing


def _count(conn, sql: str, params: tuple) -> int:
    """COUNT 조회 — 테이블이 아직 없으면(원장 첫 사용 전) 0."""
    try:
        row = conn.execute(sql, params).fetchone()
    except DB_ERRORS:
        return 0
    return int(row[0]) if row and row[0] else 0


def backfill(*, confirm: bool = False) -> tuple[BackfillPlan, dict[str, bool]]:
    """07-13 소급 백필. 기본 dry-run(쓰기 0) — ``confirm=True`` 일 때만 기록.

    기록: sent_date=2026-07-13 1회, rule_version='v0', source='reconstructed'.
    07-10 스냅샷이 없고 screened 가 동결 상태면 confirm 시 reconstructed 스냅샷도 함께 저장.
    """
    conn = _conn() if confirm else _rconn()     # dry-run 은 스키마 생성조차 하지 않음
    plan = plan_backfill(*_gather_backfill_inputs(conn))
    written: dict[str, bool] = {}
    if not (confirm and plan.ok):
        return plan, written
    for m, bm in plan.markets.items():
        if bm.data_source == "screened":
            snapshot_reconstructed(markets=(m,))
        written[m] = record_send(
            m, BACKFILL_DATE, bm.picks, rule_version=BACKFILL_RULE_VERSION,
            source="reconstructed", score_asof=BACKFILL_ASOF,
            detail={"reconstructed_from": bm.data_source},
        )
    return plan, written


def format_backfill_plan(plan: BackfillPlan, *, confirm: bool = False,
                         written: dict[str, bool] | None = None) -> str:
    """백필 계획 출력 (평문, 심사 목업 [8] 형식)."""
    ms = plan.markets
    g = []
    for m, bm in ms.items():
        mx = (bm.screened_max or "-")[:16].replace("+00:00", "")
        g.append(f"{m} {mx}Z ({bm.data_source or '출처 없음'})")
    live_ok = all(bm.live_rows == 0 for bm in ms.values())
    lines = [
        f"[가드] screened MAX(updated_at): {', '.join(g)} · 기준 ≤ 2026-07-11 · "
        f"live 행 {sum(bm.live_rows for bm in ms.values())} {'✓' if live_ok else '✗'}",
        "[교차검증] 명시 리스트 == 스냅샷 v0 정렬 상위 6: " + " ".join(
            f"{m} {'✓' if bm.computed == bm.explicit else '✗'}" for m, bm in ms.items()),
    ]
    if not plan.ok:
        lines.append("[중단] " + " / ".join(plan.reasons))
        lines.append("쓰기 0 — 백필하지 않았습니다.")
        return "\n".join(lines)
    mode = "기록 완료" if (confirm and written) else "DRY-RUN(쓰기 0)"
    lines.append(f"[원장] 발송일 {BACKFILL_DATE} · 규칙 {BACKFILL_RULE_VERSION} · "
                 f"source=reconstructed — {mode}")
    for m, bm in ms.items():
        cells = []
        prev_bench = None
        for i, p in enumerate(bm.picks, 1):
            ys = _yf_symbol_fallback(p.symbol, m, None, p.sector or "")
            bench = bench_for(m, ys)
            label = p.symbol if m == "US" else f"{ys} {p.name or ''}".strip()
            cell = f"{i} {label} {p.total_score}"
            if bench != prev_bench:
                cell += f"→{bench}"
                prev_bench = bench
            note = BACKFILL_NOTES.get(p.symbol)
            if note:
                cell += f" ({note})"
            cells.append(cell)
        lines.append(f"  {m} " + " | ".join(cells))
        if bm.existing_rows:
            lines.append(f"     ({m} {BACKFILL_DATE} 행 {bm.existing_rows}개 이미 있음 — 중복은 무시됨)")
    if confirm and written:
        lines.append("결과: " + " ".join(f"{m} {'✓' if ok else '✗'}" for m, ok in written.items()))
    else:
        lines.append("텔레그램 07-13 메시지의 종목과 같으면: python scripts/ledger.py backfill --confirm")
    return "\n".join(lines)
