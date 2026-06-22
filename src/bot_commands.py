"""
src/bot_commands.py
===================
Phase 11b — 인터랙티브 봇 조회 명령 응답 (호스트 무관 코어).

상시 호스트(폴링 워커)가 getUpdates 로 받은 조회 명령을 여기서 처리. 무거운 분석은
cron 이 Turso 에 사전계산해 두고, 이 응답기는 **유니버스 DB 읽기 위주(경량)** +
유저별 rate limit 으로 남용을 막는다.

명령 (조회 전용):
  /stock <티커>   종목 점수 + 근거 분해 (universe.lookup + lookup_detail)
  /scan [us|kr]   시장 저평가 상위 (universe.scan)
  /help           도움말

구독 명령(/start·/stop·/approve…)은 subscribers.py 담당 — parse_command 는 그 외 명령을
"unknown" 으로 흘려보내고, 실제 라우팅(조회 vs 구독)은 폴링 루프에서 한다(호스트 결정 후).

설계: parse_command/format_* 는 **순수 함수**(오프라인 테스트), handle_* 는 DB 읽기,
respond() 가 파싱→rate limit→핸들러 디스패치. 응답 문자열만 반환하고 전송은 호출부(notifier).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from src.logger import get_logger

logger = get_logger(__name__)

SCAN_LIMIT = 10


@dataclass
class Command:
    """파싱된 조회 명령 (순수)."""
    kind: str            # stock | scan | help | unknown
    arg: str | None = None


def parse_command(text: str) -> Command:
    """명령 텍스트 → Command. `/cmd@botname`(그룹) 허용. 조회 명령만 인식."""
    parts = (text or "").split()
    if not parts:
        return Command("unknown")
    cmd = parts[0].lower().split("@", 1)[0]
    arg = parts[1] if len(parts) > 1 else None
    if cmd == "/stock":
        return Command("stock", arg)
    if cmd == "/scan":
        return Command("scan", arg)
    if cmd in ("/help", "/menu"):
        return Command("help")
    return Command("unknown")


# 버튼(reply keyboard) 라벨 → 명령. 봇 루프가 수신 텍스트를 이 표로 정규화한 뒤 디스패치.
BUTTON_TO_COMMAND = {
    "🇺🇸 미국 추천": "/scan us",
    "🇰🇷 한국 추천": "/scan kr",
    "❓ 도움말": "/help",
    "📋 구독자": "/subscribers",   # 관리자 — subscribers.apply_events 가 처리
    "⏳ 승인 대기": "/pending",    # 관리자
}


def main_keyboard(is_owner: bool = False) -> dict:
    """지속형 reply keyboard. 탭하면 라벨이 전송되고 BUTTON_TO_COMMAND 로 명령화됨.

    소유자에겐 관리자 버튼(구독자/대기 목록) 추가. /stock 은 인자가 필요해 버튼 대신 직접 입력.
    """
    rows = [["🇺🇸 미국 추천", "🇰🇷 한국 추천"], ["❓ 도움말"]]
    if is_owner:
        rows.append(["📋 구독자", "⏳ 승인 대기"])
    return {"keyboard": rows, "resize_keyboard": True}


# ---------------------------------------------------------------------------
# 포매팅 (순수 — 데이터 주면 문자열)
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📖 *명령어*\n"
    "`/stock <티커>` — 종목 점수와 근거 (예: `/stock AAPL`, `/stock 005930`)\n"
    "`/scan [us|kr]` — 시장 저평가 상위 (기본 us)\n"
    "`/menu` — 버튼 메뉴 열기\n"
    "\n구독: `/start` 가입 요청 · `/stop` 해지\n"
    "\n👇 아래 버튼으로도 이용할 수 있어요."
)

_MKT_FLAG = {"US": "🇺🇸", "KR": "🇰🇷", "CRYPTO": "🪙"}
_MKT_LABEL = {"US": "🇺🇸 미국", "KR": "🇰🇷 한국", "CRYPTO": "🪙 크립토"}


def _per_pbr(row) -> str:
    """PER/PBR 꼬리표 (있는 것만). KR(DART) 종목에 의미."""
    bits = []
    if row.per:
        bits.append(f"PER {row.per:.1f}")
    if row.pbr:
        bits.append(f"PBR {row.pbr:.2f}")
    return f" ({', '.join(bits)})" if bits else ""


def format_stock(row, detail: dict | None) -> str:
    """종목 점수 + 근거 분해. row=ScanRow, detail=lookup_detail() 결과(없으면 None)."""
    flag = _MKT_FLAG.get(row.market, "")
    lines = [f"📊 *{row.symbol}* {row.name} · {flag}",
             f"종합 *{row.total_score}*  (밸류 {row.value_score} / 건전성 {row.health_score})"]
    extras = []
    if row.roe is not None:
        extras.append(f"ROE {row.roe:.1f}%")
    pp = _per_pbr(row).strip(" ()")
    if pp:
        extras.append(pp)
    if extras:
        lines.append(" · ".join(extras))

    if detail:
        for key, title in (("value", "💰 밸류"), ("health", "💪 건전성")):
            card = detail.get(key)
            if not card:
                continue
            lines.append("")
            lines.append(f"*{title} {card.get('total', '')}*")
            for comp in card.get("components", []):
                # comp = [label, points, max, detail]
                label, pts, mx, det = (list(comp) + [None] * 4)[:4]
                tail = f" ({det})" if det and det != "—" else ""
                lines.append(f"  • {label} {pts}/{mx}{tail}")
    return "\n".join(lines)


def format_scan(market: str, rows: list) -> str:
    """시장 저평가 상위 목록. rows=ScanRow 리스트."""
    label = _MKT_LABEL.get(market, market)
    if not rows:
        return f"{label}: 발굴 종목이 없습니다 (build_universe --enrich 필요)."
    lines = [f"*📈 {label} 저평가 상위*"]
    for r in rows:
        lines.append(
            f"  `{r.symbol}` {r.name} — 종합 *{r.total_score}* "
            f"(밸류 {r.value_score}/건전성 {r.health_score}){_per_pbr(r)}"
        )
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
        return f"`{sym}` 을(를) 유니버스에서 찾지 못했습니다 (미발굴/미보강일 수 있어요)."
    return format_stock(row, universe.lookup_detail(sym))


def handle_scan(arg: str | None) -> str:
    from src import universe

    market = (arg or "us").upper()
    if market not in ("US", "KR"):
        return "사용법: `/scan [us|kr]`"
    return format_scan(market, universe.scan(market=market, limit=SCAN_LIMIT))


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
        return handle_scan(cmd.arg)
    if cmd.kind == "help":
        return HELP_TEXT
    return None
