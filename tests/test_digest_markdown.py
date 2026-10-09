"""다이제스트 v2 Markdown 안전성 — tdlib legacy 파서 포팅(tests/md_legacy.py)으로 전 조합 검증.

KR/US × 정상/저하/보류 × 변경일/유지일 × 첫 실행 × 적대적 회사명 → parse OK + 굵게 외 엔티티 0 +
길이 예산. 네트워크·DB 없음 (render_digest 순수 경로).
"""

from __future__ import annotations

import itertools

import pytest

from src import digest as dg
from src.health import HealthReport
from src.signals import Alert
from src.utils import md_safe, strip_md
from tests import test_digest as T
from tests.md_legacy import assert_bold_only, parse

ADVERSARIAL_ROWS = [T._row(r.symbol, T.ADVERSARIAL + f" {i}", r.total_score, r.value_score,
                           r.health_score, r.market, r.sector)
                    for i, r in enumerate(T.KR_ROWS)]


def _alerts():
    return [Alert("critical", "shock", "KOSPI 하루 -10.8% (기준 ±5%) · 52주 고점 대비 -34%",
                  key="shock:KOSPI:2026-07-28"),
            Alert("warning", "regime", "시장 국면 전환: 🟢 위험선호 (Risk-on) → ⚪ 중립 / 혼조",
                  key="regime:x"),
            Alert("warning", "drawdown", "금 52주 고점 대비 -12.3% — 경보 임계 -10% 진입",
                  key="dd:금:enter"),
            Alert("warning", "fx", "원/달러 20일 +5.2% (원화 약세) _*`[", key="fx:enter")]


def _inputs(market, condition, change, first_run, adversarial):
    rows = (ADVERSARIAL_ROWS if adversarial else T.KR_ROWS)
    if market == "US":
        rows = [T._row(r.symbol, r.name, r.total_score, r.value_score, r.health_score, "US", "Energy")
                for r in rows]
    now = T.NOW_KR if market == "KR" else T.NOW_US
    health = T._health(market)
    if condition == "degraded":
        health = T._health(market, universe_as_of="2026-09-29", regime_ok=2, regime_total=3,
                           regime_failures=["장단기 금리차 조회 실패"], storage="error")
    alerts = _alerts() if change else []
    report = T._report(market, alerts=alerts, held=(condition == "degraded"), kr=(market == "KR"))
    if first_run:
        diff = dg.diff_lists([], rows)
        views = [dg.PickView(row=r, kind="base", metrics=T._metrics(100, 1, -5, -10, 20)) for r in rows]
    elif change:
        prev = T._prev(*[(r.symbol, r.total_score) for r in rows[2:]], ("OLD_X", 95), ("OLD*Y", 93))
        diff = dg.diff_lists(prev, rows, {"OLD_X": None, "OLD*Y": T._row("OLD*Y", "x*y", 70, 1, 1)})
        views = [dg.PickView(row=r, kind="new" if r.symbol in diff.new else "keep",
                             metrics=T._metrics(100, 1, -5, -10, 20), since_pct=-3.3,
                             evidence=md_safe("PER 4.0배 · PBR 0.46 · ROE 11.5% · " + T.ADVERSARIAL))
                 for r in rows]
    else:
        diff = dg.ListDiff("2026-10-07", False, kept=[r.symbol for r in rows])
        views = [dg.PickView(row=r, kind="keep", metrics=T._metrics(100, 1, -5, -10, 20), since_pct=1.5)
                 for r in rows]
    warn = sum(1 for p in health.problems(now) if p.user_text)
    dd10 = ["대웅 선정 후 -10.3% — 보유 시 점검 (1회 알림)"] if change else []
    verdict = dg.decide_actions(diff, alerts, [md_safe(T.ADVERSARIAL + " -11%")] if change else [],
                                warn_count=warn)
    summary = T.SUMMARY_KR if market == "KR" else T.SUMMARY_US
    if condition == "degraded":
        summary = dict(summary, computed_at="2026-10-01T00:00:00+00:00")
    return dg.DigestInputs(
        market=market, now=now, verdict=verdict, diff=diff, views=views, report=report,
        health=health, price_asof="2026-10-07", score_asof="2026-09-29" if condition == "degraded" else "2026-10-05",
        stale_warn=(condition == "degraded"), hold_age=16 if condition == "hold" else None,
        list_since="2026-09-29", dd10_lines=dd10,
        overnight_line=T.OVERNIGHT if market == "KR" else None,
        ledger_summary=summary, ledger_has_history=not first_run,
    )


COMBOS = list(itertools.product(["KR", "US"], ["normal", "degraded", "hold"],
                                [False, True], [False, True], [False, True]))


@pytest.mark.parametrize("market,condition,change,first_run,adversarial", COMBOS)
def test_all_combinations_parse_ok_bold_only_and_within_budget(market, condition, change,
                                                                 first_run, adversarial):
    inp = _inputs(market, condition, change, first_run, adversarial)
    text = dg.render_with_budget(inp)
    ents = assert_bold_only(text)
    # 엔티티는 정적 제목·판정 헤더뿐 — 동적 문자열(회사명·알림)이 굵게 안에 들어가지 않음
    assert all(inner.startswith(("🇰🇷", "🇺🇸", "🔔", "🌐", "🇰🇷", "📋", "📒", "🔮")) for _, inner in ents)
    assert "`" not in text and "[" not in text and "\\" not in text
    assert all("_" not in inner for _, inner in ents)
    limit = dg.LEN_BUDGET_CHANGE if (change or first_run) else dg.LEN_BUDGET_QUIET[market]
    assert len(text) <= limit, (len(text), limit)
    assert text.splitlines()[-1] == dg.DISCLAIMER
    assert "A_B*C`D[E" not in text
    if adversarial and condition != "hold":          # 보류일은 종목 줄 자체가 없음
        assert "A＿B∗CʼD［E" in text


def test_adversarial_name_is_lookalike_and_truncated():
    s = md_safe(T.ADVERSARIAL, 24)
    assert s == "A＿B∗CʼD［E Holdings, Inc." and len(s) == 24
    assert parse(f"*제목* {s} _x")[0] != "OK"            # 참고: 원본 이탤릭 열림은 실패
    assert parse(f"*제목* {s}")[0] == "OK"


def test_raw_adversarial_breaks_parser_but_rendered_text_does_not():
    bad = "*🇰🇷 제목* " + T.ADVERSARIAL
    assert parse(bad)[0].startswith("ERROR")
    inp = _inputs("KR", "normal", True, False, True)
    assert parse(dg.render_with_budget(inp))[0] == "OK"


def test_strip_md_removes_bold_and_code_markers_only():
    assert strip_md("*a* `b` [c](d) john_doe") == "a b c(d) john_doe"


def test_owner_hold_and_status_texts_have_no_markdown_openers():
    h = HealthReport(market="KR", started_at=T.NOW_KR, storage="timeout", universe_as_of=None)
    for text in (h.owner_text(T.NOW_KR, held=True), h.owner_text(T.NOW_KR, held=False)):
        assert isinstance(text, str) and text        # 평문(parse_mode=None) 으로만 전송
    line = dg.render_status(T._health(universe_as_of="2026-07-10"), T.NOW_KR, ["성적표 3일째 미갱신(마지막 10-02)"])
    assert not set(line) & set("_*`[]")
