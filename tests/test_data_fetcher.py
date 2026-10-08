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


# ---------------- FMP TTM (WP-SCORE) ----------------


def _ttm_fixture():
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures" / "fmp_ttm_responses.json").read_text(encoding="utf-8"))


def test_fetch_key_metrics_ttm_returns_first_row_and_hits_ttm_endpoint(monkeypatch):
    import src.data_fetcher as df

    rows = _ttm_fixture()["EOG"]["key-metrics-ttm"]
    calls = []

    def fake_get(endpoint, params=None):
        calls.append((endpoint, params))
        return rows

    monkeypatch.setattr(df, "_fmp_get", fake_get)
    out = df.fetch_key_metrics_ttm("EOG")
    assert calls == [("key-metrics-ttm", {"symbol": "EOG"})]
    assert out["evToEBITDATTM"] == pytest.approx(5.78, abs=0.01)
    assert out["marketCap"] == 76810428090


def test_fetch_ratios_ttm_returns_first_row_and_hits_ttm_endpoint(monkeypatch):
    import src.data_fetcher as df

    rows = _ttm_fixture()["EOG"]["ratios-ttm"]
    calls = []
    monkeypatch.setattr(df, "_fmp_get", lambda e, p=None: calls.append((e, p)) or rows)
    out = df.fetch_ratios_ttm("EOG")
    assert calls == [("ratios-ttm", {"symbol": "EOG"})]
    assert "priceToBookRatioTTM" in out and "grossProfitMarginTTM" in out


@pytest.mark.parametrize("payload", [[], None, {}, [None], ["x"], {"Error Message": "x"}])
def test_fetch_ttm_unsupported_or_malformed_returns_empty_dict(monkeypatch, payload):
    import src.data_fetcher as df

    monkeypatch.setattr(df, "_fmp_get", lambda *a, **k: payload)
    assert df.fetch_key_metrics_ttm("ZZZ") == {}
    assert df.fetch_ratios_ttm("ZZZ") == {}


def test_fetch_ttm_requires_key_before_any_network(no_api_keys):
    from src.data_fetcher import fetch_key_metrics_ttm, fetch_ratios_ttm

    with pytest.raises(MissingApiKeyError):
        fetch_key_metrics_ttm("EOG")
    with pytest.raises(MissingApiKeyError):
        fetch_ratios_ttm("EOG")


def test_ttm_fetchers_use_distinct_cache_namespaces():
    """@cached 네임스페이스가 함수에 올바르게 붙었는지 (§4.10 #2) — 소스 정적 확인."""
    import inspect
    import re

    import src.data_fetcher as df

    src_text = inspect.getsource(df)
    pairs = {ns: fn for ns, fn in re.findall(r'@cached\("(\w+)",[^\n]*\)\ndef (\w+)\(', src_text)}
    assert pairs["fmp_key_metrics_ttm"] == "fetch_key_metrics_ttm"
    assert pairs["fmp_ratios_ttm"] == "fetch_ratios_ttm"
    assert pairs["fmp_key_metrics"] == "fetch_key_metrics"
    assert pairs["fmp_ratios"] == "fetch_ratios"
    assert pairs["fmp_quote"] == "fetch_quote"


# ---------------- FRED REST 직접 호출 — 상태코드별 도메인 예외 (WP-SIGNAL, T3a §8) ----------------

_FAKE_FRED_KEY = "fake_fred_key_0123456789abcdef"


class _FredResp:
    """requests.Response 최소 대역 — status_code / ok / json()."""

    def __init__(self, status_code, body=None, *, raw_text=False):
        self.status_code = status_code
        self.ok = status_code < 400
        self._body = body
        self._raw_text = raw_text

    def json(self):
        if self._raw_text:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


@pytest.fixture
def fred_session(monkeypatch):
    """FRED 세션 대역 + 가짜 키. set(resp) 로 다음 응답 지정, calls 로 요청 확인."""
    import src.data_fetcher as df

    state = {"resp": None, "calls": []}

    class _Session:
        def get(self, url, params=None, **k):
            state["calls"].append((url, dict(params or {})))
            if isinstance(state["resp"], BaseException):
                raise state["resp"]
            return state["resp"]

    monkeypatch.setattr(df, "get_http_session", lambda: _Session())
    prev = df.settings.fred_api_key
    object.__setattr__(df.settings, "fred_api_key", _FAKE_FRED_KEY)
    state["set"] = lambda r: state.__setitem__("resp", r)
    yield state
    object.__setattr__(df.settings, "fred_api_key", prev)


def test_fetch_macro_rest_happy_path_drops_missing_dot(fred_session):
    from src.data_fetcher import FRED_OBSERVATIONS_URL, fetch_macro

    fred_session["set"](_FredResp(200, {"count": 3, "observations": [
        {"realtime_start": "2026-10-08", "date": "2026-10-06", "value": "0.40"},
        {"realtime_start": "2026-10-08", "date": "2026-10-07", "value": "."},
        {"realtime_start": "2026-10-08", "date": "2026-10-08", "value": "0.36"},
    ]}))
    s = fetch_macro("T10Y2Y", start="2026-01-01")
    assert list(s.values) == [0.40, 0.36]
    assert s.name == "T10Y2Y" and str(s.index[-1].date()) == "2026-10-08"
    url, params = fred_session["calls"][0]
    assert url == FRED_OBSERVATIONS_URL
    assert params["series_id"] == "T10Y2Y" and params["file_type"] == "json"
    assert params["observation_start"] == "2026-01-01"


@pytest.mark.parametrize("resp, exc_name", [
    (_FredResp(200, {"observations": []}), "DataValidationError"),
    (_FredResp(200, {"observations": [{"date": "2026-10-07", "value": "."}]}),
     "DataValidationError"),
    (_FredResp(400, {"error_code": 400, "error_message":
                     "Bad Request.  Invalid value for variable series_id.  Series IDs "
                     "should be 25 or less alphanumeric characters."}),
     "DataValidationError"),
    (_FredResp(400, {"error_code": 400, "error_message":
                     "Bad Request.  The value for variable api_key is not registered.  "
                     "Read https://fred.stlouisfed.org/docs/api/api_key.html for more "
                     "information."}),
     "ApiAuthError"),
    (_FredResp(404, None, raw_text=True), "DataValidationError"),
    (_FredResp(401, None, raw_text=True), "ApiAuthError"),
    (_FredResp(403, None, raw_text=True), "ApiAuthorizationError"),
    (_FredResp(429, None, raw_text=True), "RateLimitError"),
    (_FredResp(502, None, raw_text=True), "ApiHttpError"),
    (_FredResp(200, None, raw_text=True), "DataFetchError"),
])
def test_fetch_macro_status_mapping_and_no_key_in_message(fred_session, resp, exc_name):
    import src.exceptions as ex
    from src.data_fetcher import fetch_macro

    fred_session["set"](resp)
    with pytest.raises(getattr(ex, exc_name)) as info:
        fetch_macro("T10Y2Y")
    assert type(info.value).__name__ == exc_name
    assert _FAKE_FRED_KEY not in str(info.value)
    assert "api_key=" not in str(info.value) and "stlouisfed" not in str(info.value)


def test_fetch_macro_502_keeps_status_code_and_is_not_validation_error(fred_session):
    """502 가 '시리즈 없음'(DataValidationError)으로 오분류되던 사고 회귀."""
    from src.data_fetcher import fetch_macro
    from src.exceptions import ApiHttpError, DataValidationError

    fred_session["set"](_FredResp(502, None, raw_text=True))
    with pytest.raises(ApiHttpError) as info:
        fetch_macro("T10Y2Y")
    assert info.value.status_code == 502
    assert not isinstance(info.value, DataValidationError)


def test_fetch_macro_network_errors_map_to_timeout_and_connection(fred_session):
    import requests

    from src.data_fetcher import fetch_macro
    from src.exceptions import ApiConnectionError, ApiTimeoutError

    fred_session["set"](requests.exceptions.ReadTimeout("read timed out"))
    with pytest.raises(ApiTimeoutError):
        fetch_macro("T10Y2Y")
    fred_session["set"](requests.exceptions.ConnectionError("refused"))
    with pytest.raises(ApiConnectionError):
        fetch_macro("T10Y2Y")


def test_fetch_macro_requires_key_before_any_network(no_api_keys, monkeypatch):
    import src.data_fetcher as df

    def no_network():
        raise AssertionError("키 없이 네트워크 호출 시도")

    monkeypatch.setattr(df, "get_http_session", no_network)
    with pytest.raises(MissingApiKeyError):
        df.fetch_macro("T10Y2Y")


def test_fetch_macro_cached_mapping_unchanged():
    """@cached("macro") 가 fetch_macro 에 붙어 있고 stale 폴백이 부착됨 (§4.10 #2)."""
    from src.data_fetcher import fetch_macro

    assert fetch_macro.__name__ == "fetch_macro"
    assert callable(getattr(fetch_macro, "stale", None))


def test_korea_trade_series_drop_dead_balance_and_name_exports():
    from src.data_fetcher import KOREA_EXPORTS_LABEL, KOREA_TRADE_SERIES

    assert KOREA_EXPORTS_LABEL in KOREA_TRADE_SERIES
    assert "XTNTVA01KRM664S" not in KOREA_TRADE_SERIES.values()   # 2024-12 업데이트 중단
