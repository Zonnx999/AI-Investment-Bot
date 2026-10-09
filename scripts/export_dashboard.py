"""
scripts/export_dashboard.py
===========================
Phase 12 — Turso DB → dashboard/*.json 내보내기.

매일 cron 이 digest 전송 후 이 스크립트를 실행해 GitHub Pages 가 서빙할
정적 JSON 을 갱신. 생성 파일:
  · dashboard/kr_data.json          — KR 종목 (enriched 완료 종목만)
  · dashboard/us_data.json          — US 종목
  · dashboard/crypto_data.json      — CRYPTO 종목
  · dashboard/regime_data.json      — 시장 국면 (macro_analyzer)
  · dashboard/predictions_data.json — 선행지표 예측 (predictors)

실행:
    source .venv/bin/activate
    python scripts/export_dashboard.py

종료코드 (GitHub Actions 가 실패를 드러내도록 — 낡은/빈 데이터로 공개 파일을 덮어쓰지 않음):
    0  성공 (국면·예측 실패는 ::warning:: 만)
    1  DB 연결 불가 (StorageError)
    2  Turso 설정됐는데 초기 동기화가 ok 아님 (강등/stale) — 파일 미기록
    3  US 또는 KR 보강 종목 0행 / screened 읽기 실패 — 파일 미기록
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.exceptions import InsufficientDataError, QuantBotError
from src.findings import Finding, from_prediction, from_regime
from src.logger import get_logger
from src.storage import DB_ERRORS, StorageError, get_storage

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
KST = ZoneInfo("Asia/Seoul")

# screened 테이블 컬럼 목록 (SELECT 절 순서와 _row_to_* 인덱스가 1:1 대응).
# 뒤에 붙는 선택 컬럼 3개(updated_at, seen_at, excluded)는 _fetch_market_rows 가 존재 여부를 확인해
# 붙이고(없으면 NULL), converter 는 길이가 짧은 구형 튜플도 받는다 (기준일/제외 사유 — CMAP-03/06).
_US_COLS = (
    "symbol, name, sector, price, market_cap, "
    "dividend_yield, roe, health_score, value_score, total_score"
)
_KR_COLS = (
    "symbol, name, sector, price, market_cap, "
    "dividend_yield, roe, per, pbr, health_score, value_score, total_score"
)
_CRYPTO_COLS = (
    "symbol, name, price, market_cap, health_score, value_score, total_score"
)


# ----------------------------------------------------------------------
# 순수 변환 헬퍼 (오프라인 테스트 가능)
# ----------------------------------------------------------------------


def clean_float(v) -> float | None:
    """NaN / inf → None (JSON null). 정상 float 은 그대로."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if not math.isfinite(f) else round(f, 6)
    except (TypeError, ValueError):
        return None


def _date_part(ts) -> str | None:
    """ISO 타임스탬프/날짜 → 'YYYY-MM-DD' (없으면 None)."""
    if not ts:
        return None
    t = str(ts)
    return t[:10] if len(t) >= 10 else None


def _asof_fields(extra: tuple) -> dict:
    """행 끝의 (updated_at, seen_at, excluded) → 기준일/제외 사유 필드.

    fund_asof = 재무 보강 기준일, price_asof = 가격/시총 기준일, excluded = 제외 사유(None=정상).
    """
    updated_at, seen_at, excluded = (list(extra) + [None] * 3)[:3]
    return {
        "fund_asof": _date_part(updated_at),
        "price_asof": _date_part(seen_at),
        "excluded": excluded or None,
    }


def row_to_us(row: tuple) -> dict:
    """US screened 행(tuple) → JSON dict."""
    symbol, name, sector, price, mcap, div_yield, roe, hs, vs, ts = row[:10]
    return {
        "symbol": symbol or "",
        "name": name or "",
        "sector": sector or "",
        "price": clean_float(price),
        "market_cap": clean_float(mcap),
        "dividend_yield": clean_float(div_yield),
        "roe": clean_float(roe),
        "health_score": int(hs or 0),
        "value_score": int(vs or 0),
        "total_score": int(ts or 0),
        **_asof_fields(row[10:]),
    }


def row_to_kr(row: tuple) -> dict:
    """KR screened 행(tuple) → JSON dict."""
    symbol, name, sector, price, mcap, div_yield, roe, per, pbr, hs, vs, ts = row[:12]
    return {
        "symbol": symbol or "",
        "name": name or "",
        "sector": sector or "",
        "price": clean_float(price),
        "market_cap": clean_float(mcap),
        "dividend_yield": clean_float(div_yield),
        "roe": clean_float(roe),
        "per": clean_float(per),
        "pbr": clean_float(pbr),
        "health_score": int(hs or 0),
        "value_score": int(vs or 0),
        "total_score": int(ts or 0),
        **_asof_fields(row[12:]),
    }


def row_to_crypto(row: tuple) -> dict:
    """CRYPTO screened 행(tuple) → JSON dict."""
    symbol, name, price, mcap, hs, vs, ts = row[:7]
    return {
        "symbol": symbol or "",
        "name": name or "",
        "price": clean_float(price),
        "market_cap": clean_float(mcap),
        "rank_score": int(hs or 0),    # health_score = rank_score (발굴 단계 매핑)
        "volatility_score": int(vs or 0),
        "total_score": int(ts or 0),
        **_asof_fields(row[7:]),
    }


def finding_to_json(finding: Finding) -> dict:
    """Finding.to_dict() + score NaN/inf 정리 (JSON null 안전)."""
    d = finding.to_dict()
    d["score"] = clean_float(d["score"])
    return d


def regime_to_dict(summary: dict) -> dict:
    """market_summary() 반환값 → JSON 직렬화 가능 dict.

    국면 부분은 다이제스트와 같은 Finding shape(13a)를 거쳐 조립 —
    label/score/signals 는 Finding 필드에서, 표 형태인 panel/correlations 와
    진단 정보 failures 는 summary/RegimeReport 에서 직접. 기존 키는 전부
    유지(대시보드 JS 호환), `finding` 키만 추가.
    """
    regime = summary["regime"]
    finding = from_regime(regime)
    panel = []
    for name in summary["cumulative_returns_pct"].index:
        panel.append({
            "name": name,
            "return_6m": clean_float(summary["cumulative_returns_pct"].get(name)),
            "vol": clean_float(summary["annualized_vol_pct"].get(name)),
            "sharpe": clean_float(summary["sharpe_ratio"].get(name)),
            "drawdown": clean_float(summary["current_drawdown_pct"].get(name)),
        })

    corr = summary["correlation"]
    correlations: dict[str, dict[str, float | None]] = {}
    for col in corr.columns:
        correlations[col] = {
            row: clean_float(corr.loc[row, col])
            for row in corr.index
            if row != col
        }

    available = getattr(regime, "available", None)
    total = getattr(regime, "total", None)
    # 지표가 하나라도 빠지면 라벨이 '판정 보류' — 대시보드는 점수를 숨기고 보류로 표시
    held = bool(total) and available is not None and available < total
    return {
        "label": finding.title,                 # = regime.regime
        "score": regime.score,                  # int 유지 (finding.score 는 float)
        "signals": list(finding.evidence),      # = regime.signals
        "failures": regime.failures,
        "available": available,                 # 평가 성공 지표 수 / 전체 (추가 키)
        "total": total,
        "held": held,
        "panel": panel,
        "correlations": correlations,
        "finding": finding_to_json(finding),    # 13a 공통 shape (추가 키)
    }


def prediction_to_dict(name: str, result) -> dict:
    """LeadLagResult → JSON dict.

    다이제스트와 같은 Finding shape(13a)를 거쳐 조립 — name/r²/notes 는
    Finding 필드에서, 회귀 세부(leading/lag/방향 등)는 결과 객체에서 직접.
    기존 키는 전부 유지(대시보드 JS 호환), `finding` 키만 추가.
    """
    finding = from_prediction(result, name=name)
    return {
        "name": finding.title,                             # = name
        "leading": result.leading_name,
        "target": result.target_name,
        "best_lag_months": result.best_lag_months,
        "correlation": clean_float(result.correlation),
        "r_squared": clean_float(finding.score),           # = result.r_squared
        "direction": result.direction,
        "predicted_change_pct": clean_float(result.predicted_change_pct),
        "reliable": result.reliable,
        "n_obs": result.n_obs,
        "notes": list(finding.evidence),                   # = result.notes
        "finding": finding_to_json(finding),               # 13a 공통 shape (추가 키)
    }


# ----------------------------------------------------------------------
# DB 조회
# ----------------------------------------------------------------------


def _fetch_market_rows(conn: sqlite3.Connection, market: str) -> list[dict]:
    """시장별 enriched 종목 전체를 점수 내림차순으로 조회."""
    if market == "US":
        cols, converter = _US_COLS, row_to_us
    elif market == "KR":
        cols, converter = _KR_COLS, row_to_kr
    elif market == "CRYPTO":
        cols, converter = _CRYPTO_COLS, row_to_crypto
    else:
        raise ValueError(f"알 수 없는 시장: {market!r}")

    # 기준일/제외 사유 컬럼 — stale 레플리카·구스키마엔 없을 수 있어 존재할 때만 참조 (§4.10 #9)
    have = {r[1] for r in conn.execute("PRAGMA table_info(screened)").fetchall()}
    extra = ", ".join(c if c in have else "NULL" for c in ("updated_at", "seen_at", "excluded"))
    # 동점은 시총 내림차순 (universe.scan 과 같은 순서 — CMAP-10)
    rows = conn.execute(
        f"SELECT {cols}, {extra} FROM screened WHERE market=? AND enriched=1 "
        f"ORDER BY total_score DESC, market_cap DESC",
        (market,),
    ).fetchall()
    return [converter(r) for r in rows]


# ----------------------------------------------------------------------
# JSON 쓰기
# ----------------------------------------------------------------------


def _write_json(path: Path, obj: dict) -> int:
    """obj 를 JSON 파일로 저장. 바이트 수 반환."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode())


# ----------------------------------------------------------------------
# 메인 내보내기 단계
# ----------------------------------------------------------------------


def _data_as_of(conn: sqlite3.Connection, market: str) -> str | None:
    """시장별 데이터 기준 시각 = 재무 보강 시점 max(updated_at) (UTC ISO). 없으면 None.

    generated_at(내보낸 시각)과 별개 — 대시보드가 '며칠 묵은 데이터인지' 표시할 근거.
    """
    row = conn.execute(
        "SELECT max(updated_at) FROM screened WHERE market=? AND enriched=1",
        (market,),
    ).fetchone()
    return row[0] if row and row[0] else None


_MARKETS = ("KR", "US", "CRYPTO")


def export_screener(
    conn: sqlite3.Connection,
    prefetched: dict[str, tuple[list[dict], str | None]] | None = None,
) -> dict[str, int]:
    """KR / US / CRYPTO 종목을 각각 JSON 으로 저장. {market: 종목수} 반환.

    prefetched: main() 이 먼저 읽고 검증한 {market: (items, data_as_of)} — 주면 DB 를
    다시 읽지 않고 그 시장들만 씀 (검증 통과 전엔 아무 파일도 안 건드리기 위함).
    """
    now_str = datetime.now(KST).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    if prefetched is None:
        prefetched = {m: (_fetch_market_rows(conn, m), _data_as_of(conn, m)) for m in _MARKETS}

    for market, (items, as_of) in prefetched.items():
        counts[market] = len(items)

        fname = {"KR": "kr_data.json", "US": "us_data.json", "CRYPTO": "crypto_data.json"}[market]
        # price_as_of: 가격/시총 기준일 중 최신 (행별 price_asof 에서 도출 — 없으면 None)
        price_dates = [i["price_asof"] for i in items if i.get("price_asof")]
        payload = {"generated_at": now_str, "data_as_of": as_of,
                   "price_as_of": max(price_dates) if price_dates else None,
                   "excluded_count": sum(1 for i in items if i.get("excluded")),
                   # count 는 하위호환(제외 행 포함 전체 행 수) 유지, eligible_count = 후보 대상(excluded 없는 행) 수
                   "eligible_count": sum(1 for i in items if not i.get("excluded")),
                   "count": len(items), "items": items}
        size = _write_json(DASHBOARD_DIR / fname, payload)
        logger.info("%s 내보내기 완료: %d종목 → %s (%.1fKB)", market, len(items), fname, size / 1024)

    return counts


def export_regime() -> bool:
    """market_summary() → regime_data.json. 실패 시 False 반환."""
    from src.macro_analyzer import market_summary

    now_str = datetime.now(KST).isoformat(timespec="seconds")
    try:
        summary = market_summary(period="6mo")
    except QuantBotError as e:
        logger.warning("시장 국면 조회 실패 — regime_data.json 에 빈 결과 기록: %s", e)
        payload = {"generated_at": now_str, "error": str(e), "label": None, "panel": [], "correlations": {}}
        _write_json(DASHBOARD_DIR / "regime_data.json", payload)
        return False

    try:
        data = regime_to_dict(summary)
    except Exception as e:  # noqa: BLE001 — pandas 직렬화 에러 최후 방어
        logger.warning("시장 국면 직렬화 실패: %s", e, exc_info=True)
        payload = {"generated_at": now_str, "error": str(e), "label": None, "panel": [], "correlations": {}}
        _write_json(DASHBOARD_DIR / "regime_data.json", payload)
        return False

    payload = {"generated_at": now_str, **data}
    size = _write_json(DASHBOARD_DIR / "regime_data.json", payload)
    logger.info("시장 국면 내보내기 완료 → regime_data.json (%.1fKB)", size / 1024)
    return True


def export_predictions() -> int:
    """PREDICTORS 전체 실행 → predictions_data.json. 성공한 관계 수 반환."""
    from src.predictors import PREDICTORS

    now_str = datetime.now(KST).isoformat(timespec="seconds")
    items: list[dict] = []

    for name, fn in PREDICTORS.items():
        try:
            result = fn()
            items.append(prediction_to_dict(name, result))
            logger.debug("예측 완료: %s (R²=%.2f)", name, result.r_squared)
        except InsufficientDataError as e:
            logger.warning("예측 데이터 부족 (%s) — 스킵: %s", name, e)
        except QuantBotError as e:
            logger.warning("예측 실패 (%s) — 스킵: %s", name, e)

    payload = {"generated_at": now_str, "count": len(items), "items": items}
    size = _write_json(DASHBOARD_DIR / "predictions_data.json", payload)
    logger.info("선행지표 예측 내보내기 완료: %d/%d → predictions_data.json (%.1fKB)",
                len(items), len(PREDICTORS), size / 1024)
    return len(items)


# ----------------------------------------------------------------------
# 진입점
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="대시보드 JSON 내보내기")
    parser.add_argument("--skip-regime", action="store_true", help="시장 국면 스킵 (FRED 키 없을 때)")
    parser.add_argument("--skip-predictions", action="store_true", help="선행지표 예측 스킵")
    args = parser.parse_args()

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    # --- DB 연결 ---
    try:
        storage = get_storage()
    except StorageError as e:
        logger.error("DB 연결 실패 — 내보내기 중단: %s", e)
        print(f"::error::DB 연결 실패 — 내보내기 중단 ({type(e).__name__})")
        return 1

    # --- 원격 동기화 확인: 강등(오프라인 파일)·stale 레플리카로 공개 파일을 덮어쓰지 않음 ---
    # (`::error::`/`::warning::` 는 GitHub Actions 워크플로 명령 — stdout 으로 내보내야 주석이 됨)
    try:
        storage.require_remote("export_dashboard")
    except StorageError as e:
        logger.error("원격 동기화 미완료 — 내보내기 중단 (파일 미기록): %s", e)
        print(f"::error::Turso 초기 동기화 상태 {getattr(storage, 'init_sync', '?')} — "
              "낡은 데이터로 덮어쓰지 않도록 내보내기 중단")
        return 2

    conn = storage.conn

    # --- 스크리너: 세 시장을 **먼저 전부 읽고** 검증한 뒤에만 쓴다 ---
    try:
        data = {m: (_fetch_market_rows(conn, m), _data_as_of(conn, m)) for m in _MARKETS}
    except DB_ERRORS as e:
        logger.error("screened 읽기 실패 — 내보내기 중단 (파일 미기록): %s", e)
        print("::error::screened 테이블 읽기 실패 — 내보내기 중단")
        return 3
    empty = [m for m in ("US", "KR") if not data[m][0]]
    if empty:
        logger.error("보강 종목 0행 (%s) — 기존 파일 보존, 내보내기 중단", "/".join(empty))
        print(f"::error::{'/'.join(empty)} 보강 종목 0행 — 기존 대시보드 파일을 덮어쓰지 않음")
        return 3
    if not data["CRYPTO"][0]:
        # CRYPTO 는 부가 데이터 — 비었으면 그 파일만 보존하고 나머지는 진행
        logger.warning("CRYPTO 0행 — crypto_data.json 보존")
        print("::warning::CRYPTO 0행 — crypto_data.json 은 갱신하지 않음")
        del data["CRYPTO"]
    counts = export_screener(conn, prefetched=data)

    # --- 시장 국면 ---
    regime_ok = True
    if not args.skip_regime:
        regime_ok = export_regime()
        if not regime_ok:
            print("::warning::시장 국면 내보내기 실패 — regime_data.json 에 오류 기록")

    # --- 선행지표 예측 ---
    from src.predictors import PREDICTORS

    pred_count = 0
    if not args.skip_predictions:
        pred_count = export_predictions()
        if pred_count < len(PREDICTORS):
            print(f"::warning::선행지표 예측 {len(PREDICTORS) - pred_count}개 실패/스킵")

    # --- stdout 요약 (대시보드 deliverable) ---
    total = sum(counts.values())
    print(f"내보내기 완료: US {counts.get('US', 0)} / KR {counts.get('KR', 0)} / "
          f"CRYPTO {counts.get('CRYPTO', 0)}종목 (합계 {total})")
    print("데이터 기준(보강 시점, UTC): "
          + " / ".join(f"{m} {data[m][1] or '없음'}" for m in data))
    print(f"시장 국면: {'OK' if regime_ok else '실패'}")
    print(f"선행지표: {pred_count}/{len(PREDICTORS)}개")
    print(f"출력 폴더: {DASHBOARD_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
