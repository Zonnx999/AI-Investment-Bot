"""
src/subscribers.py
==================
Phase 11a — 멀티유저 브로드캐스트 구독 관리 (소유자 승인제).

문제: 친구에게 다이제스트를 공유하려면 chat_id 를 일일이 시크릿에 넣어야 했음.
해결: 봇에게 `/start` 를 보내면 **가입 요청(pending)** 으로 접수되고, 소유자가
`/approve <chat_id>` 로 승인해야 구독(active). 유출될 비밀코드가 없고, 소유자가
한 명씩 직접 통제. 봇이 요청자의 chat_id·이름을 소유자에게 알려줘 수동관리 부담도 없앰.

상태 모델: pending(승인 대기) → active(수신) / inactive(해지·거절)

설계
----
- `subscribers` 테이블 (Turso 영속 — ephemeral cron 러너 간에도 구독 유지)
- **순수 파싱**(`parse_updates`: 텍스트→명령)과 **fetch+DB+권한**(`sync_subscribers`) 분리.
  파싱은 owner 를 모름 → 권한(approve/deny/pending 은 소유자만)은 오케스트레이터가 판정.
- 명령:
  - `/start`            → 가입 요청(pending) + 소유자에게 승인 요청 알림
  - `/stop`             → 해지(inactive)
  - `/approve <id>`     → (소유자) 해당 요청 승인(active)
  - `/deny <id>`        → (소유자) 거절(inactive)
  - `/pending`          → (소유자) 대기 목록 조회
- 명령 처리는 cron 폴링 주기에 반영(실시간 아님) — 일일 다이제스트 봇이라 무방.

cron 흐름: send_digest 가 sync_subscribers() 로 명령 수거 후 active 구독자에게 브로드캐스트.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import settings
from src.exceptions import DataFetchError
from src.logger import get_logger
from src.storage import add_column_if_missing, get_storage

logger = get_logger(__name__)

# 텔레그램 update 오프셋 보관 위치 (영속 state — TTL 없음)
_OFFSET_NS = "telegram"
_OFFSET_KEY = "updates_offset"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id        TEXT PRIMARY KEY,
    name           TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | active | inactive
    subscribed_at  TEXT,
    updated_at     TEXT
);
"""


@dataclass
class SubEvent:
    """파싱된 명령 한 건 (순수 — DB/네트워크/권한 무관)."""
    update_id: int | None
    chat_id: str            # 명령을 보낸 사람
    name: str
    kind: str               # request|unsubscribe|approve|deny|pending|list|announce|ignore
    target: str | None = None   # approve/deny 의 대상 chat_id, 또는 announce 의 공지 본문


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# 스키마 초기화를 이미 마친 Storage 인스턴스 (프로세스당 1회 초기화).
# 매 호출 executescript 는 libsql 임베디드 레플리카에서 쓰기 배치 = 원격(Turso) 왕복이라,
# 상시 봇(scripts/bot.py)이 메시지마다 _conn() 을 타면 왕복이 누적됨 → memoize.
# Storage 인스턴스로 키잉해 테스트의 싱글톤 리셋(tmp DB 교체) 시 자동 재초기화.
_schema_ready_store = None


def _conn():
    global _schema_ready_store
    store = get_storage()
    conn = store.conn
    if _schema_ready_store is not store:
        conn.executescript(_SCHEMA)
        # 마이그레이션: 구버전 테이블(status 없음)에 컬럼 추가 — Turso stale 레플리카 안전(중복 삼킴)
        add_column_if_missing(conn, "subscribers", "status", "TEXT NOT NULL DEFAULT 'pending'")
        _schema_ready_store = store
    return conn


# ----------------------------------------------------------------------
# 순수 파싱 (오프라인 테스트 대상)
# ----------------------------------------------------------------------


def _classify(text: str) -> tuple[str, str | None]:
    """명령 텍스트 → (종류, 대상). `/cmd@botname`(그룹) 형태 허용.

    approve/deny 의 대상 chat_id 는 두 번째 토큰. 권한 검사는 호출부(오케스트레이터).
    """
    parts = text.split()
    if not parts:
        return "ignore", None
    cmd = parts[0].lower().split("@", 1)[0]
    arg = parts[1] if len(parts) > 1 else None
    if cmd == "/start":
        return "request", None
    if cmd == "/stop":
        return "unsubscribe", None
    if cmd == "/approve":
        return "approve", arg
    if cmd == "/deny":
        return "deny", arg
    if cmd == "/pending":
        return "pending", None
    if cmd == "/subscribers":
        return "list", None
    if cmd == "/announce":
        # 공지 본문 = 명령 뒤 전체(여러 단어). 권한(소유자)·전송은 apply_events 가 판정.
        body = text.split(None, 1)[1].strip() if len(parts) > 1 else None
        return "announce", body
    return "ignore", None


def parse_updates(updates: list[dict]) -> tuple[list[SubEvent], int | None]:
    """텔레그램 getUpdates 결과 → (이벤트 목록, 다음 offset).

    다음 offset = 처리한 update_id 중 최댓값 + 1 (없으면 None). 순수 함수.
    """
    events: list[SubEvent] = []
    max_uid: int | None = None
    for u in updates:
        uid = u.get("update_id")
        if isinstance(uid, int):
            max_uid = uid if max_uid is None else max(max_uid, uid)
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            continue
        name = chat.get("username") or chat.get("first_name") or ""
        kind, target = _classify(text)
        events.append(SubEvent(uid, str(chat_id), name, kind, target))
    next_offset = (max_uid + 1) if max_uid is not None else None
    return events, next_offset


# ----------------------------------------------------------------------
# DB 연산
# ----------------------------------------------------------------------


def upsert_request(conn, chat_id: str, name: str) -> None:
    """가입 요청 등록(pending). 기존 inactive/pending 이면 pending 으로 되돌림."""
    conn.execute(
        "INSERT INTO subscribers (chat_id, name, status, subscribed_at, updated_at) "
        "VALUES (?, ?, 'pending', ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET status='pending', name=excluded.name, "
        "updated_at=excluded.updated_at",
        (chat_id, name, _utcnow(), _utcnow()),
    )


def set_status(conn, chat_id: str, status: str, name: str | None = None) -> None:
    """상태 변경(active/inactive 등). name 주면 함께 갱신. 행 없으면 새로 생성(active 승인 등)."""
    conn.execute(
        "INSERT INTO subscribers (chat_id, name, status, subscribed_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at"
        + (", name=excluded.name" if name is not None else ""),
        (chat_id, name or "", status, _utcnow(), _utcnow()),
    )


def get_status(conn, chat_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM subscribers WHERE chat_id=?", (chat_id,)
    ).fetchone()
    return row[0] if row else None


def subscriber_status(chat_id: str) -> str | None:
    """chat_id 의 구독 상태 (public API — conn 관리는 내부에서).

    외부 모듈(scripts/bot.py 등)이 private `_conn()` 을 직접 만지지 않도록 하는 파사드.
    반환: 'pending' | 'active' | 'inactive' | None(기록 없음).
    """
    return get_status(_conn(), chat_id)


def get_updates_offset() -> int | None:
    """텔레그램 getUpdates offset (영속 state). 없으면 None (public API)."""
    return get_storage().get_state(_OFFSET_NS, _OFFSET_KEY)


def set_updates_offset(offset: int) -> None:
    """텔레그램 getUpdates offset 저장 (public API — 폴링 루프/cron 공용)."""
    get_storage().put_state(_OFFSET_NS, _OFFSET_KEY, offset)


def active_subscribers() -> list[tuple[str, str]]:
    """수신 대상 [(chat_id, name), ...] — status='active' 만."""
    conn = _conn()
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT chat_id, name FROM subscribers WHERE status='active' ORDER BY subscribed_at"
    ).fetchall()]


def pending_requests() -> list[tuple[str, str]]:
    """승인 대기 [(chat_id, name), ...]."""
    conn = _conn()
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT chat_id, name FROM subscribers WHERE status='pending' ORDER BY subscribed_at"
    ).fetchall()]


def ensure_owner() -> None:
    """설정된 telegram_chat_id 를 항상 active 로 보장 (소유자는 승인 없이 늘 받음)."""
    owner = settings.telegram_chat_id
    if not owner:
        return
    conn = _conn()
    set_status(conn, str(owner), "active", name="owner")
    conn.commit()


def stats() -> dict[str, int]:
    conn = _conn()
    out = {"total": 0, "active": 0, "pending": 0, "inactive": 0}
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM subscribers GROUP BY status"
    ).fetchall():
        out[status] = n
        out["total"] += n
    return out


# ----------------------------------------------------------------------
# 오케스트레이터 (fetch + DB + 권한)
# ----------------------------------------------------------------------


_MSG_REQUESTED = "📨 가입 요청이 접수되었습니다. 소유자 승인을 기다려 주세요."
_MSG_ALREADY_PENDING = "⏳ 이미 가입 요청 중입니다. 승인을 기다려 주세요."
_MSG_ALREADY_ACTIVE = "✅ 이미 구독 중입니다. 해지는 /stop"
_MSG_APPROVED = "✅ 승인되었습니다! 매일 아침 투자 다이제스트를 받습니다. 해지는 /stop"
_MSG_DENIED = "가입이 거절되었습니다."
_MSG_UNSUBSCRIBED = "👋 구독이 해지되었습니다. 다시 받으려면 /start"


def _broadcast(text: str) -> dict[str, int]:
    """active 구독자 전원에게 평문 전송 (#10 — 공지/관리 메시지는 평문). best-effort.

    소유자도 active 라 함께 받음(전송본 그대로 확인). 개별 실패는 카운트만.
    Returns {"sent","failed","recipients"}.
    """
    from src.notifier import send_safe

    recipients = active_subscribers()
    sent = sum(1 for cid, _nm in recipients if send_safe(text, cid, parse_mode=None))
    return {"sent": sent, "failed": len(recipients) - sent, "recipients": len(recipients)}


def apply_events(events: list[SubEvent], send_notifications: bool = True) -> dict[str, int]:
    """파싱된 구독 이벤트들을 DB 에 반영 + 알림. (fetch/offset 무관 — 재사용 가능)

    cron(sync_subscribers)과 상시 폴링 루프(scripts/bot.py)가 공유. approve/deny/pending 은
    **소유자(telegram_chat_id)** 가 보낸 것만 처리. 조회 명령 등 "ignore" 는 무시.
    Returns {"requests","approved","denied","unsubscribed","ignored_admin"}.
    """
    from src.notifier import send_safe

    owner = str(settings.telegram_chat_id) if settings.telegram_chat_id else None
    st = {"requests": 0, "approved": 0, "denied": 0, "unsubscribed": 0,
          "ignored_admin": 0, "announced": 0}
    conn = _conn()

    def notify(text: str, chat_id: str | None) -> None:
        # 구독 안내·관리 메시지는 평문 전송 — username 의 '_' 등이 Markdown 엔티티
        # 파싱 오류를 내지 않도록 (legacy Markdown 은 백슬래시 이스케이프 미지원).
        if send_notifications and chat_id:
            send_safe(text, chat_id, parse_mode=None)

    for ev in events:
        is_owner = owner is not None and ev.chat_id == owner

        if ev.kind == "request":
            prev = get_status(conn, ev.chat_id)
            if prev == "active":
                notify(_MSG_ALREADY_ACTIVE, ev.chat_id)
            elif prev == "pending":
                notify(_MSG_ALREADY_PENDING, ev.chat_id)
            else:                                    # 신규/재요청(inactive)
                upsert_request(conn, ev.chat_id, ev.name)
                st["requests"] += 1
                notify(_MSG_REQUESTED, ev.chat_id)
                # 소유자에게 승인 요청 알림 — 신규 요청일 때만 (중복 알림 방지)
                notify(f"🔔 가입 요청: {ev.name or '(이름없음)'} (chat_id={ev.chat_id})\n"
                       f"승인: /approve {ev.chat_id}    거절: /deny {ev.chat_id}", owner)

        elif ev.kind == "unsubscribe":
            set_status(conn, ev.chat_id, "inactive")
            st["unsubscribed"] += 1
            notify(_MSG_UNSUBSCRIBED, ev.chat_id)

        elif ev.kind == "announce":
            # 소유자 전용 공지 — active 구독자 전원에게 평문 브로드캐스트.
            if not is_owner:
                st["ignored_admin"] += 1
                continue
            if not ev.target:
                notify("사용법: /announce <공지 내용> — active 구독자 전원에게 전송", owner)
            elif send_notifications:
                result = _broadcast(f"📢 공지\n\n{ev.target}")
                st["announced"] += 1
                tail = f" (실패 {result['failed']})" if result["failed"] else ""
                notify(f"📢 공지 전송: {result['sent']}/{result['recipients']}명{tail}", owner)

        elif ev.kind in ("approve", "deny", "pending", "list"):
            if not is_owner:
                st["ignored_admin"] += 1            # 비소유자의 관리명령 무시
                continue
            if ev.kind == "list":
                rows = active_subscribers()
                body = ("📋 구독자 없음" if not rows else
                        f"📋 구독자 {len(rows)}명:\n" + "\n".join(
                            f"• {nm or '(이름없음)'} ({cid})" for cid, nm in rows))
                notify(body, owner)
            elif ev.kind == "pending":
                rows = pending_requests()
                body = ("승인 대기 없음" if not rows else
                        "승인 대기:\n" + "\n".join(
                            f"• {nm or '(이름없음)'} — /approve {cid}" for cid, nm in rows))
                notify(body, owner)
            elif ev.target is None:
                notify(f"사용법: /{ev.kind} <chat_id>", owner)
            elif get_status(conn, ev.target) is None:
                notify(f"⚠️ {ev.target}: 가입 요청 기록이 없습니다.", owner)
            elif ev.kind == "approve":
                set_status(conn, ev.target, "active")
                st["approved"] += 1
                notify(f"승인 완료: {ev.target}", owner)
                notify(_MSG_APPROVED, ev.target)
            else:                                    # deny
                set_status(conn, ev.target, "inactive")
                st["denied"] += 1
                notify(f"거절 완료: {ev.target}", owner)
                notify(_MSG_DENIED, ev.target)

    conn.commit()
    logger.info("구독 처리: %s", st)
    return st


def sync_subscribers(send_notifications: bool = True) -> dict[str, int]:
    """텔레그램 명령 수거(getUpdates) → apply_events. cron(GitHub Actions) 폴링용.

    offset 을 영속 state 에 저장해 재처리 방지. getUpdates 실패는 best-effort.
    ⚠️ 상시 봇(scripts/bot.py)이 돌 때는 그쪽이 getUpdates 를 소유하므로 cron 은
       이걸 호출하면 안 됨 (offset 경합) → send_digest.py --no-sync 사용.
    """
    from src.notifier import get_updates

    offset = get_updates_offset()
    try:
        updates = get_updates(offset=offset)
    except DataFetchError as e:
        logger.warning("getUpdates 실패 — 구독 동기화 스킵: %s", e)
        return {"requests": 0, "approved": 0, "denied": 0, "unsubscribed": 0,
                "ignored_admin": 0, "announced": 0}

    events, next_offset = parse_updates(updates)
    st = apply_events(events, send_notifications=send_notifications)
    if next_offset is not None:
        set_updates_offset(next_offset)
    get_storage().sync()
    return st
