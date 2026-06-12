"""공용 픽스처. 모든 테스트는 네트워크 없이 돌아야 합니다 (합성 데이터/로컬 서버만)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config import settings

_KEY_ATTRS = ("fred_api_key", "fmp_api_key", "anthropic_api_key", "news_api_key")


@pytest.fixture(autouse=True)
def _cache_off(monkeypatch):
    """모든 테스트에서 SQLite 캐시 비활성화 — 결과가 캐시 상태에 좌우되지 않도록.

    캐시 자체를 검증하는 test_storage.py 는 이 fixture 를 명시적으로 override.
    """
    monkeypatch.setenv("QUANT_BOT_CACHE", "off")


def _set_keys(values: dict[str, str]):
    """frozen dataclass 우회 setter (demo_exceptions.py 와 같은 패턴)."""
    for k, v in values.items():
        object.__setattr__(settings, k, v)


@pytest.fixture
def no_api_keys():
    """모든 API 키를 비워서 '키 미설정' 시나리오 강제. 종료 시 원복."""
    backup = {k: getattr(settings, k) for k in _KEY_ATTRS}
    _set_keys({k: "" for k in _KEY_ATTRS})
    yield
    _set_keys(backup)


@pytest.fixture
def fake_fmp_key():
    """마스킹 테스트용 가짜 FMP 키 주입. 종료 시 원복."""
    fake = "test_fake_fmp_key_0123456789abcdef"
    backup = settings.fmp_api_key
    _set_keys({"fmp_api_key": fake})
    yield fake
    _set_keys({"fmp_api_key": backup})


@pytest.fixture
def price_series() -> pd.Series:
    """결정론적 합성 가격 시리즈 (250 거래일, 양수 보장)."""
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2024-01-01", periods=250)
    log_returns = rng.normal(loc=0.0005, scale=0.02, size=250)
    prices = 100 * np.exp(np.cumsum(log_returns))
    return pd.Series(prices, index=dates, name="price")


@pytest.fixture
def ohlcv_frame(price_series: pd.Series) -> pd.DataFrame:
    """yfinance 형태의 OHLCV DataFrame (Adj Close 포함)."""
    return pd.DataFrame(
        {
            "Open": price_series * 0.99,
            "High": price_series * 1.01,
            "Low": price_series * 0.98,
            "Close": price_series,
            "Adj Close": price_series * 0.995,
            "Volume": 1_000_000,
        }
    )
