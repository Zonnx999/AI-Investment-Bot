"""src/screener.py — 재설계된 점수 엔진 (ScoreCard + 교정 필드, 오프라인)."""

from __future__ import annotations

from src.screener import (
    Component,
    ScoreCard,
    _safe,
    calculate_health_score,
    calculate_value_score,
    has_fundamentals,
    health_scorecard,
    value_scorecard,
)

# 우량 + 저평가 종목 (병합된 key-metrics + ratios 필드)
GOOD = {
    "grossProfitMargin": 0.45,          # ratios
    "returnOnInvestedCapital": 0.18,    # key-metrics (ROIC)
    "returnOnEquity": 0.20,
    "netDebtToEBITDA": 0.5,
    "incomeQuality": 1.2,
    "currentRatio": 2.0,
    "evToEBITDA": 8.0,
    "evToSales": 2.0,
    "priceToBookRatio": 1.5,            # ratios
    "earningsYield": 0.07,
    "freeCashFlowYield": 0.05,
}


def test_health_scorecard_breakdown_shape():
    card = health_scorecard(GOOD)
    assert isinstance(card, ScoreCard)
    labels = [c.label for c in card.components]
    assert "매출총이익률" in labels and "ROIC" in labels  # 신규 팩터 반영 (SCORE-11 라벨 정정)
    assert card.total == sum(round(c.points) for c in [Component("", card.total, 100, "")]) or card.total <= 100
    # 각 항목 points <= max_points
    assert all(c.points <= c.max_points + 1e-9 for c in card.components)


def test_health_high_for_quality_stock():
    assert calculate_health_score(GOOD) >= 80


def test_value_high_for_cheap_stock():
    quote = {"price": 100.0, "lastDividend": 2.0, "sector": "Industrials", "industry": "Machinery"}
    assert calculate_value_score(quote, GOOD) >= 60


def test_value_no_dividend_growth_not_penalized():
    """무배당 성장주(NVDA류): 배당 0 이어도 이익수익률·EV/EBITDA 로 정당 평가."""
    quote = {"price": 500.0, "lastDividend": 0.0, "sector": "Technology", "industry": "Semiconductors"}
    metrics = {"evToEBITDA": 22.0, "evToSales": 12.0, "priceToBookRatio": 20.0,
               "earningsYield": 0.035, "freeCashFlowYield": 0.03}
    # 배당 0 이지만 점수가 바닥(<10)은 아니어야 (구 공식은 배당 35점이라 과벌점)
    assert calculate_value_score(quote, metrics) >= 15


def test_gross_profitability_drives_health():
    """총이익률(GP)이 건전성의 최대 단일 항목 — 높으면 점수 상승."""
    low_gp = dict(GOOD, grossProfitMargin=0.05)
    high_gp = dict(GOOD, grossProfitMargin=0.50)
    assert health_scorecard(high_gp).total > health_scorecard(low_gp).total


def test_financial_sector_ev_sales_neutral():
    bank = {"price": 50.0, "sector": "Financial Services", "industry": "Banks"}
    metrics = dict(GOOD, evToSales=15.0)   # 은행은 EV/Sales 의미 적음 → 중립
    card = value_scorecard(bank, metrics)
    ev_sales = next(c for c in card.components if c.label == "EV/Sales")
    assert ev_sales.detail == "금융중립"


def test_scorecard_to_dict_serializable():
    import json
    card = health_scorecard(GOOD)
    s = json.dumps(card.to_dict(), ensure_ascii=False)   # DB detail 저장용
    back = json.loads(s)
    assert back["total"] == card.total
    assert len(back["components"]) == len(card.components)


def test_empty_metrics_safe():
    assert calculate_health_score({}) >= 0
    assert calculate_value_score({}, {}) >= 0


def test_negative_ev_ebitda_gets_zero_not_full():
    """적자 EBITDA(음수 EV/EBITDA) 는 만점이 아니라 0점 (역설 방지)."""
    quote = {"price": 100.0, "sector": "Technology", "industry": "Software"}
    loss = {"evToEBITDA": -5.0, "priceToBookRatio": 2.0, "earningsYield": 0.0}
    card = value_scorecard(quote, loss)
    ev = next(c for c in card.components if c.label == "EV/EBITDA")
    assert ev.points == 0.0
    assert "적자" in ev.detail


def test_negative_pbr_gets_zero():
    """자본잠식(음수 PBR) 도 0점."""
    quote = {"price": 100.0, "sector": "Industrials", "industry": "x"}
    card = value_scorecard(quote, {"evToEBITDA": 10.0, "priceToBookRatio": -3.0})
    pbr = next(c for c in card.components if c.label == "PBR")
    assert pbr.points == 0.0


def test_positive_ev_ebitda_still_scores():
    quote = {"price": 100.0, "sector": "Industrials", "industry": "x"}
    card = value_scorecard(quote, {"evToEBITDA": 5.0, "priceToBookRatio": 1.0})
    ev = next(c for c in card.components if c.label == "EV/EBITDA")
    assert ev.points > 0


# -------- 순부채/EBITDA 음수 분기 (순현금 vs 적자 EBITDA) --------


def _nde(card):
    return next(c for c in card.components if c.label == "순부채/EBITDA")


def test_net_cash_with_positive_ebitda_full_credit():
    """순현금(netDebtToEBITDA<0) + EBITDA>0 → 우량 → 만점 유지."""
    card = health_scorecard(dict(GOOD, netDebtToEBITDA=-1.0, evToEBITDA=8.0))
    c = _nde(card)
    assert c.points == 20.0
    assert "순현금" in c.detail


def test_negative_ebitda_net_debt_not_rewarded():
    """적자 EBITDA(evToEBITDA<0)로 비율이 음수가 된 부실기업 → 만점 아님, 0점."""
    card = health_scorecard(dict(GOOD, netDebtToEBITDA=-2.0, evToEBITDA=-5.0))
    c = _nde(card)
    assert c.points == 0.0
    assert "EBITDA 적자" in c.detail


def test_negative_nde_without_ev_ebitda_is_conservative():
    """순현금 여부를 확인할 evToEBITDA 가 없으면 만점 주지 않음(0점)."""
    metrics = {k: v for k, v in GOOD.items() if k != "evToEBITDA"}
    card = health_scorecard(dict(metrics, netDebtToEBITDA=-2.0))
    assert _nde(card).points == 0.0


# ---------------- _safe (결측/비숫자 방어) ----------------


def test_safe_handles_non_numeric_and_nan():
    """'N/A' 문자열·NaN·Inf 는 크래시/만점이 아니라 default 로 (#버그수정)."""
    assert _safe({"x": "N/A"}, "x") == 0.0            # 문자열 → ValueError 안 나고 default
    assert _safe({"x": float("nan")}, "x", 1.0) == 1.0  # NaN → default(만점 방지)
    assert _safe({"x": float("inf")}, "x") == 0.0     # Inf → default
    assert _safe({}, "x", 5.0) == 5.0                 # 키 없음 → default
    assert _safe({"x": None}, "x", 5.0) == 5.0        # None → default
    assert _safe({"x": "12.5"}, "x") == 12.5          # 정상 숫자 문자열은 변환


def test_nan_metric_scores_zero_not_full():
    """NaN 컴포넌트가 _clip 통과해 만점 받던 버그 — 이제 0점."""
    card = health_scorecard({"grossProfitMargin": float("nan")})
    gp = next(c for c in card.components if c.label == "매출총이익률")
    assert gp.points == 0.0   # NaN → default(0) → 0점 (이전엔 만점 25)


# ---------------- has_fundamentals (빈 fundamentals → skip 판정) ----------------


def test_has_fundamentals_rejects_empty_and_all_missing():
    """빈 dict / 전부 None·NaN·빈 문자열 → False (점수 생략 대상)."""
    assert has_fundamentals({}) is False
    assert has_fundamentals({"returnOnEquity": None, "evToSales": None}) is False
    assert has_fundamentals({"returnOnEquity": float("nan"), "x": float("inf")}) is False
    assert has_fundamentals({"symbol": "", "returnOnEquity": None}) is False


def test_has_fundamentals_accepts_real_data_including_zero():
    """정당한 0 값은 결측이 아님 — True (0 과 '데이터 없음' 구분)."""
    assert has_fundamentals({"netDebtToEBITDA": 0.0}) is True
    assert has_fundamentals({"returnOnEquity": 0.15, "evToSales": None}) is True


def test_screen_one_skips_when_fundamentals_empty(monkeypatch):
    """fundamentals 가 빈 종목은 0점 랭킹 바닥 대신 skip(None) (#backlog 점수정확성)."""
    from src import screener

    monkeypatch.setattr(
        "src.screener.fetch_quote",
        lambda t: {"price": 100.0, "name": "Empty Co", "marketCap": 5e9},
    )
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda t: {})
    assert screener.screen_one("EMPTY") is None

    monkeypatch.setattr(
        "src.screener.latest_fundamentals", lambda t: {"returnOnEquity": None}
    )
    assert screener.screen_one("ALLNONE") is None


def test_screen_one_scores_when_fundamentals_present(monkeypatch):
    from src import screener

    monkeypatch.setattr(
        "src.screener.fetch_quote",
        lambda t: {"price": 100.0, "name": "Good Co", "marketCap": 5e9,
                   "lastDividend": 2.0, "sector": "Industrials", "industry": "Machinery"},
    )
    monkeypatch.setattr("src.screener.latest_fundamentals", lambda t: dict(GOOD))
    row = screener.screen_one("GOOD")
    assert row is not None
    assert row["total_score"] > 0


def test_has_fundamentals_ignores_identifier_strings():
    """실제 FMP 행 형태 — 수치 메트릭이 전부 null 이어도 식별자 문자열
    (symbol/fiscalYear/period/reportedCurrency)은 항상 존재. 문자열을 데이터로
    세면 skip 가드가 실데이터에서 절대 발화하지 않으므로 전부 무시해야 함."""
    shell_row = {
        "symbol": "XYZ", "fiscalYear": "2024", "period": "FY",
        "reportedCurrency": "USD",
        "returnOnEquity": None, "earningsYield": None, "evToSales": float("nan"),
    }
    assert has_fundamentals(shell_row) is False
    # 식별자 + 실제 수치 하나라도 있으면 True
    assert has_fundamentals({**shell_row, "returnOnEquity": 0.12}) is True


def test_health_nde_negative_from_loss_scores_zero():
    """음수 NDE 의 두 얼굴 회귀: 적자발(EBITDA<0, evToEBITDA<0)은 0점 '적자',
    순현금발(netDebt<0, EBITDA>0)은 종전대로 만점."""
    loss = {"netDebtToEBITDA": -4.0, "evToEBITDA": -6.0}
    sc = health_scorecard(loss)
    comp = next(c for c in sc.components if c.label == "순부채/EBITDA")
    assert comp.points == 0.0 and "적자" in comp.detail

    net_cash = {"netDebtToEBITDA": -1.0, "evToEBITDA": 8.0}
    sc2 = health_scorecard(net_cash)
    comp2 = next(c for c in sc2.components if c.label == "순부채/EBITDA")
    assert comp2.points == 20.0 and "적자" not in comp2.detail


# ======================================================================
# TTM 전환 · 금융 중립화 · 이상치 격리 (WP-SCORE)
# 픽스처 = FMP key-metrics-ttm / ratios-ttm 실응답 원문 (EOG·HG·CALM·JPM·MGR)
# ======================================================================

import json
from pathlib import Path

import pandas as pd
import pytest

from src import screener
from src.screener import (
    is_balance_sheet_financial,
    latest_fundamentals,
    merge_ttm,
    strip_ttm,
    ttm_usable,
)

_FIX = Path(__file__).parent / "fixtures"
_TTM = json.loads((_FIX / "fmp_ttm_responses.json").read_text(encoding="utf-8"))
_GOLD = json.loads((_FIX / "golden_scores.json").read_text(encoding="utf-8"))


def _comp(card, label):
    return next(c for c in card.components if c.label == label)


def _patch_ttm(monkeypatch, symbol):
    """실응답 픽스처를 fetch_*_ttm 대용으로 주입 (네트워크 없음)."""
    monkeypatch.setattr(screener, "fetch_key_metrics_ttm", lambda t: _TTM[symbol]["key-metrics-ttm"][0])
    monkeypatch.setattr(screener, "fetch_ratios_ttm", lambda t: _TTM[symbol]["ratios-ttm"][0])


# ---------------- 골든 값 (실응답 TTM + 스크리너 가격/배당) ----------------


@pytest.mark.parametrize("symbol", ["EOG", "CALM", "HG", "JPM", "MGR"])
def test_golden_scores_exact(monkeypatch, symbol):
    _patch_ttm(monkeypatch, symbol)
    inp = _GOLD["inputs"][symbol]
    want = _GOLD["golden"][symbol]

    m = latest_fundamentals(symbol)
    assert m["_basis"] == "ttm"
    assert ttm_usable(m) is want["ttm_usable"]

    is_fin = is_balance_sheet_financial(inp["sector"], inp["industry"])
    assert is_fin is want["is_fin"]

    health = health_scorecard(m, is_fin=is_fin)
    value = value_scorecard(inp, m, is_fin=is_fin)
    assert health.total == want["health"]
    assert value.total == want["value"]
    assert round((health.total + value.total) / 2) == want["total"]


def test_golden_literal_values():
    """픽스처 파일이 바뀌어도 스펙의 골든 값 자체는 고정."""
    lit = {"EOG": (88, 79, 84), "CALM": (42, 47, 44), "HG": (60, 73, 66),
           "JPM": (60, 65, 62), "MGR": (60, 70, 65)}
    for sym, (h, v, t) in lit.items():
        g = _GOLD["golden"][sym]
        assert (g["health"], g["value"], g["total"]) == (h, v, t)


def test_value_scorecard_infers_financial_from_quote_when_is_fin_none(monkeypatch):
    """is_fin=None 이면 quote 의 sector/industry 로 판정 (JPM = 은행 -> 중립 적용)."""
    _patch_ttm(monkeypatch, "JPM")
    m = latest_fundamentals("JPM")
    inp = _GOLD["inputs"]["JPM"]
    assert value_scorecard(inp, m).total == _GOLD["golden"]["JPM"]["value"]
    assert calculate_value_score(inp, m) == _GOLD["golden"]["JPM"]["value"]


# ---------------- TTM 어댑터 ----------------


def test_strip_ttm_suffix_only_when_suffix():
    row = {"evToEBITDATTM": 5.0, "marketCap": 10, "symbol": "X", "TTM": 1, "currentRatioTTM": 2.0}
    out = strip_ttm(row)
    assert out["evToEBITDA"] == 5.0 and out["currentRatio"] == 2.0
    assert out["marketCap"] == 10 and out["symbol"] == "X"
    assert out["TTM"] == 1   # 접미사만 있는 키는 그대로


def test_merge_ttm_key_metrics_wins_on_conflict():
    merged = merge_ttm({"currentRatioTTM": 1.0, "evToEBITDATTM": 5.0},
                       {"currentRatioTTM": 9.0, "priceToBookRatioTTM": 2.0})
    assert merged["currentRatio"] == 1.0
    assert merged["priceToBookRatio"] == 2.0
    assert merge_ttm(None, None) == {}


def test_real_fixture_has_expected_ttm_keys():
    """실응답 확인 — priceToBook/grossProfit 는 ratios-ttm 에만, 나머지는 key-metrics-ttm."""
    km = _TTM["EOG"]["key-metrics-ttm"][0]
    rt = _TTM["EOG"]["ratios-ttm"][0]
    assert "priceToBookRatioTTM" not in km and "priceToBookRatioTTM" in rt
    assert "grossProfitMarginTTM" not in km and "grossProfitMarginTTM" in rt
    assert "evToEBITDATTM" in km and "earningsYieldTTM" in km


def test_ttm_usable_requires_enough_finite_core_keys():
    assert ttm_usable({}) is False
    assert ttm_usable({"marketCap": 1e9, "symbol": "NEW"}) is False   # 신규상장: marketCap 만
    assert ttm_usable({"evToEBITDA": 5.0, "evToSales": 2.0}) is False  # 2개 < 3
    assert ttm_usable({"evToEBITDA": 5.0, "evToSales": 2.0, "earningsYield": 0.0}) is True  # 정당한 0 포함
    assert ttm_usable({"evToEBITDA": float("nan"), "evToSales": None,
                       "earningsYield": "N/A", "returnOnEquity": True}) is False
    assert ttm_usable({"evToEBITDA": float("inf"), "evToSales": 1, "earningsYield": 1}) is False


# ---------------- latest_fundamentals 폴백 / _basis ----------------


def _annual_frames(monkeypatch, km=None, rt=None):
    monkeypatch.setattr(screener, "fetch_key_metrics",
                        lambda t, **k: pd.DataFrame([km] if km else []))
    monkeypatch.setattr(screener, "fetch_ratios",
                        lambda t, **k: pd.DataFrame([rt] if rt else []))


def test_latest_fundamentals_falls_back_to_annual_when_ttm_empty(monkeypatch):
    monkeypatch.setattr(screener, "fetch_key_metrics_ttm", lambda t: {})
    monkeypatch.setattr(screener, "fetch_ratios_ttm", lambda t: {})
    _annual_frames(monkeypatch, km={"evToEBITDA": 7.0, "returnOnEquity": 0.12},
                   rt={"grossProfitMargin": 0.4, "returnOnEquity": 0.99})
    m = latest_fundamentals("OLD")
    assert m["_basis"] == "annual"
    assert m["evToEBITDA"] == 7.0 and m["grossProfitMargin"] == 0.4
    assert m["returnOnEquity"] == 0.12   # key-metrics 우선


def test_latest_fundamentals_falls_back_when_ttm_not_usable(monkeypatch):
    """marketCap 만 있는 신규상장 TTM 행 -> 조용한 0점 대신 연간 폴백."""
    monkeypatch.setattr(screener, "fetch_key_metrics_ttm", lambda t: {"symbol": "NEW", "marketCap": 2e9})
    monkeypatch.setattr(screener, "fetch_ratios_ttm", lambda t: {})
    _annual_frames(monkeypatch, km={"evToEBITDA": 7.0, "evToSales": 2.0, "earningsYield": 0.05})
    m = latest_fundamentals("NEW")
    assert m["_basis"] == "annual" and m["evToEBITDA"] == 7.0


def test_latest_fundamentals_annual_basis_skips_ttm(monkeypatch):
    def boom(t):
        raise AssertionError("basis='annual' 인데 TTM 을 호출함")
    monkeypatch.setattr(screener, "fetch_key_metrics_ttm", boom)
    monkeypatch.setattr(screener, "fetch_ratios_ttm", boom)
    _annual_frames(monkeypatch, km={"evToEBITDA": 7.0})
    assert latest_fundamentals("X", basis="annual")["_basis"] == "annual"


def test_latest_fundamentals_both_empty_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(screener, "fetch_key_metrics_ttm", lambda t: {})
    monkeypatch.setattr(screener, "fetch_ratios_ttm", lambda t: {})
    _annual_frames(monkeypatch)
    m = latest_fundamentals("GONE")
    assert m == {}
    assert has_fundamentals(m) is False   # screen_one/_compute_enrich 가 skip 판정


def test_basis_marker_does_not_count_as_fundamentals():
    assert has_fundamentals({"_basis": "ttm"}) is False


def test_screen_one_uses_ttm_by_default(monkeypatch):
    monkeypatch.setattr(screener, "fetch_quote",
                        lambda t: {"price": 144.21, "name": "EOG", "marketCap": 7.6e10,
                                   "lastAnnualDividend": 4.08})
    _patch_ttm(monkeypatch, "EOG")
    row = screener.screen_one("EOG")
    assert row is not None
    assert row["health_score"] == 88   # TTM 건전성 골든 값


# ---------------- 금융 중립화 ----------------


def test_is_balance_sheet_financial_rules():
    assert is_balance_sheet_financial("Financial Services", "Banks - Diversified") is True
    assert is_balance_sheet_financial("Financial Services", "Insurance - Reinsurance") is True
    assert is_balance_sheet_financial("Financial Services", "Asset Management") is True
    # 자산경량 예외 — 정상 지표 적용
    assert is_balance_sheet_financial("Financial Services", "Financial - Data & Stock Exchanges") is False
    assert is_balance_sheet_financial("Financial Services", "Insurance - Brokers") is False
    assert is_balance_sheet_financial("Technology", "Software") is False
    assert is_balance_sheet_financial(None, None) is False
    assert is_balance_sheet_financial("", "Banks") is False


def test_financial_health_neutralizes_everything_but_roe():
    card = health_scorecard(GOOD, is_fin=True)
    for label, mx in (("매출총이익률", 25), ("ROIC", 20), ("순부채/EBITDA", 20),
                      ("이익의 질", 10), ("유동비율", 5)):
        c = _comp(card, label)
        assert c.points == mx * 0.5 and c.max_points == mx
        assert c.detail == "금융중립"
    roe = _comp(card, "ROE")
    assert roe.detail != "금융중립" and roe.points > 0
    # 건전성 최대 60 = 중립 40 + ROE 20
    assert health_scorecard(dict(GOOD, returnOnEquity=0.50), is_fin=True).total == 60
    assert sum(c.max_points for c in card.components) == 100   # 구조 불변


def test_financial_health_ignores_garbage_inputs():
    """HG: ROIC 92.8%·currentRatio 0 이어도 금융이면 영향 없음."""
    junk = dict(GOOD, returnOnInvestedCapital=0.928, currentRatio=0, netDebtToEBITDA=-9.0)
    assert health_scorecard(junk, is_fin=True).total == health_scorecard(GOOD, is_fin=True).total


def test_financial_value_neutralizes_multiples_and_fcf():
    quote = {"price": 100.0, "lastAnnualDividend": 2.0}
    card = value_scorecard(quote, GOOD, is_fin=True)
    for label, mx in (("EV/EBITDA", 25), ("EV/Sales", 15), ("FCF수익률", 10)):
        c = _comp(card, label)
        assert c.points == mx * 0.5 and c.detail == "금융중립"
    assert _comp(card, "PBR").detail != "금융중립"
    assert _comp(card, "이익수익률").detail != "금융중립"
    # 최대 75: 중립 25 + 실측(PBR 15 + EY 25 + 배당 10) = 75 상한
    best = dict(GOOD, priceToBookRatio=0.1, earningsYield=0.30)
    assert value_scorecard({"price": 10.0, "lastAnnualDividend": 5.0}, best, is_fin=True).total <= 75


def test_non_financial_not_neutralized_by_default():
    card = health_scorecard(GOOD)
    assert all(c.detail != "금융중립" for c in card.components)
    card = value_scorecard({"price": 100.0, "sector": "Industrials", "industry": "Machinery"}, GOOD)
    assert all(c.detail != "금융중립" for c in card.components)


def test_explicit_is_fin_false_overrides_quote_sector():
    bank = {"price": 50.0, "sector": "Financial Services", "industry": "Banks"}
    card = value_scorecard(bank, GOOD, is_fin=False)
    assert _comp(card, "EV/Sales").detail != "금융중립"


# ---------------- 이상치 격리 ----------------


def test_earnings_yield_outlier_neutral_in_any_sector():
    quote = {"price": 100.0, "sector": "Industrials", "industry": "x"}
    card = value_scorecard(quote, dict(GOOD, earningsYield=1.10), is_fin=False)   # MGR 연간 110%
    ey = _comp(card, "이익수익률")
    assert ey.points == 12.5 and "이상치" in ey.detail and "→중립" in ey.detail
    # 경계: 정확히 35% 는 실측 (초과만 이상치)
    edge = _comp(value_scorecard(quote, dict(GOOD, earningsYield=0.35), is_fin=False), "이익수익률")
    assert "이상치" not in edge.detail and edge.points == 25.0


def test_fcf_yield_outlier_neutral_in_any_sector():
    quote = {"price": 100.0, "sector": "Industrials", "industry": "x"}
    card = value_scorecard(quote, dict(GOOD, freeCashFlowYield=1.54), is_fin=False)
    fcf = _comp(card, "FCF수익률")
    assert fcf.points == 5.0 and "이상치" in fcf.detail
    edge = _comp(value_scorecard(quote, dict(GOOD, freeCashFlowYield=0.40), is_fin=False), "FCF수익률")
    assert "이상치" not in edge.detail and edge.points == 10.0


def test_negative_fcf_yield_scores_zero_not_neutral():
    quote = {"price": 100.0, "sector": "Industrials", "industry": "x"}
    fcf = _comp(value_scorecard(quote, dict(GOOD, freeCashFlowYield=-0.18), is_fin=False), "FCF수익률")
    assert fcf.points == 0.0 and "이상치" not in fcf.detail


def test_negative_earnings_yield_scores_zero():
    quote = {"price": 100.0, "sector": "Industrials", "industry": "x"}
    ey = _comp(value_scorecard(quote, dict(GOOD, earningsYield=-0.05), is_fin=False), "이익수익률")
    assert ey.points == 0.0


# ---------------- 유동비율 결측/0 중립 ----------------


@pytest.mark.parametrize("cr", [None, 0, 0.0, -1.0, "N/A", float("nan")])
def test_missing_or_zero_current_ratio_is_neutral(cr):
    c = _comp(health_scorecard(dict(GOOD, currentRatio=cr)), "유동비율")
    assert c.points == 2.5 and c.max_points == 5 and "중립" in c.detail


def test_missing_current_ratio_key_is_neutral():
    metrics = {k: v for k, v in GOOD.items() if k != "currentRatio"}
    assert _comp(health_scorecard(metrics), "유동비율").points == 2.5


def test_real_current_ratio_still_scored():
    c = _comp(health_scorecard(dict(GOOD, currentRatio=2.0)), "유동비율")
    assert c.points == pytest.approx(3.34) and "중립" not in c.detail
    c = _comp(health_scorecard(dict(GOOD, currentRatio=0.3)), "유동비율")
    assert 0 < c.points < 1


# ---------------- 라벨 정정 (SCORE-11) · 음수/0 가드 회귀 ----------------


def test_gross_margin_label_corrected():
    labels = [c.label for c in health_scorecard(GOOD).components]
    assert "매출총이익률" in labels
    assert not any("Novy" in lbl or "(GP)" in lbl for lbl in labels)


def test_zero_ebitda_and_pbr_guards_hold_with_new_flags():
    quote = {"price": 100.0, "sector": "Industrials", "industry": "x"}
    z = value_scorecard(quote, {"evToEBITDA": 0.0, "priceToBookRatio": 0.0}, is_fin=False)
    assert _comp(z, "EV/EBITDA").points == 0.0
    assert _comp(z, "PBR").points == 0.0
    # 적자기업(음수 배수)이 금융 아님 -> 만점 아님
    neg = value_scorecard(quote, {"evToEBITDA": -40.0, "priceToBookRatio": -2.0}, is_fin=False)
    assert _comp(neg, "EV/EBITDA").points == 0.0 and _comp(neg, "PBR").points == 0.0


def test_loss_making_real_fixture_not_rewarded():
    """CALM(FCF -1%)·JPM(FCF -18%) 실데이터: 음수 FCF 수익률은 0점."""
    for sym in ("CALM", "JPM"):
        m = merge_ttm(_TTM[sym]["key-metrics-ttm"][0], _TTM[sym]["ratios-ttm"][0])
        assert m["freeCashFlowYield"] < 0
        quote = _GOLD["inputs"][sym]
        assert _comp(value_scorecard(quote, m, is_fin=False), "FCF수익률").points == 0.0


def test_score_total_is_int_and_bounded():
    for sym in _TTM:
        m = merge_ttm(_TTM[sym]["key-metrics-ttm"][0], _TTM[sym]["ratios-ttm"][0])
        for is_fin in (False, True):
            h = health_scorecard(m, is_fin=is_fin)
            v = value_scorecard(_GOLD["inputs"][sym], m, is_fin=is_fin)
            assert isinstance(h.total, int) and 0 <= h.total <= 100
            assert isinstance(v.total, int) and 0 <= v.total <= 100
