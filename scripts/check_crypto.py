"""
scripts/check_crypto.py
=======================
CoinGecko 무료 API 로 비트코인·이더리움 최근 6개월 가격을 받아 요약.

실행 (API 키 불필요):
    source .venv/bin/activate
    python scripts/check_crypto.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_fetcher import fetch_crypto  # noqa: E402

COINS = {
    "bitcoin": "BTC 비트코인",
    "ethereum": "ETH 이더리움",
}


def main() -> None:
    print("=" * 60)
    print("AI-Investment-Bot · 암호화폐 동향 (CoinGecko)")
    print("=" * 60)

    for coin_id, label in COINS.items():
        print(f"\n[{label}]")
        print("-" * 60)
        try:
            df = fetch_crypto(coin_id, days=180)
            latest = df.iloc[-1]
            first = df.iloc[0]

            change_6m = (latest["price"] / first["price"] - 1) * 100
            mcap_b = latest["market_cap"] / 1e9
            vol_b = latest["volume"] / 1e9

            print(f"  현재가:        ${latest['price']:>12,.2f}")
            print(f"  6개월 수익률:  {change_6m:>+12.2f}%")
            print(f"  시가총액:      ${mcap_b:>12,.1f} B")
            print(f"  24h 거래대금:  ${vol_b:>12,.1f} B")

            # 30일 변동성 (일별 수익률 표준편차 × √365 → 연환산)
            rets = df["price"].pct_change().dropna().tail(30)
            ann_vol = float(rets.std() * (365**0.5)) * 100
            print(f"  연환산 변동성: {ann_vol:>12.2f}%   (최근 30일 기준)")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  에러: {e}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
