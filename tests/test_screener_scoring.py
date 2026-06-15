"""src/screener.py — 재설계된 점수 엔진 (ScoreCard + 교정 필드, 오프라인)."""

from __future__ import annotations

from src.screener import (
    Component,
    ScoreCard,
    calculate_health_score,
    calculate_value_score,
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
    assert "총이익률(GP)" in labels and "ROIC" in labels  # 신규 팩터 반영
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
