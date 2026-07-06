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
from src.storage import StorageError, get_storage

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
KST = ZoneInfo("Asia/Seoul")

# screened 테이블 컬럼 목록 (SELECT 절 순서와 _row_to_* 인덱스가 1:1 대응)
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


def row_to_us(row: tuple) -> dict:
    """US screened 행(tuple) → JSON dict."""
    symbol, name, sector, price, mcap, div_yield, roe, hs, vs, ts = row
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
    }


def row_to_kr(row: tuple) -> dict:
    """KR screened 행(tuple) → JSON dict."""
    symbol, name, sector, price, mcap, div_yield, roe, per, pbr, hs, vs, ts = row
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
    }


def row_to_crypto(row: tuple) -> dict:
    """CRYPTO screened 행(tuple) → JSON dict."""
    symbol, name, price, mcap, hs, vs, ts = row
    return {
        "symbol": symbol or "",
        "name": name or "",
        "price": clean_float(price),
        "market_cap": clean_float(mcap),
        "rank_score": int(hs or 0),    # health_score = rank_score (발굴 단계 매핑)
        "volatility_score": int(vs or 0),
        "total_score": int(ts or 0),
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

    return {
        "label": finding.title,                 # = regime.regime
        "score": regime.score,                  # int 유지 (finding.score 는 float)
        "signals": list(finding.evidence),      # = regime.signals
        "failures": regime.failures,
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

    rows = conn.execute(
        f"SELECT {cols} FROM screened WHERE market=? AND enriched=1 "
        f"ORDER BY total_score DESC",
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


def export_screener(conn: sqlite3.Connection) -> dict[str, int]:
    """KR / US / CRYPTO 종목을 각각 JSON 으로 저장. {market: 종목수} 반환."""
    now_str = datetime.now(KST).isoformat(timespec="seconds")
    counts: dict[str, int] = {}

    for market in ("KR", "US", "CRYPTO"):
        items = _fetch_market_rows(conn, market)
        counts[market] = len(items)

        fname = {"KR": "kr_data.json", "US": "us_data.json", "CRYPTO": "crypto_data.json"}[market]
        payload = {"generated_at": now_str, "count": len(items), "items": items}
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
        return 1

    conn = storage.conn

    # --- 스크리너 (KR / US / CRYPTO) ---
    counts = export_screener(conn)

    # --- 시장 국면 ---
    regime_ok = True
    if not args.skip_regime:
        regime_ok = export_regime()

    # --- 선행지표 예측 ---
    pred_count = 0
    if not args.skip_predictions:
        pred_count = export_predictions()

    # --- stdout 요약 (대시보드 deliverable) ---
    total = sum(counts.values())
    print(f"내보내기 완료: US {counts.get('US', 0)} / KR {counts.get('KR', 0)} / "
          f"CRYPTO {counts.get('CRYPTO', 0)}종목 (합계 {total})")
    print(f"시장 국면: {'OK' if regime_ok else '실패'}")
    print(f"선행지표: {pred_count}/{len(__import__('src.predictors', fromlist=['PREDICTORS']).PREDICTORS)}개")
    print(f"출력 폴더: {DASHBOARD_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
