"""DART 파싱 + 한국 점수 (순수 함수, 오프라인)."""

from __future__ import annotations

import pytest

from src.data_fetcher import _parse_dart_accounts
from src.universe import calculate_kr_scores


def _items(fs_div, **accts):
    return [{"account_nm": k, "fs_div": fs_div, "thstrm_amount": v} for k, v in accts.items()]


# ---------------- _parse_dart_accounts ----------------


def test_parse_prefers_cfs_over_ofs():
    items = (
        _items("CFS", **{"당기순이익(손실)": "100", "자본총계": "1,000"})
        + _items("OFS", **{"당기순이익(손실)": "50", "자본총계": "500"})
    )
    out = _parse_dart_accounts(items)
    assert out["net_income"] == 100.0   # CFS 우선
    assert out["equity"] == 1000.0      # 콤마 제거
    assert out["fs_div"] == "CFS"


def test_parse_falls_back_to_ofs():
    out = _parse_dart_accounts(_items("OFS", **{"자본총계": "500", "당기순이익(손실)": "50"}))
    assert out["equity"] == 500.0 and out["fs_div"] == "OFS"


def test_parse_dedupes_duplicate_net_income():
    # DART 는 '당기순이익(손실)' 을 두 번 줌 → 첫 값만
    items = _items("CFS") + [
        {"account_nm": "당기순이익(손실)", "fs_div": "CFS", "thstrm_amount": "45"},
        {"account_nm": "당기순이익(손실)", "fs_div": "CFS", "thstrm_amount": "45"},
    ]
    assert _parse_dart_accounts(items)["net_income"] == 45.0


def test_parse_handles_missing_and_bad_amounts():
    out = _parse_dart_accounts([{"account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": "-"}])
    assert out["equity"] is None
    assert out["net_income"] is None


# ---------------- calculate_kr_scores ----------------


def test_kr_scores_good_value_stock():
    # 흑자 + 저PER/저PBR + 고ROE + 저부채 → 높은 점수
    fin = {"net_income": 200, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": 400}
    sc = calculate_kr_scores(fin, market_cap=1500)   # PER 7.5, PBR 1.5, ROE 20%
    assert sc["roe"] == pytest.approx(20.0)
    assert sc["per"] == pytest.approx(7.5)
    assert sc["pbr"] == pytest.approx(1.5)
    assert sc["total_score"] > 55
    assert 0 <= sc["health_score"] <= 100 and 0 <= sc["value_score"] <= 100


def test_kr_scores_expensive_stock_lower():
    cheap = calculate_kr_scores(
        {"net_income": 200, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": 400}, 1500)
    pricey = calculate_kr_scores(
        {"net_income": 200, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": 400}, 9000)
    assert pricey["value_score"] < cheap["value_score"]   # 같은 실적, 비싼 시총 → 저평가도↓


def test_kr_scores_loss_maker_no_per():
    sc = calculate_kr_scores(
        {"net_income": -50, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": -10}, 1500)
    assert sc["per"] is None          # 적자 → PER 없음
    assert sc["roe"] == pytest.approx(-5.0)


def test_kr_scores_missing_data_safe():
    sc = calculate_kr_scores({"net_income": None, "equity": None}, market_cap=0)
    assert sc["total_score"] == 0
    assert sc["roe"] is None and sc["per"] is None and sc["pbr"] is None
