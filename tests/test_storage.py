"""src/storage.py — SQLite 캐시 라운드트립 / TTL / 데코레이터 (tmp DB, 오프라인)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

import src.storage as storage_mod
from src.storage import Storage, cached

TTL = timedelta(hours=1)


@pytest.fixture
def store(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def cache_on(tmp_path, monkeypatch):
    """데코레이터 테스트용: 캐시 켜고 싱글톤을 tmp DB 로 격리."""
    monkeypatch.setenv("QUANT_BOT_CACHE", "on")
    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "deco.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)  # 싱글톤 리셋
    yield
    monkeypatch.setattr(storage_mod, "_storage", None)


# ---------------- 라운드트립 ----------------


# 참고: 캐시는 datetime 을 항상 ns 단위로 복원 (storage._normalize_datetimes).
# pandas 3.x 의 date_range 기본 단위는 us 라서, 라운드트립 비교용 픽스처는
# ns 로 명시해 "캐시 출력 = ns" 라는 계약을 그대로 검증한다.


def test_dataframe_roundtrip_preserves_dtypes_and_index(store, ohlcv_frame):
    df = ohlcv_frame.copy()
    df.index = pd.RangeIndex(len(df))  # 정수 인덱스 라운드트립
    store.put_dataframe("ns", "k", df)
    back = store.get_dataframe("ns", "k", TTL)
    pd.testing.assert_frame_equal(back, df)


def test_dataframe_roundtrip_datetime_index_restored_as_ns(store, ohlcv_frame):
    df = ohlcv_frame.copy()
    df.index = pd.date_range("2024-01-01", periods=len(df)).as_unit("ns")
    store.put_dataframe("ns", "k", df)
    back = store.get_dataframe("ns", "k", TTL)
    pd.testing.assert_frame_equal(back, df, check_freq=False)


def test_series_roundtrip_preserves_name(store):
    idx = pd.date_range("2024-01-01", periods=2).as_unit("ns")
    s = pd.Series([1.0, 2.0], index=idx, name="T10Y2Y")
    store.put_series("ns", "k", s)
    back = store.get_series("ns", "k", TTL)
    pd.testing.assert_series_equal(back, s, check_freq=False)
    assert back.name == "T10Y2Y"


def test_json_roundtrip(store):
    obj = {"symbol": "CPNG", "price": 17.25, "한글": True}
    store.put_json("ns", "k", obj)
    assert store.get_json("ns", "k", TTL) == obj


def test_crypto_like_normalized_datetime_index_roundtrip(store):
    # fetch_crypto 의 인덱스 형태 (자정 정규화 DatetimeIndex) 가 무손실인지
    idx = pd.to_datetime(["2024-01-01", "2024-01-02"]).normalize().as_unit("ns")
    df = pd.DataFrame({"price": [1.0, 2.0]}, index=idx)
    df.index.name = "date"
    store.put_dataframe("crypto", "btc", df)
    back = store.get_dataframe("crypto", "btc", TTL)
    pd.testing.assert_frame_equal(back, df, check_freq=False)


# ---------------- miss / TTL / 격리 ----------------


def test_miss_returns_none(store):
    assert store.get_dataframe("ns", "nope", TTL) is None


def test_expired_entry_is_miss(store, ohlcv_frame):
    store.put_dataframe("ns", "k", ohlcv_frame)
    assert store.get_dataframe("ns", "k", timedelta(seconds=-1)) is None


def test_namespaces_are_isolated(store):
    store.put_json("a", "k", {"v": 1})
    assert store.get_json("b", "k", TTL) is None


def test_corrupt_payload_is_miss_not_crash(store):
    store._put_payload("ns", "k", "dataframe", "{이건 JSON 아님")
    assert store.get_dataframe("ns", "k", TTL) is None


def test_purge_and_stats(store):
    store.put_json("a", "k1", {})
    store.put_json("b", "k2", {})
    assert store.stats() == {"a": 1, "b": 1}
    assert store.purge() == 2
    assert store.stats() == {}


# ---------------- cached 데코레이터 ----------------


def test_cached_decorator_hits_after_first_call(cache_on):
    calls = {"n": 0}

    @cached("test_ns", TTL, "dataframe")
    def fetch(ticker: str, period: str = "1y") -> pd.DataFrame:
        calls["n"] += 1
        idx = pd.date_range("2024-01-01", periods=1).as_unit("ns")
        return pd.DataFrame({"x": [1.0]}, index=idx)

    a = fetch("CPNG")
    b = fetch("CPNG", period="1y")  # positional/keyword 달라도 같은 키여야 함
    assert calls["n"] == 1
    pd.testing.assert_frame_equal(a, b, check_freq=False)

    fetch("NVDA")  # 다른 인자 → 다른 키 → 실호출
    assert calls["n"] == 2


def test_cached_decorator_off_mode_bypasses(cache_on, monkeypatch):
    monkeypatch.setenv("QUANT_BOT_CACHE", "off")
    calls = {"n": 0}

    @cached("test_ns", TTL, "json")
    def fetch() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    assert fetch() == {"v": 1}
    assert fetch() == {"v": 2}  # off — 매번 실호출


def test_cached_decorator_refresh_mode_rewrites(cache_on, monkeypatch):
    calls = {"n": 0}

    @cached("test_ns", TTL, "json")
    def fetch() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    assert fetch() == {"v": 1}
    monkeypatch.setenv("QUANT_BOT_CACHE", "refresh")
    assert fetch() == {"v": 2}          # 읽기 무시, 새로 받아 캐시 갱신
    monkeypatch.setenv("QUANT_BOT_CACHE", "on")
    assert fetch() == {"v": 2}          # 갱신된 캐시 적중


def test_cached_decorator_does_not_cache_empty(cache_on):
    calls = {"n": 0}

    @cached("test_ns", TTL, "dataframe")
    def fetch() -> pd.DataFrame:
        calls["n"] += 1
        return pd.DataFrame()

    fetch()
    fetch()
    assert calls["n"] == 2  # 빈 결과는 캐시 안 함 → 매번 재시도
