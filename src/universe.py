"""
src/universe.py
===============
Phase 8 — 전 종목 유니버스 DB + 오프라인 전수 스크리닝.

고정 워치리스트(~40종목) 대신, FMP company-screener 로 투자가능 유니버스를
발굴하고 SQLite 에 영속 저장 → 종목별 key-metrics 로 보강(주 1회 배치) →
이후 스캔은 **API 0콜 오프라인**으로 전수조사.

3단계
-----
1. discover()  : company-screener(US/KR) + CoinGecko(crypto) → `screened` 테이블 upsert
                 (할인 없이 시총·가격·섹터; 크립토는 이때 바로 점수)
                 US 는 보통주 분류기(classify_us_securities)로 상장채·우선주·OTC·
                 저유동성 라인을 `excluded` 사유와 함께 표시 (삭제하지 않음 — 복구 가능)
2. enrich()    : 보강 안 됐거나 오래된 주식만 key-metrics → 점수 → DB 갱신 (재개 가능)
3. scan()      : 순수 SQL 정렬 → 저평가 상위 발굴 / 특정 종목 순위 조회
   pick_top()  : 섹터 상한(US)·지주사 상한(KR)을 적용한 최종 선정 (다이제스트용)

점수 공식은 screener.py 의 calculate_* 재사용 (단일 진실 공급원).
"""

from __future__ import annotations

import json
import math
import numbers
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.exceptions import (
    ApiAuthError,
    ApiAuthorizationError,
    DataFetchError,
    RateLimitError,
)
from src.logger import get_logger
from src.screener import (
    calculate_crypto_scores,
    calculate_health_score,
    calculate_value_score,
)
from src.storage import add_column_if_missing, get_storage
from src.utils import clip

logger = get_logger(__name__)

# 시장별 발굴 설정 (시총 floor 는 사용자 합의: 중대형주만)
MARKET_CAP_FLOOR = 1_000_000_000          # $1B (US)
KR_MARKET_CAP_FLOOR = 500_000_000_000     # 5천억 KRW (한국 중대형, MKTCAP 는 원화)
CRYPTO_MCAP_FLOOR = 1_000_000_000         # $1B
CRYPTO_TOP_N = 100
ENRICH_MAX_AGE = timedelta(days=7)        # 펀더멘털 재보강 주기
DISCOVER_CHUNK = 200                      # 발굴 upsert 배치 크기 (executescript 1회)
ABORT_AFTER_RATE_LIMITS = 5               # 연속 429 N회면 보강 루프 중단
KR_MIN_LIST_SIZE = 1500                   # KRX 일별 목록이 이보다 작으면 'not_listed' 판정 보류

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screened (
    symbol           TEXT NOT NULL,
    market           TEXT NOT NULL,          -- US | KR | CRYPTO
    name             TEXT,
    sector           TEXT,
    industry         TEXT,
    price            REAL,
    market_cap       REAL,
    dividend_yield   REAL,
    roe              REAL,
    ev_to_sales      REAL,
    fcf_yield        REAL,
    net_debt_ebitda  REAL,
    health_score     INTEGER,
    value_score      INTEGER,
    total_score      INTEGER,
    per              REAL,                          -- KR(DART): 주가수익비율
    pbr              REAL,                          -- KR(DART): 주가순자산비율
    detail           TEXT,                          -- 점수 분해 JSON (--check/텔레그램)
    enriched         INTEGER NOT NULL DEFAULT 0,   -- 0=발굴만, 1=보강+점수 완료
    discovered_at    TEXT,
    updated_at       TEXT,
    excluded         TEXT,                          -- NULL=정상, 값=제외 사유 (sibling/otc/...)
    exchange         TEXT,                          -- US: exchangeShortName, KR: KOSPI/KOSDAQ
    adv_value        REAL,                          -- 일 거래대금 (US=USD, KR=KRW)
    seen_at          TEXT,                          -- 마지막 발굴 확인 시각 (KR 은 KRX 기준일)
    PRIMARY KEY (symbol, market)   -- 주식 'M'(Macy's)과 크립토 'M' 충돌 방지
);
CREATE INDEX IF NOT EXISTS idx_screened_market_score
    ON screened (market, total_score DESC);
"""

# 기존 DB 마이그레이션 — Turso stale 레플리카 안전(중복 컬럼 오류 삼킴, §4.10 #9)
_MIGRATIONS = (
    ("per", "REAL"), ("pbr", "REAL"), ("detail", "TEXT"),              # 9b
    ("excluded", "TEXT"), ("exchange", "TEXT"),                         # 수정 라운드
    ("adv_value", "REAL"), ("seen_at", "TEXT"),
)
_NEW_COLS = ("excluded", "exchange", "adv_value", "seen_at")


@dataclass
class ScanRow:
    symbol: str
    market: str
    name: str
    sector: str
    price: float
    market_cap: float
    total_score: int
    value_score: int
    health_score: int
    roe: float | None
    per: float | None = None
    pbr: float | None = None
    # --- 수정 라운드 신규 (끝에 기본값 → ScanRow(*r) 위치 생성 호환) ---
    exchange: str | None = None
    excluded: str | None = None            # NULL=정상, 값=제외 사유
    flags: tuple[str, ...] = ()            # detail.flags (holding/one_off/fin_neutral/...)
    fund_asof: str | None = None           # 재무 보강 기준일 (updated_at 날짜)
    price_asof: str | None = None          # 가격/시총 기준일 (seen_at 날짜, KR 은 KRX 기준일)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = get_storage().conn
    conn.executescript(_SCHEMA)
    for col, coltype in _MIGRATIONS:
        add_column_if_missing(conn, "screened", col, coltype)
    return conn


def _cols(conn) -> set[str]:
    """screened 의 컬럼 집합 (PRAGMA — libsql 에선 로컬 레플리카 기준).

    원격 ALTER 직후 stale 로컬 레플리카가 새 컬럼을 아직 모를 수 있다 → 새 컬럼을
    참조하는 SELECT 는 이 결과로 가드해 'no such column' 크래시를 막는다 (§4.10 #9).
    """
    return {r[1] for r in conn.execute("PRAGMA table_info(screened)").fetchall()}


def _excluded_clause(cols: set[str]) -> str:
    return "AND excluded IS NULL" if "excluded" in cols else ""


def _date_part(ts) -> str | None:
    """ISO 타임스탬프/날짜 문자열 → 'YYYY-MM-DD' (없으면 None)."""
    if not ts:
        return None
    s = str(ts)
    return s[:10] if len(s) >= 10 else s


# ----------------------------------------------------------------------
# 배치 쓰기 (Turso 왕복 최소화)
# ----------------------------------------------------------------------
#
# libsql 임베디드 레플리카는 쓰기 statement 마다 클라우드(태평양) 왕복을 한다
# (실측 ~1s/statement). executemany 도 내부적으로 statement 루프라 효과 없음.
# 반면 executescript 는 다중 statement 를 **한 요청으로 배치** 전송 → 실측 ~40ms/row.
# 따라서 재점수 UPDATE·발굴 UPSERT 들을 모아 executescript 한 방으로 흘려보낸다
# (전수 재점수 240분 → ~1-2분). executescript 는 파라미터 바인딩을 못 받으므로 값은
# _sql_lit 로 직렬화한다 (회사명 아포스트로피·detail JSON 따옴표·한글 안전 처리).
#
# 주의: 레플리카는 자기 쓰기를 sync() 전에는 로컬에 안 비춘다 → 루프 도중 방금 쓴
# 행을 다시 읽지 말 것. 최종 반영은 호출부(build_universe)의 get_storage().sync() 가 담당.


def _sql_lit(v) -> str:
    """파이썬 값을 SQLite 리터럴 문자열로 직렬화 (executescript 용, 파라미터 불가).

    None→NULL, 정수→그대로, 실수→유한치만(NaN/inf→NULL), 그 외→작은따옴표 문자열
    (내부 작은따옴표는 '' 로 이스케이프 — SQLite 표준). numpy 스칼라도 numbers ABC 로 포착.
    """
    if v is None:
        return "NULL"
    if isinstance(v, numbers.Integral):       # int / numpy 정수 / bool
        return str(int(v))
    if isinstance(v, numbers.Real):            # float / numpy 실수
        f = float(v)
        return repr(f) if math.isfinite(f) else "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def _build_update(set_cols: dict, symbol: str, market: str) -> str:
    """screened 한 행 UPDATE 문 생성 (값은 리터럴로 인라인). 복합키로 한정."""
    sets = ", ".join(f"{col}={_sql_lit(val)}" for col, val in set_cols.items())
    return (f"UPDATE screened SET {sets} "
            f"WHERE symbol={_sql_lit(symbol)} AND market={_sql_lit(market)}")


def _flush_updates(conn, statements: list[str]) -> None:
    """모인 UPDATE/UPSERT 들을 executescript 한 방으로 전송 (Turso 왕복 1회). 빈 리스트는 no-op."""
    if not statements:
        return
    conn.executescript(";\n".join(statements) + ";")
    conn.commit()


# ----------------------------------------------------------------------
# 0. US 보통주 분류기 (순수 함수 — 네트워크/DB 없음)
# ----------------------------------------------------------------------
#
# FMP company-screener 는 상장 후순위채(MGR/AFGE…)·우선주·OTC 라인을 isEtf/isFund=false 인
# '일반 주식'으로 주고 회사명도 모회사명으로 준다 (UNI-01/02). 가격 기반 배수가 저가
# 파생증권에서 극단값이 되어 저평가 랭킹을 지배하므로, 발굴 단계에서 사유를 붙여 제외한다.
# 규칙·임계는 실데이터 2세트(6월 2190행/10월 2110행) 전수 검토로 정했고 감시 22종목
# (GOOG/GOOGL, BRK-A/B, FOX/FOXA, PFBC, PTRN, BMNR …) 오탐 0 — 단 표본 내 보정이라
# 오탐이 발견되면 WHITELIST 에 심볼을 추가한다 (블랙리스트는 만들지 않음).

MIN_ADV_USD = 1_000_000          # 일 거래대금 하한 (평균거래량 × 가격)
SIBLING_PRICE_CAP = 30.0         # 채권형 액면(~$25) 부근
SIBLING_PRICE_RATIO = 0.6        # 형제(주력) 가격 대비
SIBLING_ADV_DOMINANCE = 5.0      # 주력 ADV >= 5 x 후보 ADV
SIBLING_THIN_RATIO = 0.01        # 후보 ADV <= 1% x 주력 ADV  (BMNP/STRD/STRK/MKC-V/WLYB)
OTC_EXCLUDED_EXCHANGES = frozenset({"OTC", "PNK"})
WHITELIST: frozenset[str] = frozenset()   # 오탐 발견 시 심볼 추가 (수동 오버라이드)

_STRONG_NONCOMMON = re.compile(
    r"\b(notes?|nts|debentures?|junior|subordinated|jr ?sub|depositary|perpetual|perp|pfd|"
    r"warrants?|non-?cumulative|tangible equity|(corporate|equity) units?)\b"
    r"|\bpreferred (stock|shares?|securities|units?|series|stk)\b"
    r"|\bdue (in )?20\d\d\b"
    r"|\d+(\.\d+)?\s*%"
    r"|\bcollateral|\b\d+\.\d+\b", re.I)
_WEAK_NONCOMMON = re.compile(r"\bseries\b", re.I)
_COMMON_HINT = re.compile(r"\bcommon (stock|shares?)\b|\bordinary shares\b|\bclass [a-z]\b", re.I)
_PREFERRED_SUFFIX = re.compile(r"-P[A-Z]?$")


def _norm_name(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def name_is_noncommon(name: str | None) -> bool:
    """회사명만으로 비보통주(채권/우선주/워런트) 판정.

    STRONG 패턴은 단독 제외; WEAK('series')는 이름에 common stock/ordinary shares/class X
    가 없을 때만 (PTRN 'Series A Common Stock' 보호, SOJE 'Southern Company Series 2' 포착).
    """
    nm = name or ""
    if _STRONG_NONCOMMON.search(nm):
        return True
    return bool(_WEAK_NONCOMMON.search(nm)) and not _COMMON_HINT.search(nm)


def normalize_screener_row(r: dict) -> dict:
    """FMP company-screener 행 → 분류기 입력 dict.

    adv_usd = price × (avgVolume if not None else volume). 둘 다 없으면 None (규칙 건너뜀).
    """
    v = r.get("avgVolume")
    if v is None:
        v = r.get("volume")
    price = r.get("price") or 0.0
    return {"symbol": r["symbol"], "name": r.get("companyName") or "", "price": price,
            "industry": r.get("industry") or "", "exchange": r.get("exchangeShortName"),
            "adv_usd": (price * v) if v is not None else None}


def classify_us_securities(rows: list[dict], *, min_adv: float = MIN_ADV_USD,
                           whitelist: frozenset[str] | None = None) -> dict[str, str]:
    """US 행 목록 → {symbol: 제외사유}. 사유 없는(정상 보통주) 심볼은 키에 없음.

    입력 행: symbol, name, price, industry, exchange, adv_usd (normalize_screener_row 형태).
    적용 순서(첫 매칭): otc → shell → suffix → name → sibling → illiquid.
    exchange/adv_usd 가 None 이면 해당 규칙만 건너뜀 (오탐 방지).
    형제 규칙: 같은 정규화 회사명 ≥2 일 때 **주력 = 거래대금 최대 형제**. 주력 ADV ≥ 5×후보
    이고 (0<가격<30 이고 가격 ≤ 0.6×주력가격) 또는 후보 ADV ≤ 1%×주력 ADV 면 제외.
    """
    wl = WHITELIST if whitelist is None else whitelist
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[_norm_name(r.get("name"))].append(r)
    out: dict[str, str] = {}
    for r in rows:
        sym = r["symbol"]
        if sym in wl:
            continue
        if r.get("exchange") in OTC_EXCLUDED_EXCHANGES:
            out[sym] = "otc"
            continue
        if r.get("industry") == "Shell Companies":
            out[sym] = "shell"
            continue
        if _PREFERRED_SUFFIX.search(sym):
            out[sym] = "suffix"
            continue
        if name_is_noncommon(r.get("name")):
            out[sym] = "name"
            continue
        key = _norm_name(r.get("name"))
        g = groups[key]
        adv = r.get("adv_usd")
        if len(g) >= 2 and adv is not None and key:
            prim = max(g, key=lambda x: x.get("adv_usd") or 0.0)
            prim_adv = prim.get("adv_usd") or 0.0
            if prim is not r and prim_adv >= SIBLING_ADV_DOMINANCE * adv:
                p, pp = r.get("price") or 0.0, prim.get("price") or 0.0
                cheap_line = 0 < p < SIBLING_PRICE_CAP and p <= SIBLING_PRICE_RATIO * pp
                thin_line = adv <= SIBLING_THIN_RATIO * prim_adv
                if cheap_line or thin_line:
                    out[sym] = "sibling"
                    continue
        if adv is not None and adv < min_adv:
            out[sym] = "illiquid"
    return out


# ----------------------------------------------------------------------
# 1. 발굴 (discover)
# ----------------------------------------------------------------------


def _build_upsert(*, symbol, market, name, sector, industry, price, market_cap,
                  dividend_yield, exchange=None, adv_value=None, excluded=None,
                  seen_at=None) -> str:
    """발굴 필드 UPSERT 문 생성 (값은 리터럴 — executescript 배치용).

    기존 보강 점수(roe/scores/enriched)는 보존. updated_at 은 ON CONFLICT 에서
    **갱신하지 않음**: 이 컬럼은 symbols_needing_enrichment 가 쓰는 '펀더멘털
    신선도' 마커라, 주간 배치의 discover 가 enrich 직전에 이를 now 로 밀어버리면
    만기 지난 종목이 전부 '신선함' 으로 위장되어 재보강이 영원히 스킵됨
    (신규 INSERT 시에만 세팅, 이후 갱신은 enrich/enrich_kr 담당).
    excluded/exchange/adv_value/seen_at 은 매 발굴마다 **재평가** (WHITELIST·규칙 변경 반영).
    """
    now = _utcnow()
    vals = {
        "symbol": symbol, "market": market, "name": name, "sector": sector,
        "industry": industry, "price": price, "market_cap": market_cap,
        "dividend_yield": dividend_yield, "exchange": exchange, "adv_value": adv_value,
        "excluded": excluded, "seen_at": seen_at, "discovered_at": now, "updated_at": now,
    }
    cols = ", ".join(vals)
    lits = ", ".join(_sql_lit(v) for v in vals.values())
    upd = ", ".join(f"{c}=excluded.{c}" for c in
                    ("name", "sector", "industry", "price", "market_cap", "dividend_yield",
                     "exchange", "adv_value", "excluded", "seen_at"))
    return (f"INSERT INTO screened ({cols}) VALUES ({lits}) "
            f"ON CONFLICT(symbol, market) DO UPDATE SET {upd}")


def _upsert_universe_row(conn, *, symbol, market, name, sector, industry,
                         price, market_cap, dividend_yield, exchange=None,
                         adv_value=None, excluded=None, seen_at=None) -> None:
    """발굴 한 행 즉시 upsert (단건용 — 대량은 _build_upsert + _flush_updates 배치)."""
    conn.execute(_build_upsert(
        symbol=symbol, market=market, name=name, sector=sector, industry=industry,
        price=price, market_cap=market_cap, dividend_yield=dividend_yield,
        exchange=exchange, adv_value=adv_value, excluded=excluded, seen_at=seen_at,
    ))


def _store_crypto_scored(conn, *, symbol, name, price, market_cap, scores) -> None:
    """크립토는 발굴 즉시 점수까지 (CoinGecko 가 필요한 데이터 다 줌)."""
    conn.execute(
        """
        INSERT INTO screened (symbol, market, name, price, market_cap,
                              total_score, value_score, health_score,
                              enriched, discovered_at, updated_at)
        VALUES (?, 'CRYPTO', ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(symbol, market) DO UPDATE SET
            name=excluded.name, price=excluded.price, market_cap=excluded.market_cap,
            total_score=excluded.total_score, value_score=excluded.value_score,
            health_score=excluded.health_score, enriched=1, updated_at=excluded.updated_at
        """,
        (symbol, name, price, market_cap, scores["total_score"],
         scores["volatility_score"], scores["rank_score"], _utcnow(), _utcnow()),
    )


def _discover_us(conn, market_cap_floor: float) -> int:
    """FMP company-screener → 보통주 분류 → 배치 upsert. 발굴 행 수 반환 (제외 포함).

    미확인 행(DB 에 있는데 오늘 응답에 없는 US 행)은 **자동 제외하지 않는다** — HG·DDS 등
    정상 기업이 FMP country 재분류로 빠지는 사례가 있어(원인 미규명) seen_at 만 남기고 경고.
    """
    from src.data_fetcher import fetch_company_screener

    rows = fetch_company_screener(country="US", market_cap_min=market_cap_floor)
    norm = [normalize_screener_row(r) for r in rows]
    reasons = classify_us_securities(norm)
    # 쓰기 전에 읽기 — 레플리카는 자기 쓰기를 sync 전엔 못 보므로 미확인 집계는 메모리로.
    existing = {r[0] for r in conn.execute(
        "SELECT symbol FROM screened WHERE market='US'").fetchall()}
    seen_at = _utcnow()

    batch: list[str] = []
    for r, nr in zip(rows, norm):
        price = r.get("price") or 0.0
        last_div = r.get("lastAnnualDividend") or 0.0
        div_yield = (last_div / price * 100) if price else 0.0
        batch.append(_build_upsert(
            symbol=r["symbol"], market="US",
            name=r.get("companyName", ""), sector=r.get("sector", ""),
            industry=r.get("industry", ""), price=price,
            market_cap=r.get("marketCap") or 0.0, dividend_yield=div_yield,
            exchange=nr["exchange"], adv_value=nr["adv_usd"],
            excluded=reasons.get(r["symbol"]), seen_at=seen_at,
        ))
        if len(batch) >= DISCOVER_CHUNK:
            _flush_updates(conn, batch)
            batch.clear()
    _flush_updates(conn, batch)

    unseen = existing - {r["symbol"] for r in rows}
    if unseen:
        logger.warning("US 미확인 %d행 (오늘 스크리너 응답에 없음, 자동 제외 안 함): %s%s",
                       len(unseen), ", ".join(sorted(unseen)[:10]),
                       " …" if len(unseen) > 10 else "")
    logger.info("US 발굴: %d행, 제외 %d (%s)", len(rows), len(reasons),
                dict(Counter(reasons.values())))
    return len(rows)


def _latest_kr_trading_day() -> tuple[str | None, list[dict]]:
    """오늘(KST)부터 거슬러 올라가며 KRX 데이터가 있는 영업일 찾기 (최대 10일).

    Returns (basDd, KOSPI 일별데이터) — 못 찾으면 (None, []).
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from src.data_fetcher import fetch_krx_daily

    now = _dt.now(ZoneInfo("Asia/Seoul"))
    for back in range(10):
        bas_dd = (now - timedelta(days=back)).strftime("%Y%m%d")
        try:
            data = fetch_krx_daily("KOSPI", bas_dd)
        except DataFetchError as e:
            logger.warning("KRX 일별 조회 실패 (%s) — %s", bas_dd, e)
            return None, []
        if data:
            return bas_dd, data
    return None, []


def _common_stock_codes(bas_dd: str) -> set[str]:
    """기본정보에서 '주권 + 보통주' 6자리 코드만 (ETF/리츠/우선주 제외)."""
    from src.data_fetcher import fetch_krx_base_info

    codes: set[str] = set()
    for market in ("KOSPI", "KOSDAQ"):
        for b in fetch_krx_base_info(market, bas_dd):
            if b.get("SECUGRP_NM") == "주권" and b.get("KIND_STKCERT_TP_NM") == "보통주":
                codes.add(b.get("ISU_SRT_CD"))
    return codes


def _discover_kr_rows(conn, floor: float = KR_MARKET_CAP_FLOOR) -> tuple[int, dict[str, float]]:
    """KRX 한국 전 종목 발굴 (코스피+코스닥) + 위생 처리. (정상 발굴 수, {코드: 시총}) 반환.

    보통주만, 시총 floor 이상 → excluded NULL. 위생(매 발굴 재평가, 복귀 시 NULL):
      · below_floor : DB 에 있던 종목이 시총 하한 미달 (가격·시총은 계속 갱신)
      · halted      : 당일 거래대금 0 (거래정지)
      · not_listed  : KRX 전체 목록(정상 수신일 ≥ KR_MIN_LIST_SIZE)에 코드가 없음 (상장폐지)
      · kr_adr      : 6자리 코드가 아닌 심볼 (과거 FMP 가 섞어 넣은 US ADR)
    exchange=MKT_NM(KOSPI/KOSDAQ), adv_value=ACC_TRDVAL(원), seen_at=KRX 기준일.
    재무지표는 KRX 에 없음 → enriched=0 (점수는 DART, enrich_kr).
    """
    from src.data_fetcher import fetch_krx_daily

    bas_dd, kospi = _latest_kr_trading_day()
    if not bas_dd:
        logger.warning("KRX 최근 영업일 데이터를 찾지 못함 — KR 발굴 스킵")
        return 0, {}
    try:
        kosdaq = fetch_krx_daily("KOSDAQ", bas_dd)
    except DataFetchError as e:
        # KOSPI 실패가 (None, []) 로 우아하게 처리되는 것과 일관 — KOSDAQ 만 실패하면
        # KOSPI 발굴은 계속한다 (이 예외가 위로 새면 discover() KR 블록을 거쳐 CRYPTO 발굴까지 중단).
        logger.warning("KOSDAQ 발굴 실패 — KOSPI 만으로 진행: %s", e)
        kosdaq = []
    common = _common_stock_codes(bas_dd)
    seen_at = f"{bas_dd[:4]}-{bas_dd[4:6]}-{bas_dd[6:8]}"
    all_rows = kospi + kosdaq
    listed = {row.get("ISU_CD") for row in all_rows}
    # 쓰기 전에 읽기 (레플리카 self-read 제약)
    existing = {r[0] for r in conn.execute(
        "SELECT symbol FROM screened WHERE market='KR'").fetchall()}

    n = 0
    mcaps: dict[str, float] = {}
    excl: Counter = Counter()
    batch: list[str] = []
    for row in all_rows:
        code = row.get("ISU_CD")
        if code not in common:                       # 우선주/리츠/ETF 제외
            continue
        try:
            price = float(row.get("TDD_CLSPRC") or 0)
            mcap = float(row.get("MKTCAP") or 0)
            raw_trdval = row.get("ACC_TRDVAL")
            # 거래대금 필드가 없으면 None → 'halted' 규칙만 건너뜀 (결측을 거래정지로 오판 금지)
            trdval = float(str(raw_trdval).replace(",", "")) if raw_trdval not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if mcap < floor:
            if code not in existing:                 # 신규 소형주는 애초에 안 넣음
                continue
            excluded = "below_floor"                 # 기존 행은 가격·시총 갱신 + 사유
        elif trdval is not None and trdval <= 0:
            excluded = "halted"
        else:
            excluded = None
        batch.append(_build_upsert(
            symbol=code, market="KR", name=row.get("ISU_NM", ""),
            sector=row.get("SECT_TP_NM", ""), industry="",
            price=price, market_cap=mcap, dividend_yield=0.0,   # 배당은 DART(9b)
            exchange=row.get("MKT_NM") or None, adv_value=trdval,
            excluded=excluded, seen_at=seen_at,
        ))
        if excluded is None:
            n += 1
            mcaps[code] = mcap
        else:
            excl[excluded] += 1
        if len(batch) >= DISCOVER_CHUNK:
            _flush_updates(conn, batch)
            batch.clear()

    # 위생: 상장폐지(목록에 없음) — 정상 수신일에만 (부분 수신으로 전 종목이 사라지는 사고 방지)
    if len(listed) >= KR_MIN_LIST_SIZE:
        gone = sorted(s for s in existing if len(s) == 6 and s not in listed)
        for s in gone:
            batch.append(_build_update({"excluded": "not_listed"}, s, "KR"))
        excl["not_listed"] += len(gone)
    else:
        logger.warning("KRX 목록 %d행 < %d — 'not_listed' 판정 보류", len(listed), KR_MIN_LIST_SIZE)
    # 위생: ADR/비6자리 심볼 (알파뉴메릭 6자리 0126Z0 는 정상 — 길이로만 판정, GLOB 금지)
    adr = sorted(s for s in existing if len(s) != 6)
    for s in adr:
        batch.append(_build_update({"excluded": "kr_adr"}, s, "KR"))
    excl["kr_adr"] += len(adr)
    _flush_updates(conn, batch)

    logger.info("KR 발굴: %d종목 (기준일 %s), 제외 %s", n, bas_dd, dict(excl))
    return n, mcaps


def _discover_kr(conn, floor: float = KR_MARKET_CAP_FLOOR) -> int:
    """KRX 발굴 (발굴 종목 수 반환) — 상세는 _discover_kr_rows."""
    return _discover_kr_rows(conn, floor)[0]


def discover(
    markets: tuple[str, ...] = ("US", "KR", "CRYPTO"),
    market_cap_floor: float = MARKET_CAP_FLOOR,
) -> dict[str, int]:
    """유니버스 발굴 → screened 테이블 upsert. 시장별 발굴 종목 수 반환."""
    from src.data_fetcher import fetch_crypto_top

    conn = _conn()
    counts: dict[str, int] = {}

    for mkt in markets:
        if mkt == "US":
            try:
                counts["US"] = _discover_us(conn, market_cap_floor)
            except DataFetchError as e:
                logger.warning("발굴 실패 US — %s", e)
                counts["US"] = 0

        elif mkt == "KR":
            # US/CRYPTO 와 동일하게 시장 단위로 격리 — 기본정보(_common_stock_codes)
            # 등 다른 KRX 호출이 실패해도 CRYPTO 발굴까지 막지 않도록.
            try:
                counts["KR"] = _discover_kr(conn)
            except DataFetchError as e:
                logger.warning("발굴 실패 KR — %s", e)
                counts["KR"] = 0

        elif mkt == "CRYPTO":
            try:
                coins = fetch_crypto_top(top_n=CRYPTO_TOP_N)
            except DataFetchError as e:
                logger.warning("발굴 실패 CRYPTO — %s", e)
                counts["CRYPTO"] = 0
                continue
            n = 0
            for c in coins:
                if (c.get("market_cap") or 0) < CRYPTO_MCAP_FLOOR:
                    continue
                _store_crypto_scored(
                    conn, symbol=(c.get("symbol") or "").upper(),
                    name=c.get("name", ""), price=c.get("current_price") or 0.0,
                    market_cap=c.get("market_cap") or 0.0,
                    scores=calculate_crypto_scores(c),
                )
                n += 1
            counts["CRYPTO"] = n

    conn.commit()
    logger.info("발굴 완료: %s", counts)
    return counts


def reclassify_us(conn=None) -> dict:
    """DB 의 US 행을 현재 규칙으로 재분류 (오프라인, API 0콜). 바뀐 행만 배치 UPDATE.

    Returns {"changed": n, "excluded": 제외 총수, "by_reason": {...}, "excluded_symbols": set}.
    컬럼이 NULL(첫 discover 전)인 행은 해당 규칙만 건너뜀.
    """
    conn = conn or _conn()
    cols = _cols(conn)
    if not all(c in cols for c in _NEW_COLS):
        logger.warning("screened 에 신규 컬럼이 아직 없음 — US 재분류 스킵 (레플리카 sync 후 재시도)")
        return {"changed": 0, "excluded": 0, "by_reason": {}, "excluded_symbols": set()}
    rows = conn.execute(
        "SELECT symbol, name, price, industry, exchange, adv_value, excluded "
        "FROM screened WHERE market='US'"
    ).fetchall()
    norm = [{"symbol": r[0], "name": r[1] or "", "price": r[2] or 0.0,
             "industry": r[3] or "", "exchange": r[4], "adv_usd": r[5]} for r in rows]
    reasons = classify_us_securities(norm)
    # KR 위생 사유 등 분류기 밖 사유는 건드리지 않음 (US 에선 현재 없음)
    batch: list[str] = []
    for r in rows:
        new = reasons.get(r[0])
        if new != r[6]:
            batch.append(_build_update({"excluded": new}, r[0], "US"))
    _flush_updates(conn, batch)
    out = {"changed": len(batch), "excluded": len(reasons),
           "by_reason": dict(Counter(reasons.values())), "excluded_symbols": set(reasons)}
    logger.info("US 재분류: 변경 %d행, 제외 %d (%s)", out["changed"], out["excluded"], out["by_reason"])
    return out


# ----------------------------------------------------------------------
# 2. 보강 (enrich) — 주식만, key-metrics 종목별
# ----------------------------------------------------------------------


def symbols_needing_enrichment(
    max_age: timedelta = ENRICH_MAX_AGE, market: str = "US"
) -> list[tuple[str, str]]:
    """보강이 필요한 (symbol, market) — 미보강/오래된 주식 (시장 지정 가능, 제외 행 제외).

    기본 US(FMP key-metrics). KR 은 FMP 가 6자리 코드를 모름 → DART 로 별도 보강
    (Phase 9b, enrich_kr 이 market="KR" 로 호출).
    """
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - max_age).isoformat()
    rows = conn.execute(
        f"""
        SELECT symbol, market FROM screened
        WHERE market = ?
          AND (enriched = 0 OR updated_at < ?)
          {_excluded_clause(_cols(conn))}
        ORDER BY market_cap DESC
        """,
        (market, cutoff),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


_FIN_EXEMPT_INDUSTRIES = ("Financial - Data & Stock Exchanges", "Insurance - Brokers")


def _is_balance_sheet_financial(sector: str | None, industry: str | None) -> bool:
    """WP-SCORE 계약 screener.is_balance_sheet_financial 소비 (병합 전엔 같은 규칙으로 폴백)."""
    from src import screener

    fn = getattr(screener, "is_balance_sheet_financial", None)
    if fn is not None:
        return bool(fn(sector or "", industry or ""))
    return (sector or "") == "Financial Services" and (industry or "") not in _FIN_EXEMPT_INDUSTRIES


def _fetch_fundamentals(symbol: str) -> dict:
    """WP-SCORE 계약 latest_fundamentals(ticker, *, basis="ttm") 호출.

    전환기 호환: 병합 전 구 시그니처(basis 없음)면 연간 경로로 폴백 — WP-SCORE 병합 후
    이 폴백은 죽은 코드가 되므로 제거 가능.
    """
    from src.screener import latest_fundamentals

    try:
        return latest_fundamentals(symbol, basis="ttm")
    except TypeError as e:
        if "basis" not in str(e):
            raise
        logger.debug("latest_fundamentals 가 basis 를 모름 — 연간 경로 (전환기)")
        return latest_fundamentals(symbol)


def _scorecards(quote_like: dict, m: dict, is_fin: bool):
    """WP-SCORE 계약 health_scorecard(m, is_fin=)·value_scorecard(q, m, is_fin=) 호출 (전환기 폴백 포함)."""
    from src.screener import health_scorecard, value_scorecard

    try:
        return health_scorecard(m, is_fin=is_fin), value_scorecard(quote_like, m, is_fin=is_fin)
    except TypeError as e:
        if "is_fin" not in str(e):
            raise
        logger.debug("scorecard 가 is_fin 을 모름 — 구 공식 (전환기)")
        return health_scorecard(m), value_scorecard(quote_like, m)


def _finite(v) -> float | None:
    """숫자이고 유한하면 float, 아니면 None (결측과 0 을 구분 — UNI-10)."""
    if v is None or isinstance(v, (str, bool)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pct(v) -> float | None:
    f = _finite(v)
    return f * 100 if f is not None else None


def _compute_enrich(conn, symbol: str, market: str) -> dict | None:
    """한 종목 key-metrics+ratios(TTM 우선) → 점수카드 → UPDATE 할 컬럼 dict. 데이터 없으면 None.

    순수 계산만 (DB 쓰기는 호출부가 배치로 — executescript 왕복 최소화).
    base SELECT 는 발굴 단계 데이터(로컬 레플리카에 이미 sync 됨)라 읽기 안전.
    """
    from src import screener
    from src.screener import has_fundamentals

    m = _fetch_fundamentals(symbol)   # key-metrics + ratios 병합 (TTM, 불가 시 연간)
    # 빈 dict 뿐 아니라 '전부 None/NaN' 도 no_data 처리 — 0점 대신 미보강(NULL) 유지
    # → scan(enriched=1 필터)에서 자연 제외 ('데이터 없음' ≠ '나쁜 점수').
    if not has_fundamentals(m):
        return None

    # 발굴 단계에서 저장한 가격/섹터/배당을 quote 대용으로 사용 (value 점수에 필요).
    # 배당: TTM 의 dividendPerShare 가 있으면 그것(스크리너 lastAnnualDividend 와 일치 확인),
    # 없으면 저장된 dividend_yield(%) 에서 절대액을 역산 (없으면 배당점수 0).
    base = conn.execute(
        "SELECT price, sector, industry, dividend_yield FROM screened WHERE symbol=? AND market=?",
        (symbol, market),
    ).fetchone()
    price, sector, industry, div_yield = base if base else (0.0, "", "", 0.0)
    dps = _finite(m.get("dividendPerShare"))
    last_div = dps if dps is not None else (
        (div_yield / 100.0) * price if (div_yield and price) else 0.0)
    quote_like = {"price": price, "sector": sector, "industry": industry,
                  "lastAnnualDividend": last_div}

    is_fin = _is_balance_sheet_financial(sector, industry)
    health, value = _scorecards(quote_like, m, is_fin)
    total = round((health.total + value.total) / 2)

    flags: list[str] = []
    if is_fin:
        flags.append("fin_neutral")
    ey, fcf, cr = _finite(m.get("earningsYield")), _finite(m.get("freeCashFlowYield")), _finite(m.get("currentRatio"))
    if ey is not None and ey > getattr(screener, "EY_OUTLIER", 0.35):
        flags.append("ey_outlier")
    if fcf is not None and fcf > getattr(screener, "FCF_OUTLIER", 0.40):
        flags.append("fcf_outlier")
    if cr is None or cr <= 0:
        flags.append("cr_missing")
    detail = json.dumps({"health": health.to_dict(), "value": value.to_dict(),
                         "basis": m.get("_basis") or "annual", "flags": flags, "fin": is_fin},
                        ensure_ascii=False)

    return {
        "roe": _pct(m.get("returnOnEquity")),            # 결측은 None (0 과 구분)
        "ev_to_sales": _finite(m.get("evToSales")),
        "fcf_yield": _pct(m.get("freeCashFlowYield")),
        "net_debt_ebitda": _finite(m.get("netDebtToEBITDA")),
        "health_score": health.total,
        "value_score": value.total,
        "total_score": total,
        "detail": detail,
        "enriched": 1,
        "updated_at": _utcnow(),
    }


def _run_enrich_loop(targets, compute, conn, chunk_size, on_progress, stats, label) -> None:
    """enrich/enrich_kr 공통 루프 — 배치 flush + 중단 규칙.

    중단: 연속 RateLimitError ABORT_AFTER_RATE_LIMITS 회, 또는 ApiAuthorizationError/
    ApiAuthError 1회 → stats["aborted"]=True 로 루프 종료 (수천 번 백오프 재시도 낭비 방지).
    성공/no_data/기타 DataFetchError 가 끼면 연속 카운트 리셋.
    """
    total = len(targets)
    batch: list[str] = []
    consecutive_rl = 0
    for i, target in enumerate(targets, 1):
        symbol = target[0] if isinstance(target, tuple) else target
        try:
            stmt = compute(target)
            consecutive_rl = 0
            if stmt is None:
                stats["no_data"] += 1
            else:
                batch.append(stmt)
                stats["enriched"] += 1
        except (ApiAuthorizationError, ApiAuthError) as e:
            stats["failed"] += 1
            stats["aborted"] = True
            logger.error("%s 중단 — 키/플랜 오류 %s: %s", label, symbol, e)
            break
        except RateLimitError as e:
            stats["failed"] += 1
            consecutive_rl += 1
            logger.warning("%s 실패 %s — %s (연속 %d)", label, symbol, e, consecutive_rl)
            if consecutive_rl >= ABORT_AFTER_RATE_LIMITS:
                stats["aborted"] = True
                logger.error("%s 중단 — 호출 한도 연속 %d회 (남은 %d종목은 다음 실행에서)",
                             label, consecutive_rl, total - i)
                break
        except DataFetchError as e:
            consecutive_rl = 0
            logger.warning("%s 실패 %s — %s", label, symbol, e)
            stats["failed"] += 1
        finally:
            if len(batch) >= chunk_size:
                _flush_updates(conn, batch)
                batch.clear()
            if on_progress is not None:
                on_progress(i, total, stats)
            if i % 100 == 0:
                logger.info("%s 진행 %d/%d (%s)", label, i, total, stats)
    _flush_updates(conn, batch)  # 남은 분 flush (중단 시에도)


def enrich(
    max_age: timedelta = ENRICH_MAX_AGE,
    limit: int | None = None,
    chunk_size: int = 200,
    on_progress: "Callable[[int, int, dict], None] | None" = None,
) -> dict:
    """보강 필요한 주식들을 key-metrics 로 채움. 재개 가능.

    시작 시 reclassify_us (오프라인) 로 제외 행을 최신 규칙으로 갱신하고 대상에서 뺀다.
    chunk_size: N종목마다 UPDATE 들을 executescript 한 방으로 flush (Turso 왕복 1회).
      종목별 쓰기는 태평양 왕복이라 매우 느림(~1s/행) → 배치로 왕복 횟수를 N분의 1 로.
      중단 시 마지막 미flush 분만 재작업, 원본은 캐시라 재계산 저렴.
    on_progress(i, total, stats): 매 종목 호출 (CLI 진행바 등). None 이면 미사용.

    Returns {"enriched": n, "no_data": n, "failed": n, "aborted": bool}.
    """
    conn = _conn()
    # 재분류 결과는 레플리카 sync 전 로컬 SELECT 에 안 보일 수 있어 메모리 집합으로도 거른다
    newly_excluded = reclassify_us(conn)["excluded_symbols"]
    targets = [t for t in symbols_needing_enrichment(max_age) if t[0] not in newly_excluded]
    if limit:
        targets = targets[:limit]

    stats = {"enriched": 0, "no_data": 0, "failed": 0, "aborted": False}
    logger.info("보강 시작: %d종목 (chunk_size=%d)", len(targets), chunk_size)

    def compute(target):
        symbol, market = target
        cols = _compute_enrich(conn, symbol, market)
        return _build_update(cols, symbol, market) if cols is not None else None

    _run_enrich_loop(targets, compute, conn, chunk_size, on_progress, stats, "보강")
    logger.info("보강 완료: %s", stats)
    return stats


# ----------------------------------------------------------------------
# 2b. 한국 보강 (DART 펀더멘털) — Phase 9b
# ----------------------------------------------------------------------

ONE_OFF_RATIO = 1.5                          # 순이익 > 1.5×영업이익 → 일회성 의심
KR_FIN_LIAB_RATIO = 0.8                      # 부채/자산 > 0.8 (이름 AND 수치)
_KR_FIN_NAME = re.compile(r"증권|생명|손해보험|해상|화재|보험|은행|뱅크|금융|카드|캐피탈|저축")
_KR_HOLDING_NAME = re.compile(r"홀딩스|지주|Holdings", re.I)


def normalize_net_income(fin: dict) -> tuple[float | None, str | None]:
    """일회성 이익 가드 → (평가용 순이익, flag). flag: None | 'one_off'.

    의심 = ni > 0 이고 op 가 있으며 (op ≤ 0 또는 ni > 1.5×op). 의심이면
      · 3개년(ni, ni_prev1, ni_prev2) 모두 있으면 min(ni, 3년 평균)
      · 아니면 op > 0 일 때 min(ni, op), op ≤ 0 이면 None (PER·이익수익률·흑자 보너스 0)
    영업이익이 없는 금융사(op None)는 비적용. 임계·방식은 표본 내 휴리스틱(스펙 §3.3).
    """
    ni, op = fin.get("net_income"), fin.get("op_income")
    if ni is None or ni <= 0 or op is None:
        return ni, None
    if op > 0 and ni <= ONE_OFF_RATIO * op:
        return ni, None
    hist = [v for v in (ni, fin.get("ni_prev1"), fin.get("ni_prev2")) if v is not None]
    if len(hist) == 3:
        return min(ni, sum(hist) / 3.0), "one_off"
    if op > 0:
        return min(ni, op), "one_off"
    return None, "one_off"


def kr_is_financial(name: str | None, debt: float | None, assets: float | None) -> bool:
    """KR 금융업 판정 — 이름 패턴 **AND** 부채/자산 > 0.8 (숫자만 쓰면 항공사 오분류)."""
    if not name or not _KR_FIN_NAME.search(name):
        return False
    if debt is None or assets is None or assets <= 0:
        return False
    return debt / assets > KR_FIN_LIAB_RATIO


def kr_is_holding(name: str | None) -> bool:
    """KR 지주사 판정 — 이름에 홀딩스/지주/Holdings (금융지주는 금융으로 먼저 판정해 제외).

    이름에 없는 지주사(대웅·한화·CJ·LG·SK 등)는 못 잡음 — 수치 규칙은 오탐이 많아 미채택.
    """
    if not name or _KR_FIN_NAME.search(name):
        return False
    return bool(_KR_HOLDING_NAME.search(name))


def dart_target_year(today: date | None = None) -> int:
    """DART 사업보고서 기준연도 — 3월 말 제출이므로 4월 이전엔 2년 전 (UNI-11)."""
    today = today or datetime.now(timezone.utc).date()
    return today.year - 1 if today.month >= 4 else today.year - 2


def _kr_pbr_points(pbr: float | None) -> float:
    """KR PBR → 저평가 점수 (0~35, 단조 감소).

    구곡선 `35 − pbr×17.5` 는 PBR 2.0 에서 이미 0점 — 한국 중대형주(PBR 1~3 흔함)에
    과도하게 가혹했음 → 완화 (2026-06 코드리뷰 backlog).

    브레이크포인트:
      · PBR ≤ 0.5  → 35점 (만점 — 청산가치 이하 딥밸류)
      · 0.5 ~ 4.5  → 선형 감소: 35 × (4.5 − pbr) / 4.0
                     (PBR 1.0 ≈ 30.6, 2.0 ≈ 21.9, 3.0 ≈ 13.1)
      · PBR ≥ 4.5  → 0점
      · None/0/음수 → 0점 (자본잠식 — '낮을수록 좋음' 공식이 음수에서 뒤집히는
                     역설 방지, CLAUDE.md §4.10 #5)
    """
    if not pbr or pbr <= 0:
        return 0.0
    if pbr <= 0.5:
        return 35.0
    return clip(35.0 * (4.5 - pbr) / 4.0, 0.0, 35.0)


def calculate_kr_scores(fin: dict, market_cap: float, *, name: str = "") -> dict:
    """DART 재무제표 + KRX 시총 → 한국 종목 점수 (순수 함수).

    health(건전성): ROE + 부채비율 + 영업이익률 + 흑자 보너스
    value(저평가): PER 낮을수록 + PBR 낮을수록 + 이익수익률(1/PER)
    임계·가중치는 튜닝 지점 (미국 screener.calculate_* 와 철학 동일, 입력만 다름).

    fin 은 fetch_dart_financials 반환(지배주주 귀속 net_income/equity + equity_total 등).
      · PER/ROE 는 일회성 가드(normalize_net_income)를 거친 순이익, PBR 은 귀속 자본
      · 부채비율은 총자본(equity_total) 분모 — 귀속자본으로 나누면 지주사가 부풀려짐
      · 구형 dict(equity_total 없음)는 equity 로 폴백 (하위호환)
      · 금융(kr_is_financial)이면 부채비율·영업이익률을 만점의 50% 로 중립 대치
    반환 "flags": one_off / fin_neutral / holding / attrib_total.
    """
    eq, debt, assets = fin.get("equity"), fin.get("debt"), fin.get("assets")
    eq_total = fin.get("equity_total")
    if eq_total is None:
        eq_total = eq
    rev, op = fin.get("revenue"), fin.get("op_income")
    ni, one_off = normalize_net_income(fin)

    flags: list[str] = []
    if one_off:
        flags.append("one_off")
    is_fin = kr_is_financial(name, debt, assets)
    if is_fin:
        flags.append("fin_neutral")
    if kr_is_holding(name):
        flags.append("holding")
    if fin.get("attrib") == "total":
        flags.append("attrib_total")

    # 자본잠식(eq ≤ 0) 가드 — 음수NI/음수EQ 는 '큰 양수 ROE', 부채/음수EQ 는
    # '음수 부채비율(만점 클립)' 이 되어 최악의 재무제표가 만점을 받는 부호
    # 함정 (§4.10 #5). eq > 0 일 때만 산출, 아니면 None → 0점·"—" 경로.
    pos_eq = eq is not None and eq > 0
    pos_eq_total = eq_total is not None and eq_total > 0
    roe = (ni / eq * 100) if (ni is not None and pos_eq) else None
    per = (market_cap / ni) if (ni and ni > 0) else None
    pbr = (market_cap / eq) if pos_eq else None
    debt_ratio = (debt / eq_total * 100) if (debt is not None and pos_eq_total) else None
    op_margin = (op / rev * 100) if (op is not None and rev) else None
    profit = ni is not None and ni > 0

    from src.screener import Component, ScoreCard

    if is_fin:
        debt_comp = Component("부채비율", 15.0, 30, "금융중립")
        opm_comp = Component("영업이익률", 10.0, 20, "금융중립")
    else:
        debt_comp = Component("부채비율", clip(30.0 - (debt_ratio or 999) / 10, 0, 30), 30,
                              f"{debt_ratio:.0f}%" if debt_ratio is not None else "—")
        opm_comp = Component("영업이익률", clip((op_margin or 0) * 2, 0, 20), 20,
                             f"{op_margin:.1f}%" if op_margin is not None else "—")
    h = [
        Component("ROE", clip((roe or 0) * 2.5, 0, 35), 35,
                  f"{roe:.1f}%" if roe is not None else "—"),
        debt_comp,
        opm_comp,
        Component("흑자", 15.0 if profit else 0.0, 15, "흑자" if profit else "적자"),
    ]
    per_detail = f"{per:.1f}" if per else "—"
    if one_off:
        has_hist = fin.get("ni_prev1") is not None and fin.get("ni_prev2") is not None
        per_detail += " (일회성 의심→3년평균)" if has_hist else " (일회성 의심→영업이익 상한)"
    v = [
        Component("PER", clip(40.0 - per * 1.5, 0, 40) if (per and per > 0) else 0, 40, per_detail),
        Component("이익수익률", clip((1.0 / per) * 100 * 2.5, 0, 25) if (per and per > 0) else 0, 25,
                  f"{100/per:.1f}%" if (per and per > 0) else "—"),
        Component("PBR", _kr_pbr_points(pbr), 35,
                  f"{pbr:.2f}" if pbr else "—"),
    ]
    health_card = ScoreCard(round(min(100.0, sum(c.points for c in h))), h)
    value_card = ScoreCard(round(min(100.0, sum(c.points for c in v))), v)
    return {
        "health_score": health_card.total, "value_score": value_card.total,
        "total_score": round((health_card.total + value_card.total) / 2),
        "roe": roe, "per": per, "pbr": pbr, "flags": flags,
        "detail": {"health": health_card.to_dict(), "value": value_card.to_dict(),
                   "flags": flags},
    }


def _kr_symbols_needing_enrichment(max_age: timedelta) -> list[str]:
    """KR 보강 대상 심볼 — symbols_needing_enrichment(market="KR") 재사용."""
    return [s for s, _m in symbols_needing_enrichment(max_age, market="KR")]


def _kr_update_cols(sc: dict, fin: dict, year: int | None) -> dict:
    """calculate_kr_scores 결과 → UPDATE 컬럼 dict. detail 에 raw(재무 원본) 저장 →
    이후 시총만 바뀌면 DART 재호출 없이 rescore_kr_offline 으로 재계산 가능."""
    detail = dict(sc["detail"])
    detail["raw"] = fin
    if year is not None:
        detail["basis"] = f"FY{year}"
    return {"roe": sc["roe"], "per": sc["per"], "pbr": sc["pbr"],
            "health_score": sc["health_score"], "value_score": sc["value_score"],
            "total_score": sc["total_score"],
            "detail": json.dumps(detail, ensure_ascii=False),
            "enriched": 1, "updated_at": _utcnow()}


def enrich_kr(
    year: int | None = None,
    max_age: timedelta = ENRICH_MAX_AGE,
    limit: int | None = None,
    chunk_size: int = 200,
    on_progress: "Callable[[int, int, dict], None] | None" = None,
) -> dict:
    """한국 종목 DART 펀더멘털 보강 → ROE/PER/PBR 점수. 재개 가능.

    year=None 이면 dart_target_year() (4월 이전엔 2년 전 사업보고서).
    chunk_size: N종목마다 executescript 한 방으로 flush (Turso 왕복 최소화 — enrich() 참고).
    중단 규칙은 enrich() 와 동일 (stats["aborted"]).
    """
    from src.data_fetcher import fetch_dart_corp_codes, fetch_dart_financials

    conn = _conn()
    year = year or dart_target_year()
    stats = {"enriched": 0, "no_data": 0, "failed": 0, "aborted": False}
    try:
        corp_map = fetch_dart_corp_codes()
    except DataFetchError as e:
        logger.warning("DART corpCode 실패 — KR 보강 중단: %s", e)
        return stats

    targets = _kr_symbols_needing_enrichment(max_age)
    if limit:
        targets = targets[:limit]
    logger.info("KR 보강 시작: %d종목 (DART %d년, chunk_size=%d)", len(targets), year, chunk_size)

    def compute(symbol):
        corp_code = corp_map.get(symbol)
        if not corp_code:
            return None
        fin = fetch_dart_financials(corp_code, year)
        base = conn.execute(
            "SELECT market_cap, name FROM screened WHERE symbol=? AND market='KR'", (symbol,)
        ).fetchone()
        if not (fin and fin.get("equity") and base and base[0]):
            return None
        sc = calculate_kr_scores(fin, float(base[0]), name=base[1] or "")
        return _build_update(_kr_update_cols(sc, fin, year), symbol, "KR")

    _run_enrich_loop(targets, compute, conn, chunk_size, on_progress, stats, "KR 보강")
    logger.info("KR 보강 완료: %s", stats)
    return stats


def rescore_kr_offline(market_caps: dict[str, float] | None = None) -> dict[str, int]:
    """KR 행의 detail.raw(재무 원본) + 시총으로 점수 재계산 — DART 호출 0.

    market_caps={코드: 시총} 이 주어지면 그 값을 쓴다 (방금 발굴한 시총 — 레플리카는
    자기 쓰기를 sync 전 못 읽으므로 메모리로 전달). 없으면 DB 의 market_cap.
    raw 가 없는 구형 행은 건너뜀. Returns {"rescored": n, "skipped": n}.
    """
    conn = _conn()
    cols = _cols(conn)
    rows = conn.execute(
        f"""SELECT symbol, name, market_cap, detail FROM screened
            WHERE market='KR' AND enriched=1 AND detail IS NOT NULL
            {_excluded_clause(cols)}"""
    ).fetchall()
    out = {"rescored": 0, "skipped": 0}
    batch: list[str] = []
    for symbol, name, mcap, detail in rows:
        try:
            raw = (json.loads(detail) or {}).get("raw")
        except (json.JSONDecodeError, TypeError):
            raw = None
        if market_caps is not None and symbol in market_caps:
            mcap = market_caps[symbol]
        if not raw or not raw.get("equity") or not mcap:
            out["skipped"] += 1
            continue
        sc = calculate_kr_scores(raw, float(mcap), name=name or "")
        cols_upd = _kr_update_cols(sc, raw, None)
        cols_upd.pop("updated_at")        # 재무 신선도 마커는 유지 (시총만 바뀜)
        cols_upd.pop("enriched")
        batch.append(_build_update(cols_upd, symbol, "KR"))
        out["rescored"] += 1
        if len(batch) >= DISCOVER_CHUNK:
            _flush_updates(conn, batch)
            batch.clear()
    _flush_updates(conn, batch)
    logger.info("KR 오프라인 재점수: %s", out)
    return out


def refresh_kr_prices() -> dict[str, int]:
    """KR 일간 갱신 — KRX 일별(2콜, 12h 캐시)로 가격·시총 갱신 + 오프라인 재점수 (DART 0콜)."""
    conn = _conn()
    n, mcaps = _discover_kr_rows(conn)
    conn.commit()
    out = {"discovered": n}
    out.update(rescore_kr_offline(mcaps) if mcaps else {"rescored": 0, "skipped": 0})
    return out


# ----------------------------------------------------------------------
# 3. 스캔 (scan) — 오프라인, API 0콜
# ----------------------------------------------------------------------

_SCAN_BASE_COLS = ("symbol, market, name, sector, price, market_cap, total_score, "
                   "value_score, health_score, roe, per, pbr")


def _scan_select(cols: set[str]) -> str:
    """ScanRow 용 SELECT 절 — 신규 컬럼은 존재할 때만 참조 (stale 레플리카 가드)."""
    ex = "exchange" if "exchange" in cols else "NULL"
    exc = "excluded" if "excluded" in cols else "NULL"
    seen = "seen_at" if "seen_at" in cols else "NULL"
    return f"SELECT {_SCAN_BASE_COLS}, {ex}, {exc}, detail, updated_at, {seen} FROM screened"


def _flags_from_detail(detail) -> tuple[str, ...]:
    if not detail:
        return ()
    try:
        flags = (json.loads(detail) or {}).get("flags") or ()
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ()
    return tuple(str(f) for f in flags)


def _row_to_scan(r) -> ScanRow:
    base, exchange, excluded, detail, updated_at, seen_at = r[:12], r[12], r[13], r[14], r[15], r[16]
    return ScanRow(*base, exchange=exchange, excluded=excluded,
                   flags=_flags_from_detail(detail),
                   fund_asof=_date_part(updated_at), price_asof=_date_part(seen_at))


def scan(
    market: str | None = None,
    limit: int = 50,
    min_total: int = 0,
    sector: str | None = None,
) -> list[ScanRow]:
    """점수 내림차순 전수 스캔 (보강 완료 + 미제외 종목만). 저평가 상위 발굴용."""
    conn = _conn()
    cols = _cols(conn)
    sql = [_scan_select(cols), "WHERE enriched=1 AND total_score >= ?", _excluded_clause(cols)]
    args: list = [min_total]
    if market:
        sql.append("AND market = ?"); args.append(market)
    if sector:
        sql.append("AND sector = ?"); args.append(sector)
    sql.append("ORDER BY total_score DESC, market_cap DESC LIMIT ?"); args.append(limit)

    # libsql(Turso)은 파라미터를 tuple 로만 받음 (sqlite3 는 list 도 허용) → tuple 강제
    rows = conn.execute(" ".join(sql), tuple(args)).fetchall()
    return [_row_to_scan(r) for r in rows]


def lookup(symbol: str) -> ScanRow | None:
    """특정 종목의 점수·순위 조회 (내 종목이 저평가인지 확인용).

    같은 심볼이 여러 시장에 있으면(주식 vs 동명 크립토) 주식(US→KR) 우선.
    **제외 행도 반환** — ScanRow.excluded 에 사유 (예: 'sibling' = 보통주 아님).
    """
    conn = _conn()
    r = conn.execute(
        f"""{_scan_select(_cols(conn))}
           WHERE symbol = ? AND enriched = 1
           ORDER BY CASE market WHEN 'US' THEN 0 WHEN 'KR' THEN 1 ELSE 2 END
           LIMIT 1""",
        (symbol.upper(),),
    ).fetchone()
    return _row_to_scan(r) if r else None


def lookup_detail(symbol: str) -> dict | None:
    """종목의 점수 분해 JSON (--check / 텔레그램 /stock 용). 없으면 None.

    반환 형태: {"health": {total, components:[[label,pts,max,detail],...]},
                "value": {...}, (신형) "basis", "flags", "fin"/"raw"}
    """
    conn = _conn()
    r = conn.execute(
        "SELECT detail FROM screened WHERE symbol=? AND enriched=1 AND detail IS NOT NULL "
        "ORDER BY CASE market WHEN 'US' THEN 0 WHEN 'KR' THEN 1 ELSE 2 END LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    if not r or not r[0]:
        return None
    try:
        return json.loads(r[0])
    except (json.JSONDecodeError, TypeError):
        return None


def _is_holding_row(row: ScanRow) -> bool:
    return "holding" in row.flags or kr_is_holding(row.name)


def pick_top(
    market: str = "US",
    n: int = 6,
    *,
    max_per_sector: int | None = 2,
    max_holding: int | None = 2,
    min_total: int = 0,
) -> list[ScanRow]:
    """최종 선정 — scan 상위에서 그리디로 n개 (다이제스트 발굴 섹션용).

    · US(및 KR 외 시장): 같은 sector 최대 max_per_sector (에너지 쏠림 방지). sector 가 빈
      행(크립토)은 상한 미적용.
    · KR: 지주사(flags 'holding' 또는 이름 규칙) 최대 max_holding. KR sector 는 KRX 소속부라
      섹터 상한 미적용.
    상한 때문에 건너뛴 자리는 다음 순위가 채움. None 이면 해당 상한 끔. DB 가 비면 [].
    """
    rows = scan(market=market, limit=max(n * 12, n), min_total=min_total)
    picked: list[ScanRow] = []
    per_sector: Counter = Counter()
    holdings = 0
    for r in rows:
        if len(picked) >= n:
            break
        if market == "KR":
            is_h = _is_holding_row(r)
            if is_h and max_holding is not None and holdings >= max_holding:
                continue
            holdings += int(is_h)
        elif max_per_sector is not None and r.sector:
            if per_sector[r.sector] >= max_per_sector:
                continue
            per_sector[r.sector] += 1
        picked.append(r)
    return picked


def top_symbols(n: int = 6, market: str = "US") -> list[str]:
    """pick_top 의 심볼 리스트 래퍼 (다이제스트 발굴 종목 연동용). DB 가 비면 빈 리스트."""
    return [row.symbol for row in pick_top(market, n)]


def freshness(market: str = "US") -> dict:
    """시장별 데이터 기준일 — {"fund_asof", "price_asof", "age_days"} (다이제스트 노후 경고용).

    범위: enriched=1 AND excluded IS NULL. fund_asof=max(updated_at) 날짜(재무 보강),
    price_asof=max(seen_at) 날짜(가격/시총; KR 은 KRX 기준일). age_days 는 둘 중 **더 오래된**
    기준일로부터 오늘(UTC)까지 일수 — 둘 다 없으면 None.
    """
    conn = _conn()
    cols = _cols(conn)
    seen = "MAX(seen_at)" if "seen_at" in cols else "NULL"
    r = conn.execute(
        f"SELECT MAX(updated_at), {seen} FROM screened "
        f"WHERE market=? AND enriched=1 {_excluded_clause(cols)}",
        (market,),
    ).fetchone()
    fund_asof, price_asof = _date_part(r[0] if r else None), _date_part(r[1] if r else None)
    ages = []
    today = datetime.now(timezone.utc).date()
    for d in (fund_asof, price_asof):
        try:
            ages.append((today - date.fromisoformat(d)).days if d else None)
        except ValueError:
            ages.append(None)
    valid = [a for a in ages if a is not None]
    return {"fund_asof": fund_asof, "price_asof": price_asof,
            "age_days": max(valid) if valid else None}


def yf_symbol(symbol: str, market: str, exchange: str | None = None, sector: str = "") -> str:
    """유니버스 심볼 → yfinance 심볼.

    KR: exchange=="KOSDAQ" → .KQ, "KOSPI" → .KS; exchange 없으면 KRX 소속부 규칙
    (sector 비어있음 ⇔ KOSPI, 507/507 일치 검증). 알파뉴메릭 코드(0126Z0)도 그대로.
    US: 그대로(BRK-B). CRYPTO: SYM-USD.
    """
    if market == "KR":
        if exchange:
            return f"{symbol}.KQ" if exchange.upper() == "KOSDAQ" else f"{symbol}.KS"
        return f"{symbol}.KQ" if sector else f"{symbol}.KS"
    if market == "CRYPTO":
        return f"{symbol}-USD"
    return symbol


def yf_symbol_alternates(symbol: str, market: str, exchange: str | None = None,
                         sector: str = "") -> list[str]:
    """yf_symbol 실패 시 재시도할 대안 (KR 은 반대 접미사 1개, 그 외 없음)."""
    if market != "KR":
        return []
    primary = yf_symbol(symbol, market, exchange, sector)
    return [f"{symbol}.KS" if primary.endswith(".KQ") else f"{symbol}.KQ"]


def stats() -> dict[str, int]:
    """시장별 발굴/보강/제외 현황."""
    conn = _conn()
    cols = _cols(conn)
    excl = ("SUM(CASE WHEN excluded IS NOT NULL THEN 1 ELSE 0 END)"
            if "excluded" in cols else "0")
    out: dict[str, int] = {}
    for mkt, total, enr, exc in conn.execute(
        f"SELECT market, COUNT(*), SUM(enriched), {excl} FROM screened GROUP BY market"
    ).fetchall():
        out[f"{mkt}_total"] = total
        out[f"{mkt}_enriched"] = enr or 0
        out[f"{mkt}_excluded"] = exc or 0
    return out
