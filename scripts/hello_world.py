"""
scripts/hello_world.py
======================
가장 먼저 실행해볼 스크립트.

CPNG / NVDA / 비트코인 / 금의 최근 6개월 종가를 받아와 콘솔에 출력합니다.
이게 한 번 정상 동작하면, Phase 1 의 데이터 파이프라인 절반이 끝난 셈입니다.

실행:
    source .venv/bin/activate
    python scripts/hello_world.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 프로젝트 루트를 import 경로에 추가 (scripts/ 에서 src/ 를 부르려고)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_fetcher import fetch_fundamentals, fetch_prices  # noqa: E402
from src.exceptions import DataFetchError  # noqa: E402
from src.logger import get_logger  # noqa: E402
from src.utils import close_series  # noqa: E402

logger = get_logger(__name__)

WATCHLIST = {
    "CPNG": "쿠팡",
    "NVDA": "엔비디아",
    "BTC-USD": "비트코인",
    "GC=F": "금 선물",
}


def main() -> None:
    print("=" * 60)
    print("AI-Investment-Bot · Hello World")
    print("=" * 60)

    for ticker, korean_name in WATCHLIST.items():
        print(f"\n[{ticker}] {korean_name}")
        print("-" * 60)

        try:
            df = fetch_prices(ticker, period="6mo")
            closes = close_series(df)
            print("최근 3 거래일 종가:")
            for date, price in closes.tail(3).items():
                print(f"  {date.date()}  ${float(price):>10,.2f}")

            change = (
                float(closes.iloc[-1]) / float(closes.iloc[0]) - 1
            ) * 100
            print(f"6개월 누적 수익률: {change:+.2f}%")

            # 주식만 펀더멘털 출력 (BTC, 금 선물은 .info 가 비어있음)
            if "-USD" not in ticker and "=" not in ticker:
                f = fetch_fundamentals(ticker)
                mcap = f["market_cap"]
                pe = f["forward_pe"]
                roe = f["return_on_equity"]
                mcap_str = f"${mcap:,}" if isinstance(mcap, (int, float)) else "N/A"
                pe_str = f"{pe:.2f}" if isinstance(pe, (int, float)) else "N/A"
                roe_str = f"{roe*100:.2f}%" if isinstance(roe, (int, float)) else "N/A"
                print(f"시가총액: {mcap_str}  |  PER(예상): {pe_str}  |  ROE: {roe_str}")
        except DataFetchError as e:
            logger.warning("Ticker '%s' 처리 스킵 — %s", ticker, e)
        except Exception:
            # 예상치 못한 예외 — 풀 트레이스백 기록 후 다음 티커로
            logger.exception("Ticker '%s' 처리 중 예상치 못한 예외", ticker)

    print("\n" + "=" * 60)
    print("끝. 여기까지 동작하면 Phase 1 의 절반은 완성된 겁니다.")
    print("=" * 60)


if __name__ == "__main__":
    main()
