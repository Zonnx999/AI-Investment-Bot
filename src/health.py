"""
src/health.py
=============
운영 헬스 리포트 — 다이제스트 1회 실행의 '데이터·인프라 상태' 를 모아
(1) 구독자 메시지 하단 '⚙️ 상태' 한 줄, (2) 소유자 평문 DM, (3) 발송 보류 판정,
(4) DM 중복 억제용 signature, (5) state 저장용 dict 로 바꿔 주는 **순수 모듈**.

DB·네트워크·텔레그램을 건드리지 않음 — 값 수집(storage.init_sync, 유니버스 기준일,
국면 지표 개수, 발송 결과 등)과 전송·state 저장은 호출부(send_digest / digest) 책임.

메시지 규칙
-----------
- 구독자 '상태' 줄은 **해석에 영향을 주는 항목만**, 고정 어휘로만 만든다. 동적 문자열
  (지표 id·예외 메시지 등)은 절대 넣지 않는다 — 텔레그램 legacy Markdown 은 `_ * [ ] \``
  하나만 불균형이어도 메시지 전체가 파싱 실패한다 (CLAUDE.md §4.10 #10).
- 소유자 DM 은 평문(parse_mode=None) 전제라 실패 상세(지표 id, 예외 이름)를 넣어도 된다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.exceptions import ApiTimeoutError, ModelRetiredError

KST = ZoneInfo("Asia/Seoul")

# 유니버스(재무 보강) 기준일 경과 임계 — 주 1회 재점수 + 하루 여유
UNIVERSE_STALE_DAYS = 8
# 가격 기준일 경과 임계 (영업일) — KR 가격 일일 갱신(kr-refresh)을 쓰는 경우
PRICE_STALE_BDAYS = 3

# legacy Markdown 에서 엔티티를 여는 문자 — 구독자에게 가는 문구에 금지
MD_FORBIDDEN = frozenset("_*[]`")

_STORAGE_OWNER = {
    "ok": "Turso 정상 (초기 pull ok)",
    "error": "Turso 초기 pull 실패 — 이전 레플리카로 진행 (데이터가 낡았을 수 있음)",
    "timeout": "Turso 초기 pull 타임아웃 — 오프라인 파일로 강등 (발굴 종목·구독자 목록 없음)",
    "offline": "Turso 우회 (QUANT_BOT_OFFLINE)",
    "local": "로컬 DB (Turso 미설정)",
}


def _md_safe(text: str) -> str:
    """구독자 문구 방어 — 금지 문자를 제거 (고정 어휘라 정상이면 아무 것도 안 바뀜)."""
    return "".join(ch for ch in text if ch not in MD_FORBIDDEN)


def _to_kst(dt: datetime) -> datetime:
    """tz 없는 datetime 은 KST 로 간주 (서버 로컬 시각 혼동 방지)."""
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def _parse_iso(value: str | None) -> datetime | None:
    """DB 의 ISO 문자열(UTC, '+00:00' 또는 'Z' 또는 날짜만) → aware datetime. 실패 시 None."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:            # 저장 규약상 UTC (storage._utcnow / universe updated_at)
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def _age_days(as_of: datetime, now: datetime) -> int:
    """KST 달력 날짜 기준 경과일 (예: 07-10 → 10-08 = 90)."""
    return (_to_kst(now).date() - _to_kst(as_of).date()).days


def _business_days_between(start: date, end: date) -> int:
    """start(제외) ~ end(포함) 사이 평일 수. 휴장일은 모름 — 임계에 여유를 둠."""
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def llm_status(exc: BaseException | None) -> str:
    """LLM 요약 결과 → HealthReport.llm 값. None(성공) 이면 "ok".

    끈 상태("off")는 호출부가 직접 넣는다 (llm_enabled() False 면 호출 자체가 없음).
    """
    if exc is None:
        return "ok"
    if isinstance(exc, ModelRetiredError):
        return "retired"
    if isinstance(exc, ApiTimeoutError):
        return "timeout"
    return f"error:{type(exc).__name__}"


@dataclass
class Problem:
    """헬스 문제 1건. user_text=None 이면 소유자에게만 알림 (구독자 상태 줄에 안 나감)."""

    code: str
    user_text: str | None
    owner_text: str


@dataclass
class HealthReport:
    """다이제스트 1회 실행의 상태 스냅샷. 값은 호출부가 채움 (이 모듈은 판정·문구만)."""

    market: str                                # "KR" / "US"
    started_at: datetime
    storage: str                               # Storage.init_sync
    universe_as_of: str | None                 # max(updated_at) WHERE market=? AND enriched=1 (UTC ISO)
    price_as_of: str | None = None             # 가격 기준일 (데이터 쪽이 제공할 때만)
    regime_ok: int | None = None               # 국면 지표 중 반영된 개수
    regime_total: int | None = None            # 국면 지표 전체 개수
    regime_failures: list[str] = field(default_factory=list)   # 소유자용 상세 (예: "yield_curve — FRED HTTP 502")
    llm: str = "off"                           # off | ok | retired | timeout | error:<Cls>
    optional_failures: list[str] = field(default_factory=list)  # predictions / weights / names 등
    state_write_ok: bool = True
    sent: int = 0
    failed: int = 0
    recipients: int = 0
    sync_timeout_s: float | None = None        # 보류 DM 의 '몇 초 타임아웃' 표기용 (선택)

    # ---------------- 판정 ----------------

    def problems(self, now: datetime) -> list[Problem]:
        """현재 문제 목록 (구독자 상태 줄 순서 = 리스트 순서)."""
        out: list[Problem] = []
        mk = self.market.upper()

        # 1) 유니버스(발굴 데이터) 기준일
        uni = _parse_iso(self.universe_as_of)
        if uni is None:
            out.append(Problem(
                "universe_missing", "발굴 데이터 기준일 없음",
                f"유니버스: {mk} 기준일 없음 (보강된 종목 0개) — 재점수 필요",
            ))
        else:
            age = _age_days(uni, now)
            if age > UNIVERSE_STALE_DAYS:
                mmdd = _to_kst(uni).strftime("%m-%d")
                out.append(Problem(
                    "universe_stale", f"발굴 데이터 {mmdd} 기준({age}일 전)",
                    f"유니버스: {mk} {mmdd} 기준 ({age}일 전) — 재점수 타이머 확인",
                ))

        # 2) 가격 기준일 (제공될 때만)
        price = _parse_iso(self.price_as_of)
        if price is not None:
            bdays = _business_days_between(_to_kst(price).date(), _to_kst(now).date())
            if bdays > PRICE_STALE_BDAYS:
                mmdd = _to_kst(price).strftime("%m-%d")
                out.append(Problem(
                    "price_stale", f"가격 {mmdd} 기준",
                    f"가격: {mk} {mmdd} 기준 ({bdays}영업일 전) — 가격 갱신 확인",
                ))

        # 3) 국면 지표 일부 실패
        if (self.regime_total and self.regime_ok is not None
                and self.regime_ok < self.regime_total):
            detail = f" (실패: {', '.join(self.regime_failures)})" if self.regime_failures else ""
            out.append(Problem(
                "regime_partial", f"국면 지표 {self.regime_ok}/{self.regime_total}",
                f"국면 지표: {self.regime_ok}/{self.regime_total} 반영{detail}",
            ))

        # 4) 저장소
        if self.storage == "error":
            out.append(Problem("storage_error", "일부 데이터 갱신 지연",
                               "저장소: " + _STORAGE_OWNER["error"]))
        elif self.storage == "timeout":
            out.append(Problem("storage_timeout", None, "저장소: " + _STORAGE_OWNER["timeout"]))
        elif self.storage == "offline":
            out.append(Problem("storage_offline", None, "저장소: " + _STORAGE_OWNER["offline"]))

        # 5) 소유자 전용 — LLM·선택 기능·상태 쓰기·발송
        if self.llm == "retired":
            out.append(Problem("llm_retired", None, "LLM 요약: 모델 퇴역 — MINIMAX_MODEL 교체"))
        elif self.llm == "timeout":
            out.append(Problem("llm_timeout", None, "LLM 요약: 타임아웃"))
        elif self.llm.startswith("error"):
            out.append(Problem("llm_error", None, f"LLM 요약: 실패 ({self.llm[6:] or '?'})"))
        for name in self.optional_failures:
            out.append(Problem(f"optional:{name}", None, f"선택 기능 실패: {name}"))
        if not self.state_write_ok:
            out.append(Problem("state_write", None, "상태 저장: 실패 (다음 실행의 비교 기준이 갱신 안 됨)"))
        if self.failed > 0:
            out.append(Problem("send_failed", None, f"발송: {self.failed}건 실패"))
        return out

    def status_line(self, now: datetime) -> str | None:
        """구독자 메시지 하단 '상태' 줄 — 해석에 영향을 주는 문제가 없으면 None."""
        parts = [_md_safe(p.user_text) for p in self.problems(now) if p.user_text]
        if not parts:
            return None
        return "⚙️ 상태: " + " · ".join(parts)

    def is_hold(self, universe_count: int) -> bool:
        """발송 보류 여부 — 오프라인 강등(구독자·발굴 종목 없는 cold 파일)이거나
        해당 시장 보강 종목이 0개면 브로드캐스트하지 않는다."""
        return self.storage == "timeout" or universe_count == 0

    def signature(self, now: datetime) -> list[str]:
        """문제 코드 정렬 목록 — 직전 실행과 같으면 소유자 DM 을 다시 보내지 않음."""
        return sorted(p.code for p in self.problems(now))

    def needs_owner_dm(self, now: datetime, prev_state: dict | None, held: bool = False) -> bool:
        """소유자 DM 조건: (a) 보류 (b) signature 가 직전 state 와 다름 (c) 발송 실패 있음.

        정상이고 변화 없으면 보내지 않음 — 다이제스트 자체가 하트비트.
        """
        if held or self.failed > 0:
            return True
        prev_sig = list((prev_state or {}).get("signature") or [])
        return self.signature(now) != prev_sig

    # ---------------- 문구 ----------------

    def _unit(self) -> str:
        return f"quant-digest@{self.market.lower()}.service"

    def owner_text(self, now: datetime, held: bool) -> str:
        """소유자 평문 DM (parse_mode=None 전제). held=True 면 '발송 보류' 형식."""
        mk = self.market.upper()
        stamp = _to_kst(now).strftime("%Y-%m-%d %H:%M")
        if held:
            if self.storage == "timeout":
                secs = f" {self.sync_timeout_s:.0f}초" if self.sync_timeout_s else ""
                cause = (f"Turso 초기 동기화{secs} 타임아웃 → 오프라인 파일 강등 "
                         "(발굴 종목·구독자 목록 없음)")
            else:
                cause = f"{mk} 발굴 종목 0개 (유니버스 비어 있음 또는 미보강)"
            return "\n".join([
                f"[quant-bot 발송 보류] {mk} 다이제스트 · {stamp} KST",
                f"원인: {cause}",
                "조치: 구독자 전원 미발송. 다시 보내려면 서버에서:",
                f"sudo systemctl start {self._unit()}",
            ])

        probs = {p.code: p for p in self.problems(now)}
        lines = [f"[quant-bot 상태] {mk} 다이제스트 · {stamp} KST"]
        send = f"발송: {self.sent}/{self.recipients} 성공"
        if self.failed:
            send += f", 실패 {self.failed}"
        lines.append(send)
        lines.append("저장소: " + _STORAGE_OWNER.get(self.storage, self.storage))

        uni_p = probs.get("universe_stale") or probs.get("universe_missing")
        if uni_p is not None:
            lines.append(uni_p.owner_text)
        else:
            uni = _parse_iso(self.universe_as_of)
            if uni is not None:
                lines.append(f"유니버스: {mk} {_to_kst(uni):%m-%d} 기준 ({_age_days(uni, now)}일 전)")
        if "price_stale" in probs:
            lines.append(probs["price_stale"].owner_text)
        if self.regime_total:
            lines.append(probs["regime_partial"].owner_text if "regime_partial" in probs
                         else f"국면 지표: {self.regime_ok}/{self.regime_total} 반영")

        llm_txt = {"off": "끔", "ok": "정상"}.get(self.llm)
        if llm_txt is not None:
            lines.append(f"LLM 요약: {llm_txt}")
        for code in ("llm_retired", "llm_timeout", "llm_error"):
            if code in probs:
                lines.append(probs[code].owner_text)
        for p in probs.values():
            if p.code.startswith("optional:") or p.code == "state_write":
                lines.append(p.owner_text)
        lines.append(f"조치: journalctl -u {self._unit()} -n 50 --no-pager")
        return "\n".join(lines)

    # ---------------- 직렬화 ----------------

    def to_state(self) -> dict:
        """put_state("health", f"digest:{market}", ...) 용 JSON 직렬화 dict.

        signature 는 실행 시각(started_at) 기준으로 계산해 함께 저장 (다음 실행이 비교)."""
        d = asdict(self)
        d["started_at"] = _to_kst(self.started_at).isoformat()
        d["signature"] = self.signature(self.started_at)
        return d

    @classmethod
    def from_state(cls, d: dict) -> HealthReport:
        """to_state() 의 역변환 (signature 등 파생 키는 무시)."""
        names = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in names}
        kwargs["started_at"] = datetime.fromisoformat(d["started_at"])
        return cls(**kwargs)
