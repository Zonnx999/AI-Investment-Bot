"""
tests/test_export_dashboard.py
==============================
Phase 12 — export_dashboard.py 순수 헬퍼 오프라인 테스트.

네트워크 / DB / API 없음. 순수 변환 함수만 검증.
"""

from __future__ import annotations

import pytest

from scripts.export_dashboard import (
    clean_float,
    finding_to_json,
    prediction_to_dict,
    regime_to_dict,
    row_to_crypto,
    row_to_kr,
    row_to_us,
)


# ----------------------------------------------------------------------
# clean_float
# ----------------------------------------------------------------------


def test_clean_float_none():
    assert clean_float(None) is None


def test_clean_float_nan():
    assert clean_float(float("nan")) is None


def test_clean_float_inf():
    assert clean_float(float("inf")) is None
    assert clean_float(float("-inf")) is None


def test_clean_float_normal():
    assert clean_float(1.5) == 1.5


def test_clean_float_int():
    assert clean_float(42) == 42.0


def test_clean_float_string_invalid():
    assert clean_float("abc") is None


def test_clean_float_rounds_to_6_places():
    result = clean_float(1.1234567890)
    assert result == pytest.approx(1.123457, rel=1e-5)


# ----------------------------------------------------------------------
# row_to_us
# ----------------------------------------------------------------------

_US_ROW = ("AAPL", "Apple Inc.", "Technology", 189.3, 2_900_000_000_000.0, 0.6, 147.0, 85, 45, 65)


def test_row_to_us_basic():
    d = row_to_us(_US_ROW)
    assert d["symbol"] == "AAPL"
    assert d["name"] == "Apple Inc."
    assert d["sector"] == "Technology"
    assert d["price"] == pytest.approx(189.3)
    assert d["health_score"] == 85
    assert d["value_score"] == 45
    assert d["total_score"] == 65


def test_row_to_us_scores_are_int():
    d = row_to_us(_US_ROW)
    assert isinstance(d["health_score"], int)
    assert isinstance(d["value_score"], int)
    assert isinstance(d["total_score"], int)


def test_row_to_us_none_name_becomes_empty():
    row = (None, None, None, 10.0, 1e9, None, None, 50, 50, 50)
    d = row_to_us(row)
    assert d["symbol"] == ""
    assert d["name"] == ""
    assert d["dividend_yield"] is None
    assert d["roe"] is None


def test_row_to_us_nan_price_becomes_none():
    row = ("T", "AT&T", "Comm.", float("nan"), 1e9, 0.0, 5.0, 60, 40, 50)
    d = row_to_us(row)
    assert d["price"] is None


# ----------------------------------------------------------------------
# row_to_kr
# ----------------------------------------------------------------------

_KR_ROW = ("005930", "삼성전자", "전기전자", 72000.0, 4.3e14, 2.1, 8.5, 18.2, 1.4, 72, 58, 65)


def test_row_to_kr_basic():
    d = row_to_kr(_KR_ROW)
    assert d["symbol"] == "005930"
    assert d["name"] == "삼성전자"
    assert d["per"] == pytest.approx(18.2)
    assert d["pbr"] == pytest.approx(1.4)
    assert d["total_score"] == 65


def test_row_to_kr_none_per_pbr():
    row = ("000001", "테스트", "금융", 5000.0, 1e12, 0.0, None, None, None, 40, 30, 35)
    d = row_to_kr(row)
    assert d["per"] is None
    assert d["pbr"] is None
    assert d["roe"] is None


# ----------------------------------------------------------------------
# row_to_crypto
# ----------------------------------------------------------------------

_CRYPTO_ROW = ("BTC", "Bitcoin", 67000.0, 1.3e12, 90, 85, 87)


def test_row_to_crypto_basic():
    d = row_to_crypto(_CRYPTO_ROW)
    assert d["symbol"] == "BTC"
    assert d["name"] == "Bitcoin"
    assert d["rank_score"] == 90     # health_score = rank_score
    assert d["volatility_score"] == 85
    assert d["total_score"] == 87


def test_row_to_crypto_scores_are_int():
    d = row_to_crypto(_CRYPTO_ROW)
    assert isinstance(d["rank_score"], int)
    assert isinstance(d["volatility_score"], int)
    assert isinstance(d["total_score"], int)


# ----------------------------------------------------------------------
# regime_to_dict (pandas import 필요 — 오프라인에서 사용 가능)
# ----------------------------------------------------------------------


def _make_fake_summary():
    import pandas as pd

    from src.macro_analyzer import RegimeReport

    idx = ["S&P 500", "금"]
    regime = RegimeReport(
        regime="🟢 위험선호 (Risk-on)",
        score=2,
        signals=["장단기 금리차 +0.5%p 정상", "HY 스프레드 안정"],
        raw={"T10Y2Y": 0.5},
        failures=[],
    )
    return {
        "period": "6mo",
        "prices": pd.DataFrame(),
        "cumulative_returns_pct": pd.Series([12.3, 5.1], index=idx),
        "annualized_vol_pct": pd.Series([18.2, 12.0], index=idx),
        "current_drawdown_pct": pd.Series([-3.1, -1.5], index=idx),
        "sharpe_ratio": pd.Series([1.2, 0.8], index=idx),
        "correlation": pd.DataFrame([[1.0, -0.1], [-0.1, 1.0]], index=idx, columns=idx),
        "regime": regime,
    }


def test_regime_to_dict_label():
    d = regime_to_dict(_make_fake_summary())
    assert d["label"] == "🟢 위험선호 (Risk-on)"
    assert d["score"] == 2


def test_regime_to_dict_panel_structure():
    d = regime_to_dict(_make_fake_summary())
    assert len(d["panel"]) == 2
    sp = next(p for p in d["panel"] if p["name"] == "S&P 500")
    assert sp["return_6m"] == pytest.approx(12.3)
    assert sp["vol"] == pytest.approx(18.2)
    assert sp["drawdown"] == pytest.approx(-3.1)


def test_regime_to_dict_correlations_exclude_self():
    d = regime_to_dict(_make_fake_summary())
    # 자기 자신(대각선)은 제외
    assert "S&P 500" not in d["correlations"]["S&P 500"]
    assert "금" in d["correlations"]["S&P 500"]


def test_regime_to_dict_signals():
    d = regime_to_dict(_make_fake_summary())
    assert len(d["signals"]) == 2
    assert d["failures"] == []


def test_regime_to_dict_legacy_keys_unchanged():
    # 대시보드 JS(index.html renderRegime)가 읽는 기존 키 — 이름 변경/삭제 금지 (13a)
    d = regime_to_dict(_make_fake_summary())
    assert {"label", "score", "signals", "failures", "panel", "correlations"} <= set(d)
    assert isinstance(d["score"], int)      # JS 가 숫자 비교 — 기존 int 유지


def test_regime_to_dict_has_finding_shape():
    # 13a: 다이제스트와 동일한 Finding shape 를 추가 키로 공유
    d = regime_to_dict(_make_fake_summary())
    fd = d["finding"]
    assert fd["kind"] == "regime"
    assert fd["title"] == d["label"]
    assert fd["score"] == pytest.approx(d["score"])
    assert fd["evidence"] == d["signals"]


# ----------------------------------------------------------------------
# prediction_to_dict
# ----------------------------------------------------------------------


def _make_fake_result():
    from src.predictors import LeadLagResult

    return LeadLagResult(
        leading_name="M2 증가율",
        target_name="BTC 수익률",
        best_lag_months=3,
        correlation=0.62,
        r_squared=0.42,
        slope=1.5,
        intercept=0.2,
        n_obs=48,
        latest_leading_value=5.3,
        predicted_change_pct=15.3,
        direction="상승 ↑",
        reliable=True,
        notes=["최적 선행 3개월", "예측 +15.3%"],
    )


def test_prediction_to_dict_basic():
    d = prediction_to_dict("M2 → 비트코인", _make_fake_result())
    assert d["name"] == "M2 → 비트코인"
    assert d["leading"] == "M2 증가율"
    assert d["target"] == "BTC 수익률"
    assert d["best_lag_months"] == 3
    assert d["r_squared"] == pytest.approx(0.42)
    assert d["direction"] == "상승 ↑"
    assert d["reliable"] is True
    assert d["n_obs"] == 48
    assert len(d["notes"]) == 2


def test_prediction_to_dict_unreliable():
    result = _make_fake_result()
    result.reliable = False
    result.r_squared = 0.08
    d = prediction_to_dict("테스트", result)
    assert d["reliable"] is False
    assert d["r_squared"] == pytest.approx(0.08)


def test_prediction_to_dict_legacy_keys_unchanged():
    # 대시보드 JS(index.html renderPredictions)가 읽는 기존 키 — 이름 변경/삭제 금지 (13a)
    d = prediction_to_dict("M2 → 비트코인", _make_fake_result())
    assert {"name", "leading", "target", "best_lag_months", "correlation", "r_squared",
            "direction", "predicted_change_pct", "reliable", "n_obs", "notes"} <= set(d)
    assert d["notes"] == ["최적 선행 3개월", "예측 +15.3%"]   # 리스트 유지 (JSON 배열)


def test_prediction_to_dict_has_finding_shape():
    # 13a: 다이제스트와 동일한 Finding shape 를 추가 키로 공유
    d = prediction_to_dict("M2 → 비트코인", _make_fake_result())
    fd = d["finding"]
    assert fd["kind"] == "prediction"
    assert fd["title"] == "M2 → 비트코인"
    assert fd["score"] == pytest.approx(d["r_squared"])
    assert fd["confidence"] == "신뢰"
    assert fd["evidence"] == d["notes"]


# ----------------------------------------------------------------------
# finding_to_json
# ----------------------------------------------------------------------


def test_finding_to_json_cleans_nan_score():
    from src.findings import Finding

    fd = Finding(kind="prediction", title="t", score=float("nan"),
                 confidence="약함", summary="s", evidence=("e",))
    d = finding_to_json(fd)
    assert d["score"] is None            # NaN → JSON null (json.dumps 'NaN' 방지)
    assert d["evidence"] == ["e"]


# ----------------------------------------------------------------------
# main() 가드 — 강등/빈 DB 로 공개 파일을 덮어쓰지 않음 + data_as_of (WP-OPS-CORE)
# tmp DB + tmp DASHBOARD_DIR, 국면·예측은 --skip (네트워크 없음)
# ----------------------------------------------------------------------

import json  # noqa: E402

import scripts.export_dashboard as ed  # noqa: E402
import src.storage as storage_mod  # noqa: E402

_SCREENED_MIN = """
CREATE TABLE IF NOT EXISTS screened (
    symbol TEXT NOT NULL, market TEXT NOT NULL, name TEXT, sector TEXT,
    price REAL, market_cap REAL, dividend_yield REAL, roe REAL,
    health_score INTEGER, value_score INTEGER, total_score INTEGER,
    per REAL, pbr REAL, enriched INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (symbol, market)
);
"""


@pytest.fixture
def export_env(tmp_path, monkeypatch):
    """격리 DB(env 경로) + 싱글톤 리셋 + tmp 대시보드 폴더 + 오프라인 인자."""
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "export.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    out = tmp_path / "dashboard"
    monkeypatch.setattr(ed, "DASHBOARD_DIR", out)
    monkeypatch.setattr("sys.argv", ["export_dashboard.py", "--skip-regime", "--skip-predictions"])
    yield out
    if storage_mod._storage is not None:
        storage_mod._storage.close()
    monkeypatch.setattr(storage_mod, "_storage", None)


def _seed(rows):
    """rows: (symbol, market, enriched, updated_at)."""
    conn = storage_mod.get_storage().conn
    conn.executescript(_SCREENED_MIN)
    for sym, market, enriched, updated in rows:
        conn.execute(
            "INSERT INTO screened (symbol, market, name, sector, price, market_cap, "
            "dividend_yield, roe, health_score, value_score, total_score, per, pbr, "
            "enriched, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sym, market, sym, "Tech", 10.0, 1e9, 0.01, 0.1, 50, 50, 50, 8.0, 1.0,
             enriched, updated),
        )
    conn.commit()


def test_main_writes_data_as_of(export_env):
    _seed([
        ("AAA", "US", 1, "2026-07-10T05:00:00+00:00"),
        ("BBB", "US", 1, "2026-07-09T05:00:00+00:00"),
        ("ZZZ", "US", 0, "2026-10-01T05:00:00+00:00"),   # 미보강 — 기준 시각에 안 들어감
        ("005930", "KR", 1, "2026-07-08T01:00:00+00:00"),
        ("BTC", "CRYPTO", 1, "2026-07-11T00:00:00+00:00"),
    ])
    assert ed.main() == 0
    us = json.loads((export_env / "us_data.json").read_text(encoding="utf-8"))
    kr = json.loads((export_env / "kr_data.json").read_text(encoding="utf-8"))
    assert us["data_as_of"] == "2026-07-10T05:00:00+00:00"
    assert us["count"] == 2
    assert kr["data_as_of"] == "2026-07-08T01:00:00+00:00"
    assert (export_env / "crypto_data.json").exists()


@pytest.mark.parametrize("missing", ["US", "KR"])
def test_main_returns_3_and_keeps_files_when_market_empty(export_env, missing):
    export_env.mkdir(parents=True)
    old = '{"old": true}'
    for f in ("us_data.json", "kr_data.json", "crypto_data.json"):
        (export_env / f).write_text(old, encoding="utf-8")
    rows = [("AAA", "US", 1, "2026-07-10"), ("005930", "KR", 1, "2026-07-08"),
            ("BTC", "CRYPTO", 1, "2026-07-11")]
    _seed([r for r in rows if r[1] != missing])
    assert ed.main() == 3
    for f in ("us_data.json", "kr_data.json", "crypto_data.json"):
        assert (export_env / f).read_text(encoding="utf-8") == old   # 하나도 안 덮어씀


def test_main_returns_3_when_screened_table_missing(export_env):
    storage_mod.get_storage()                       # 빈 DB (screened 없음)
    assert ed.main() == 3
    assert not (export_env / "us_data.json").exists()


def test_main_keeps_crypto_file_when_crypto_empty(export_env):
    export_env.mkdir(parents=True)
    (export_env / "crypto_data.json").write_text('{"old": true}', encoding="utf-8")
    _seed([("AAA", "US", 1, "2026-07-10"), ("005930", "KR", 1, "2026-07-08")])
    assert ed.main() == 0
    assert (export_env / "crypto_data.json").read_text(encoding="utf-8") == '{"old": true}'
    assert (export_env / "us_data.json").exists()


def test_main_returns_2_without_writing_when_turso_degraded(export_env, monkeypatch, capsys):
    """Turso 설정 + 초기 pull 타임아웃(오프라인 파일 강등) → exit 2, 파일 미생성."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "libsql_experimental",
                        types.ModuleType("libsql_experimental"))   # import 확인만 통과
    monkeypatch.setattr(storage_mod.Storage, "_probe_initial_sync",
                        staticmethod(lambda *a, **k: "timeout"))
    object.__setattr__(storage_mod.settings, "turso_database_url", "libsql://x.turso.io")
    assert ed.main() == 2
    assert "::error::" in capsys.readouterr().out
    assert list(export_env.glob("*.json")) == []


# ----------------------------------------------------------------------
# WP-SURFACE — 기준일/제외 사유 payload, 국면 보류, index.html 문구
# ----------------------------------------------------------------------

from pathlib import Path  # noqa: E402

_INDEX_HTML = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def test_row_converters_add_asof_and_excluded_fields():
    us = row_to_us(_US_ROW + ("2026-07-10T16:52:00+00:00", "2026-07-09T00:00:00+00:00", "sibling"))
    assert us["fund_asof"] == "2026-07-10"
    assert us["price_asof"] == "2026-07-09"
    assert us["excluded"] == "sibling"
    kr = row_to_kr(_KR_ROW + ("2026-07-08T01:00:00+00:00", None, None))
    assert kr["fund_asof"] == "2026-07-08" and kr["price_asof"] is None and kr["excluded"] is None
    cr = row_to_crypto(_CRYPTO_ROW + ("2026-06-15T00:00:00+00:00", None, None))
    assert cr["fund_asof"] == "2026-06-15" and cr["excluded"] is None


def test_row_converters_old_tuple_shape_still_works():
    # 구형(컬럼 수가 짧은) 튜플 — 기준일 필드는 None 으로 채워짐 (하위호환)
    for d in (row_to_us(_US_ROW), row_to_kr(_KR_ROW), row_to_crypto(_CRYPTO_ROW)):
        assert d["fund_asof"] is None and d["price_asof"] is None and d["excluded"] is None


def test_regime_to_dict_marks_hold_when_indicators_missing():
    summary = _make_fake_summary()
    summary["regime"].regime = "⚪ 판정 보류 (지표 2/3)"
    summary["regime"].available = 2
    summary["regime"].total = 3
    summary["regime"].failures = ["hy_spread"]
    d = regime_to_dict(summary)
    assert d["held"] is True and d["available"] == 2 and d["total"] == 3
    assert "판정 보류" in d["label"]


def test_regime_to_dict_complete_is_not_held():
    summary = _make_fake_summary()
    summary["regime"].available = 3
    summary["regime"].total = 3
    assert regime_to_dict(summary)["held"] is False
    assert {"label", "score", "signals", "failures", "panel", "correlations"} <= set(regime_to_dict(summary))


def _seed_with_asof(rows):
    """rows: (symbol, market, enriched, updated_at, seen_at, excluded, market_cap, total)."""
    conn = storage_mod.get_storage().conn
    conn.executescript(_SCREENED_MIN)
    for col in ("seen_at", "excluded"):
        conn.execute(f"ALTER TABLE screened ADD COLUMN {col} TEXT")
    for sym, market, enriched, updated, seen, excl, mcap, total in rows:
        conn.execute(
            "INSERT INTO screened (symbol, market, name, sector, price, market_cap, "
            "dividend_yield, roe, health_score, value_score, total_score, per, pbr, "
            "enriched, updated_at, seen_at, excluded) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sym, market, sym, "Tech", 10.0, mcap, 0.01, 0.1, 50, 50, total, 8.0, 1.0,
             enriched, updated, seen, excl),
        )
    conn.commit()


def test_main_payload_has_asof_and_excluded_fields(export_env):
    _seed_with_asof([
        ("AAA", "US", 1, "2026-07-10T05:00:00+00:00", "2026-07-09T00:00:00+00:00", None, 2e9, 70),
        ("BBB", "US", 1, "2026-07-09T05:00:00+00:00", "2026-07-08T00:00:00+00:00", None, 3e9, 70),
        ("AFGE", "US", 1, "2026-07-10T05:00:00+00:00", None, "sibling", 9e9, 85),
        ("005930", "KR", 1, "2026-07-08T01:00:00+00:00", "2026-07-08T00:00:00+00:00", None, 1e12, 60),
        ("BTC", "CRYPTO", 1, "2026-07-11T00:00:00+00:00", None, None, 1e12, 80),
    ])
    assert ed.main() == 0
    us = json.loads((export_env / "us_data.json").read_text(encoding="utf-8"))
    assert us["data_as_of"] == "2026-07-10T05:00:00+00:00"          # main/_data_as_of 동작 불변
    assert us["price_as_of"] == "2026-07-09"
    assert us["excluded_count"] == 1
    assert us["count"] == 3 and us["eligible_count"] == 2             # count 하위호환 + 후보 대상 수 (S7)
    by = {i["symbol"]: i for i in us["items"]}
    assert by["AFGE"]["excluded"] == "sibling"
    assert by["AAA"]["excluded"] is None
    assert by["AAA"]["fund_asof"] == "2026-07-10" and by["AAA"]["price_asof"] == "2026-07-09"
    # 점수 동점(70)은 시총 내림차순 — universe.scan 과 같은 순서 (CMAP-10)
    order = [i["symbol"] for i in us["items"]]
    assert order == ["AFGE", "BBB", "AAA"]
    kr = json.loads((export_env / "kr_data.json").read_text(encoding="utf-8"))
    assert kr["items"][0]["price_asof"] == "2026-07-08"


def test_main_payload_without_new_columns_degrades_to_none(export_env):
    # 구스키마(seen_at/excluded 없음) 레플리카 — SELECT 가 깨지지 않고 필드는 None
    _seed([("AAA", "US", 1, "2026-07-10T05:00:00+00:00"), ("005930", "KR", 1, "2026-07-08")])
    assert ed.main() == 0
    us = json.loads((export_env / "us_data.json").read_text(encoding="utf-8"))
    item = us["items"][0]
    assert item["fund_asof"] == "2026-07-10" and item["price_asof"] is None and item["excluded"] is None
    assert us["price_as_of"] is None and us["excluded_count"] == 0


def test_index_html_vocabulary_and_banners():
    html = _INDEX_HTML.read_text(encoding="utf-8")
    assert "하루 1회(지연 가능)" in html
    assert "매일 갱신" not in html and "하루 1회 갱신" not in html
    assert "강력 추천" not in html and "비추천" not in html
    assert "저평가" not in html                       # 라벨은 '밸류'
    assert ">밸류 <span" in html and "건전성" in html
    assert "STALE_DAYS = 8" in html and "data.data_as_of" in html
    assert "x.excluded" in html                        # 제외 종목은 목록에서 뺌
    assert "검증 미통과 · 참고용" in html
    assert "신뢰</span>" not in html
    assert "판정 보류" in html and "isHold" in html
    assert "투자 자문 아님, 모든 결정은 본인 책임." in html


# ---------------------------------------------------------------------------
# SEC-02 — 공개 regime_data.json 의 error 필드는 마스킹 + 길이 제한
# ---------------------------------------------------------------------------

def test_regime_error_field_is_masked_and_truncated(export_env, monkeypatch, fake_fmp_key):
    import src.macro_analyzer as ma
    from src.exceptions import DataFetchError

    export_env.mkdir(parents=True, exist_ok=True)

    def boom(period="6mo"):
        raise DataFetchError(f"https://x/?apikey={fake_fmp_key} 실패 " + "가" * 500)

    monkeypatch.setattr(ma, "market_summary", boom)
    assert ed.export_regime() is False
    payload = json.loads((export_env / "regime_data.json").read_text(encoding="utf-8"))
    assert fake_fmp_key not in payload["error"]
    assert "***REDACTED***" in payload["error"]
    assert len(payload["error"]) <= 200


def test_regime_serialization_error_is_masked(export_env, monkeypatch, fake_fmp_key):
    import src.macro_analyzer as ma

    export_env.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ma, "market_summary", lambda period="6mo": object())

    def boom(summary):
        raise ValueError(f"bad {fake_fmp_key}")

    monkeypatch.setattr(ed, "regime_to_dict", boom)
    assert ed.export_regime() is False
    payload = json.loads((export_env / "regime_data.json").read_text(encoding="utf-8"))
    assert fake_fmp_key not in payload["error"]
    assert "***REDACTED***" in payload["error"]


def test_index_html_numeric_fields_use_helper():
    """SEC-03 — esc() 없이 보간되던 숫자 필드는 num()/isNum() 헬퍼를 거친다."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    assert "function num(v)" in html and "function isNum(v)" in html
    for f in ("health_score", "value_score", "total_score", "rank_score", "volatility_score"):
        assert f"num(it.{f})" in html
        assert f"it.{f} ??" not in html            # 원시 보간 금지
    assert "isNum(p.best_lag_months)" in html and "${p.best_lag_months}" not in html
    assert "isNum(data.available)" in html and "isNum(data.score)" in html
    assert "typeof p.direction === 'string'" in html
