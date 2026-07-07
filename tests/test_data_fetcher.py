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


# ---------------- fetch_stock_news (Phase 11b /news) ----------------


def _fake_news_payload():
    """가정한 FMP news/stock 응답 형태 (라이브 스모크 전 — fetch_stock_news docstring)."""
    return [
        {"symbol": "AAPL", "title": " Apple hits record high ",
         "publishedDate": "2026-07-04 12:30:00", "site": "Reuters",
         "url": "https://example.com/a ", "text": "body..."},
        {"symbol": "AAPL", "title": "Second story",
         "publishedDate": "2026-07-03 09:00:00", "site": "WSJ",
         "url": "https://example.com/b"},
    ]


def test_fetch_stock_news_happy_path(monkeypatch):
    """엔드포인트/파라미터가 가정대로 나가고, 4개 표준 키로 정규화·trim 되는지."""
    import src.data_fetcher as df

    calls = {}

    def fake_get(endpoint, params=None):
        calls["endpoint"], calls["params"] = endpoint, params
        return _fake_news_payload()

    monkeypatch.setattr(df, "_fmp_get", fake_get)
    out = df.fetch_stock_news("AAPL", limit=5)

    assert calls["endpoint"] == "news/stock"
    assert calls["params"] == {"symbols": "AAPL", "limit": 5}
    assert [i["title"] for i in out] == ["Apple hits record high", "Second story"]
    assert out[0]["url"] == "https://example.com/a"          # trim
    assert out[0]["site"] == "Reuters"
    assert out[0]["publishedDate"] == "2026-07-04 12:30:00"
    assert set(out[0]) == {"title", "publishedDate", "site", "url"}  # 여분 필드 미노출


def test_fetch_stock_news_applies_limit(monkeypatch):
    import src.data_fetcher as df

    rows = [{"title": f"t{i}", "url": f"https://e.com/{i}"} for i in range(10)]
    monkeypatch.setattr(df, "_fmp_get", lambda *a, **k: rows)
    assert len(df.fetch_stock_news("AAPL", limit=3)) == 3


def test_fetch_stock_news_skips_malformed_items(monkeypatch):
    """비 dict / title 누락 / url 누락 항목은 스킵하고 정상 항목만."""
    import src.data_fetcher as df

    rows = [
        "not-a-dict",
        {"title": "no url", "site": "X"},
        {"url": "https://e.com/no-title"},
        {"title": "", "url": "https://e.com/empty-title"},
        {"title": "ok", "url": "https://e.com/ok", "site": None, "publishedDate": None},
    ]
    monkeypatch.setattr(df, "_fmp_get", lambda *a, **k: rows)
    out = df.fetch_stock_news("AAPL")
    assert [i["title"] for i in out] == ["ok"]
    assert out[0]["site"] is None and out[0]["publishedDate"] is None


def test_fetch_stock_news_rejects_error_dict(monkeypatch):
    """FMP 200 + 에러 dict → 빈 리스트 (screener 와 동일 가드)."""
    import src.data_fetcher as df

    monkeypatch.setattr(df, "_fmp_get", lambda *a, **k: {"Error Message": "Invalid API KEY."})
    assert df.fetch_stock_news("AAPL") == []


def test_fetch_stock_news_requires_key_before_any_network(no_api_keys):
    from src.data_fetcher import fetch_stock_news

    with pytest.raises(MissingApiKeyError):
        fetch_stock_news("AAPL")


def test_fmp_get_rejects_error_shaped_200(monkeypatch):
    """FMP 200 + {"Error Message": ...} 를 데이터로 흘려보내지 않음 —
    fetch_quote/profile 이 에러 dict 를 반환해 조용한 엉터리 점수가 되던 회귀."""
    import src.data_fetcher as df
    from src.exceptions import DataValidationError

    class _FakeResp:
        status_code = 200
        ok = True

        def json(self):
            return {"Error Message": "Invalid API KEY."}

    class _FakeSession:
        def get(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(df, "get_http_session", lambda: _FakeSession())
    prev = df.settings.fmp_api_key
    object.__setattr__(df.settings, "fmp_api_key", "fake-key")
    try:
        with pytest.raises(DataValidationError):
            df._fmp_get("quote", {"symbol": "AAPL"})
    finally:
        object.__setattr__(df.settings, "fmp_api_key", prev)
