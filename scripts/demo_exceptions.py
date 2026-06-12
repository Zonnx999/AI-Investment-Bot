"""
scripts/demo_exceptions.py
==========================
2단계(예외 체계화) 검증용 데모 스크립트.

의도적으로 예외 5종을 발생시켜서 (1) 커스텀 예외 계층이 정확히 매핑되는지
(2) logger.exception 이 풀 트레이스백을 양쪽(콘솔 + 파일)에 찍는지
(3) classify_regime 가 signals 와 failures 를 분리하는지 검증합니다.

실행:
    python scripts/demo_exceptions.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 깨끗한 데모 상태 보장
for k in ("FRED_API_KEY", "FMP_API_KEY"):
    os.environ.pop(k, None)

from src.config import settings  # noqa: E402
from src.exceptions import (  # noqa: E402
    ConfigError,
    DataFetchError,
    DataValidationError,
    MissingApiKeyError,
    QuantBotError,
    RateLimitError,
)
from src.logger import get_logger  # noqa: E402

log = get_logger("demo.exceptions")

# 데모용: .env 에 실제 키가 있어도 강제로 빈 값처럼 동작하게 override.
# frozen dataclass 이지만 object.__setattr__ 로 우회 가능.
object.__setattr__(settings, "fred_api_key", "")
object.__setattr__(settings, "fmp_api_key", "")


def main() -> None:
    log.info("=" * 60)
    log.info("2단계 데모 시작 — 예외 체계화 검증")
    log.info("=" * 60)

    # ----- CASE 1: MissingApiKeyError -----
    log.info("")
    log.info("[CASE 1] settings.require() → MissingApiKeyError")
    try:
        settings.require("fred_api_key")
    except MissingApiKeyError as e:
        log.exception("CASE 1 잡힘 — key_name 속성=%r", e.key_name)

    # ----- CASE 2: DataValidationError (잘못된 티커) -----
    log.info("")
    log.info("[CASE 2] fetch_prices('XXXFAKE!!!') → DataValidationError")
    from src.data_fetcher import fetch_prices
    try:
        fetch_prices("XXXFAKE!!!")
    except DataValidationError as e:
        log.exception("CASE 2 잡힘 (DataValidationError) — source=%r", e.source)
    except DataFetchError as e:
        # 네트워크 차단 등으로 다운로드 자체가 실패할 수도 있음 — 그래도 부모 클래스로 잡힘
        log.exception("CASE 2 잡힘 (DataFetchError) — source=%r", e.source)

    # ----- CASE 3: fetch_macro 키 누락 → MissingApiKeyError -----
    log.info("")
    log.info("[CASE 3] fetch_macro('T10Y2Y') 키 없음 → MissingApiKeyError 전파")
    from src.data_fetcher import fetch_macro
    try:
        fetch_macro("T10Y2Y")
    except MissingApiKeyError as e:
        log.exception("CASE 3 잡힘 (MissingApiKeyError) — key=%r", e.key_name)
    except DataFetchError as e:
        log.exception("CASE 3 잡힘 (DataFetchError) — source=%r", e.source)

    # ----- CASE 4: classify_regime — signals/failures 분리 -----
    log.info("")
    log.info("[CASE 4] classify_regime() — 전체 실패 시 failures 채워지고 signals 청결")
    from src.macro_analyzer import classify_regime
    report = classify_regime()
    log.info("  regime  = %s", report.regime)
    log.info("  score   = %+d", report.score)
    log.info("  signals = %r", report.signals)
    log.info("  failures= %r", report.failures)
    assert all("실패" not in s and "에러" not in s for s in report.signals), \
        "signals 에 에러 메시지가 섞이지 않아야 함"
    log.info("  ✅ signals 에 에러 메시지 없음 — 분리 성공")

    # ----- CASE 5: QuantBotError 부모로 통합 catch -----
    log.info("")
    log.info("[CASE 5] QuantBotError 부모 클래스로 모든 도메인 예외 한 번에 catch")
    try:
        raise RateLimitError("FMP demo 호출 한도 초과", source="FMP")
    except QuantBotError as e:
        log.warning(
            "CASE 5: %s — type=%s, source=%s, status_code=%s",
            e, type(e).__name__, e.source, e.status_code,
        )

    # ----- CASE 6: ConfigError 부모로 catch -----
    log.info("")
    log.info("[CASE 6] ConfigError 부모로도 catch 가능 (MissingApiKeyError 가 상속)")
    try:
        settings.require("fmp_api_key")
    except ConfigError as e:
        log.warning("CASE 6: ConfigError 로 잡힘 — %s", e)

    log.info("")
    log.info("=" * 60)
    log.info("데모 완료")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
