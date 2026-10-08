"""DART fnlttSinglAcntAll 파서·페처 + 한국 점수 (오프라인 — 실응답 트림 픽스처 + 합성, 네트워크 없음)."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

import src.data_fetcher as df
from src.data_fetcher import _parse_dart_all, fetch_dart_financials
from src.exceptions import (
    ApiAuthError,
    ApiAuthorizationError,
    DataValidationError,
    MissingApiKeyError,
    RateLimitError,
)
from src.universe import calculate_kr_scores, normalize_net_income

FIXTURE = Path(__file__).parent / "fixtures" / "dart_fnlttSinglAcntAll_trimmed.json"


@pytest.fixture(scope="module")
def dart_fixture() -> dict:
    """4개 회사 실응답 트림 (방해 행 — CF/SCE 의 ProfitLoss/Equity — 포함)."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------- _parse_dart_all (실응답 픽스처) ----------------


def test_parse_fnf_holdings_attributes_to_owners(dart_fixture):
    fin = _parse_dart_all(dart_fixture["FNF_holdings"]["list"], "CFS")
    assert fin["net_income"] == 125_724_901_800
    assert fin["equity"] == 2_312_118_904_091
    assert fin["equity_total"] == 3_642_862_629_037
    assert fin["op_income"] == 434_194_766_171
    assert fin["attrib"] == "owners" and fin["fs_div"] == "CFS"
    # 귀속 자본 < 총자본 (비지배지분 36% — 이 차이가 PBR 0.18→0.28 보정의 근원)
    assert fin["equity"] < fin["equity_total"]


def test_parse_sm_three_year_history_from_same_response(dart_fixture):
    fin = _parse_dart_all(dart_fixture["SM"]["list"], "CFS")
    assert fin["net_income"] == 347_212_781_579
    assert fin["ni_prev1"] == 18_310_151_475
    assert fin["ni_prev2"] == 87_297_073_925
    assert fin["op_income"] == 183_033_876_467


def test_parse_samsung_uses_is_division(dart_fixture):
    items = dart_fixture["samsung"]["list"]
    divs = {x["sj_div"] for x in items
            if x.get("account_id") == "ifrs-full_ProfitLossAttributableToOwnersOfParent"}
    assert "IS" in divs                       # 삼성전자는 IS (다른 3사는 CIS)
    fin = _parse_dart_all(items, "CFS")
    assert fin["net_income"] == 44_260_956_000_000
    assert fin["attrib"] == "owners"


def test_parse_distractor_rows_do_not_pollute(dart_fixture):
    """CF/SCE 에 중복 등장하는 ProfitLoss/Equity 행이 BS/IS 값을 덮지 않아야 한다."""
    for key in ("FNF_holdings", "SM", "cuckoo", "samsung"):
        items = dart_fixture[key]["list"]
        bs_only = [x for x in items if x.get("sj_div") in ("BS", "IS", "CIS")]
        assert len(bs_only) < len(items), f"{key}: 픽스처에 방해 행이 없음"
        assert _parse_dart_all(items, "CFS") == _parse_dart_all(bs_only, "CFS")


# ---------------- _parse_dart_all (합성) ----------------


def _row(sj_div, account_id=None, account_nm=None, cur="1", prev="2", prev2="3"):
    return {"sj_div": sj_div, "account_id": account_id or "", "account_nm": account_nm or "",
            "thstrm_amount": cur, "frmtrm_amount": prev, "bfefrmtrm_amount": prev2}


def test_parse_without_owner_accounts_falls_back_to_total():
    items = [
        _row("IS", "ifrs-full_ProfitLoss", "당기순이익(손실)", "100"),
        _row("BS", "ifrs-full_Equity", "자본총계", "1,000"),
    ]
    cfs = _parse_dart_all(items, "CFS")
    assert cfs["net_income"] == 100.0 and cfs["equity"] == 1000.0   # 콤마 제거
    assert cfs["equity_total"] == 1000.0 and cfs["net_income_total"] == 100.0
    assert cfs["attrib"] == "total"
    assert _parse_dart_all(items, "OFS")["attrib"] == "separate"


def test_parse_account_name_fallback_when_id_missing():
    items = [_row("IS", "", "당기순이익", "77"), _row("BS", "", "자본총계", "500")]
    fin = _parse_dart_all(items, "CFS")
    assert fin["net_income"] == 77.0 and fin["equity"] == 500.0


def test_parse_dash_and_empty_amounts_are_none():
    items = [
        _row("BS", "ifrs-full_Equity", "자본총계", "-", "-", ""),
        _row("IS", "ifrs-full_ProfitLoss", "당기순이익(손실)", ""),
    ]
    fin = _parse_dart_all(items, "CFS")
    assert fin["equity"] is None and fin["net_income"] is None


def test_parse_empty_list_all_none():
    fin = _parse_dart_all([], "CFS")
    assert all(fin[k] is None for k in
               ("net_income", "equity", "equity_total", "debt", "assets", "revenue", "op_income"))
    assert fin["attrib"] == "total"


def test_parse_skips_rows_without_current_amount():
    # 당기 금액이 '-' 인 첫 행을 건너뛰고 다음 매칭 행을 쓴다
    items = [
        _row("BS", "ifrs-full_Equity", "자본총계", "-"),
        _row("BS", "ifrs-full_Equity", "자본총계", "900"),
    ]
    assert _parse_dart_all(items, "CFS")["equity"] == 900.0


# ---------------- fetch_dart_financials (로컬 가짜 세션) ----------------


class _Resp:
    def __init__(self, body=None, status_code=200, raise_json=False):
        self._body, self.status_code, self._raise = body, status_code, raise_json
        self.ok = status_code < 400

    def json(self):
        if self._raise:
            raise ValueError("No JSON object could be decoded")
        return self._body


class _Session:
    """fs_div 별 응답을 돌려주고 요청 파라미터를 기록."""

    def __init__(self, by_fs_div: dict):
        self.by_fs_div, self.calls = by_fs_div, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return self.by_fs_div[params["fs_div"]]


@pytest.fixture
def dart_key(monkeypatch):
    prev = df.settings.dart_api_key
    object.__setattr__(df.settings, "dart_api_key", "fake-dart-key")
    yield
    object.__setattr__(df.settings, "dart_api_key", prev)


def _ok(items):
    return _Resp({"status": "000", "message": "정상", "list": items})


def _nodata():
    return _Resp({"status": "013", "message": "조회된 데이타가 없습니다."})


def test_fetch_cfs_then_ofs_fallback(monkeypatch, dart_key):
    ofs_items = [_row("IS", "ifrs-full_ProfitLoss", "당기순이익", "5"),
                 _row("BS", "ifrs-full_Equity", "자본총계", "50")]
    sess = _Session({"CFS": _nodata(), "OFS": _ok(ofs_items)})
    monkeypatch.setattr(df, "get_http_session", lambda: sess)
    fin = fetch_dart_financials("00369657", 2025)
    assert [c[1]["fs_div"] for c in sess.calls] == ["CFS", "OFS"]     # 2회 요청, 순서
    assert sess.calls[0][0].endswith("/fnlttSinglAcntAll.json")
    assert sess.calls[0][1]["corp_code"] == "00369657" and sess.calls[0][1]["bsns_year"] == "2025"
    assert fin["net_income"] == 5.0 and fin["fs_div"] == "OFS" and fin["attrib"] == "separate"


def test_fetch_cfs_success_makes_single_call(monkeypatch, dart_key):
    sess = _Session({"CFS": _ok([_row("BS", "ifrs-full_Equity", "자본총계", "10")])})
    monkeypatch.setattr(df, "get_http_session", lambda: sess)
    assert fetch_dart_financials("00126487", 2025)["equity"] == 10.0
    assert len(sess.calls) == 1


def test_fetch_both_nodata_returns_empty(monkeypatch, dart_key):
    sess = _Session({"CFS": _nodata(), "OFS": _nodata()})
    monkeypatch.setattr(df, "get_http_session", lambda: sess)
    assert fetch_dart_financials("00000001", 2025) == {}
    assert len(sess.calls) == 2


@pytest.mark.parametrize("status, exc", [
    ("020", RateLimitError),
    ("010", ApiAuthError), ("011", ApiAuthError), ("901", ApiAuthError),
    ("012", ApiAuthorizationError),
    ("100", DataValidationError),
])
def test_fetch_status_code_mapping(monkeypatch, dart_key, status, exc):
    sess = _Session({"CFS": _Resp({"status": status, "message": "x"})})
    monkeypatch.setattr(df, "get_http_session", lambda: sess)
    with pytest.raises(exc):
        fetch_dart_financials("00000001", 2025)


def test_fetch_json_parse_error_is_domain_exception_with_cause(monkeypatch, dart_key):
    sess = _Session({"CFS": _Resp(raise_json=True)})
    monkeypatch.setattr(df, "get_http_session", lambda: sess)
    with pytest.raises(DataValidationError) as ei:
        fetch_dart_financials("00000001", 2025)
    assert isinstance(ei.value.__cause__, ValueError)      # 원인 체인 보존 (§4.5)


def test_fetch_missing_key_raises(monkeypatch):
    prev = df.settings.dart_api_key
    object.__setattr__(df.settings, "dart_api_key", "")
    try:
        with pytest.raises(MissingApiKeyError):
            fetch_dart_financials("00000001", 2025)
    finally:
        object.__setattr__(df.settings, "dart_api_key", prev)


_MAINT_XML = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><result><status>800</status>'
              b"<message>\xec\x8b\x9c\xec\x8a\xa4\xed\x85\x9c \xec\xa0\x90\xea\xb2\x80</message></result>")


class _BytesResp:
    status_code, ok = 200, True

    def __init__(self, content):
        self.content = content


def test_corp_codes_non_zip_maps_dart_status(monkeypatch, dart_key):
    """실관측(2026-10-08): corpCode.xml 이 점검 중 status 800 XML 을 줌 — '키 확인' 오판 대신 상태코드 노출."""
    monkeypatch.setattr(df, "get_http_session",
                        lambda: type("S", (), {"get": lambda self, *a, **k: _BytesResp(_MAINT_XML)})())
    with pytest.raises(DataValidationError) as ei:
        df.fetch_dart_corp_codes()
    assert "800" in str(ei.value)
    monkeypatch.setattr(df, "get_http_session",
                        lambda: type("S", (), {"get": lambda self, *a, **k: _BytesResp(
                            _MAINT_XML.replace(b"800", b"020"))})())
    with pytest.raises(RateLimitError):
        df.fetch_dart_corp_codes()
    monkeypatch.setattr(df, "get_http_session",
                        lambda: type("S", (), {"get": lambda self, *a, **k: _BytesResp(b"not xml at all")})())
    with pytest.raises(DataValidationError):
        df.fetch_dart_corp_codes()


def test_dart_error_xml_parsing():
    assert df._dart_error_xml(_MAINT_XML) == {"status": "800", "message": "시스템 점검"}
    assert df._dart_error_xml(b"<<broken") == {"status": None, "message": None}


def test_fetch_cache_namespace_is_dart_financials_all():
    """구형 'dart_financials' 캐시(귀속 개념 없음)를 신형 코드가 읽지 않도록 네임스페이스 분리 (§4.10 #2)."""
    src = inspect.getsource(df)
    m = re.search(r'@cached\("([a-z_]+)",[^\n]*\)\s*\ndef fetch_dart_financials\(', src)
    assert m is not None, "fetch_dart_financials 바로 위에 @cached 가 없음"
    assert m.group(1) == "dart_financials_all"
    assert not hasattr(df, "_parse_dart_accounts")           # 구 파서 제거됨


# ---------------- normalize_net_income (일회성 가드) ----------------


@pytest.mark.parametrize("fin, expected", [
    ({"net_income": 100, "op_income": 90, "ni_prev1": 80, "ni_prev2": 70}, (100, None)),
    ({"net_income": 300, "op_income": 90, "ni_prev1": 80, "ni_prev2": 70}, (150, "one_off")),
    ({"net_income": 300, "op_income": 90}, (90, "one_off")),
    ({"net_income": 300, "op_income": -10}, (None, "one_off")),
    ({"net_income": -5, "op_income": 10}, (-5, None)),
    ({"net_income": 50, "op_income": None}, (50, None)),
    ({"net_income": 135, "op_income": 90}, (135, None)),          # 경계 ni == 1.5×op 는 의심 아님
    ({"net_income": None, "op_income": 90}, (None, None)),
])
def test_normalize_net_income(fin, expected):
    ni, flag = normalize_net_income(fin)
    assert flag == expected[1]
    if expected[0] is None:
        assert ni is None
    else:
        assert ni == pytest.approx(expected[0])


# ---------------- calculate_kr_scores ----------------


def test_kr_scores_good_value_stock():
    # 흑자 + 저PER/저PBR + 고ROE + 저부채 → 높은 점수
    fin = {"net_income": 200, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": 400}
    sc = calculate_kr_scores(fin, market_cap=1500)   # PER 7.5, PBR 1.5, ROE 20%
    assert sc["roe"] == pytest.approx(20.0)
    assert sc["per"] == pytest.approx(7.5)
    assert sc["pbr"] == pytest.approx(1.5)
    assert sc["total_score"] > 55
    assert 0 <= sc["health_score"] <= 100 and 0 <= sc["value_score"] <= 100


def test_kr_scores_expensive_stock_lower():
    cheap = calculate_kr_scores(
        {"net_income": 200, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": 400}, 1500)
    pricey = calculate_kr_scores(
        {"net_income": 200, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": 400}, 9000)
    assert pricey["value_score"] < cheap["value_score"]   # 같은 실적, 비싼 시총 → 저평가도↓


def test_kr_scores_loss_maker_no_per():
    sc = calculate_kr_scores(
        {"net_income": -50, "equity": 1000, "debt": 300, "revenue": 2000, "op_income": -10}, 1500)
    assert sc["per"] is None          # 적자 → PER 없음
    assert sc["roe"] == pytest.approx(-5.0)


def test_kr_scores_missing_data_safe():
    sc = calculate_kr_scores({"net_income": None, "equity": None}, market_cap=0)
    assert sc["total_score"] == 0
    assert sc["roe"] is None and sc["per"] is None and sc["pbr"] is None
