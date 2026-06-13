"""
scripts/check_predictions.py
============================
Phase 6 선행지표 예측 리포트.

가격만 보지 않고 거시·대체 데이터로 'N개월 뒤 방향'을 예측합니다.
  · M2 통화량 증가율 → 비트코인 수익률
  · 한국 수출 증가율 → 반도체 ETF(SOXX) 수익률

실행:
    python scripts/check_predictions.py

⚠️ 상관관계 기반 통계 모델입니다 (인과·보장 아님). R² 와 표본 수를 함께 보고
   신뢰도를 직접 판단하세요. Phase 10 백테스트로 검증 예정.
"""

from __future__ import annotations

from src.exceptions import QuantBotError
from src.logger import get_logger
from src.predictors import PREDICTORS

logger = get_logger(__name__)


def main() -> int:
    print("=" * 78)
    print(" 선행지표 예측 리포트 (Phase 6 — Alternative Data)")
    print("=" * 78)
    print("  ⚠️ 상관관계 기반 — 인과·보장 아님. R²·표본수로 신뢰도 판단.\n")

    failures = 0
    for name, predict in PREDICTORS.items():
        print(f"[{name}]")
        print("-" * 78)
        try:
            r = predict()
        except QuantBotError as e:
            logger.warning("예측 실패: %s — %s", name, e)
            print(f"  ❌ {type(e).__name__}: {e}\n")
            failures += 1
            continue

        flag = "✅ 참고할 만함" if r.reliable else "⚠️ 약함 (참고만)"
        print(f"  방향:       {r.direction}   (예상 변화율 {r.predicted_change_pct:+.1f}%)")
        print(f"  선행:       {r.best_lag_months}개월  |  상관 {r.correlation:+.2f}  |  R² {r.r_squared:.2f}  {flag}")
        print(f"  회귀:       y = {r.slope:+.3f}·x {r.intercept:+.2f}  (표본 {r.n_obs}개월)")
        print(f"  최신 입력:  {r.leading_name} {r.latest_leading_value:+.1f}%")
        print()

    print("=" * 78)
    print("해석:")
    print("  · R² 0.3+ = 월간 거시 기준 참고할 만한 관계 / 그 미만 = 노이즈일 수 있음")
    print("  · '선행 N개월' = 선행지표가 목표 자산을 N개월 앞선다는 통계적 추정")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
