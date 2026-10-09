"""src/health.py — 헬스 리포트 순수 판정·문구 (네트워크·DB 없음)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.exceptions import ApiHttpError, ApiTimeoutError, DataValidationError, ModelRetiredError
from src.health import (
    KST,
    MD_FORBIDDEN,
    UNIVERSE_STALE_DAYS,
    HealthReport,
    llm_status,
)

NOW = datetime(2026, 10, 8, 8, 31, tzinfo=KST)


def _healthy(**kw) -> HealthReport:
    base = dict(
        market="KR", started_at=NOW, storage="ok",
        universe_as_of="2026-10-05T03:00:00+00:00",          # 3일 전 — 신선
        regime_ok=3, regime_total=3, llm="off",
        sent=6, recipients=6,
    )
    base.update(kw)
    return HealthReport(**base)


def _today_case() -> HealthReport:
    """2026-10-08 실제 상황: 유니버스 07-10 기준 + FRED 502 로 국면 2/3."""
    return _healthy(
        universe_as_of="2026-07-10T05:12:33.123456+00:00",
        regime_ok=2, regime_total=3,
        regime_failures=["yield_curve — FRED HTTP 502"],
    )


def test_no_problems_means_no_status_line():
    h = _healthy()
    assert h.problems(NOW) == []
    assert h.status_line(NOW) is None
    assert h.signature(NOW) == []


def test_status_line_exact_for_today_case():
    assert _today_case().status_line(NOW) == "⚙️ 상태: 발굴 데이터 07-10 기준(90일 전) · 국면 지표 2/3"


def test_universe_stale_threshold_boundary():
    # 정확히 임계일 = 아직 신선, 임계+1 = 노후
    fresh = _healthy(universe_as_of="2026-09-30T03:00:00+00:00")     # 8일 전
    stale = _healthy(universe_as_of="2026-09-29T03:00:00+00:00")     # 9일 전
    assert UNIVERSE_STALE_DAYS == 8
    assert fresh.status_line(NOW) is None
    assert stale.status_line(NOW) == "⚙️ 상태: 발굴 데이터 09-29 기준(9일 전)"


def test_universe_missing_and_storage_error_user_text():
    h = _healthy(universe_as_of=None, storage="error")
    line = h.status_line(NOW)
    assert line == "⚙️ 상태: 발굴 데이터 기준일 없음 · 일부 데이터 갱신 지연"


def test_price_stale_uses_business_days():
    # 10-02(금) → 10-08(목): 영업일 4일 → 노후 / 10-05(월) → 3영업일 → 신선
    assert "가격 10-02 기준" in _healthy(price_as_of="2026-10-02").status_line(NOW)
    assert _healthy(price_as_of="2026-10-05").status_line(NOW) is None


def test_owner_only_problems_stay_out_of_status_line():
    h = _healthy(llm="retired", optional_failures=["predictions"], state_write_ok=False,
                 failed=1, sent=5, storage="offline")
    assert h.status_line(NOW) is None                         # 구독자에겐 안 보임
    sig = h.signature(NOW)
    assert sig == sorted(["llm_retired", "optional:predictions", "state_write",
                          "send_failed", "storage_offline"])


@pytest.mark.parametrize("kw", [
    {},
    {"universe_as_of": "2026-07-10T05:12:33+00:00", "regime_ok": 0, "regime_total": 3,
     "regime_failures": ["yield_curve_[x]", "credit*", "`a`_b"], "storage": "error"},
    {"universe_as_of": None, "price_as_of": "2026-01-02", "llm": "error:Weird_Name"},
])
def test_user_facing_text_has_no_markdown_specials(kw):
    h = _healthy(**kw)
    line = h.status_line(NOW) or ""
    assert not (set(line) & MD_FORBIDDEN)
    for p in h.problems(NOW):
        if p.user_text is not None:
            assert not (set(p.user_text) & MD_FORBIDDEN), p


def test_is_hold():
    assert _healthy(storage="timeout").is_hold(universe_count=500) is True
    assert _healthy().is_hold(universe_count=0) is True
    assert _healthy(storage="error").is_hold(universe_count=500) is False
    assert _healthy(storage="offline").is_hold(universe_count=500) is False


def test_needs_owner_dm_only_on_signature_change_hold_or_failure():
    h = _today_case()
    prev_same = {"signature": h.signature(NOW)}
    assert h.needs_owner_dm(NOW, prev_same) is False            # 같은 문제 → 재DM 안 함
    assert h.needs_owner_dm(NOW, None) is True                  # 첫 실행 + 문제 있음
    assert h.needs_owner_dm(NOW, {"signature": ["universe_stale"]}) is True   # 변화
    assert _healthy().needs_owner_dm(NOW, None) is False        # 정상 → 하트비트는 다이제스트 자체
    assert h.needs_owner_dm(NOW, prev_same, held=True) is True
    failed = _healthy(failed=1, sent=5)
    assert failed.needs_owner_dm(NOW, {"signature": failed.signature(NOW)}) is True


def test_owner_text_plain_with_journalctl_hint():
    text = _today_case().owner_text(NOW, held=False)
    lines = text.splitlines()
    assert lines[0] == "[quant-bot 상태] KR 다이제스트 · 2026-10-08 08:31 KST"
    assert "발송: 6/6 성공" in lines
    assert "저장소: Turso 정상 (초기 pull ok)" in lines
    assert "유니버스: KR 07-10 기준 (90일 전) — 재점수 타이머 확인" in lines
    assert "국면 지표: 2/3 반영 (실패: yield_curve — FRED HTTP 502)" in lines
    assert "LLM 요약: 끔" in lines
    assert lines[-1] == "조치: journalctl -u quant-digest@kr.service -n 50 --no-pager"


def test_owner_text_retired_llm():
    text = _healthy(llm="retired").owner_text(NOW, held=False)
    assert "LLM 요약: 모델 퇴역 — MINIMAX_MODEL 교체" in text


def test_owner_text_hold_variants():
    t = _healthy(storage="timeout", sync_timeout_s=180).owner_text(NOW, held=True)
    assert t.splitlines()[0] == "[quant-bot 발송 보류] KR 다이제스트 · 2026-10-08 08:31 KST"
    assert "Turso 초기 동기화 180초 타임아웃 → 오프라인 파일 강등" in t
    assert t.splitlines()[-1] == "sudo systemctl start quant-digest@kr.service"
    t2 = _healthy(market="US").owner_text(NOW, held=True)
    assert "US 발굴 종목 0개" in t2 and "quant-digest@us.service" in t2


def test_to_state_json_roundtrip():
    h = _today_case()
    d = h.to_state()
    back = json.loads(json.dumps(d, ensure_ascii=False))      # JSON 직렬화 가능
    assert back["signature"] == ["regime_partial", "universe_stale"]
    restored = HealthReport.from_state(back)
    assert restored == h


def test_naive_now_is_treated_as_kst():
    naive = datetime(2026, 10, 8, 8, 31)
    assert _today_case().status_line(naive) == _today_case().status_line(NOW)


def test_llm_status_mapping():
    assert llm_status(None) == "ok"
    assert llm_status(ModelRetiredError("gone", status_code=410)) == "retired"
    assert llm_status(ApiTimeoutError("slow")) == "timeout"
    assert llm_status(DataValidationError("잘림")) == "error:DataValidationError"
    assert llm_status(ApiHttpError("x", status_code=500)) == "error:ApiHttpError"


# ---------------- WP-POLISH: owner_text regime_ok None 가드 · from_state started_at 검증 ----------------


def test_owner_text_regime_ok_none_has_no_none_line():
    h = _healthy(regime_ok=None, regime_total=3)
    text = h.owner_text(NOW, held=False)
    assert "None" not in text and "국면 지표" not in text


def test_owner_text_regime_ok_zero_is_still_shown():
    """0/3 은 '측정 안 됨(None)' 과 다르다 — 줄은 나가야 한다."""
    text = _healthy(regime_ok=0, regime_total=3).owner_text(NOW, held=False)
    assert "0/3" in text


def test_owner_text_regime_full_unchanged():
    assert "국면 지표: 3/3 반영" in _healthy().owner_text(NOW, held=False)


def test_from_state_missing_started_at_raises_value_error():
    d = _healthy().to_state()
    del d["started_at"]
    with pytest.raises(ValueError, match="started_at"):
        HealthReport.from_state(d)
    d["started_at"] = None
    with pytest.raises(ValueError, match="started_at"):
        HealthReport.from_state(d)


def test_from_state_bad_started_at_raises_value_error():
    d = _healthy().to_state()
    d["started_at"] = "어제 아침"
    with pytest.raises(ValueError, match="started_at"):
        HealthReport.from_state(d)
    d["started_at"] = 12345
    with pytest.raises(ValueError, match="started_at"):
        HealthReport.from_state(d)
