"""
src/bot_commands.py
===================
Phase 11b — 인터랙티브 봇 조회 명령 응답 (호스트 무관 코어).

상시 호스트(폴링 워커)가 getUpdates 로 받은 조회 명령을 여기서 처리. 무거운 분석은
cron 이 Turso 에 사전계산해 두고, 이 응답기는 **유니버스 DB 읽기 위주(경량)** +
유저별 rate limit 으로 남용을 막는다.

명령 (조회 전용):
  /stock <티커>        종목 점수 + 근거 분해 + 기준일 (universe.lookup + lookup_detail)
  /scan [us|kr] [N]    점수 상위 후보 (universe.pick_top — 일일 리포트와 같은 선정 규칙)
  /ledger [us|kr]      지난 후보 성과 원장 (ledger.report_text, 평문)
  /news <티커>         최근 뉴스 헤드라인 (data_fetcher.fetch_stock_news — FMP, 캐시 30분)
  /help                도움말

표면 정합(WP-SURFACE): 점수는 DB(total/value/health) 단일 소스, 라벨은 LABEL_VALUE/LABEL_HEALTH
한 곳에서만 정의, 기준일(fund_asof/price_asof)·제외 사유·flags 를 같이 보여 준다.

게이팅: /stock·/scan 은 폴링 루프(scripts/bot.py)가 active 구독자(+소유자)만 통과시킴.
/news·/ledger 는 같은 기준을 **이 모듈(handle_news/handle_ledger)** 이 검사 — 루프의 게이트 튜플이 news 를
모르는 상태에서도 동일하게 잠기도록 (라이브 데이터라 남용 방지가 특히 중요).

구독 명령(/start·/stop·/approve…)은 subscribers.py 담당 — parse_command 는 그 외 명령을
"unknown" 으로 흘려보내고, 실제 라우팅(조회 vs 구독)은 폴링 루프에서 한다(호스트 결정 후).

설계: parse_command/format_* 는 **순수 함수**(오프라인 테스트), handle_* 는 DB 읽기,
respond() 가 파싱→rate limit→핸들러 디스패치. 응답 문자열만 반환하고 전송은 호출부(notifier).

인라인 버튼: 가입 요청 알림의 [✅ 승인][❌ 거절] (callback_query) 은 handle_callback 이
처리 — 소유자 검증 후 subscribers.decide_request 재사용, 모든 경로에서 answerCallbackQuery.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from datetime import date, datetime, timezone

from src.exceptions import DataFetchError, MissingApiKeyError
from src.health import UNIVERSE_STALE_DAYS
from src.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 표면 공통 상수 — 라벨·면책·문구는 이 블록 한 곳에서만 정의 (CMAP-02/09).
# WP-DIGEST 가 src/findings.py 에 LABEL_VALUE/LABEL_HEALTH 를 추가하므로, 있으면 그 값을
# 쓰고 없으면 같은 값('밸류'/'건전성')으로 폴백 → 병합 후 별도 수정 없이 단일 소스가 됨.
# ---------------------------------------------------------------------------
try:
    from src.findings import LABEL_HEALTH, LABEL_VALUE
except ImportError:  # 병합 전(상수 미존재) 폴백
    LABEL_VALUE = "밸류"
    LABEL_HEALTH = "건전성"

# 크립토는 screened 컬럼을 재사용해 value_score=변동성점수, health_score=순위점수 로 저장됨
# (universe._store_crypto_scored) → 표시 라벨을 시장별로 바꾼다 (CMAP-05).
LABEL_CRYPTO_VALUE = "변동성"
LABEL_CRYPTO_HEALTH = "순위"

DISCLAIMER = "투자 자문 아님, 모든 결정은 본인 책임."

SCAN_DEFAULT_N = 6          # 일일 리포트 후보 수와 동일
SCAN_MAX_N = 10             # /scan us 10 처럼 늘릴 수 있는 상한 (메시지 길이 보호)
NEWS_LIMIT = 5
LEDGER_MAX_CHARS = 3500     # 텔레그램 4096자 한도 여유

# 제외 사유(universe.ScanRow.excluded) → 사용자용 설명
_EXCLUDED_REASON = {
    "sibling": "보통주 아님(같은 회사의 다른 상장 라인)",
    "suffix": "보통주 아님(우선주·워런트 등)",
    "name": "보통주 아님(우선주·상장채권·신탁 등)",
    "otc": "장외(OTC) 종목",
    "shell": "껍데기 회사(SPAC 등)",
    "illiquid": "거래대금 부족",
    "below_floor": "시가총액 기준 미달",
    "halted": "거래정지",
    "not_listed": "현재 상장 목록에 없음",
    "kr_adr": "해외 예탁증서",
}

# detail.flags → 꼬리표 (UI 에 의미 있는 것만)
_FLAG_TAG = {
    "holding": "지주사",
    "one_off": "일회성 이익 보정",
    "fin_neutral": "금융업 일부 항목 중립",
    "ey_outlier": "이익수익률 이상치 보정",
    "fcf_outlier": "FCF 이상치 보정",
    "cr_missing": "유동비율 결측",
}

_MD_UNSAFE = str.maketrans({c: "" for c in "_*`[]\\"})


def _safe(text: object) -> str:
    """동적 문자열(회사명 등)에서 legacy Markdown 특수문자 제거 (§4.10 #10 — 백슬래시 이스케이프 불가)."""
    return str(text if text is not None else "").translate(_MD_UNSAFE)

# /news 티커 검증 — URL 쿼리로 그대로 나가므로 영숫자·점·하이픈 1~10자만 허용 (인젝션 방지)
_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")

# scripts/bot.py 의 _GATE_MSG 와 동일 문구 (/news 는 이 모듈이 직접 게이트)
GATE_MSG = "🔒 구독자 전용입니다. /start 로 가입 요청 후 소유자 승인을 받으면 이용할 수 있어요."


@dataclass
class Command:
    """파싱된 조회 명령 (순수)."""
    kind: str            # stock | scan | ledger | news | help | unknown
    arg: str | None = None
    arg2: str | None = None     # /scan us 10 의 두 번째 인자


def parse_command(text: str) -> Command:
    """명령 텍스트 → Command. `/cmd@botname`(그룹) 허용. 조회 명령만 인식."""
    parts = (text or "").split()
    if not parts:
        return Command("unknown")
    cmd = parts[0].lower().split("@", 1)[0]
    arg = parts[1] if len(parts) > 1 else None
    arg2 = parts[2] if len(parts) > 2 else None
    if cmd == "/stock":
        return Command("stock", arg)
    if cmd == "/scan":
        return Command("scan", arg, arg2)
    if cmd == "/ledger":
        return Command("ledger", arg)
    if cmd == "/news":
        return Command("news", arg)
    if cmd in ("/help", "/menu"):
        return Command("help")
    return Command("unknown")


# 버튼(reply keyboard) 라벨 → 명령. 봇 루프가 수신 텍스트를 이 표로 정규화한 뒤 디스패치.
BUTTON_US = "🇺🇸 미국 후보"
BUTTON_KR = "🇰🇷 한국 후보"
BUTTON_TO_COMMAND = {
    BUTTON_US: "/scan us",
    BUTTON_KR: "/scan kr",
    "❓ 도움말": "/help",
    "📋 구독자": "/subscribers",   # 관리자 — subscribers.apply_events 가 처리
    "⏳ 승인 대기": "/pending",    # 관리자
    # 구버전 키보드('추천' 어휘)가 사용자 폰에 남아 있어도 동작하도록 별칭 유지 (신규 키보드엔 미노출)
    "🇺🇸 미국 추천": "/scan us",
    "🇰🇷 한국 추천": "/scan kr",
}


def main_keyboard(is_owner: bool = False) -> dict:
    """지속형 reply keyboard. 탭하면 라벨이 전송되고 BUTTON_TO_COMMAND 로 명령화됨.

    소유자에겐 관리자 버튼(구독자/대기 목록) 추가. /stock 은 인자가 필요해 버튼 대신 직접 입력.
    """
    rows = [[BUTTON_US, BUTTON_KR], ["❓ 도움말"]]
    if is_owner:
        rows.append(["📋 구독자", "⏳ 승인 대기"])
    return {"keyboard": rows, "resize_keyboard": True}


# ---------------------------------------------------------------------------
# 인라인 승인/거절 버튼 (callback_query) — 가입 요청 원탭 처리
# ---------------------------------------------------------------------------

# callback_data 포맷: "<action>:<chat_id>" (예: "approve:12345", "deny:12345")
_CB_ACTIONS = ("approve", "deny")


def approval_keyboard(chat_id: str) -> dict:
    """가입 요청 알림에 붙는 인라인 [✅ 승인][❌ 거절] 버튼 (순수).

    callback_data 인코딩과 parse_callback 디코딩은 이 모듈에 함께 둠 (포맷 한 곳 관리).
    """
    return {"inline_keyboard": [[
        {"text": "✅ 승인", "callback_data": f"approve:{chat_id}"},
        {"text": "❌ 거절", "callback_data": f"deny:{chat_id}"},
    ]]}


def parse_callback(data: str) -> tuple[str, str] | None:
    """callback_data → (action, 대상 chat_id). 형식 불일치/미지원 action 은 None (순수)."""
    action, sep, target = (data or "").partition(":")
    if not sep or action not in _CB_ACTIONS or not target:
        return None
    return action, target


def handle_callback(callback_query: dict, owner: str | None) -> None:
    """callback_query(인라인 버튼 탭) 처리 — 승인/거절은 **소유자만**.

    텔레그램은 모든 callback_query 에 answerCallbackQuery 응답을 요구하므로,
    성공·거부·오류 어느 경로에서든 응답(토스트)을 보낸다 (finally). 상태 변경은
    subscribers.decide_request 재사용 (/approve·/deny 텍스트 명령과 동일 코어).
    이미 처리된 대상(더블탭·텍스트 명령 선처리)은 "이미 처리됨" 으로 멱등 응답.
    처리 후 원본 알림 메시지를 editMessageText 로 결과 표기 + 버튼 제거 (best-effort).
    """
    from src import subscribers
    from src.notifier import answer_callback_safe, edit_message_safe

    cq_id = str(callback_query.get("id") or "")
    sender = callback_query.get("from") or {}
    sender_id = str(sender["id"]) if sender.get("id") is not None else None
    msg = callback_query.get("message") or {}
    msg_chat = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")

    toast = "⚠️ 처리 중 오류가 발생했습니다."       # 예외 경로 기본 토스트
    mark: str | None = None                          # 원본 메시지에 남길 결과 표기
    try:
        if owner is None or sender_id != owner:
            logger.info("비소유자 콜백 거부 (from=%s, data=%s)",
                        sender_id, callback_query.get("data"))
            toast = "🔒 소유자만 사용할 수 있는 버튼입니다."
            return
        parsed = parse_callback(callback_query.get("data") or "")
        if parsed is None:
            toast = "알 수 없는 버튼입니다."
            return
        action, target = parsed
        status = subscribers.subscriber_status(target)
        if status is None:
            toast = "⚠️ 가입 요청 기록이 없습니다."
            return
        if status != "pending":
            # 더블탭 / 텍스트 명령(/approve 등)으로 선처리 → 멱등 (상태 변경 없음)
            toast = "이미 처리됨"
            return
        approve = action == "approve"
        subscribers.decide_request(target, approve=approve)   # 상태 변경 + 신청자 알림
        toast = "승인 완료" if approve else "거절 완료"
        mark = "✅ 승인됨" if approve else "❌ 거절됨"
        logger.info("콜백 %s 처리: target=%s", action, target)
    finally:
        # 어느 경로든 콜백 응답 (미응답 시 클라이언트 로딩 스피너 지속) — best-effort
        if cq_id:
            answer_callback_safe(cq_id, toast)

    # 결과를 원본 알림에 표기 + 인라인 버튼 제거 (reply_markup 생략 = 키보드 제거)
    if mark and msg_chat is not None and msg_id is not None:
        base = (msg.get("text") or "").strip()
        new_text = f"{base}\n\n→ {mark}" if base else f"→ {mark}"
        edit_message_safe(str(msg_chat), int(msg_id), new_text)


# ---------------------------------------------------------------------------
# 포매팅 (순수 — 데이터 주면 문자열)
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📖 *명령어*\n"
    "`/stock <티커>` — 종목 점수와 근거 (예: `/stock AAPL`, `/stock 005930`)\n"
    "`/scan [us|kr] [개수]` — 점수 상위 후보 (기본 us 6종목, 일일 리포트와 같은 선정)\n"
    "`/ledger [us|kr]` — 지난 후보 성과 기록\n"
    "`/news <티커>` — 최근 뉴스 헤드라인 (예: `/news TSLA`)\n"
    "`/menu` — 버튼 메뉴 열기\n"
    "\n구독: `/start` 가입 요청 · `/stop` 해지\n"
    "\n점수는 시장 안에서의 상대 점수이며, 모든 구독자에게 같은 기준으로 제공됩니다.\n"
    f"{DISCLAIMER}\n"
    "\n👇 아래 버튼으로도 이용할 수 있어요."
)

_MKT_FLAG = {"US": "🇺🇸", "KR": "🇰🇷", "CRYPTO": "🪙"}
_MKT_LABEL = {"US": "🇺🇸 미국", "KR": "🇰🇷 한국", "CRYPTO": "🪙 크립토"}


def _sub_labels(market: str) -> tuple[str, str]:
    """(value_score 라벨, health_score 라벨) — 시장별 의미 (CMAP-05).

    CRYPTO 는 screened 컬럼 재사용으로 value=변동성점수 / health=순위점수.
    """
    if market == "CRYPTO":
        return LABEL_CRYPTO_VALUE, LABEL_CRYPTO_HEALTH
    return LABEL_VALUE, LABEL_HEALTH


def _per_pbr(row) -> str:
    """PER/PBR 꼬리표 (있는 것만). KR(DART) 종목에 의미."""
    bits = []
    if row.per:
        bits.append(f"PER {row.per:.1f}")
    if row.pbr:
        bits.append(f"PBR {row.pbr:.2f}")
    return f" ({', '.join(bits)})" if bits else ""


def _flag_tags(row) -> list[str]:
    """ScanRow.flags → 사용자용 꼬리표 목록 (알려진 것만, 순서 고정)."""
    flags = getattr(row, "flags", ()) or ()
    return [_FLAG_TAG[f] for f in _FLAG_TAG if f in flags]


def _excluded_reason(row) -> str | None:
    code = getattr(row, "excluded", None)
    if not code:
        return None
    return _EXCLUDED_REASON.get(code, "후보 제외 대상")


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _age_days(*dates: str | None, today: date | None = None) -> int | None:
    """기준일들 중 **가장 오래된** 날짜로부터의 경과일. 파싱 가능한 날짜가 없으면 None (순수)."""
    today = today or _today_utc()
    ages = []
    for d in dates:
        if not d:
            continue
        try:
            ages.append((today - date.fromisoformat(str(d)[:10])).days)
        except ValueError:
            continue
    return max(ages) if ages else None


def _asof_lines(fund_asof: str | None, price_asof: str | None, *,
                today: date | None = None, fund_label: str = "재무") -> list[str]:
    """기준일 줄 + (UNIVERSE_STALE_DAYS 이상 경과 시) 경고 줄 (순수)."""
    if not fund_asof and not price_asof:
        return ["기준일 정보 없음"]
    bits = []
    if fund_asof:
        bits.append(f"{fund_label} {str(fund_asof)[:10]}")
    if price_asof:
        bits.append(f"가격 {str(price_asof)[:10]}")
    lines = ["🗓 점수 기준일: " + " · ".join(bits)]
    age = _age_days(fund_asof, price_asof, today=today)
    if age is not None and age >= UNIVERSE_STALE_DAYS:
        lines.append(f"⚠️ 점수가 {age}일 전 기준이라 최신이 아닐 수 있어요.")
    return lines


def format_stock(row, detail: dict | None, *, today: date | None = None) -> str:
    """종목 점수 + 근거 분해 + 기준일/제외 사유. row=ScanRow, detail=lookup_detail() 결과(없으면 None).

    점수는 DB(screened) 값 그대로 — 일일 리포트·/scan·대시보드와 같은 숫자/라벨.
    """
    flag = _MKT_FLAG.get(row.market, "")
    vlabel, hlabel = _sub_labels(row.market)
    lines = [f"📊 *{row.symbol}* {_safe(row.name)} · {flag}",
             f"종합 *{row.total_score}*  ({vlabel} {row.value_score} / {hlabel} {row.health_score})"]
    reason = _excluded_reason(row)
    if reason:
        lines.append(f"🚫 후보 제외 종목 — {reason}. 일일 리포트·/scan 후보에서 빠집니다.")
    tags = _flag_tags(row)
    if tags:
        lines.append("🏷 " + " · ".join(tags))
    extras = []
    if row.roe is not None:
        extras.append(f"ROE {row.roe:.1f}%")
    pp = _per_pbr(row).strip(" ()")
    if pp:
        extras.append(pp)
    if extras:
        lines.append(" · ".join(extras))
    if row.market == "CRYPTO":
        lines.append("※ 크립토 점수는 시총 순위·변동성 기반이라 주식 점수와 비교할 수 없어요.")

    if detail and row.market != "CRYPTO":
        for key, title in (("value", f"💰 {LABEL_VALUE}"), ("health", f"💪 {LABEL_HEALTH}")):
            card = detail.get(key)
            if not card:
                continue
            lines.append("")
            lines.append(f"*{title} {card.get('total', '')}*")
            for comp in card.get("components", []):
                # comp = [label, points, max, detail]
                label, pts, mx, det = (list(comp) + [None] * 4)[:4]
                tail = f" ({_safe(det)})" if det and det != "—" else ""
                lines.append(f"  • {_safe(label)} {pts}/{mx}{tail}")
    lines.append("")
    lines.extend(_asof_lines(getattr(row, "fund_asof", None), getattr(row, "price_asof", None),
                             today=today,
                             fund_label="점수 계산" if row.market == "CRYPTO" else "재무"))
    lines.append(f"시장 내 상대 점수입니다. {DISCLAIMER}")
    return "\n".join(lines)


def format_scan(market: str, rows: list, *, today: date | None = None) -> str:
    """시장별 점수 상위 후보 목록. rows=ScanRow 리스트 (universe.pick_top 결과)."""
    label = _MKT_LABEL.get(market, market)
    if not rows:
        return f"{label}: 후보가 없습니다 (유니버스 점수 갱신 필요)."
    vlabel, hlabel = _sub_labels(market)
    lines = [f"*📋 {label} 점수 상위 후보 {len(rows)}종목*"]
    for r in rows:
        tags = _flag_tags(r)
        tail = f" · {', '.join(tags)}" if tags else ""
        lines.append(
            f"  `{r.symbol}` {_safe(r.name)} — 종합 *{r.total_score}* "
            f"({vlabel} {r.value_score}/{hlabel} {r.health_score}){_per_pbr(r)}{tail}"
        )
    # 기준일: 후보 전체 중 가장 오래된 날짜 (보수적으로 표기)
    fund = [r.fund_asof for r in rows if getattr(r, "fund_asof", None)]
    price = [r.price_asof for r in rows if getattr(r, "price_asof", None)]
    lines.append("")
    lines.extend(_asof_lines(min(fund) if fund else None, min(price) if price else None,
                             today=today))
    rule = "지주사 최대 2개" if market == "KR" else "같은 업종 최대 2개"
    lines.append(f"일일 리포트와 같은 선정 규칙({rule}) · 시장 내 상대 점수")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


_NEWS_DATE_RE = re.compile(r"^\d{4}-(\d{2}-\d{2})")


def _news_date(raw: object) -> str:
    """FMP publishedDate ("YYYY-MM-DD hh:mm:ss" 추정) → "MM-DD". 형식 불명이면 "" (순수).

    실제 포맷은 라이브 스모크 전이라 미확정 (fetch_stock_news docstring 참고) —
    선두가 ISO 날짜가 아니면 조용히 생략해 메시지 자체는 항상 나가게 함.
    """
    m = _NEWS_DATE_RE.match(raw) if isinstance(raw, str) else None
    return m.group(1) if m else ""


def format_news(symbol: str, items: list[dict]) -> str:
    """뉴스 헤드라인 목록 → 평문 메시지 (순수).

    Markdown 엔티티(`*`·`_` 등)를 일부러 안 씀 — 제목/URL 의 특수문자가 legacy
    Markdown 파싱을 깨면 notifier.send_telegram 이 평문으로 폴백하므로 (§4.10 #10)
    서식 없이도 그대로 읽히는 형태로 구성. URL 은 자동 링크되도록 단독 줄에.
    """
    lines = [f"📰 {symbol} 최근 뉴스"]
    for it in items[:NEWS_LIMIT]:
        site = it.get("site") or "?"
        date = _news_date(it.get("publishedDate"))
        tail = f" ({date})" if date else ""
        lines.append(f"• [{site}] {it.get('title') or '(제목 없음)'}{tail}")
        url = it.get("url")
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 핸들러 (DB 읽기 — 사전계산된 유니버스)
# ---------------------------------------------------------------------------


def handle_stock(arg: str | None) -> str:
    from src import universe

    if not arg:
        return "사용법: `/stock <티커>` (예: `/stock AAPL`)"
    sym = arg.upper()
    row = universe.lookup(sym)
    if row is None:
        return f"`{_safe(sym)}` 을(를) 유니버스에서 찾지 못했습니다 (미발굴/미보강일 수 있어요)."
    return format_stock(row, universe.lookup_detail(sym))


def _parse_scan_n(raw: str | None) -> int | None:
    """/scan 두 번째 인자(개수) → 1..SCAN_MAX_N. 없으면 기본값, 숫자가 아니면 None."""
    if raw is None:
        return SCAN_DEFAULT_N
    try:
        n = int(raw)
    except ValueError:
        return None
    return max(1, min(n, SCAN_MAX_N))


def handle_scan(arg: str | None, arg2: str | None = None) -> str:
    """/scan [us|kr] [N] — universe.pick_top (일일 리포트와 같은 선정 규칙)."""
    from src import universe

    market = (arg or "us").upper()
    n = _parse_scan_n(arg2)
    if market not in ("US", "KR") or n is None:
        return "사용법: `/scan [us|kr] [개수]` (예: `/scan kr 10`)"
    return format_scan(market, universe.pick_top(market, n))


def _is_active_or_owner(chat_id: str) -> bool:
    """scripts/bot.py 의 _is_subscriber 와 동일 기준 (소유자 or status='active')."""
    from src import subscribers
    from src.config import settings

    owner = str(settings.telegram_chat_id) if settings.telegram_chat_id else None
    if owner and chat_id == owner:
        return True
    return subscribers.subscriber_status(chat_id) == "active"


def handle_news(arg: str | None, chat_id: str) -> str:
    """/news <티커> — FMP 뉴스 헤드라인 (라이브 fetch, 캐시 30분).

    /stock·/scan 과 달리 게이트가 폴링 루프에 없어 여기서 직접 검사 (모듈 docstring
    참고). 어떤 실패도 예외로 새지 않고 안내 문자열로 변환 — 폴링 루프의 poison
    가드까지 가지 않게 함.
    """
    if not _is_active_or_owner(chat_id):
        return GATE_MSG
    if not arg:
        return "사용법: /news <티커> (예: /news AAPL)"
    sym = arg.strip().upper()
    if not _TICKER_RE.fullmatch(sym):
        return "티커 형식이 올바르지 않습니다 (영문·숫자·점·하이픈 1~10자, 예: AAPL, BRK.B)."
    try:
        from src.data_fetcher import fetch_stock_news

        items = fetch_stock_news(sym, limit=NEWS_LIMIT)
    except MissingApiKeyError:
        logger.warning("/news 처리 불가 — FMP_API_KEY 미설정")
        return "뉴스 기능이 아직 설정되지 않았습니다 (FMP 키 없음)."
    except DataFetchError:
        logger.warning("/news 뉴스 조회 실패 (symbol=%s)", sym, exc_info=True)
        return f"{sym} 뉴스 조회에 실패했습니다. 잠시 후 다시 시도해 주세요."
    if not items:
        return f"{sym}: 최근 뉴스가 없습니다."
    return format_news(sym, items)


def handle_ledger(arg: str | None, chat_id: str) -> str:
    """/ledger [us|kr] — 발송 원장 성과 평문 (ledger.report_text, 소유자 CLI 와 같은 출력).

    /news 와 같이 게이트를 이 모듈이 직접 검사. 평문 전제라 서식 문자는 쓰지 않는다.
    """
    from src.storage import DB_ERRORS

    if not _is_active_or_owner(chat_id):
        return GATE_MSG
    market = (arg or "us").upper()
    if market not in ("US", "KR"):
        return "사용법: /ledger [us|kr]"
    try:
        from src import ledger

        text = ledger.report_text(market)
    except DB_ERRORS:
        logger.warning("/ledger 원장 조회 실패 (market=%s)", market, exc_info=True)
        return "원장을 읽지 못했습니다. 잠시 후 다시 시도해 주세요."
    if len(text) > LEDGER_MAX_CHARS:
        text = text[:LEDGER_MAX_CHARS].rstrip() + "\n…(이하 생략)"
    # scripts/bot.py 가 /ledger 를 legacy Markdown 으로 보내므로(news 만 평문) 원장 본문의
    # snake_case 토큰(no_price 등)이 짝수 밑줄이면 400 없이 이탤릭으로 조용히 깨진다 (§4.10 #10).
    # 서식 특수문자를 공백으로 치환해 어느 parse_mode 에서도 안전하게 한다.
    text = _safe(text.replace("_", " "))
    return f"{text}\n\n{DISCLAIMER}"


# ---------------------------------------------------------------------------
# Rate limit (유저별, 인메모리 — 프로세스 한정)
# ---------------------------------------------------------------------------


class RateLimiter:
    """유저별 고정 윈도우 rate limit. 인메모리(프로세스 재시작 시 리셋 — rate limit 엔 무방).

    clock 주입으로 오프라인 테스트 가능.
    """

    def __init__(self, max_calls: int = 5, window_sec: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.max_calls = max_calls
        self.window = window_sec
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, chat_id: str) -> bool:
        now = self._clock()
        dq = self._hits.setdefault(chat_id, deque())
        while dq and dq[0] <= now - self.window:
            dq.popleft()
        if len(dq) >= self.max_calls:
            return False
        dq.append(now)
        return True


# ---------------------------------------------------------------------------
# 디스패치
# ---------------------------------------------------------------------------


def respond(text: str, chat_id: str, limiter: RateLimiter | None = None) -> str | None:
    """조회 명령 → 응답 문자열. 조회 명령이 아니면 None(구독 명령 등은 호출부 처리).

    rate limit 초과 시 None(드롭) — 응답 증폭으로 인한 남용 방지.
    """
    cmd = parse_command(text)
    if cmd.kind == "unknown":
        return None
    if limiter is not None and not limiter.allow(chat_id):
        logger.info("rate limit 초과 — 드롭 (chat=%s, cmd=%s)", chat_id, cmd.kind)
        return None
    if cmd.kind == "stock":
        return handle_stock(cmd.arg)
    if cmd.kind == "scan":
        return handle_scan(cmd.arg, cmd.arg2)
    if cmd.kind == "ledger":
        return handle_ledger(cmd.arg, chat_id)
    if cmd.kind == "news":
        return handle_news(cmd.arg, chat_id)
    if cmd.kind == "help":
        return HELP_TEXT
    return None
