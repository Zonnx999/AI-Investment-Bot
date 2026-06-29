"""src/data_fetcher.py — 네트워크 없는 순수 로직만 (_fmp_to_dataframe 등)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data_fetcher import _fmp_to_dataframe
from src.exceptions import MissingApiKeyError


def test_fmp_to_dataframe_empty_input():
    assert _fmp_to_dataframe([]).empty


def test_fmp_to_dataframe_sorts_and_indexes_by_date():
    data = [
        {"date": "2024-12-31", "revenue": 200},
        {"date": "2022-12-31", "revenue": 100},
    ]
    df = _fmp_to_dataframe(data)
    assert df.index.name == "date"
    assert df.index.is_monotonic_increasing
    assert df.iloc[0]["revenue"] == 100


def test_fmp_to_dataframe_rejects_error_dict():
    # FMP 가 200 + 에러 dict 를 줄 때 잘못된 1행 프레임을 만들면 안 됨 (#5)
    assert _fmp_to_dataframe({"Error Message": "Invalid API KEY."}).empty


def test_fmp_to_dataframe_without_date_column():
    df = _fmp_to_dataframe([{"symbol": "CPNG", "price": 17.0}])
    assert df.index.name is None  # date 없으면 인덱스 설정 안 함
    assert df.iloc[0]["symbol"] == "CPNG"


def test_fmp_get_requires_key_before_any_network(no_api_keys):
    from src.data_fetcher import _fmp_get

    with pytest.raises(MissingApiKeyError):
        _fmp_get("quote", {"symbol": "CPNG"})


# ---------------- fetch_company_screener 방어 ----------------


def test_company_screener_rejects_error_dict(monkeypatch):
    """FMP 200 + 에러 dict 면 dict 이터레이션(AttributeError) 대신 빈 리스트 (#버그수정)."""
    import src.data_fetcher as df

    monkeypatch.setattr(df, "_fmp_get", lambda *a, **k: {"Error Message": "Invalid API KEY."})
    assert df.fetch_company_screener() == []


def test_company_screener_filters_funds(monkeypatch):
    """정상 list 경로: ETF/펀드는 걸러내고 일반 종목만."""
    import src.data_fetcher as df

    rows = [
        {"symbol": "AAA", "isEtf": False, "isFund": False},
        {"symbol": "SPY", "isEtf": True, "isFund": False},
        {"symbol": "FND", "isEtf": False, "isFund": True},
    ]
    monkeypatch.setattr(df, "_fmp_get", lambda *a, **k: rows)
    out = df.fetch_company_screener(exclude_funds=True)
    assert [d["symbol"] for d in out] == ["AAA"]
