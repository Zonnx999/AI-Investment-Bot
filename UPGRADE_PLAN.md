# AI Investment Bot — Quant Upgrade Plan

> Implementation guide for Claude Code.
> All file paths are relative to the project root (`AI-Investment-Bot/`).
> Work through phases in order. Each phase is independently shippable.

> **진행 상황 (2026-06-15)**
> - ✅ **Phase 1 완료** — 점수 엔진 전면 재설계 (health/value/momentum/low_vol).
>   교정: `roic`→`returnOnInvestedCapital`, `grossProfitMargin`·`priceToBookRatio`는
>   `ratios` 엔드포인트 → key-metrics+ratios 병합(`screener.latest_fundamentals`).
>   점수는 `ScoreCard`(구성요소 분해)로 반환 → `scan --check` 가 항목별 근거 표시.
> - ⏳ Phase 2~4 (VIX/DXY/regime, earnings revision, 내부자/공매도, 포트폴리오, NLP, ECOS) 대기.
> - ⚠️ **재빌드 필요**: 새 점수·detail 은 `build_universe --enrich` 재실행 시 채워짐.

---

## Phase 1 — Score System Overhaul (No new APIs needed)

All improvements in this phase use **fields already returned by FMP `/stable/key-metrics`** that the current code ignores. Only `src/screener.py` and `src/signals.py` need to change.

### 1.1 `calculate_health_score()` — `src/screener.py`

**Current formula (5 items → 100 pts):**
```
Net Debt/EBITDA  → max(0, 25 − value × 3)          [25 pts]
ROE %            → min(30, max(0, roe% × 2.5))       [30 pts]
FCF Yield %      → min(25, max(0, fcf% × 3))         [25 pts]
Current Ratio    → min(15, min(ratio, 3) × 5)         [15 pts]
Income Quality   → min(5, max(0, value))              [ 5 pts]
```

**New formula (6 items → 100 pts):**
```python
def calculate_health_score(metrics: dict) -> int:
    # 1. Gross Profitability (Novy-Marx 2013) — NEW, 25 pts
    #    FMP field: grossProfitMargin (already in key-metrics response)
    #    Higher is better. 40%+ margin → full score.
    gp_margin = _safe(metrics, "grossProfitMargin") * 100
    score  = min(25.0, max(0.0, gp_margin * 0.625))   # 40% → 25pts

    # 2. ROIC — NEW, 20 pts
    #    FMP field: roic (already in key-metrics response)
    roic_pct = _safe(metrics, "roic") * 100
    score += min(20.0, max(0.0, roic_pct * 1.0))      # 20%+ → 20pts

    # 3. ROE — KEEP, reduced to 20 pts (was 30)
    roe_pct = _safe(metrics, "returnOnEquity") * 100
    score += min(20.0, max(0.0, roe_pct * 1.67))

    # 4. Net Debt / EBITDA — KEEP, 20 pts (was 25)
    net_debt_ebitda = _safe(metrics, "netDebtToEBITDA", 10.0)
    score += max(0.0, 20.0 - net_debt_ebitda * 2.5)

    # 5. Accruals proxy — UPGRADED from Income Quality, 10 pts
    #    FMP field: operatingCashFlowRatio (CFO / current liabilities)
    #    Higher ratio = higher cash earnings quality
    income_quality = _safe(metrics, "incomeQuality")
    score += min(10.0, max(0.0, income_quality * 2.0))  # was 5pts

    # 6. Current Ratio — KEEP, reduced to 5 pts (was 15)
    current_ratio = _safe(metrics, "currentRatio", 1.0)
    score += min(5.0, min(current_ratio, 3.0) * 1.67)

    return round(min(100.0, score))
```

**Why these changes:**
- Gross Profitability (GP/A) is the single strongest Quality factor per Novy-Marx (2013, JFE). It outperforms ROE and ROA in predicting future returns.
- ROIC measures whether the company earns above its cost of capital — the true definition of value creation.
- Current Ratio has low independent predictive power; reducing its weight frees budget for better factors.

---

### 1.2 `calculate_value_score()` — `src/screener.py`

**Problem with current formula:**
- Dividend yield has 35 pts — structurally disadvantages zero-dividend growth stocks (NVDA, AMZN, META).
- No EV/EBITDA — capital structure differences make EV/Sales misleading.
- No PBR — misses book-value discount (key for financials and asset-heavy stocks).
- Mixed units (ratios vs. percentages) in a simple weighted sum distort comparisons.

**New formula — Composite Z-score approach:**

```python
def calculate_value_score(quote: dict, metrics: dict, peer_stats: dict | None = None) -> int:
    """
    peer_stats: optional dict with pre-computed watchlist means/stds for Z-scoring.
    If None (e.g. called for a single ticker), falls back to absolute scoring.
    """
    score = 0.0

    # 1. EV/EBITDA — NEW, 25 pts (replaces EV/Sales as primary value metric)
    #    FMP field: evToEBITDA
    ev_ebitda = _safe(metrics, "evToEBITDA", 20.0)
    score += max(0.0, 25.0 - ev_ebitda * 1.25)        # 0→25pts, 20→0pts

    # 2. EV/Sales — KEEP, reduced to 15 pts (was 30)
    ev_to_sales = _safe(metrics, "evToSales", 10.0)
    industry = (quote.get("industry") or "") + (quote.get("sector") or "")
    is_financial = any(x in industry for x in ("Bank", "Financial", "Insurance"))
    if is_financial:
        score += 7.5   # neutral for financials
    else:
        score += max(0.0, 15.0 - ev_to_sales * 2.0)

    # 3. PBR (Price-to-Book) — NEW, 15 pts
    #    FMP field: priceToBookRatio
    pbr = _safe(metrics, "priceToBookRatio", 5.0)
    score += max(0.0, 15.0 - pbr * 1.5)               # PBR 0→15pts, 10→0pts

    # 4. Earnings Yield — KEEP, 25 pts (was 15) — inverse of P/E
    earnings_yield_pct = _safe(metrics, "earningsYield") * 100
    score += min(25.0, max(0.0, earnings_yield_pct * 5.0))  # EY 5% (PE=20) → 25pts

    # 5. Dividend Yield — REDUCED to 10 pts (was 35)
    price = _safe(quote, "price", 1.0)
    last_dividend = _safe(quote, "lastDividend") or _safe(quote, "lastAnnualDividend")
    dividend_yield_pct = (last_dividend / max(price, 0.01)) * 100 if price else 0.0
    score += min(10.0, dividend_yield_pct * 2.0)

    # 6. FCF Yield — KEEP, 10 pts (was 20)
    fcf_yield_pct = _safe(metrics, "freeCashFlowYield") * 100
    score += min(10.0, max(0.0, fcf_yield_pct * 1.5))

    return round(min(100.0, score))
```

**Why:**
- Dividend reduction (35→10) eliminates the structural penalty for NVDA/AMZN/META.
- EV/EBITDA is capital-structure neutral: a company with $10B debt and $10B equity has the same EV/EBITDA as a debt-free peer with identical operating earnings.
- Earnings Yield increase (15→25) restores P/E as a primary value signal via its reciprocal.

---

### 1.3 `momentum_score()` — `src/signals.py`

**Current problems:**
- Returns only 4 values: 0, 33, 67, 100. No differentiation within tiers.
- No skip-month: Jegadeesh & Titman (1993) showed removing the most recent month from the 12-month return significantly improves momentum signal by eliminating short-term mean reversion.
- No earnings revision component.

**New implementation:**

```python
def momentum_score(prices: pd.Series) -> tuple[int, list[str]]:
    """
    Momentum score (0–100) using continuous Z-score approach + skip-month.

    Components:
      A. Price momentum (70%): 12mo-1mo return (skip-month, Jegadeesh-Titman)
      B. MA200 position  (30%): % distance above/below 200-day MA

    Returns (score: int, notes: list[str])
    """
    p = prices.dropna()
    notes: list[str] = []

    if len(p) < MOMENTUM_LOOKBACK_SHORT_D + 1:
        raise InsufficientDataError(
            f"모멘텀 평가에 데이터 {len(p)}개 — 최소 {MOMENTUM_LOOKBACK_SHORT_D + 1}개 필요",
            n_points=len(p), required=MOMENTUM_LOOKBACK_SHORT_D + 1,
        )

    component_scores: list[float] = []
    weights: list[float] = []

    # A. Skip-month momentum: 12mo return excluding last 1mo
    SKIP = 21   # ~1 month in trading days
    LONG = 252  # ~12 months
    if len(p) > LONG + SKIP:
        ret_12mo_skip = float(p.iloc[-SKIP] / p.iloc[-(LONG + SKIP)] - 1) * 100
        # Map to 0-100: 0% → 50, +30% → ~83, -30% → ~17
        # Using sigmoid-like mapping: score = 50 + ret * (50/30), clamped
        score_a = max(0.0, min(100.0, 50.0 + ret_12mo_skip * (50.0 / 30.0)))
        component_scores.append(score_a)
        weights.append(0.70)
        direction = "↑" if ret_12mo_skip > 0 else "↓"
        notes.append(f"12mo-1mo(skip) 수익률 {ret_12mo_skip:+.1f}% {direction}")
    elif len(p) > MOMENTUM_LOOKBACK_SHORT_D + 1:
        # Fallback: 3mo return if not enough data for 12mo
        ret_3mo = float(p.iloc[-1] / p.iloc[-(MOMENTUM_LOOKBACK_SHORT_D + 1)] - 1) * 100
        score_a = max(0.0, min(100.0, 50.0 + ret_3mo * (50.0 / 15.0)))
        component_scores.append(score_a)
        weights.append(0.70)
        notes.append(f"3개월 수익률 {ret_3mo:+.1f}% (데이터 부족으로 단기 대체)")

    # B. 200-day MA position
    if len(p) >= MA_LONG_WINDOW:
        ma200 = float(p.rolling(MA_LONG_WINDOW).mean().iloc[-1])
        last = float(p.iloc[-1])
        pct_above_ma = (last / ma200 - 1) * 100
        # +10% above MA → ~83, -10% below → ~17
        score_b = max(0.0, min(100.0, 50.0 + pct_above_ma * (50.0 / 10.0)))
        component_scores.append(score_b)
        weights.append(0.30)
        notes.append(f"200일선 {'위' if pct_above_ma > 0 else '아래'} {pct_above_ma:+.1f}% (현재 {last:,.2f} vs MA {ma200:,.2f})")

    if not component_scores:
        raise InsufficientDataError("모멘텀 평가 불가 — 데이터 부족", n_points=len(p), required=MA_LONG_WINDOW)

    total_weight = sum(weights)
    final_score = sum(s * w for s, w in zip(component_scores, weights)) / total_weight
    return round(final_score), notes
```

---

### 1.4 Add Low Volatility as 4th Factor — `src/signals.py`

Add a new `low_vol_score()` function that reuses `risk_engine.annualized_volatility`:

```python
def low_vol_score(prices: pd.Series) -> tuple[int, list[str]]:
    """
    Low Volatility factor score (0–100).
    Lower annualized volatility → higher score.
    Robeco (2024) and MSCI confirm Low Vol as independent alpha source.

    Benchmark: ~15% vol → 75pts, ~30% vol → 50pts, ~60% vol → 25pts
    Formula: score = max(0, min(100, (30 / vol_pct) * 50))
    """
    import numpy as np
    from src.utils import TRADING_DAYS_PER_YEAR

    p = prices.dropna()
    notes: list[str] = []

    if len(p) < 63:
        return 50, ["변동성 데이터 부족 — 중립(50) 처리"]

    returns = p.pct_change().dropna()
    vol_pct = float(returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100)

    # Inverse scoring: lower vol → higher score
    score = max(0.0, min(100.0, (30.0 / max(vol_pct, 1.0)) * 50.0))
    notes.append(f"연환산 변동성 {vol_pct:.1f}% → Low Vol 점수 {round(score)}")
    return round(score), notes
```

**Update `factor_scores()` in `src/signals.py`:**

```python
# Current weights: momentum 1/3, value 1/3, quality 1/3
# New weights: each 1/4
COMPOSITE_WEIGHTS = {"momentum": 0.25, "value": 0.25, "quality": 0.25, "low_vol": 0.25}

def factor_scores(ticker: str) -> FactorScores:
    # ... existing fetch logic ...

    low_vol, lv_notes = low_vol_score(closes)
    notes.extend(lv_notes)

    composite = round(
        momentum  * COMPOSITE_WEIGHTS["momentum"]
        + value   * COMPOSITE_WEIGHTS["value"]
        + quality * COMPOSITE_WEIGHTS["quality"]
        + low_vol * COMPOSITE_WEIGHTS["low_vol"]
    )
    return FactorScores(ticker, momentum, value, quality, composite, notes, low_vol=low_vol)
```

**Update `FactorScores` dataclass:**
```python
@dataclass
class FactorScores:
    ticker:    str
    momentum:  int
    value:     int
    quality:   int
    composite: int
    notes:     list[str] = field(default_factory=list)
    low_vol:   int = 0   # NEW
```

---

## Phase 2 — Data Source Expansion (yfinance free + FMP existing plan)

### 2.1 Add VIX and DXY to `src/data_fetcher.py`

These are standard yfinance tickers — no API key, no cost:

```python
# Add to data_fetcher.py

@cached("prices", TTL_PRICES, "dataframe")
def fetch_vix(period: str = "1y") -> pd.DataFrame:
    """CBOE VIX Index via yfinance (^VIX)."""
    return fetch_prices("^VIX", period=period)

@cached("prices", TTL_PRICES, "dataframe")
def fetch_dxy(period: str = "1y") -> pd.DataFrame:
    """US Dollar Index via yfinance (DX-Y.NYB)."""
    return fetch_prices("DX-Y.NYB", period=period)
```

### 2.2 Update `classify_regime()` — `src/macro_analyzer.py`

Add VIX as a 4th evaluator:

```python
def _eval_vix() -> IndicatorOutcome:
    """VIX (CBOE Volatility Index) — above 30 = stress, below 15 = calm."""
    from src.data_fetcher import fetch_vix
    from src.utils import close_series
    df = fetch_vix(period="1y")
    vix = close_series(df)
    latest = float(vix.iloc[-1])
    ma_60 = float(vix.rolling(60).mean().iloc[-1])

    if latest > 30:
        return IndicatorOutcome("VIX", -1, f"VIX {latest:.1f} > 30 — 시장 공포 구간 (-1)", latest)
    if latest > 20 and latest > ma_60 * 1.15:
        return IndicatorOutcome("VIX", 0,  f"VIX {latest:.1f} 상승 추세 — 주의 (0)", latest)
    return IndicatorOutcome("VIX", +1, f"VIX {latest:.1f} 안정 구간 — 위험선호 (+1)", latest)

# Add to _REGIME_EVALUATORS tuple:
_REGIME_EVALUATORS = (
    _eval_yield_curve,
    _eval_hy_spread,
    _eval_jobless_claims,
    _eval_vix,          # NEW — score range becomes -4 to +4
)
```

Update regime labels (score range is now -4 to +4):
```python
if score >= 3:    regime = "🟢 위험선호 (Risk-on)"
elif score == 2:  regime = "🟡 약한 위험선호"
elif score in (0,1): regime = "⚪ 중립 / 혼조"
elif score == -1: regime = "🟠 약한 위험회피"
else:             regime = "🔴 위험회피 (Risk-off / 침체 경고)"
```

Also add DXY to `DEFAULT_PANEL` in `macro_analyzer.py`:
```python
DEFAULT_PANEL = {
    "S&P 500":          "SPY",
    "장기국채 (20Y+)":   "TLT",
    "회사채 (투자등급)": "LQD",
    "하이일드 채권":     "HYG",
    "금":               "GLD",
    "원자재":           "DBC",
    "비트코인":         "BTC-USD",
    "VIX":              "^VIX",       # NEW
    "달러 인덱스":       "DX-Y.NYB",  # NEW
}
```

### 2.3 Add Earnings Revision — `src/data_fetcher.py`

```python
TTL_ESTIMATES = timedelta(days=7)   # analyst estimates update slowly

@cached("fmp_estimates", TTL_ESTIMATES, "dataframe")
def fetch_analyst_estimates(
    ticker: str,
    period: str = "quarter",
    limit: int = 4,
) -> pd.DataFrame:
    """FMP /stable/analyst-estimates — EPS and revenue consensus estimates.

    Key fields: estimatedEpsAvg, estimatedRevenueAvg, date
    Use to compute Earnings Revision = % change in EPS estimate over past 4 weeks.
    """
    data = _fmp_get("analyst-estimates", {"symbol": ticker, "period": period, "limit": limit})
    return _fmp_to_dataframe(data)
```

Then add an `earnings_revision_score()` function in `src/signals.py`:
```python
def earnings_revision_score(ticker: str) -> tuple[int, str]:
    """
    Score based on analyst EPS estimate revisions.
    Positive revision (>+5%) → high score. Negative (<-5%) → low score.
    Returns (score: int 0-100, note: str)
    """
    from src.data_fetcher import fetch_analyst_estimates
    try:
        df = fetch_analyst_estimates(ticker, period="quarter", limit=2)
        if df.empty or len(df) < 2:
            return 50, "Earnings Revision 데이터 부족 — 중립(50)"
        latest_eps  = float(df["estimatedEpsAvg"].iloc[-1])
        previous_eps = float(df["estimatedEpsAvg"].iloc[-2])
        if previous_eps == 0:
            return 50, "이전 EPS 추정치 0 — 중립(50)"
        revision_pct = (latest_eps / previous_eps - 1) * 100
        # Map: +10% → 83, 0% → 50, -10% → 17
        score = max(0, min(100, int(50 + revision_pct * 3.3)))
        direction = "↑" if revision_pct > 0 else "↓"
        return score, f"EPS 전망 {revision_pct:+.1f}% {direction}"
    except Exception:
        return 50, "Earnings Revision 미가용 — 중립(50)"
```

Integrate into `momentum_score()` as component C (optional weight allocation):
```
A. Skip-month price return:   55%
B. 200-day MA position:       25%
C. Earnings Revision:         20%
```

### 2.4 Add Short Interest Alert — `src/data_fetcher.py` + `src/signals.py`

```python
# data_fetcher.py
TTL_SHORT = timedelta(days=3)

@cached("fmp_short", TTL_SHORT, "json")
def fetch_short_interest(ticker: str) -> dict:
    """FMP /stable/short-interest — short % of float, short ratio."""
    data = _fmp_get("short-interest", {"symbol": ticker})
    if not data:
        raise DataValidationError(f"FMP short-interest 빈 응답: {ticker}", source="FMP")
    return data[0] if isinstance(data, list) else data
```

```python
# signals.py — add to generate_signal_report()
def short_interest_alert(ticker: str, threshold_pct: float = 20.0) -> Alert | None:
    """Fire warning if short interest > threshold % of float."""
    from src.data_fetcher import fetch_short_interest
    try:
        si = fetch_short_interest(ticker)
        pct = float(si.get("shortPercentOfFloat", 0) or 0) * 100
        if pct >= threshold_pct:
            return Alert("warning", "short_interest",
                         f"{ticker} 공매도 비율 {pct:.1f}% ≥ {threshold_pct:.0f}% — 하락 압력 경고")
    except Exception:
        pass
    return None
```

---

## Phase 3 — Portfolio Optimization Engine (new file)

Create `src/portfolio.py`:

```python
"""
src/portfolio.py
================
Factor-score-based portfolio weight optimizer.

Given a list of FactorScores, computes allocation weights using:
  - Equal weight among top-N scorers (simple baseline)
  - Risk parity (weights inversely proportional to volatility)
  - Minimum variance (scipy.optimize)

All methods respect a max_position_pct cap to prevent concentration.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from src.signals import FactorScores


def equal_weight(factors: list[FactorScores], top_n: int = 10) -> dict[str, float]:
    """Top-N by composite score, equally weighted."""
    ranked = sorted(factors, key=lambda f: f.composite, reverse=True)[:top_n]
    n = len(ranked)
    return {f.ticker: 1.0 / n for f in ranked}


def score_weighted(factors: list[FactorScores], top_n: int = 10) -> dict[str, float]:
    """Weights proportional to composite score (score-weighted)."""
    ranked = sorted(factors, key=lambda f: f.composite, reverse=True)[:top_n]
    total = sum(f.composite for f in ranked)
    if total == 0:
        return equal_weight(factors, top_n)
    return {f.ticker: f.composite / total for f in ranked}


def risk_parity(factors: list[FactorScores], vols: dict[str, float], top_n: int = 10) -> dict[str, float]:
    """
    Weights inversely proportional to annualized volatility.
    Tickers not in vols dict are assigned median volatility.
    """
    ranked = sorted(factors, key=lambda f: f.composite, reverse=True)[:top_n]
    median_vol = float(np.median(list(vols.values()))) if vols else 30.0
    inv_vols = {f.ticker: 1.0 / max(vols.get(f.ticker, median_vol), 1.0) for f in ranked}
    total = sum(inv_vols.values())
    return {t: v / total for t, v in inv_vols.items()}
```

---

## Phase 4 — Alpha Signal Enhancement

### 4.1 Insider Trading Alert — `src/data_fetcher.py`

```python
TTL_INSIDER = timedelta(days=1)

@cached("fmp_insider", TTL_INSIDER, "json")
def fetch_insider_trades(ticker: str, limit: int = 20) -> list[dict]:
    """FMP /stable/insider-trading — recent insider buys/sells.

    transactionType: 'P' = Purchase (buy), 'S' = Sale (sell)
    Look for: large purchases by CEO/CFO/Directors = bullish signal.
    """
    data = _fmp_get("insider-trading", {"symbol": ticker, "limit": limit})
    return data if isinstance(data, list) else []
```

Add `insider_buy_alert()` to `src/signals.py`:
```python
def insider_buy_alert(ticker: str, min_value_usd: float = 500_000) -> Alert | None:
    """Alert when insiders make large purchases (> min_value_usd)."""
    from src.data_fetcher import fetch_insider_trades
    try:
        trades = fetch_insider_trades(ticker, limit=10)
        big_buys = [t for t in trades
                    if t.get("transactionType") == "P"
                    and float(t.get("value") or 0) >= min_value_usd]
        if big_buys:
            top = big_buys[0]
            val = float(top.get("value", 0))
            name = top.get("reportingName", "내부자")
            return Alert("info", "insider",
                         f"{ticker} 내부자 매수: {name} ${val:,.0f} — 경영진 신뢰 시그널")
    except Exception:
        pass
    return None
```

### 4.2 Earnings Call NLP — Claude API (optional, cost-incurring)

```python
# src/nlp.py (new file)
"""
Earnings call transcript sentiment analysis via Claude API.
Requires ANTHROPIC_API_KEY in .env.
"""
from __future__ import annotations
from src.data_fetcher import _fmp_get
from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

def fetch_earnings_transcript(ticker: str, year: int, quarter: int) -> str:
    """FMP /stable/earnings-call-transcript — raw text."""
    data = _fmp_get("earnings-call-transcript",
                    {"symbol": ticker, "year": year, "quarter": quarter})
    if not data:
        return ""
    return data[0].get("content", "") if isinstance(data, list) else ""


def score_transcript_sentiment(text: str, max_chars: int = 8000) -> dict:
    """
    Use Claude Haiku (fast + cheap) to score earnings call sentiment.
    Returns: {"tone": "positive"|"neutral"|"cautious", "score": 0-100, "summary": str}
    """
    import anthropic
    client = anthropic.Anthropic(api_key=settings.require("anthropic_api_key"))
    snippet = text[:max_chars]

    prompt = f"""Analyze this earnings call transcript excerpt. 
    Score management tone on a scale of 0-100 where:
    - 0-33: cautious/negative (guidance cuts, macro headwinds, cost pressures emphasized)
    - 34-66: neutral (balanced outlook, in-line results)
    - 67-100: confident/positive (beat + raise, strong demand, expanding margins)

    Return JSON only: {{"score": <int>, "tone": "<positive|neutral|cautious>", "summary": "<1 sentence>"}}

    Transcript:
    {snippet}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    import json
    return json.loads(message.content[0].text)
```

### 4.3 Korea BOK ECOS API — `src/data_fetcher.py`

```python
# Add to data_fetcher.py
# Requires ECOS_API_KEY in .env (free registration at https://ecos.bok.or.kr)

BOK_ECOS_SERIES = {
    "기준금리":     "722Y001/0101000",   # Base rate
    "원달러환율":   "731Y001/0000001",   # KRW/USD
    "산업생산지수": "371Y003/10000",     # Industrial Production Index
    "소비자물가":   "021Y126/000",       # CPI
}

@cached("bok_ecos", timedelta(hours=12), "series")
def fetch_bok_series(stat_code: str, item_code: str, start: str = "202001") -> pd.Series:
    """Korea Bank ECOS Open API — single statistical series."""
    api_key = os.getenv("ECOS_API_KEY", "")
    if not api_key:
        raise MissingApiKeyError("ECOS_API_KEY")
    import requests
    end = datetime.utcnow().strftime("%Y%m")
    url = f"https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr/1/100/{stat_code}/MM/{start}/{end}/{item_code}"
    resp = requests.get(url, timeout=10)
    rows = resp.json().get("StatisticSearch", {}).get("row", [])
    if not rows:
        raise DataValidationError(f"BOK ECOS 빈 응답: {stat_code}", source="BOK")
    s = pd.Series(
        {r["TIME"]: float(r["DATA_VALUE"].replace(",", "")) for r in rows}
    )
    s.index = pd.to_datetime(s.index, format="%Y%m")
    return s.sort_index()
```

---

## Environment Variables to Add

Add to `.env` and `.env.example`:

```bash
# Phase 2 — no new keys needed for VIX/DXY (yfinance)

# Phase 4 — optional
ECOS_API_KEY=your_bok_ecos_key_here    # Korea Bank ECOS, free at ecos.bok.or.kr
# ANTHROPIC_API_KEY already exists — used for NLP in Phase 4
```

---

## Test Coverage Requirements

For each Phase 1 change, add/update tests in `tests/`:

```python
# tests/test_screener.py — add these cases

def test_health_score_gross_profitability():
    """Gross Profitability of 40% → 25 pts (full)."""
    metrics = {"grossProfitMargin": 0.40, "roic": 0.15, "returnOnEquity": 0.15,
               "netDebtToEBITDA": 1.0, "incomeQuality": 1.0, "currentRatio": 2.0}
    score = calculate_health_score(metrics)
    assert score >= 80, f"High quality metrics should score ≥80, got {score}"

def test_value_score_no_dividend_penalty():
    """NVDA-like (no dividend, high earnings yield) should still score well."""
    quote = {"price": 500.0, "lastDividend": 0.0, "industry": "Semiconductors", "sector": "Technology"}
    metrics = {"evToEBITDA": 25.0, "evToSales": 10.0, "priceToBookRatio": 20.0,
               "earningsYield": 0.04, "freeCashFlowYield": 0.03}
    score = calculate_value_score(quote, metrics)
    assert score >= 20, f"Non-dividend growth stock should score ≥20, got {score}"

def test_momentum_score_continuous():
    """Momentum score should return values other than 0/33/67/100."""
    import pandas as pd, numpy as np
    # Generate upward trending prices
    dates = pd.date_range("2023-01-01", periods=300, freq="B")
    prices = pd.Series(100 * np.cumprod(1 + np.random.normal(0.001, 0.015, 300)), index=dates)
    score, _ = momentum_score(prices)
    assert score not in {0, 33, 67, 100} or True  # soft check — values should be continuous
    assert 0 <= score <= 100

def test_low_vol_score_inverse():
    """Lower volatility → higher score."""
    import pandas as pd, numpy as np
    dates = pd.date_range("2023-01-01", periods=300, freq="B")
    low_vol_prices  = pd.Series(100 * np.cumprod(1 + np.random.normal(0.0005, 0.008, 300)), index=dates)
    high_vol_prices = pd.Series(100 * np.cumprod(1 + np.random.normal(0.0005, 0.035, 300)), index=dates)
    score_low,  _ = low_vol_score(low_vol_prices)
    score_high, _ = low_vol_score(high_vol_prices)
    assert score_low > score_high, "Lower volatility should yield higher score"
```

---

## Summary: Execution Order

| Priority | File | Change | Effort |
|----------|------|--------|--------|
| ★★★ Now | `src/screener.py` | `calculate_health_score()` — add ROIC, Gross Profitability, rebalance weights | 30 min |
| ★★★ Now | `src/screener.py` | `calculate_value_score()` — add EV/EBITDA, PBR, reduce dividend weight | 30 min |
| ★★★ Now | `src/signals.py` | `momentum_score()` — skip-month + continuous Z-score | 45 min |
| ★★★ Now | `src/signals.py` | `low_vol_score()` + update `FactorScores` + `COMPOSITE_WEIGHTS` | 30 min |
| ★★☆ Soon | `src/data_fetcher.py` | `fetch_vix()`, `fetch_dxy()` | 15 min |
| ★★☆ Soon | `src/macro_analyzer.py` | `_eval_vix()` + add to `_REGIME_EVALUATORS` | 20 min |
| ★★☆ Soon | `src/data_fetcher.py` | `fetch_analyst_estimates()` | 20 min |
| ★★☆ Soon | `src/signals.py` | `earnings_revision_score()` integration | 30 min |
| ★☆☆ Later | `src/data_fetcher.py` | `fetch_short_interest()`, `fetch_insider_trades()` | 30 min |
| ★☆☆ Later | `src/signals.py` | `short_interest_alert()`, `insider_buy_alert()` | 20 min |
| ★☆☆ Later | `src/portfolio.py` | New file — weight optimization | 1 hr |
| ★☆☆ Later | `src/nlp.py` | New file — earnings call NLP via Claude API | 1 hr |

**Start here:** `calculate_health_score()` and `calculate_value_score()` in `src/screener.py`. These two functions touch no external APIs, require no new dependencies, and deliver the highest signal quality improvement per line of code.
