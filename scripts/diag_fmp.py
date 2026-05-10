"""
scripts/diag_fmp.py
===================
FMP 키가 어떤 엔드포인트에 접근 가능한지 진단.

403 이 뜨면 "이 엔드포인트는 유료 플랜에서만 풀린다" 라는 뜻입니다.

실행:
    python scripts/diag_fmp.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src.config import settings  # noqa: E402

# 2025-08-31 이후 가입자는 /stable/ 만 접근 가능. /api/v3/ 는 옛 가입자 전용.
ENDPOINTS_TO_TEST = [
    ("회사 프로필",            "stable/profile?symbol=CPNG"),
    ("실시간 시세",            "stable/quote?symbol=CPNG"),
    ("과거 가격 (EOD)",        "stable/historical-price-eod/light?symbol=CPNG"),
    ("손익계산서",             "stable/income-statement?symbol=CPNG&limit=5"),
    ("재무상태표",             "stable/balance-sheet-statement?symbol=CPNG&limit=5"),
    ("현금흐름표",             "stable/cash-flow-statement?symbol=CPNG&limit=5"),
    ("핵심 비율 (P/E·ROE)",    "stable/key-metrics?symbol=CPNG&limit=5"),
    ("재무 비율",              "stable/ratios?symbol=CPNG&limit=5"),
]


def main() -> None:
    api_key = settings.require("fmp_api_key")
    print(f"FMP 키 로딩 OK (...{api_key[-4:]})")
    print(f"베이스 URL: https://financialmodelingprep.com/<stable | api/v3>")
    print()
    print(f"{'엔드포인트':<28} {'상태':<10} 의미")
    print("-" * 78)

    for label, path in ENDPOINTS_TO_TEST:
        sep = "&" if "?" in path else "?"
        url = f"https://financialmodelingprep.com/{path}{sep}apikey={api_key}"
        try:
            r = requests.get(url, timeout=15)
            code = r.status_code
            if code == 200:
                status = "✅ 200"
                meaning = "접근 가능"
            elif code == 401:
                status = "❌ 401"
                meaning = "키 인증 실패 (키 자체가 무효)"
            elif code == 403:
                status = "🔒 403"
                meaning = "유료 플랜 전용 (현 플랜에선 차단)"
            elif code == 429:
                status = "⏳ 429"
                meaning = "일일 호출 한도 초과 (250/day)"
            else:
                status = f"⚠️  {code}"
                meaning = r.text[:60]
        except Exception as e:  # noqa: BLE001
            status = "ERR"
            meaning = str(e)[:60]

        print(f"{label:<28} {status:<10} {meaning}")

    print()
    print("판정:")
    print("  · 200 만 보이면: 무료 플랜에서 가능한 데이터만 사용 (가격·프로필)")
    print("  · 일부 403: Starter 이상 결제 시 모두 풀림")
    print("  · 모두 401: 키 자체 문제 → 새로 발급")


if __name__ == "__main__":
    main()
