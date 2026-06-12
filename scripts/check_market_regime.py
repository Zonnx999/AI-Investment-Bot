"""
scripts/check_market_regime.py
==============================
매일 아침 한 번 돌려보는 시장 조망 대시보드.

출력:
  1) 자산군별 6개월 누적 수익률
  2) 자산 간 상관관계 매트릭스
  3) 자산별 연환산 변동성
  4) 거시 지표 기반 시장 국면 (Risk-on / Risk-off)

실행:
    source .venv/bin/activate
    python scripts/check_market_regime.py
"""

from __future__ import annotations


import pandas as pd

from src.logger import get_logger
from src.macro_analyzer import market_summary

logger = get_logger(__name__)

# pandas 출력 폭 늘리기 (상관관계 매트릭스가 잘리지 않게)
pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 20)
pd.set_option("display.float_format", lambda x: f"{x:>7.3f}")


def main() -> None:
    print("=" * 78)
    print(" AI-Investment-Bot · 시장 조망 대시보드")
    print("=" * 78)

    summary = market_summary(period="6mo")

    # ----- 1. 누적 수익률 -----
    print("\n[1] 자산군별 6개월 누적 수익률")
    print("-" * 78)
    cr = summary["cumulative_returns_pct"].sort_values(ascending=False)
    for asset, ret in cr.items():
        bar = "█" * max(0, int(abs(ret) / 2))
        sign = "+" if ret >= 0 else "-"
        print(f"  {asset:<18} {sign}{abs(ret):>6.2f}%   {bar}")

    # ----- 2. 상관관계 -----
    print("\n[2] 자산 간 상관관계 (일별 수익률 기준)")
    print("-" * 78)
    corr = summary["correlation"]
    print(corr.to_string())
    print()
    print("  해석: 1.00 에 가까울수록 같은 방향, -1.00 은 반대 방향, 0 은 무관.")
    print("  포트폴리오는 음의/낮은 상관 자산을 섞을수록 분산 효과가 커집니다.")

    # ----- 3. 변동성 + 샤프 + 현재 낙폭 (한 표로) -----
    print("\n[3] 위험·수익 요약  (변동성 / 샤프비율 / 현재 낙폭)")
    print("-" * 78)
    print(f"  {'자산':<18} {'변동성':>10} {'샤프':>8} {'현재낙폭':>10}")
    vol = summary["annualized_vol_pct"]
    sharpe = summary["sharpe_ratio"]
    dd = summary["current_drawdown_pct"]
    for asset in vol.sort_values(ascending=False).index:
        print(
            f"  {asset:<18} "
            f"{vol[asset]:>9.2f}% "
            f"{sharpe[asset]:>8.2f} "
            f"{dd[asset]:>9.2f}%"
        )
    print()
    print("  · 샤프 1.0 이상 = 우수, 2.0 이상 = 매우 우수")
    print("  · 현재낙폭이 -10% 넘는 자산은 변동성 국면 진입 가능성")

    # ----- 4. 시장 국면 -----
    print("\n[4] 거시 지표 기반 시장 국면")
    print("-" * 78)
    print(summary["regime"])

    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
