"""
scripts/check_macro.py
======================
FRED 에서 주요 거시 지표 5종을 받아와 가장 최근 값과 1년 전 대비 변화를 출력.

실행 전:
1. https://fredaccount.stlouisfed.org/apikeys 에서 API 키 무료 발급
2. 프로젝트 루트의 .env 에 FRED_API_KEY=... 추가

실행:
    source .venv/bin/activate
    python scripts/check_macro.py
"""

from __future__ import annotations


import pandas as pd

from src.data_fetcher import FRED_SERIES, fetch_macro_dashboard  # noqa: E402, F401
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    print("=" * 70)
    print("AI-Investment-Bot · 거시 지표 대시보드 (FRED)")
    print("=" * 70)

    df = fetch_macro_dashboard()

    if df.empty:
        logger.error("FRED 지표를 하나도 못 가져왔습니다. .env 의 FRED_API_KEY 확인 필요")
        return

    print(f"\n{'지표':<28} {'최신일자':<12} {'최신값':>12} {'1년전':>12} {'변화':>12}")
    print("-" * 80)

    for col in df.columns:
        s = df[col].dropna()
        if s.empty:
            continue

        latest_date = s.index[-1].date()
        latest_val = s.iloc[-1]

        # 1년 전 값: 1년 전 날짜 이전의 가장 마지막 관측
        one_year_ago_date = s.index[-1] - pd.Timedelta(days=365)
        prior = s[s.index <= one_year_ago_date]
        prior_val = prior.iloc[-1] if not prior.empty else None

        if prior_val is not None and prior_val != 0:
            change = (latest_val - prior_val) / abs(prior_val) * 100
            change_str = f"{change:+.2f}%"
        else:
            change_str = "N/A"

        prior_str = f"{prior_val:.2f}" if prior_val is not None else "N/A"
        print(
            f"{col:<28} {str(latest_date):<12} "
            f"{latest_val:>12.2f} {prior_str:>12} {change_str:>12}"
        )

    print("\n해석 힌트:")
    print("  · 장단기 금리차가 음수(-) 면 경기침체 신호로 자주 인용")
    print("  · 주간 실업수당 청구 급등 = 노동시장 악화 선행지표")
    print("  · 하이일드 스프레드 급등 = 신용 경색, 위험자산 회피 신호")


if __name__ == "__main__":
    main()
