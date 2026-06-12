"""
scripts/check_korea.py
======================
한국 월간 수출/수입/무역수지 + 전년동월대비 증감률.

한국은 글로벌 IT 하드웨어/반도체 공급망의 핵심이라, 한국 수출 둔화는
반도체 사이클의 '탄광 속 카나리아' 역할을 합니다.

실행 (FRED 키만 있으면 됨):
    source .venv/bin/activate
    python scripts/check_korea.py
"""

from __future__ import annotations


import pandas as pd

from src.data_fetcher import fetch_korea_trade
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    print("=" * 70)
    print("AI-Investment-Bot · 한국 무역통계 (FRED · OECD 출처)")
    print("=" * 70)

    df = fetch_korea_trade()
    if df.empty:
        logger.error("한국 무역통계를 가져오지 못했습니다. FRED_API_KEY 확인 필요")
        return

    df = df.dropna(how="all")

    # 최근 12개월
    recent = df.tail(12)

    print(f"\n최근 12개월 추이 (단위: USD)\n" + "-" * 70)
    print(f"{'월':<10} {'수출':>15} {'수입':>15} {'무역수지':>15}")
    for date, row in recent.iterrows():
        exp = row.get("수출(금액, USD)")
        imp = row.get("수입(금액, USD)")
        bal = row.get("무역수지(USD)")
        exp_s = f"${exp/1e9:>10,.2f} B" if exp else "—"
        imp_s = f"${imp/1e9:>10,.2f} B" if imp else "—"
        bal_s = f"${bal/1e9:>+10,.2f} B" if bal else "—"
        print(f"{str(date.date()):<10} {exp_s:>15} {imp_s:>15} {bal_s:>15}")

    # 전년동월대비 증감률 (가장 최근 월)
    if len(df) >= 13:
        latest = df.iloc[-1]
        year_ago = df.iloc[-13]
        print(f"\n전년동월대비 증감률 (vs {year_ago.name.date()})\n" + "-" * 70)
        for col in df.columns:
            cur = latest[col]
            prev = year_ago[col]
            if pd.notna(cur) and pd.notna(prev) and prev != 0:
                change = (cur / prev - 1) * 100
                print(f"  {col:<22} {change:+.2f}%")

    print("\n해석 힌트:")
    print("  · 수출 YoY 가 마이너스면 글로벌 수요 둔화 신호 — NVDA·반도체株 주의")
    print("  · 수출 YoY 가 +20% 넘으면 IT 사이클 호황 진입 신호")


if __name__ == "__main__":
    main()
