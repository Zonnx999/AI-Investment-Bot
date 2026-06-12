"""
scripts/check_fundamentals.py
=============================
FMP API 로 종목의 5년 재무제표 + 핵심 비율 추세를 한 화면에 정리.

yfinance 의 .info 가 스냅샷이라면, 이 스크립트는 '시간에 따른 변화'를
보여줘서 펀더멘털이 개선 중인지 악화 중인지 한 번에 판단할 수 있습니다.

실행:
    source .venv/bin/activate
    python scripts/check_fundamentals.py CPNG
    python scripts/check_fundamentals.py NVDA
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from src.data_fetcher import (  # noqa: E402
    fetch_financial_statements,
    fetch_financials_yf,
    fetch_key_metrics,
)
from src.exceptions import (  # noqa: E402
    ApiAuthError,
    ApiAuthorizationError,
    DataFetchError,
)
from src.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


def _fmp_or_yf(ticker: str, fmp_statement: str, yf_statement: str):
    """FMP 시도 후 401/403 이면 yfinance 로 자동 폴백."""
    try:
        df = fetch_financial_statements(ticker, fmp_statement, limit=5)
        if not df.empty:
            return df, "FMP"
    except (ApiAuthError, ApiAuthorizationError) as e:
        logger.info("FMP %s 차단됨 — yfinance 로 폴백 (%s)", fmp_statement, e)
    except DataFetchError as e:
        logger.warning("FMP %s 호출 실패 — yfinance 로 폴백: %s", fmp_statement, e)
    except Exception:
        logger.exception("FMP %s 호출 중 예상치 못한 예외 — yfinance 로 폴백", fmp_statement)

    df = fetch_financials_yf(ticker, yf_statement)
    return df, "yfinance"


def fmt_money(value) -> str:
    """달러 금액을 B/M 단위로 보기 좋게."""
    if value is None or (isinstance(value, float) and value != value):
        return "—"
    if abs(value) >= 1e9:
        return f"${value/1e9:>8,.2f} B"
    if abs(value) >= 1e6:
        return f"${value/1e6:>8,.2f} M"
    return f"${value:>10,.0f}"


def fmt_pct(value) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "—"
    return f"{value*100:>+7.2f}%"


def fmt_ratio(value) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "—"
    return f"{value:>7.2f}"


def main(ticker: str = "CPNG") -> None:
    print("=" * 78)
    print(f" {ticker} · 펀더멘털 5년 추세 (FMP)")
    print("=" * 78)

    # 컬럼 이름은 소스에 따라 다르므로 후보 리스트로 처리.
    def pick(row, candidates):
        for c in candidates:
            if c in row.index and pd.notna(row[c]):
                return row[c]
        return None

    # --- 손익계산서 ---
    income, src = _fmp_or_yf(ticker, "income-statement", "income")
    if income.empty:
        logger.error("'%s' 데이터를 어떤 소스에서도 받지 못했습니다. 티커 확인 필요", ticker)
        return

    print(f"\n[손익계산서]  (출처: {src})")
    print("-" * 78)
    print(f"  {'연도':<8} {'매출':>14} {'영업이익':>14} {'순이익':>14} {'영업마진':>10}")
    for date, row in income.iterrows():
        rev = pick(row, ["revenue", "Total Revenue"])
        op = pick(row, ["operatingIncome", "Operating Income"])
        ni = pick(row, ["netIncome", "Net Income"])
        op_margin = (op / rev) if (rev and op is not None) else None
        print(
            f"  {date.year:<8} {fmt_money(rev):>14} {fmt_money(op):>14} "
            f"{fmt_money(ni):>14} {fmt_pct(op_margin):>10}"
        )

    # --- 현금흐름 ---
    cash, src = _fmp_or_yf(ticker, "cash-flow-statement", "cashflow")
    print(f"\n[현금흐름표]  (출처: {src})")
    print("-" * 78)
    print(f"  {'연도':<8} {'영업CF':>14} {'CapEx':>14} {'잉여CF (FCF)':>16}")
    for date, row in cash.iterrows():
        ocf = pick(row, ["operatingCashFlow", "Operating Cash Flow"])
        capex = pick(row, ["capitalExpenditure", "Capital Expenditure"])
        fcf = pick(row, ["freeCashFlow", "Free Cash Flow"])
        # yfinance 는 FCF 가 없을 수 있어 OCF + CapEx 로 계산
        if fcf is None and ocf is not None and capex is not None:
            fcf = ocf + capex  # CapEx 는 음수로 들어옴
        print(
            f"  {date.year:<8} {fmt_money(ocf):>14} {fmt_money(capex):>14} "
            f"{fmt_money(fcf):>16}"
        )

    # --- 재무상태표 ---
    bs, src = _fmp_or_yf(ticker, "balance-sheet-statement", "balance")
    print(f"\n[재무상태표]  (출처: {src})")
    print("-" * 78)
    print(f"  {'연도':<8} {'현금':>14} {'총부채':>14} {'자기자본':>14}")
    for date, row in bs.iterrows():
        cash_eq = pick(row, [
            "cashAndCashEquivalents", "cashAndShortTermInvestments",
            "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments",
        ])
        total_debt = pick(row, ["totalDebt", "Total Debt"])
        equity = pick(row, ["totalStockholdersEquity", "Stockholders Equity", "Total Equity Gross Minority Interest"])
        print(
            f"  {date.year:<8} {fmt_money(cash_eq):>14} {fmt_money(total_debt):>14} "
            f"{fmt_money(equity):>14}"
        )

    # --- 핵심 비율 (FMP 만 — yfinance 에는 시계열 없음) ---
    try:
        metrics = fetch_key_metrics(ticker, limit=5)
    except (ApiAuthError, ApiAuthorizationError) as e:
        logger.info("FMP key-metrics 접근 불가 — 핵심 비율 섹션 생략 (%s)", e)
        metrics = pd.DataFrame()

    # FMP stable 의 /key-metrics 는 P/E·P/FCF 를 직접 안 주고 yield 형태로 줍니다.
    # → 1/yield 로 역산. debt/equity 는 위에서 받아둔 재무상태표(bs)에서 계산.
    if not metrics.empty:
        print("\n[핵심 비율]  (출처: FMP, 일부는 yield 역산·BS 계산)")
        print("-" * 78)
        print(f"  {'연도':<8} {'P/E':>10} {'P/FCF':>10} {'ROE':>10} {'부채/자본':>12}")

        # bs 를 연도 기준으로 lookup 가능한 dict 로 변환 (가장 최근 항목 우선).
        bs_by_year: dict[int, pd.Series] = {}
        if not bs.empty:
            for d, r in bs.iterrows():
                bs_by_year.setdefault(d.year, r)

        for date, row in metrics.iterrows():
            ey = pick(row, ["earningsYield"])
            pe = (1.0 / ey) if (ey not in (None, 0)) else None

            fcfy = pick(row, ["freeCashFlowYield"])
            pfcf = (1.0 / fcfy) if (fcfy not in (None, 0)) else None

            roe = pick(row, ["returnOnEquity", "roe"])

            # 부채/자본 = 총부채 / 자기자본 (해당 연도 BS 행에서 계산)
            debt_eq = None
            bs_row = bs_by_year.get(date.year)
            if bs_row is not None:
                td = pick(bs_row, ["totalDebt", "Total Debt"])
                eq = pick(bs_row, [
                    "totalStockholdersEquity", "Stockholders Equity",
                    "Total Equity Gross Minority Interest",
                ])
                if td is not None and eq:
                    debt_eq = td / eq

            print(
                f"  {date.year:<8} {fmt_ratio(pe):>10} {fmt_ratio(pfcf):>10} "
                f"{fmt_pct(roe):>10} {fmt_ratio(debt_eq):>12}"
            )
    else:
        print("\n[핵심 비율]  (응답 없음, 생략)")

    print("\n" + "=" * 78)
    print("해석 힌트:")
    print("  · 매출 성장률 둔화 + 영업마진 악화 = 가격 결정력 약화 신호")
    print("  · FCF 가 음수면 사업 자체가 현금을 못 만든다는 뜻")
    print("  · ROE 하락 추세는 자본 효율성 저하 — 가치 함정 가능성")


if __name__ == "__main__":
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "CPNG"
    main(ticker)
