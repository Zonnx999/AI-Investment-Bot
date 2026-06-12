"""src/exceptions.py — 계층 구조와 속성이 설계대로인지."""

from __future__ import annotations

import pytest

from src.config import settings
from src.exceptions import (
    ApiAuthError,
    ApiAuthorizationError,
    ApiHttpError,
    ConfigError,
    DataFetchError,
    MissingApiKeyError,
    QuantBotError,
    RateLimitError,
)


def test_http_errors_carry_status_codes():
    assert ApiAuthError("x").status_code == 401
    assert ApiAuthorizationError("x").status_code == 403
    assert RateLimitError("x").status_code == 429


def test_hierarchy_allows_broad_catches():
    e = RateLimitError("over quota", source="FMP")
    assert isinstance(e, ApiHttpError)
    assert isinstance(e, DataFetchError)
    assert isinstance(e, QuantBotError)
    assert e.source == "FMP"


def test_missing_key_is_config_error_not_fetch_error():
    e = MissingApiKeyError(key_name="FMP_API_KEY")
    assert isinstance(e, ConfigError)
    assert not isinstance(e, DataFetchError)
    assert e.key_name == "FMP_API_KEY"


def test_settings_require_raises_missing_key(no_api_keys):
    with pytest.raises(MissingApiKeyError) as exc_info:
        settings.require("fred_api_key")
    assert exc_info.value.key_name == "FRED_API_KEY"
