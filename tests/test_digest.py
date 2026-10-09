"""src/digest.py — 다이제스트 v2 순수 렌더러·판정·조립 (오프라인, 합성 객체 + tmp DB)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

import src.storage as storage_mod
from src import digest as dg
from src.config import settings
from src.health import HealthReport
from src.signals import Alert, AssetObs, KrContext, RegimeView, SignalReport
from src.universe import ScanRow
from tests.md_legacy import assert_bold_only

NOW_KR = datetime(2026, 10, 8, 8, 30, tzinfo=dg.KST)
NOW_US = datetime(2026, 10, 8, 22, 0, tzinfo=dg.KST)

ADVERSARIAL = "A_B*C`D[E Holdings, Inc."


# ----------------------------------------------------------------------
# 합성 입력 헬퍼
# ----------------------------------------------------------------------


def _row(sym, name, total, v, h, market="KR", sector="", flags=(), excluded=None):
    return ScanRow(symbol=sym, market=market, name=name, sector=sector, price=0.0,
                   market_cap=1e9, total_score=total, value_score=v, health_score=h, roe=None,
                   flags=tuple(flags), excluded=excluded, fund_asof="2026-10-05")


def _obs(label, last, chg, dd, ma, chg20=None, asof="2026-10-07"):
    return AssetObs(label=label, asof=asof, last=last, chg_1d_pct=chg, chg_1d_abs=None,
                    chg_20d_pct=chg20, dd_52w_pct=dd, vs_ma200_pct=ma, vol_ratio=1.0, n_52w=250)


def _metrics(price, ma, dd, mdd, vol, adj=None):
    return dg.PriceMetrics(price=price, asof="2026-10-07", adj_last=adj if adj is not None else price,
                           vs_ma200_pct=ma, dd_52w_pct=dd, mdd_1y_pct=mdd, vol_pct=vol)


_RISK_ON = "🟢 위험선호 (Risk-on)"
_REASON = "근거: 금리차 +0.51%p(+1) · HY 3.03%(0) · 실업수당 z-1.4(+1) — 3/3 지표, 10-07 기준"
_ACTIVE = ["KOSPI -25% (07-03~, 임계 -12%)", "KOSDAQ -27% (임계 -15%)",
           "장기국채 (20Y+) -12% (임계 -10%)", "금 -24% (임계 -10%)", "비트코인 -33% (임계 -25%)"]


def _report(market="KR", alerts=(), held=False, first_run=False, active=None, pending=None,
            kr=True):
    shown = f"{_RISK_ON} · 직전 판정 유지 (지표 2/3)" if held else _RISK_ON
    rv = RegimeView(shown_label=shown, score=2, available=2 if held else 3, total=3, held=held,
                    pending=pending, prev_label=None, reason_line=_REASON,
                    pending_count=1 if pending else 0)
    ctx = None
    if market == "KR" and kr:
        ctx = KrContext(kospi=_obs("KOSPI", 6804, -2.0, -25.4, 7.1),
                        kosdaq=_obs("KOSDAQ", 898, -2.3, -26.7, -8.7),
                        usdkrw=_obs("원/달러", 1339.7, -0.3, -3.0, -8.0, chg20=-0.3))
    return SignalReport(generated_at="2026-10-07T23:30:00+00:00", regime_label=shown, factors=[],
                        candidates=[], alerts=list(alerts), first_run=first_run, regime=rv, kr=ctx,
                        active=list(_ACTIVE if active is None else active),
                        pending_state={"v": 2, "pick_dd10": {}})


KR_ROWS = [_row("009970", "영원무역홀딩스", 92, 94, 90), _row("007700", "F&F홀딩스", 92, 97, 88),
           _row("284740", "쿠쿠홈시스", 92, 94, 89), _row("041510", "에스엠", 90, 84, 95, sector="우량기업부"),
           _row("073240", "금호타이어", 90, 94, 85), _row("003090", "대웅", 90, 92, 89)]
KR_PX = [170900, 13470, 24900, 79000, 6660, 15700]
KR_MA = [-10, -22, 5, -15, 10, -23]
KR_SINCE = [-11.5, -9.4, 18.0, 5.5, -7.0, -9.7]
OVERNIGHT = "간밤 미국 S&P500 7,802 -0.2% · 나스닥 27,539 -0.2% · 10년물 5.28% · VIX 15.1"

SUMMARY_KR = {
    "market": "KR", "rule_version": "v1", "since": "2026-07-13", "entry_date": "2026-07-13",
    "price_asof": "2026-10-07", "computed_at": "2026-10-07T23:00:00+00:00",
    "cum_port": -2.35, "cum_bench": -0.04, "excess_pp": -2.31, "mdd_port": -6.8, "mdd_bench": -25.0,
    "bench_label": "홈지수", "blocks": {"n": 3, "mean": -1.5, "t": -0.6, "blocks": [1.0, -2.0, -3.0]},
    "status": "pending", "has_reconstructed": True, "events": {"n": 6},
}
SUMMARY_US = dict(SUMMARY_KR, market="US", bench_label="SPY", cum_port=-0.55, cum_bench=4.0,
                  excess_pp=-4.55)


def _kr_views(rows=KR_ROWS, kind="keep", since=KR_SINCE):
    return [dg.PickView(row=r, kind=kind, metrics=_metrics(p, m, -20, -30, 40), since_pct=s,
                        since_date="2026-07-13")
            for r, p, m, s in zip(rows, KR_PX, KR_MA, since)]


def _health(market="KR", **kw):
    base = dict(market=market, started_at=NOW_KR if market == "KR" else NOW_US, storage="ok",
                universe_as_of="2026-10-05", regime_ok=3, regime_total=3)
    base.update(kw)
    return HealthReport(**base)


def _kr_quiet_inputs(**over) -> dg.DigestInputs:
    diff = dg.ListDiff(prev_date="2026-10-07", first_run=False, kept=[r.symbol for r in KR_ROWS])
    base = dict(
        market="KR", now=NOW_KR, verdict=dg.decide_actions(diff, []), diff=diff, views=_kr_views(),
        report=_report(), health=_health(), price_asof="2026-10-07", score_asof="2026-10-05",
        stale_warn=False, list_since="2026-09-29", overnight_line=OVERNIGHT,
        ledger_summary=SUMMARY_KR, ledger_has_history=True,
    )
    base.update(over)
    return dg.DigestInputs(**base)


US_ROWS = [_row("UVE", "Universal Insurance Holdings, Inc.", 85, 81, 89, "US", "Financial Services", ("fin_neutral",)),
           _row("VSNT", "Versant Media Group, Inc. Class A", 84, 90, 77, "US", "Industrials"),
           _row("CALM", "Cal-Maine Foods, Inc.", 92, 92, 92, "US", "Consumer Defensive"),
           _row("HG", "Hamilton Insurance Group, Ltd.", 88, 88, 88, "US", "Financial Services"),
           _row("EOG", "EOG Resources, Inc.", 86, 81, 91, "US", "Energy"),
           _row("SM", "SM Energy Company", 86, 89, 84, "US", "Energy")]


def _us_change_inputs(**over) -> dg.DigestInputs:
    views = [
        dg.PickView(row=US_ROWS[0], kind="new", metrics=_metrics(45.25, 21, 0.0, -18, 38),
                    evidence="이익수익률 19% · EV/EBITDA 2.5배 · ROE 33% · 금융업"),
        dg.PickView(row=US_ROWS[1], kind="new", metrics=_metrics(31.44, -14, -32, -42, 42),
                    evidence="이익수익률 14% · EV/EBITDA 3.3배 · ROE 9%"),
        dg.PickView(row=US_ROWS[2], kind="keep", metrics=_metrics(62.7, -21, -30, -35, 30), since_pct=-24.8),
        dg.PickView(row=US_ROWS[3], kind="keep", metrics=_metrics(34.32, 10, -5, -12, 25), since_pct=-2.4),
        dg.PickView(row=US_ROWS[4], kind="keep", metrics=_metrics(144.21, 10, -3, -15, 28), since_pct=4.1),
        dg.PickView(row=US_ROWS[5], kind="keep", metrics=_metrics(35.25, 25, -2, -20, 45), since_pct=18.6),
    ]
    diff = dg.ListDiff(prev_date="2026-10-07", first_run=False, new=["UVE", "VSNT"],
                       dropped=[dg.DroppedPick("MGR", "Affiliated Managers", "필터 제외 (보통주 아님 등)"),
                                dg.DroppedPick("AFGE", "American Financial", "필터 제외 (보통주 아님 등)")],
                       kept=["CALM", "HG", "EOG", "SM"])
    base = dict(
        market="US", now=NOW_US, verdict=dg.decide_actions(diff, []), diff=diff, views=views,
        report=_report("US", kr=False), health=_health("US"), price_asof="2026-10-07",
        score_asof="2026-10-05", stale_warn=False, ledger_summary=SUMMARY_US, ledger_has_history=True,
    )
    base.update(over)
    return dg.DigestInputs(**base)


# ----------------------------------------------------------------------
# 골든 목업 (현재 구현 기준으로 고정 — 형식이 바뀌면 의도된 변경인지 확인할 것)
# ----------------------------------------------------------------------

GOLDEN_A_KR_QUIET = """\
*🇰🇷 10-08(목) 08:30 · 오늘 할 일: 없음* — 후보 변화 없음 · 새 알림 없음
기준: 시세 10-07 종가 · 점수 10-05 (3일 전)

*🇰🇷 한국 시장*
코스피 6,804 -2.0% · 200일선 +7% · 52주 고점比 -25% ⚠️ 경보 중
코스닥 898 -2.3% · 200일선 -9% · 52주 고점比 -27% ⚠️ 경보 중
원/달러 1,339.7 · 1개월 -0.3% · 200일선 -8%
간밤 미국 S&P500 7,802 -0.2% · 나스닥 27,539 -0.2% · 10년물 5.28% · VIX 15.1

*🌐 글로벌* (아침 1회)
국면 🟢 위험선호 (Risk-on) · 지표 3/3, 현황 설명용 — 금리차 +0.51%p(+1) · HY 3.03%(0) · 실업수당 z-1.4(+1)
새 알림 없음 · 경보 유지 3: 장기국채 (20Y+) -12% · 금 -24% · 비트코인 -33%

*📋 한국 스크리닝 후보 6* — 변화 없음 (09-29 이후 동일)
009970 영원무역홀딩스 92 · 170,900원 · 200일선 -10% · 선정 후 -11.5%
007700 F&F홀딩스 92 · 13,470원 · 200일선 -22% · 선정 후 -9.4%
284740 쿠쿠홈시스 92 · 24,900원 · 200일선 +5% · 선정 후 +18.0%
041510 에스엠 90 · 79,000원 · 200일선 -15% · 선정 후 +5.5%
073240 금호타이어 90 · 6,660원 · 200일선 +10% · 선정 후 -7.0%
003090 대웅 90 · 15,700원 · 200일선 -23% · 선정 후 -9.7%
점수 = 재무점수(밸류+건전성, DART 연간, 가격요인 없음) · 가격 줄은 참고용 · 상세 /stock 코드

*📒 지난 후보 성과*
성적: 07-13 이후 이 리스트 보유 -2.4% · 홈지수 -0.0% · 초과 -2.3%p (10-07 종가, 비용 포함) · 소급 포함 · ⏳검증 전 3/6

규칙 기반 참고 자료 · 투자 자문 아님 — 투자 결정은 본인 책임"""

GOLDEN_B_US_CHANGE = """\
*🇺🇸 10-08(목) 22:00 · 오늘 할 일: 매수 검토 2 · 탈락 2* — 후보 변경일 · 새 알림 없음
기준: 시세 10-07 종가 · 점수 10-05 (3일 전) · 개장 전

*🌐 아침 이후 변화* — 없음 (국면·자산 경보는 아침 메시지 참고)

*📋 미국 스크리닝 후보 6* — 신규 2 · 탈락 2 · 유지 4
🆕 UVE Universal Insurance Hold — 85 (밸류 81 · 건전성 89)
   $45.25 · 52주 신고가 · 200일선 +21% · 1년 최대낙폭 -18% · 변동성 38%
   근거: 이익수익률 19% · EV/EBITDA 2.5배 · ROE 33% · 금융업
🆕 VSNT Versant Media Group, Inc — 84 (밸류 90 · 건전성 77)
   $31.44 · 52주 고점比 -32% · 200일선 -14% · 1년 최대낙폭 -42% · 변동성 42%
   근거: 이익수익률 14% · EV/EBITDA 3.3배 · ROE 9%
➖ MGR · AFGE 탈락 — 필터 제외 (보통주 아님 등)
유지 4
CALM Cal-Maine Foods, Inc. 92 · $62.70 · 200일선 -21% · 선정 후 -24.8%
HG Hamilton Insurance Group 88 · $34.32 · 200일선 +10% · 선정 후 -2.4%
EOG EOG Resources, Inc. 86 · $144.21 · 200일선 +10% · 선정 후 +4.1%
SM SM Energy Company 86 · $35.25 · 200일선 +25% · 선정 후 +18.6%
점수 = 재무점수(밸류+건전성, FMP 최근 4분기) · 가격 줄은 참고용, 점수 미반영 · 상세 /stock 티커

*📒 지난 후보 성과*
성적: 07-13 이후 이 리스트 보유 -0.6% · SPY +4.0% · 초과 -4.5%p (10-07 종가, 비용 포함) · 소급 포함 · ⏳검증 전 3/6

규칙 기반 참고 자료 · 투자 자문 아님 — 투자 결정은 본인 책임"""


def test_golden_a_kr_quiet_day():
    text = dg.render_with_budget(_kr_quiet_inputs())
    assert text == GOLDEN_A_KR_QUIET
    assert len(text) <= dg.LEN_BUDGET_QUIET["KR"]
    assert_bold_only(text)


def test_golden_b_us_change_day():
    text = dg.render_with_budget(_us_change_inputs())
    assert text == GOLDEN_B_US_CHANGE
    assert len(text) <= dg.LEN_BUDGET_CHANGE
    assert_bold_only(text)


def test_golden_c_kr_degraded_day_status_line_and_basis_warning():
    """FRED 1개 실패(국면 2/3) + 점수 9일 경과 + 성적표 게이트 미통과."""
    health = _health(universe_as_of="2026-09-29", regime_ok=2, regime_total=3,
                     regime_failures=["장단기 금리차 조회 실패"])
    diff = dg.ListDiff(prev_date="2026-10-07", first_run=False, kept=[r.symbol for r in KR_ROWS])
    warn = sum(1 for p in health.problems(NOW_KR) if p.user_text)
    inp = _kr_quiet_inputs(
        verdict=dg.decide_actions(diff, [], warn_count=warn), health=health,
        report=_report(held=True), score_asof="2026-09-29", stale_warn=True,
        ledger_summary=dict(SUMMARY_KR, computed_at="2026-10-01T00:00:00+00:00"),
    )
    text = dg.render_with_budget(inp)
    lines = text.splitlines()
    assert lines[0] == ("*🇰🇷 10-08(목) 08:30 · 오늘 할 일: 없음* — 후보 변화 없음 · 새 알림 없음 "
                        "· ⚠️ 데이터 점검 2건")
    assert lines[1] == "기준: 시세 10-07 종가 · ⚠️ 점수 09-29 (9일 전, 주간 갱신 지연)"
    assert "국면 🟢 위험선호 (Risk-on) · 직전 판정 유지 (지표 2/3) — 금리차" in text
    assert "*📒 지난 후보 성과*" not in text                     # 게이트 미통과 → 섹션 생략
    assert lines[-2] == ("⚙️ 상태: 발굴 데이터 09-29 기준(9일 전) · 국면 지표 2/3 · "
                         "성적표 6일째 미갱신(마지막 10-07)")
    assert lines[-1] == dg.DISCLAIMER
    assert len(text) <= dg.LEN_BUDGET_QUIET["KR"]
    assert_bold_only(text)


# ----------------------------------------------------------------------
# decide_actions / diff_lists (순수)
# ----------------------------------------------------------------------


def _prev(*pairs):
    from src.ledger import LedgerPick
    return [LedgerPick("2026-10-07", "KR", i + 1, s, s, "069500.KS", "v1", "live", name=s,
                       total_score=t) for i, (s, t) in enumerate(pairs)]


def test_decide_actions_quiet_day():
    diff = dg.ListDiff(prev_date="2026-10-07", first_run=False, kept=["A"])
    v = dg.decide_actions(diff, [])
    assert v.label == "없음" and v.quiet
    assert v.reasons == ["후보 변화 없음", "새 알림 없음"]


def test_decide_actions_counts_and_fixed_vocabulary():
    diff = dg.ListDiff(prev_date="2026-10-07", first_run=False, new=["X", "Y"],
                       dropped=[dg.DroppedPick("Z", "z", "순위 밖")], kept=["A"])
    alerts = [Alert("critical", "shock", "KOSPI 하루 -10.8%", key="shock:KOSPI:2026-07-28"),
              Alert("warning", "regime", "국면 전환", key="regime:x"),
              Alert("warning", "drawdown", "금 -12%", key="dd:금:enter")]
    v = dg.decide_actions(diff, alerts, ["대웅 -10%"], warn_count=1)
    assert v.label == "매수 검토 2 · 탈락 1 · 리스크 점검 4"
    assert v.reasons == ["후보 변경일", "KOSPI 급변", "국면 전환 확정", "외 2건", "⚠️ 데이터 점검 1건"]


def test_decide_actions_first_run_is_baseline_not_buy():
    """첫 원장 실행(직전 리스트 없음)은 '기준 수립' — 6종목이 전부 매수 검토가 되지 않는다."""
    diff = dg.diff_lists([], KR_ROWS)
    assert diff.first_run and diff.new == [] and len(diff.kept) == 6
    v = dg.decide_actions(diff, [])
    assert v.label == "없음"
    assert v.reasons[0] == "후보 목록 기준 수립 (첫 기록)"


def test_decide_actions_held_day_neutralizes_list_diff_but_keeps_alerts():
    """D1: 후보 보류일 — diff 가 있어도 매수/탈락/기준 수립 0, 새 알림은 그대로 '리스크 점검'."""
    diff = dg.ListDiff(prev_date="2026-07-13", first_run=False, new=["X"],
                       dropped=[dg.DroppedPick("Z", "z", "순위 밖")], kept=["A"])
    v = dg.decide_actions(diff, [], held_age=91)
    assert v.label == "없음" and v.n_buy == 0 and v.n_drop == 0
    assert v.reasons == ["후보 보류 (점수 91일 경과)", "새 알림 없음"]
    # 원장이 비어 있는(첫 기록) 보류일도 '기준 수립' 이 아니다
    v0 = dg.decide_actions(dg.diff_lists([], KR_ROWS), [], held_age=16)
    assert v0.label == "없음" and "기준 수립" not in " ".join(v0.reasons)
    alerts = [Alert("warning", "drawdown", "금 -12%", key="dd:금:enter")]
    v2 = dg.decide_actions(diff, alerts, warn_count=1, held_age=16)
    assert v2.label == "리스크 점검 1"
    assert v2.reasons == ["후보 보류 (점수 16일 경과)", "금 낙폭 경보", "⚠️ 데이터 점검 1건"]


def test_diff_lists_reasons_filter_score_rank():
    prev = _prev(("009970", 92), ("MGR", 86), ("CALM", 92), ("GONE", 80))
    cur = [KR_ROWS[0], _row("NEW1", "신규", 95, 90, 90)]
    dropped_rows = {
        "MGR": _row("MGR", "Affiliated", 86, 80, 80, excluded="sibling"),
        "CALM": _row("CALM", "Cal-Maine", 79, 70, 70),
        "GONE": None,
    }
    d = dg.diff_lists(prev, cur, dropped_rows, prev_date="2026-10-07")
    assert d.new == ["NEW1"] and d.kept == ["009970"]
    assert {x.symbol: x.reason for x in d.dropped} == {
        "MGR": "필터 제외 (보통주 아님 등)", "CALM": "점수 하락 92→79", "GONE": "순위 밖"}
    assert d.changed and d.swaps == []


def test_diff_lists_tie_swap_is_not_a_change():
    """탈락·신규의 total_score 가 같으면(시총 tie-break) '↔ 동점 교체' — 판정 카운트 제외."""
    prev = _prev(("A", 90), ("B", 90))
    cur = [_row("A", "a", 90, 90, 90), _row("C", "c", 90, 90, 90)]
    d = dg.diff_lists(prev, cur, {"B": _row("B", "b", 90, 90, 90)})
    assert d.swaps == [("B", "C")] and d.new == [] and d.dropped == [] and not d.changed
    v = dg.decide_actions(d, [])
    assert v.label == "없음" and v.reasons[0] == "후보 변화 없음 (동점 교체만)"
    lines = dg.render_candidates("KR", [dg.PickView(row=r, kind="keep", metrics=_metrics(1, 0, 0, 0, 0))
                                        for r in cur], d)
    assert "↔ 동점 교체: B → C" in lines
    assert lines[0].endswith("동점 교체 1")


# ----------------------------------------------------------------------
# 섹션 렌더러
# ----------------------------------------------------------------------


def test_render_header_and_basis_formats():
    v = dg.Verdict(label="없음", reasons=["후보 변화 없음", "새 알림 없음"])
    assert dg.render_header("KR", NOW_KR, v) == "*🇰🇷 10-08(목) 08:30 · 오늘 할 일: 없음* — 후보 변화 없음 · 새 알림 없음"
    assert dg.render_basis("US", NOW_US, "2026-10-07", "2026-10-05", stale_warn=False) == \
        "기준: 시세 10-07 종가 · 점수 10-05 (3일 전) · 개장 전"
    # 시세가 2영업일 이상 전이면 '휴장 또는 지연' (휴장 단정 금지, D6), 점수 기준일 없으면 명시
    assert dg.render_basis("KR", NOW_KR, "2026-10-02", None, stale_warn=False) == \
        "기준: 시세 10-02 종가 (휴장 또는 지연) · 점수 기준일 없음"
    assert "(휴장" not in dg.render_basis("KR", NOW_KR, "2026-10-07", None, stale_warn=False)


def test_render_candidates_hold_replaces_section():
    lines = dg.render_candidates("KR", _kr_views(), dg.ListDiff(None, True), hold_age=16)
    assert lines == ["*📋 한국 스크리닝 후보* — 보류", "후보 보류 — 점수 16일 경과, 재점수 필요"]


def test_render_candidates_first_run_lines_use_52w_not_since():
    views = _kr_views(kind="base", since=[None] * 6)
    lines = dg.render_candidates("KR", views, dg.diff_lists([], KR_ROWS))
    assert lines[0] == "*📋 한국 스크리닝 후보 6* — 기준 수립 (첫 기록)"
    assert lines[1] == "009970 영원무역홀딩스 92 · 170,900원 · 200일선 -10% · 52주 고점比 -20%"
    assert not any(ln.startswith("🆕") for ln in lines)


def test_render_candidates_price_failure_line():
    v = dg.PickView(row=KR_ROWS[0], kind="keep", metrics=None)
    lines = dg.render_candidates("KR", [v], dg.ListDiff("2026-10-07", False, kept=["009970"]))
    assert lines[1] == "009970 영원무역홀딩스 92 · 가격 조회 실패"


def test_render_candidates_more_than_six_new_collapses():
    rows = [_row(f"S{i}", f"n{i}", 90, 90, 90) for i in range(8)]
    views = [dg.PickView(row=r, kind="new", metrics=_metrics(1, 0, -1, -1, 1)) for r in rows]
    diff = dg.ListDiff("2026-10-07", False, new=[r.symbol for r in rows])
    lines = dg.render_candidates("KR", views, diff)
    assert sum(1 for ln in lines if ln.startswith("🆕")) == dg.MAX_NEW_LINES
    assert "외 2 — /scan kr" in lines


def test_evidence_from_detail_us_and_kr():
    us = {"health": {"components": [["ROE", 20, 20, "47.6%"]]},
          "value": {"components": [["EV/EBITDA", 21.9, 25, "2.5x"], ["이익수익률", 25, 25, "26.1%"]]}}
    assert dg.evidence_from_detail(us, "US", ("fin_neutral",)) == "이익수익률 26.1% · EV/EBITDA 2.5배 · ROE 47.6% · 금융업"
    kr = {"health": {"components": [["ROE", 28.7, 35, "11.5%"]]},
          "value": {"components": [["PER", 34.0, 40, "4.0"], ["PBR", 35.0, 35, "0.46"]]}}
    assert dg.evidence_from_detail(kr, "KR", (), is_holding=True) == "PER 4.0배 · PBR 0.46 · ROE 11.5% · 지주사"
    assert dg.evidence_from_detail(None, "US") == ""
    assert dg.evidence_from_detail(kr, "KR", ("one_off",)).endswith("일회성이익 가드")


def test_render_alerts_merge_shock_and_cap_five_lines():
    alerts = [Alert("critical", "shock", "KOSPI 하루 -10.8%", key="shock:KOSPI:d"),
              Alert("critical", "shock", "KOSDAQ 하루 -7.7%", key="shock:KOSDAQ:d")]
    alerts += [Alert("warning", "drawdown", f"자산{i} 낙폭", key=f"dd:자산{i}:enter") for i in range(6)]
    lines = dg.render_alert_lines(alerts, ["대웅 선정 후 -10.3% — 보유 시 점검 (1회 알림)"])
    assert lines[0] == "🚨 KOSPI 하루 -10.8% · KOSDAQ 하루 -7.7%"
    assert len(lines) == dg.MAX_ALERT_LINES + 1 and lines[-1] == "외 3건"   # 8줄 중 5줄 표시
    assert dg.render_alerts_kr([]) == []
    assert dg.render_changes_us([]) == ["*🌐 아침 이후 변화* — 없음 (국면·자산 경보는 아침 메시지 참고)"]


def test_render_global_pending_and_prev_label():
    rep = _report(pending="⚪ 중립 / 혼조")
    rep.regime.prev_label = None
    lines = dg.render_global(rep)
    assert "변화 관찰 중: ⚪ 중립 / 혼조 1/2" in lines[1]
    rep2 = _report()
    rep2.regime.prev_label = "🟡 약한 위험선호"
    assert "(어제: 🟡 약한 위험선호)" in dg.render_global(rep2)[1]
    assert dg.render_global(None) == ["*🌐 글로벌* (아침 1회)", "국면 조회 실패"]


def test_render_global_label_no_double_parens_and_kr_assets_deduped():
    """D5: 비유지 라벨은 유지 경로와 같은 ' · ' 구분(괄호 연속 금지), 경보 유지에서 KOSPI·KOSDAQ 제외."""
    lines = dg.render_global(_report())
    assert lines[1].startswith("국면 🟢 위험선호 (Risk-on) · 지표 3/3, 현황 설명용 — ")
    assert ") (" not in lines[1]
    assert lines[2] == "새 알림 없음 · 경보 유지 3: 장기국채 (20Y+) -12% · 금 -24% · 비트코인 -33%"
    # KOSPI·KOSDAQ 만 경보 중이면 '경보 없음'
    only_kr = dg.render_global(_report(active=["KOSPI -25% (임계 -12%)", "KOSDAQ -27% (임계 -15%)"]))
    assert only_kr[2] == "새 알림 없음 · 경보 없음"
    # 3개 초과는 '외 N' — 제외 후 개수 기준
    many = dg.render_global(_report(active=_ACTIVE + ["원자재 -11% (임계 -10%)"]))
    assert many[2].endswith("· 외 1") and "경보 유지 4:" in many[2]


def test_decide_actions_baseline_note_for_rule_transition():
    """규칙 전환일: 바뀐 목록이어도 매수 검토·탈락 대신 재구성 사유 1개 (원장이 새 기준선을 기록)."""
    diff = dg.ListDiff(prev_date="07-13", first_run=False, new=["A", "B"],
                       dropped=[dg.DroppedPick("C", "c", "순위 밖"), dg.DroppedPick("D", "d", "순위 밖")])
    v = dg.decide_actions(diff, [], baseline_note="규칙 v1 로 후보 재구성 (편입 2 · 탈락 2)")
    assert (v.label, v.n_buy, v.n_drop) == ("없음", 0, 0)
    assert v.reasons[0] == "규칙 v1 로 후보 재구성 (편입 2 · 탈락 2)"
    # 보류일이 우선 — 보류 사유만
    vh = dg.decide_actions(diff, [], held_age=20, baseline_note="규칙 v1 로 후보 재구성")
    assert vh.reasons[0] == "후보 보류 (점수 20일 경과)"
    # 노트 없으면 기존 동작
    assert dg.decide_actions(diff, []).label == "매수 검토 2 · 탈락 2"


def test_render_global_keeps_kr_alerts_when_kr_section_failed():
    """한국 시장 섹션이 조회 실패로 지수를 못 그린 날은 KOSPI·KOSDAQ 경보를 글로벌 목록에 남긴다."""
    act = ["KOSPI -25% (임계 -12%)", "KOSDAQ -27% (임계 -15%)"]
    no_kr = dg.render_global(_report(kr=False, active=act))
    assert no_kr[2] == "새 알림 없음 · 경보 유지 2: KOSPI -25% · KOSDAQ -27%"
    rep = _report(active=act)
    rep.kr.kospi = None                                         # 코스피만 조회 실패
    assert dg.render_global(rep)[2] == "새 알림 없음 · 경보 유지 1: KOSPI -25%"


def test_render_kr_market_handles_missing_context():
    rep = _report(kr=False)
    lines = dg.render_kr_market(rep, None)
    assert lines == ["*🇰🇷 한국 시장*", "코스피·코스닥 조회 실패"]


def test_render_ledger_variants(monkeypatch):
    lines, footer = dg.render_ledger(None, now=NOW_KR, has_history=False)
    assert lines == ["*📒 지난 후보 성과*", "첫 발송 — 성과 기록 시작"] and footer is None
    lines, footer = dg.render_ledger(None, now=NOW_KR, has_history=True)
    assert lines[1] == "채점 전 — 발송 후 자동 채점"
    # D1: 후보 보류일 + 요약 없음 → '첫 발송'/'자동 채점' 대신 '원장 미기록' (기록이 실제로 안 되므로)
    for hist in (False, True):
        lines, footer = dg.render_ledger(None, now=NOW_KR, has_history=hist, held=True)
        assert lines == ["*📒 지난 후보 성과*", "후보 보류 중 — 원장 미기록"] and footer is None
    # 요약이 있으면 보류일에도 지난 성과는 그대로 보여 준다
    lines, _ = dg.render_ledger(SUMMARY_KR, now=NOW_KR, has_history=True, held=True)
    assert lines[1].startswith("성적: 07-13 이후")
    stale = dict(SUMMARY_KR, computed_at="2026-10-01T00:00:00+00:00")
    lines, footer = dg.render_ledger(stale, now=NOW_KR, has_history=True)
    assert lines == [] and footer.startswith("성적표 6일째 미갱신")
    # 월요일(현지) 확장 블록
    monday = datetime(2026, 10, 12, 8, 30, tzinfo=dg.KST)
    fresh = dict(SUMMARY_KR, computed_at="2026-10-11T23:00:00+00:00", price_asof="2026-10-09")
    lines, _ = dg.render_ledger(fresh, now=monday, has_history=True, weekly=True)
    assert any("주간 성적표" in ln for ln in lines)


def test_render_status_none_when_healthy_and_combines_footer():
    assert dg.render_status(_health(), NOW_KR) is None
    assert dg.render_status(_health(), NOW_KR, ["성적표 3일째 미갱신(마지막 10-02)"]) == \
        "⚙️ 상태: 성적표 3일째 미갱신(마지막 10-02)"


def test_price_metrics_from_frame_hand_calc():
    idx = pd.bdate_range("2025-10-01", periods=260)
    close = pd.Series([100.0] * 259 + [90.0], index=idx)
    df = pd.DataFrame({"Close": close, "Adj Close": close * 0.98})
    m = dg.price_metrics_from_frame(df)
    assert m.price == 90.0 and m.adj_last == pytest.approx(88.2)
    assert m.dd_52w_pct == pytest.approx(-10.0)
    assert m.vs_ma200_pct == pytest.approx((90 / ((199 * 100 + 90) / 200) - 1) * 100)
    assert m.mdd_1y_pct == pytest.approx(-10.0)
    assert m.asof == idx[-1].strftime("%Y-%m-%d")
    assert dg.price_metrics_from_frame(pd.DataFrame()) is None
    # D2: Adj Close 시리즈를 함께 돌려준다 (tz 없는 날짜 index)
    assert m.adj_close is not None and len(m.adj_close) == 260 and m.adj_close.index.tz is None
    assert float(m.adj_close.iloc[0]) == pytest.approx(98.0)
    # tz-aware index 도 날짜로 정규화
    df_tz = df.copy()
    df_tz.index = idx.tz_localize("America/New_York")
    m2 = dg.price_metrics_from_frame(df_tz)
    assert m2.adj_close.index.tz is None and str(m2.adj_close.index[0])[:10] == "2025-10-01"


# ----------------------------------------------------------------------
# D2: 진입 기준가는 같은 프레임에서 — 분할 소급에도 '선정 후' 가 실제 수익률
# ----------------------------------------------------------------------


def _split_frame(entry="2026-07-13", periods=80, split_at=40, ratio=2.0):
    """진입 후 ratio:1 분할 — yfinance 식으로 과거 Adj Close 가 전부 1/ratio 로 소급됨.
    분할 전 '원래' 가격 100 → 분할 소급 후 50, 마지막 날은 실제 +10% (55)."""
    idx = pd.bdate_range(entry, periods=periods)
    raw = [100.0] * split_at + [110.0 / ratio] * (periods - split_at)   # 분할 후 거래 가격
    adj = [v / ratio if i < split_at else v for i, v in enumerate(raw)]   # 소급 반영
    return pd.Series(adj, index=idx)


def test_entry_base_from_frame_rules():
    adj = _split_frame()
    assert dg.entry_base_from_frame(adj, "2026-07-13", None) == (50.0, "frame")
    # entry_date 없으면 first_sent_date 당일 또는 이후 첫 거래일 (07-18 토요일 → 07-20 월요일)
    assert dg.entry_base_from_frame(adj, None, "2026-07-18") == (50.0, "frame")
    assert dg.entry_base_from_frame(adj, None, "2026-07-14") == (50.0, "frame")
    # 프레임 첫 관측보다 앞선 발송일은 (하루 차이라도) 창 밖 — 프레임이 진입일을 덮는지 알 수 없음
    assert dg.entry_base_from_frame(adj, None, "2026-07-11") == (None, "out_of_window")
    # 프레임 첫 관측 이전 진입 → 저장 entry_px 폴백 대상
    assert dg.entry_base_from_frame(adj, "2025-01-02", None) == (None, "out_of_window")
    assert dg.entry_base_from_frame(None, "2026-07-13", None) == (None, "missing")
    assert dg.entry_base_from_frame(adj, None, None) == (None, "missing")
    assert dg.entry_base_from_frame(adj, "2030-01-01", None) == (None, "missing")


def test_since_pct_uses_frame_entry_after_split_not_stored_px():
    """저장 entry_px 는 분할 전 100, 오늘 adj 55 → 순진 계산 -45%. 프레임 기준은 +10%."""
    adj = _split_frame()
    m = dg.PriceMetrics(price=55.0, asof="2026-10-30", adj_last=55.0, vs_ma200_pct=None,
                        dd_52w_pct=None, mdd_1y_pct=None, vol_pct=None, adj_close=adj)
    ent = {"entry_px": 100.0, "entry_date": "2026-07-13", "first_sent_date": "2026-07-13"}
    since, src = dg.since_pct_for(m, ent)
    assert src == "frame" and since == pytest.approx(10.0)
    # 창 밖 진입 → 저장 entry_px
    ent_old = {"entry_px": 50.0, "entry_date": "2025-01-02", "first_sent_date": "2025-01-02"}
    since2, src2 = dg.since_pct_for(m, ent_old)
    assert src2 == "stored" and since2 == pytest.approx(10.0)
    # 시리즈 없음(구 테스트 스텁) → 저장 entry_px
    m_no = dg.PriceMetrics(price=55.0, asof="x", adj_last=55.0, vs_ma200_pct=None, dd_52w_pct=None,
                           mdd_1y_pct=None, vol_pct=None)
    assert dg.since_pct_for(m_no, {"entry_px": 50.0}) == (pytest.approx(10.0), "stored")
    assert dg.since_pct_for(m_no, {"entry_px": None}) == (None, "none")
    assert dg.since_pct_for(None, ent) == (None, "none")
    assert dg.since_pct_for(m, None) == (None, "none")
    # 진입일이 프레임 마지막 관측 이후(낡은 캐시 프레임) → 저장 entry_px 와 섞지 않고 None
    ent_future = {"entry_px": 50.0, "entry_date": "2030-01-02", "first_sent_date": "2030-01-02"}
    assert dg.since_pct_for(m, ent_future) == (None, "none")


def test_predictions_and_weights_only_behind_flags():
    assert dg.render_predictions([]) == []
    assert dg._weights_line({"CALM": 0.2, "HG": 0.18}) == "제안 비중(참고, 검증 전): CALM 20% · HG 18% · 현금 62%"
    assert dg._weights_line(None) is None
    assert dg.with_summary("본문", None) == "본문"
    assert dg.with_summary("본문", "요약_x") == "🧠 요약＿x\n\n본문"      # 요약도 md_safe


# ----------------------------------------------------------------------
# 길이 예산 — 하드캡 초과 시 유지 종목 줄부터 축약 (줄 중간 자르기 금지)
# ----------------------------------------------------------------------


def test_render_with_budget_collapses_keep_lines_first():
    new_rows = [_row(f"N{i}", "회사이름이아주긴회사명입니다" * 2, 95, 90, 90) for i in range(6)]
    keep_rows = [_row(f"K{i}", "회사이름이아주긴회사명입니다" * 2, 90, 90, 90) for i in range(6)]
    views = [dg.PickView(row=r, kind="new", metrics=_metrics(100, 1, -1, -1, 1),
                         evidence="PER 4.0배 · PBR 0.46 · ROE 11.5% · 지주사") for r in new_rows]
    views += [dg.PickView(row=r, kind="keep", metrics=_metrics(100, 1, -1, -1, 1), since_pct=1.0)
              for r in keep_rows]
    alerts = [Alert("warning", "drawdown", "자산 낙폭 " + "설명 " * 60, key=f"dd:a{i}:enter") for i in range(5)]
    diff = dg.ListDiff("2026-10-07", False, new=[r.symbol for r in new_rows],
                       kept=[r.symbol for r in keep_rows])
    inp = _kr_quiet_inputs(views=views, diff=diff, report=_report(alerts=alerts),
                           verdict=dg.decide_actions(diff, alerts))
    assert len(dg.render_digest(inp)) > dg.LEN_HARD_CAP
    text = dg.render_with_budget(inp)
    assert len(text) <= dg.LEN_HARD_CAP
    assert "유지 6 — /scan kr" in text                      # 유지 종목 줄부터 축약
    assert not any(ln.startswith("K0 ") for ln in text.splitlines())
    assert all(len(ln) < dg.LEN_HARD_CAP for ln in text.splitlines())   # 줄 중간 자르기 없음
    assert_bold_only(text)


# ----------------------------------------------------------------------
# assemble_daily_digest — fetch/DB 경계를 스텁 (쓰기 0, tmp DB)
# ----------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    import src.ledger as ledger_mod

    monkeypatch.setenv("QUANT_BOT_DB_PATH", str(tmp_path / "digest.db"))
    monkeypatch.setattr(storage_mod, "_storage", None)
    monkeypatch.setattr(ledger_mod, "_schema_ready_store", None)
    yield tmp_path / "digest.db"
    monkeypatch.setattr(storage_mod, "_storage", None)


def _wire(monkeypatch, *, picks, prev=(), starts=None, entries=None, summary=None,
          report=None, fresh=None, metrics=None, dd_state=None):
    """assemble_daily_digest 의 외부 경계 전부 스텁."""
    import src.ledger as ledger_mod
    import src.signals as sig
    import src.universe as uni

    monkeypatch.setattr(uni, "pick_top", lambda market, n=6, **k: list(picks))
    monkeypatch.setattr(uni, "freshness", lambda market="US": fresh or
                        {"fund_asof": "2026-10-05", "price_asof": "2026-10-07", "age_days": 3})
    monkeypatch.setattr(uni, "lookup", lambda s, market=None: None)
    monkeypatch.setattr(uni, "lookup_detail", lambda s, market=None: None)
    rep = report if report is not None else _report(market=picks[0].market if picks else "KR")
    if dd_state is not None and rep.pending_state is not None:
        rep.pending_state["pick_dd10"] = dd_state
    monkeypatch.setattr(sig, "generate_signal_report", lambda **k: rep)
    monkeypatch.setattr(ledger_mod, "last_sent_list",
                        lambda m: ("2026-10-07" if prev else None, list(prev)))
    monkeypatch.setattr(ledger_mod, "inclusion_start", lambda m, syms=None: dict(starts or {}))
    # 실제 entry_prices(market, starts) 는 (symbol, starts[symbol]) 정확 일치만 돌려준다 (D3) —
    # 스텁은 entries 에 first_sent_date 가 있으면 같은 규칙을 흉내 내고, 없으면 그대로.
    def _entries(m, st=None):
        out = {}
        for s, e in (entries or {}).items():
            if st is not None and e.get("first_sent_date") and st.get(s) != e["first_sent_date"]:
                continue
            out[s] = e
        return out
    monkeypatch.setattr(ledger_mod, "entry_prices", _entries)
    monkeypatch.setattr(ledger_mod, "load_summary", lambda m: summary)
    mt = metrics or {}
    monkeypatch.setattr(dg, "_price_metrics",
                        lambda sym, market, ex, sec: mt.get(sym, _metrics(100.0, 1.0, -5.0, -10.0, 20.0)))
    monkeypatch.setattr(dg, "_overnight_us_line", lambda: OVERNIGHT)
    monkeypatch.setattr(dg, "_bench_price_asof", lambda market: "2026-10-07")
    return rep


def test_assemble_quiet_day_bundle(fresh_db, monkeypatch):
    prev = _prev(*[(r.symbol, r.total_score) for r in KR_ROWS])
    _wire(monkeypatch, picks=KR_ROWS, prev=prev,
          starts={r.symbol: "2026-07-13" for r in KR_ROWS},
          entries={r.symbol: {"entry_px": 100.0} for r in KR_ROWS}, summary=SUMMARY_KR)
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert b.market == "KR" and not b.hold and b.record_ledger and b.dd10_new == {}
    assert b.verdict.label == "없음" and b.diff.kept == [r.symbol for r in KR_ROWS]
    assert b.score_asof == "2026-10-05" and b.health.price_as_of == "2026-10-07"
    assert "선정 후 +0.0%" in b.text and "변화 없음 (07-13 이후 동일)" in b.text
    assert b.silent is False                        # 기본: 항상 소리
    assert_bold_only(b.text)


def test_assemble_pick_dd10_fires_once_and_updates_pending_state(fresh_db, monkeypatch):
    prev = _prev(("009970", 92), ("007700", 92))
    rows = KR_ROWS[:2]
    rep = _wire(monkeypatch, picks=rows, prev=prev,
                starts={"009970": "2026-07-13", "007700": "2026-07-13"},
                entries={"009970": {"entry_px": 100.0}, "007700": {"entry_px": 100.0}},
                metrics={"009970": _metrics(89.0, -5, -11, -11, 30, adj=89.0),
                         "007700": _metrics(95.0, -5, -5, -5, 30, adj=95.0)})
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert b.dd10_new == {"009970": "2026-07-13"}
    assert b.verdict.label == "리스크 점검 1" and "영원무역홀딩스 -11%" in b.verdict.reasons
    assert "⚠️ 영원무역홀딩스 선정 후 -11.0% — 보유 시 점검 (1회 알림)" in b.text
    assert "새 알림 1건 (위) · 경보 유지" in b.text          # 🌐 줄의 카운트에 종목 알림 포함
    assert rep.pending_state["pick_dd10"] == {"KR": {"009970": "2026-07-13"}}
    # 같은 진입(시작일 동일)이 이미 기록돼 있으면 재발화하지 않음
    _wire(monkeypatch, picks=rows, prev=prev,
          starts={"009970": "2026-07-13", "007700": "2026-07-13"},
          entries={"009970": {"entry_px": 100.0}}, metrics={"009970": _metrics(80.0, -5, -20, -20, 30)},
          dd_state={"KR": {"009970": "2026-07-13"}})
    b2 = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert b2.dd10_new == {} and b2.verdict.label == "없음"


def test_assemble_stale_scores_warn_and_hold(fresh_db, monkeypatch):
    rows = [ScanRow(**{**r.__dict__, "fund_asof": "2026-09-29"}) for r in KR_ROWS]
    _wire(monkeypatch, picks=rows, fresh={"fund_asof": "2026-09-29", "price_asof": None, "age_days": 9})
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "⚠️ 점수 09-29 (9일 전, 주간 갱신 지연)" in b.text and b.record_ledger
    rows15 = [ScanRow(**{**r.__dict__, "fund_asof": "2026-09-23"}) for r in KR_ROWS]
    _wire(monkeypatch, picks=rows15, fresh={"fund_asof": "2026-09-23", "price_asof": None, "age_days": 15})
    b2 = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "후보 보류 — 점수 15일 경과, 재점수 필요" in b2.text
    assert b2.record_ledger is False and not b2.hold          # 보류 섹션이지만 발송은 함


def test_assemble_held_day_verdict_is_neutral_even_if_list_differs(fresh_db, monkeypatch):
    """D1 수용: 점수 16일 전 + 직전 원장과 다른 picks → 첫 줄에 매수/탈락/기준 수립 없음, '후보 보류' 포함."""
    rows16 = [ScanRow(**{**r.__dict__, "fund_asof": "2026-09-22"}) for r in KR_ROWS]
    prev = _prev(("OLD1", 95), ("OLD2", 94), *[(r.symbol, r.total_score) for r in KR_ROWS[2:]])
    _wire(monkeypatch, picks=rows16, prev=prev,
          fresh={"fund_asof": "2026-09-22", "price_asof": None, "age_days": 16})
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    first = b.text.splitlines()[0]
    assert "오늘 할 일: 없음" in first and "후보 보류 (점수 16일 경과)" in first
    assert "매수 검토" not in first and "탈락" not in first and "기준 수립" not in first
    assert b.diff.changed and b.verdict.n_buy == 0 and b.verdict.n_drop == 0   # diff 는 남되 판정만 중립
    assert b.record_ledger is False and b.dd10_new == {}
    # 원장이 빈 보류일: '첫 발송 — 성과 기록 시작' 대신 '후보 보류 중 — 원장 미기록'
    _wire(monkeypatch, picks=rows16, fresh={"fund_asof": "2026-09-22", "price_asof": None, "age_days": 16})
    b0 = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "첫 발송" not in b0.text and "후보 보류 중 — 원장 미기록" in b0.text
    assert "기준 수립" not in b0.text.splitlines()[0]
    # 보류일에도 새 알림은 '리스크 점검' 으로 남는다
    alert = Alert("warning", "drawdown", "금 52주 고점 대비 -12%", key="dd:금:enter")
    _wire(monkeypatch, picks=rows16, prev=prev, report=_report(alerts=[alert]),
          fresh={"fund_asof": "2026-09-22", "price_asof": None, "age_days": 16})
    b2 = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "오늘 할 일: 리스크 점검 1" in b2.text.splitlines()[0]
    assert "후보 보류 (점수 16일 경과)" in b2.text.splitlines()[0]
    assert_bold_only(b2.text)


def test_assemble_since_pct_split_safe_and_anomaly_guard(fresh_db, monkeypatch):
    """D2 수용: 진입 후 2:1 분할 — 저장 entry_px 100(분할 전), 프레임 adj 는 50→55 → +10%, dd10 미발화.
    |선정 후| > 60% 는 이상치 — 표시 52주 위치·dd10 미발화·소유자 상세."""
    rows = KR_ROWS[:2]
    prev = _prev(*[(r.symbol, r.total_score) for r in rows])
    split_m = dg.PriceMetrics(price=55.0, asof="2026-10-07", adj_last=55.0, vs_ma200_pct=-5.0,
                              dd_52w_pct=-11.0, mdd_1y_pct=-11.0, vol_pct=30.0, adj_close=_split_frame())
    bad_idx = pd.bdate_range("2026-07-13", periods=60)
    bad_m = dg.PriceMetrics(price=30.0, asof="2026-10-07", adj_last=30.0, vs_ma200_pct=-5.0,
                            dd_52w_pct=-70.0, mdd_1y_pct=-70.0, vol_pct=30.0,
                            adj_close=pd.Series([100.0] * 59 + [30.0], index=bad_idx))
    ent = {"entry_px": 100.0, "entry_date": "2026-07-13", "first_sent_date": "2026-07-13"}
    _wire(monkeypatch, picks=rows, prev=prev,
          starts={"009970": "2026-07-13", "007700": "2026-07-13"},
          entries={"009970": dict(ent), "007700": dict(ent)},
          metrics={"009970": split_m, "007700": bad_m})
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    lines = b.text.splitlines()
    assert any(ln.startswith("009970 ") and "선정 후 +10.0%" in ln for ln in lines)
    assert any(ln.startswith("007700 ") and "52주 고점比 -70%" in ln and "선정 후" not in ln for ln in lines)
    assert b.dd10_new == {} and b.verdict.label == "없음"
    assert any("선정 후 수익률 이상치 007700 (-70.0%" in x for x in b.owner_extra)
    # 고정 키 — 소유자 DM signature 에 들어가 처음 나타난 날 1회 알림 (구독자 상태 줄에는 안 나감)
    assert "선정 후 수익률 이상치" in b.health.optional_failures
    assert "이상치" not in (b.health.status_line(NOW_KR) or "")
    assert b.owner_extra is b.health.notes                     # D4: 상세는 health.notes 로 흘러간다
    # 창 밖 진입(프레임이 진입일을 못 덮음) → 저장 entry_px 그대로 (소급 위험은 채점기와 같은 폴백)
    old_ent = {"entry_px": 50.0, "entry_date": "2025-01-02", "first_sent_date": "2025-01-02"}
    _wire(monkeypatch, picks=rows[:1], prev=prev[:1], starts={"009970": "2025-01-02"},
          entries={"009970": old_ent}, metrics={"009970": split_m})
    b2 = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "선정 후 +10.0%" in b2.text


def test_assemble_entry_must_match_inclusion_round_exactly(fresh_db, monkeypatch):
    """D3 수용: v0 백필(07-13) 진입가만 있고 현재 회차(v1, 10-08) 결과가 없으면 since_pct None·dd10 미발화.
    v1 결과가 생기면 v1 진입가 기준."""
    rows = KR_ROWS[:1]
    prev = _prev(("009970", 92))                                # v1 live 리스트 (10-07)
    v0_entry = {"entry_px": 100.0, "entry_date": "2026-07-13", "first_sent_date": "2026-07-13"}
    m = _metrics(78.0, -5, -22, -22, 30, adj=78.0)             # v0 기준이면 -22% → 가짜 dd10
    _wire(monkeypatch, picks=rows, prev=prev, starts={"009970": "2026-10-08"},
          entries={"009970": v0_entry}, metrics={"009970": m})
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "선정 후" not in b.text and "52주 고점比 -22%" in b.text
    assert b.dd10_new == {} and b.verdict.label == "없음"
    v1_entry = {"entry_px": 80.0, "entry_date": "2026-10-08", "first_sent_date": "2026-10-08"}
    _wire(monkeypatch, picks=rows, prev=prev, starts={"009970": "2026-10-08"},
          entries={"009970": v1_entry}, metrics={"009970": m})
    b2 = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "선정 후 -2.5%" in b2.text and b2.dd10_new == {}


def test_assemble_rule_version_transition_day_notes_restart(fresh_db, monkeypatch):
    """D3: 직전 리스트가 옛 규칙(v0)이면 그날 1회 '규칙 v1 기준으로 성과 집계 재시작' + 옛 진입가 미사용."""
    from src.ledger import LedgerPick

    rows = KR_ROWS[:2]
    prev = [LedgerPick("2026-07-13", "KR", i + 1, r.symbol, r.symbol, "069500.KS", "v0", "reconstructed",
                       name=r.name, total_score=r.total_score) for i, r in enumerate(rows)]
    ent = {"entry_px": 100.0, "entry_date": "2026-07-13", "first_sent_date": "2026-07-13"}
    _wire(monkeypatch, picks=rows, prev=prev, starts={r.symbol: "2026-07-13" for r in rows},
          entries={r.symbol: dict(ent) for r in rows},
          metrics={r.symbol: _metrics(70.0, -5, -30, -30, 30, adj=70.0) for r in rows})
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert "규칙 v1 기준으로 성과 집계 재시작" in b.text
    assert "선정 후" not in b.text and b.dd10_new == {}
    # 판정: 원장이 새 규칙 기준선을 기록하는 날 — 매수 검토·탈락 대신 '재구성'
    assert b.verdict.n_buy == 0 and b.verdict.n_drop == 0
    assert "규칙 v1 로 후보 재구성" in b.verdict.reasons
    assert any("규칙 버전 전환 v0 → v1" in x for x in b.owner_extra)
    assert_bold_only(b.text)
    # 직전 리스트가 현재 규칙이면 표기 없음
    _wire(monkeypatch, picks=rows, prev=_prev(*[(r.symbol, r.total_score) for r in rows]))
    assert "성과 집계 재시작" not in dg.assemble_daily_digest("kr", now=NOW_KR).text


def test_assemble_price_failure_uses_fixed_key_and_notes(fresh_db, monkeypatch):
    """D4: optional_failures 는 고정 키('가격 조회 실패'), 개수·종목은 notes — signature 가 매일 안 바뀐다."""
    _wire(monkeypatch, picks=KR_ROWS)
    monkeypatch.setattr(dg, "_price_metrics", lambda sym, market, ex, sec: None if sym in ("009970", "007700")
                        else _metrics(100.0, 1.0, -5.0, -10.0, 20.0))
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert b.health.optional_failures == ["가격 조회 실패"]
    assert "가격 조회 실패 2종목: 009970, 007700" in b.health.notes
    assert b.health.signature(NOW_KR) == ["optional:가격 조회 실패"]
    assert "가격 조회 실패" in b.text and "2종목" not in b.text


def test_assemble_owner_ledger_line_mode_puts_line_in_notes(fresh_db, monkeypatch):
    """D7: QUANT_BOT_LEDGER_LINE=owner — 구독자 본문에는 📒 없음, 소유자 상세에 성과 줄."""
    prev = _prev(*[(r.symbol, r.total_score) for r in KR_ROWS])
    _wire(monkeypatch, picks=KR_ROWS, prev=prev, summary=SUMMARY_KR)
    object.__setattr__(settings, "ledger_line", "owner")
    try:
        b = dg.assemble_daily_digest("kr", now=NOW_KR)
    finally:
        object.__setattr__(settings, "ledger_line", "on")
    assert "*📒 지난 후보 성과*" not in b.text
    assert any(x.startswith("성과: 성적: 07-13 이후") for x in b.health.notes)


def test_assemble_hold_when_no_picks_or_degraded(fresh_db, monkeypatch):
    _wire(monkeypatch, picks=[])
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert b.hold and b.hold_reason == "KR 후보 0개" and b.record_ledger is False
    _wire(monkeypatch, picks=KR_ROWS)
    monkeypatch.setattr(storage_mod.get_storage(), "init_sync", "timeout", raising=False)
    b2 = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert b2.hold and b2.degraded and b2.hold_reason.startswith("강등")


def test_assemble_us_has_no_kr_sections_and_silent_option(fresh_db, monkeypatch):
    prev = _prev(*[(r.symbol, r.total_score) for r in US_ROWS])
    _wire(monkeypatch, picks=US_ROWS, prev=prev, report=_report("US", kr=False),
          starts={r.symbol: "2026-07-13" for r in US_ROWS})
    object.__setattr__(settings, "silent_quiet", True)
    try:
        b = dg.assemble_daily_digest("us", now=NOW_US)
    finally:
        object.__setattr__(settings, "silent_quiet", False)
    assert "한국 시장" not in b.text and "*🌐 글로벌*" not in b.text
    assert "*🌐 아침 이후 변화* — 없음" in b.text and "개장 전" in b.text
    assert b.silent is True and len(b.text) <= dg.LEN_BUDGET_QUIET["US"]
    assert_bold_only(b.text)


def test_assemble_survives_signal_report_failure(fresh_db, monkeypatch):
    import src.signals as sig
    from src.exceptions import DataFetchError

    _wire(monkeypatch, picks=KR_ROWS)

    def boom(**k):
        raise DataFetchError("panel down")

    monkeypatch.setattr(sig, "generate_signal_report", boom)
    b = dg.assemble_daily_digest("kr", now=NOW_KR)
    assert b.report is None and "코스피·코스닥 조회 실패" in b.text and "국면 조회 실패" in b.text
    assert "신호 리포트" in b.health.optional_failures
    assert_bold_only(b.text)


def test_build_daily_digest_keeps_string_contract(fresh_db, monkeypatch):
    _wire(monkeypatch, picks=KR_ROWS)
    out = dg.build_daily_digest(market="kr", top_n=6, tickers=("NVDA",), suggest_weights=True)
    assert isinstance(out, str) and out.startswith("*🇰🇷 ")
