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
    gp = next(c for c in card.components if c.label == "총이익률(GP)")
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
