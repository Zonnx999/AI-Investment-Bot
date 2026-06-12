"""
scripts/check_risk.py
=====================
단일 종목의 종합 리스크 리포트.

출력 4섹션:
  1) VaR / CVaR — 일별 통계적 최악 손실
  2) 최대 낙폭 (MDD) — 과거 기간 중 가장 큰 낙폭과 회복 여부
  3) Monte Carlo — N일 후 가격 분포 (10,000 경로)
  4) Scenario Analysis — 매출/마진/멀티플 충격 시 가격 영향

실행:
    source .venv/bin/activate
    python scripts/check_risk.py CPNG
    python scripts/check_risk.py NVDA
    python scripts/check_risk.py NVDA --days 60
"""

from __future__ import annotations

import argparse


from src.logger import get_logger
from src.risk_engine import risk_report, scenario_impact

logger = get_logger(__name__)


# 시나리오 프리셋 — "이 정도 사건이면 어떻게 빠질까" 직관 키우는 용도
SCENARIO_PRESETS = [
    {
        "name": "🟢 가벼운 실적 미스",
        "revenue_shock_pct": -3,
        "margin_shock_pp": -0.5,
        "multiple_shock_pct": -10,
    },
    {
        "name": "🟡 중간 악재 (CEO 사임 / 1회성 손실)",
        "revenue_shock_pct": -7,
        "margin_shock_pp": -1.5,
        "multiple_shock_pct": -20,
    },
    {
        "name": "🟠 심각 (대형 데이터 유출 / 규제 제재)",
        "revenue_shock_pct": -15,
        "margin_shock_pp": -2.5,
        "multiple_shock_pct": -30,
    },
    {
        "name": "🔴 디재스터 (회계 부정 / 핵심 사업 붕괴)",
        "revenue_shock_pct": -30,
        "margin_shock_pp": -5,
        "multiple_shock_pct": -50,
    },
]


def main(ticker: str, days: int) -> None:
    print("=" * 78)
    print(f" {ticker} · 종합 리스크 리포트 (Edward Thorp 스타일)")
    print("=" * 78)

    rep = risk_report(ticker, mc_days=days)

    px = rep["current_price"]
    print(f"\n현재가: ${px:,.2f}    연환산 변동성: {rep['annualized_vol_pct']:.1f}%")

    # ---------- 1. VaR / CVaR ----------
    print("\n[1] 일별 VaR / CVaR (최근 2년 분포)")
    print("-" * 78)
    print(f"  {'지표':<32} {'%':>10}    의미")
    print(
        f"  {'95% Historical VaR':<32} {rep['var_95_hist_pct']:>9.2f}%  "
        f"  하위 5% 손실 컷오프 (실제 분포)"
    )
    print(
        f"  {'95% Parametric VaR':<32} {rep['var_95_param_pct']:>9.2f}%  "
        f"  정규분포 가정 (보통 더 낙관적)"
    )
    print(
        f"  {'95% Expected Shortfall':<32} {rep['es_95_pct']:>9.2f}%  "
        f"  하위 5% 의 평균 손실"
    )
    print(
        f"  {'99% Historical VaR':<32} {rep['var_99_hist_pct']:>9.2f}%  "
        f"  100일에 1번 일어날 손실"
    )
    print(
        f"  {'99% Expected Shortfall':<32} {rep['es_99_pct']:>9.2f}%  "
        f"  하위 1% 의 평균 손실 (꼬리)"
    )
    print()
    print("  · Historical 과 Parametric 차이가 크면 fat-tail (정규분포로 안 잡힘)")
    print("  · ES > VaR 차이가 크면 극단 손실이 평균보다 훨씬 깊음")

    # ---------- 2. Max Drawdown ----------
    print("\n[2] 최대 낙폭 (Max Drawdown)")
    print("-" * 78)
    dd = rep["max_drawdown"]
    print(f"  최대 낙폭:      {dd.max_dd_pct:>+7.2f}%")
    print(f"  고점 → 저점:    {dd.peak_date.date()} → {dd.trough_date.date()}  "
          f"({dd.duration_days}일)")
    if dd.recovery_date is not None:
        print(f"  저점 → 회복:    {dd.trough_date.date()} → {dd.recovery_date.date()}  "
              f"({dd.recovery_days}일)")
    else:
        print("  회복:            아직 회복하지 못함 ⚠️")

    # ---------- 3. Monte Carlo ----------
    mc = rep["monte_carlo"]
    print(f"\n[3] Monte Carlo 시뮬레이션 ({mc.days_forward}일 후, "
          f"{mc.n_paths:,} 경로, GBM 가정)")
    print("-" * 78)
    print(mc.summary())
    print()
    print("  · P05 = 비관 시나리오 (5% 확률로 이보다 더 나쁨)")
    print("  · P50 = 기대값 / P95 = 낙관 시나리오")

    # ---------- 4. Scenario Analysis ----------
    print("\n[4] 시나리오 분석 — 악재 카테고리별 가격 영향")
    print("-" * 78)
    print(f"  {'시나리오':<36} {'매출':>7} {'마진':>7} {'멀티플':>8} {'예상 주가':>11} {'변화':>8}")
    for sc in SCENARIO_PRESETS:
        result = scenario_impact(
            current_price=px,
            revenue_shock_pct=sc["revenue_shock_pct"],
            margin_shock_pp=sc["margin_shock_pp"],
            multiple_shock_pct=sc["multiple_shock_pct"],
            current_operating_margin_pct=5,  # 일반 가정. 실제는 종목별로 조정
        )
        print(
            f"  {sc['name']:<36} "
            f"{sc['revenue_shock_pct']:>+5.0f}% "
            f"{sc['margin_shock_pp']:>+5.1f}p "
            f"{sc['multiple_shock_pct']:>+6.0f}% "
            f"  ${result['new_price']:>8,.2f}  "
            f"{result['price_change_pct']:>+6.1f}%"
        )
    print()
    print("  · 매출·마진·멀티플 셋이 동시에 작용 — 디재스터 시나리오의 무서움")
    print("  · 영업마진 가정은 5% 고정. 종목별 실제 마진 반영하면 더 정확")

    print("\n" + "=" * 78)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="종목별 리스크 리포트")
    parser.add_argument("ticker", help="티커 (예: CPNG, NVDA, BTC-USD)")
    parser.add_argument("--days", type=int, default=90, help="Monte Carlo 미래 일수 (기본 90)")
    args = parser.parse_args()
    main(args.ticker.upper(), args.days)
