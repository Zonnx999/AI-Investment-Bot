"""
src/exceptions.py
=================
프로젝트 도메인 예외 계층.

설계 원칙
---------
1. 모든 커스텀 예외는 `QuantBotError` 를 상속 → 호출자가 한 줄로 잡을 수 있음.
2. 외부 예외(requests.HTTPError 등)는 라이브러리 경계에서 specific 한 도메인
   예외로 변환(re-raise) — 호출자는 외부 라이브러리 detail 을 몰라도 됨.
3. `raise CustomError(...) from original_error` 패턴으로 원인 체인 보존
   → `logger.exception()` 에서 풀 traceback 이 보임.

계층
----
QuantBotError                    프로젝트 모든 예외의 베이스
├── ConfigError                  설정/환경 관련
│   └── MissingApiKeyError       특정 API 키 누락
├── DataFetchError               외부 데이터 호출/파싱
│   ├── ApiHttpError             HTTP 4xx/5xx (status_code 보유)
│   │   ├── ApiAuthError         401 — 키 자체가 무효
│   │   ├── ApiAuthorizationError 403 — 플랜/권한 부족
│   │   └── RateLimitError       429 — 호출 한도 초과
│   ├── ApiTimeoutError          네트워크 타임아웃
│   ├── ApiConnectionError       DNS/접속 실패
│   └── DataValidationError      응답이 비었거나 스키마 위반
└── AnalysisError                분석 단계 실패
    └── InsufficientDataError    VaR/MDD 계산 데이터 부족
"""

from __future__ import annotations


class QuantBotError(Exception):
    """프로젝트 전체의 베이스 예외."""


# ----------------------------------------------------------------------
# 설정 관련
# ----------------------------------------------------------------------


class ConfigError(QuantBotError):
    """설정/환경변수 관련 예외."""


class MissingApiKeyError(ConfigError):
    """필수 API 키가 .env 에 없음.

    Attributes
    ----------
    key_name : str
        .env 에 채워야 하는 환경변수 이름.
    """

    def __init__(self, key_name: str, message: str | None = None):
        self.key_name = key_name
        super().__init__(
            message
            or f".env 에 {key_name} 가 설정되어 있지 않습니다. "
            f".env.example 을 참고해 채워주세요."
        )


# ----------------------------------------------------------------------
# 외부 데이터 호출 관련
# ----------------------------------------------------------------------


class DataFetchError(QuantBotError):
    """외부 데이터 호출·파싱 실패의 베이스."""

    def __init__(self, message: str, source: str | None = None):
        self.source = source  # 예: "FRED", "yfinance", "FMP", "CoinGecko"
        super().__init__(message)


class ApiHttpError(DataFetchError):
    """외부 API 가 4xx/5xx 응답.

    Attributes
    ----------
    status_code : int
        HTTP 상태 코드.
    """

    def __init__(self, message: str, status_code: int, source: str | None = None):
        self.status_code = status_code
        super().__init__(message, source=source)


class ApiAuthError(ApiHttpError):
    """HTTP 401 — 키가 무효하거나 형식이 잘못됨."""

    def __init__(self, message: str, source: str | None = None):
        super().__init__(message, status_code=401, source=source)


class ApiAuthorizationError(ApiHttpError):
    """HTTP 403 — 키는 유효하지만 이 자원에 접근 권한 없음 (플랜 제한 등)."""

    def __init__(self, message: str, source: str | None = None):
        super().__init__(message, status_code=403, source=source)


class RateLimitError(ApiHttpError):
    """HTTP 429 — 호출 한도 초과."""

    def __init__(self, message: str, source: str | None = None):
        super().__init__(message, status_code=429, source=source)


class ApiTimeoutError(DataFetchError):
    """네트워크 타임아웃."""


class ApiConnectionError(DataFetchError):
    """DNS/접속/SSL 등 연결 실패."""


class DataValidationError(DataFetchError):
    """응답이 비었거나 예상 스키마와 다름."""


# ----------------------------------------------------------------------
# 분석 관련
# ----------------------------------------------------------------------


class AnalysisError(QuantBotError):
    """분석/계산 단계 실패의 베이스."""


class InsufficientDataError(AnalysisError):
    """통계량 계산에 필요한 최소 데이터 점이 부족."""

    def __init__(self, message: str, n_points: int | None = None, required: int | None = None):
        self.n_points = n_points
        self.required = required
        super().__init__(message)
