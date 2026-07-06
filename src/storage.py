"""
src/storage.py
==============
SQLite 캐시 레이어 (Phase 4).

목적: 같은 데이터를 하루에 두 번 부르지 않기 — 속도·API 한도 양면 개선.
data_fetcher 의 fetch 함수들이 `@cached(...)` 데코레이터로 이 레이어를
투명하게 사용합니다. 호출부(스크립트/분석 모듈)는 캐시 존재를 모릅니다.

설계 원칙
---------
1. **캐시는 best-effort** — 캐시 읽기/쓰기 실패는 경고 로그 후 무시하고
   원본 fetch 로 진행. 캐시 장애가 데이터 파이프라인을 절대 막지 않음.
   (단, DB 파일 생성 자체가 불가능한 경우는 StorageError)
2. **빈 결과는 캐시하지 않음** — 빈 DataFrame 은 폴백 신호이므로 (8단계
   컨벤션), transient 실패를 캐시했다가 폴백을 가리는 사고 방지.
3. 직렬화는 JSON (`orient="table"` — dtype/인덱스 보존). pickle 미사용
   (pandas 버전 간 깨짐 방지), parquet 미사용 (pyarrow 의존성 회피).

환경변수
--------
QUANT_BOT_DB_PATH       DB 파일 경로 override (기본: data/quant_bot.db)
QUANT_BOT_CACHE         "on"(기본) / "off"(읽기·쓰기 모두 끔) /
                        "refresh"(읽기만 끔 — 새로 받아서 캐시 갱신)
QUANT_BOT_OFFLINE       "1"/"on" 이면 TURSO_* 를 무시하고 로컬 전용으로 동작
                        (별도 오프라인 캐시 파일 — 레플리카 파일은 건드리지 않음)
QUANT_BOT_SYNC_TIMEOUT  Turso sync() 감시 타임아웃 초 (기본 20). 네트워크가
                        블랙홀이면 sync 가 예외 없이 영원히 멈추므로 필수 가드
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Literal

import pandas as pd

from src.config import settings
from src.exceptions import QuantBotError
from src.logger import get_logger

logger = get_logger(__name__)

CacheKind = Literal["dataframe", "series", "json"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,   -- ISO 8601, UTC
    PRIMARY KEY (namespace, key)
);

-- 캐시와 달리 TTL 없는 영속 상태 (예: 신호 엔진의 '지난 실행 시점 값')
CREATE TABLE IF NOT EXISTS state (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL,   -- JSON
    updated_at TEXT NOT NULL,   -- ISO 8601, UTC
    PRIMARY KEY (namespace, key)
);
"""


class StorageError(QuantBotError):
    """캐시 DB 초기화/접근 불가 (디스크 권한 등)."""


def _cache_mode() -> str:
    return os.getenv("QUANT_BOT_CACHE", "on").strip().lower()


def _offline_mode() -> bool:
    """QUANT_BOT_OFFLINE — Turso 를 완전히 우회하는 킬스위치 (노트북 오프라인 작업용)."""
    return os.getenv("QUANT_BOT_OFFLINE", "").strip().lower() in ("1", "on", "true", "yes")


def _sync_timeout() -> float:
    """Turso sync() 감시 타임아웃 (초). 실사고: 블랙홀 네트워크에서 sync() 가
    예외 없이 무한 대기 → 모든 진입점(다이제스트·fetch·봇)이 통째로 멈춤."""
    raw = os.getenv("QUANT_BOT_SYNC_TIMEOUT", "20")
    try:
        return max(1.0, float(raw))
    except ValueError:
        logger.warning("QUANT_BOT_SYNC_TIMEOUT=%r 파싱 불가 — 기본 20s", raw)
        return 20.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Storage:
    """SQLite 기반 (namespace, key) → payload 캐시.

    읽기 miss 또는 손상된 payload 는 None 반환 (= 캐시 miss 취급).
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = self._resolve_path(db_path)
        self.is_turso = False
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = self._connect()
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except (OSError, sqlite3.Error) as e:
            raise StorageError(f"캐시 DB 초기화 실패: {self.db_path}") from e
        logger.debug("DB 연결: %s (turso=%s)", self.db_path, self.is_turso)

    def _connect(self):
        """백엔드 선택: Turso 설정 시 libSQL 임베디드 레플리카, 아니면 로컬 sqlite3.

        임베디드 레플리카 = 로컬 파일에 읽고/쓰고(빠름) + sync() 로 클라우드 동기화.
        Turso 설정(db_path 명시 테스트 제외)일 때만 활성 → 테스트는 항상 로컬.
        """
        url = settings.turso_database_url
        # db_path 를 명시 주입한 경우(테스트)는 항상 로컬 — 원격 동기화 안 함
        if not url or self._db_path_was_injected:
            return sqlite3.connect(self.db_path)
        if _offline_mode():
            # 킬스위치: Turso 완전 우회. 레플리카 파일을 sqlite3 로 직접 열면
            # libsql 복제 메타데이터가 깨질 수 있어(§4.10 #9) 별도 파일 사용.
            offline = self._offline_path()
            logger.warning("QUANT_BOT_OFFLINE — Turso 우회, 로컬 전용 캐시: %s", offline)
            self.db_path = offline
            return sqlite3.connect(offline)

        try:
            import libsql_experimental as libsql
        except ImportError as e:
            raise StorageError(
                "Turso 설정됨(TURSO_DATABASE_URL) 이지만 libsql 미설치 — "
                "pip install -e \".[hosting]\""
            ) from e
        try:
            conn = libsql.connect(
                str(self.db_path), sync_url=url, auth_token=settings.turso_auth_token
            )
        except Exception as e:  # noqa: BLE001 — libsql 예외 타입을 도메인 예외로 변환
            raise StorageError(f"Turso 연결 실패: {self.db_path}") from e

        status = self._safe_sync(conn, "초기 pull")
        if status == "timeout":
            # 네트워크 블랙홀 — 임베디드 레플리카는 '쓰기'도 원격 왕복이라
            # 이 conn 을 계속 쓰면 다음 쓰기에서 또 멈춤. 스레드에 유기된 conn 과의
            # 파일 경합·복제 메타데이터 손상을 피해 **별도 오프라인 파일**로 강등.
            # 캐시는 cold 지만 파이프라인은 계속 (설계 원칙 #1: 캐시는 best-effort).
            offline = self._offline_path()
            logger.warning(
                "Turso 초기 sync 타임아웃 — 오프라인 캐시로 강등: %s "
                "(원인 후보: 네트워크 차단/레플리카 손상. QUANT_BOT_OFFLINE=1 로 "
                "명시 우회 가능, 레플리카 복구는 data/*.db* 삭제 후 재싱크)", offline,
            )
            self.db_path = offline
            return sqlite3.connect(offline)

        self.is_turso = True
        logger.info("Turso 임베디드 레플리카 연결: %s", self.db_path)
        return conn

    def _offline_path(self) -> Path:
        """Turso 우회/강등 시 쓰는 로컬 전용 캐시 파일 (레플리카와 분리)."""
        return self.db_path.with_name(self.db_path.stem + ".offline.db")

    def _safe_sync(self, conn, what: str) -> str:
        """클라우드 동기화 (best-effort + 감시 타임아웃). 반환: "ok"/"error"/"timeout".

        libsql 의 sync() 는 자체 타임아웃이 없어 블랙홀 네트워크에서 예외 없이
        무한 대기함 (실사고 2026-07-06: 다이제스트·fetch 전 진입점 행).
        데몬 스레드에서 돌리고 QUANT_BOT_SYNC_TIMEOUT(기본 20s) 만 기다림 —
        타임아웃 시 스레드는 유기(daemon)하고 호출부가 강등을 결정.
        부수 효과: Ctrl-C 가 Rust 네이티브 호출 안에서 pyo3 panic 을 내던 것도
        메인 스레드가 join 대기 중이라 깔끔한 KeyboardInterrupt 로 바뀜.
        """
        result: dict[str, Any] = {}

        def _run() -> None:
            try:
                conn.sync()
                result["ok"] = True
            except Exception as e:  # noqa: BLE001 — 동기화 실패가 파이프라인을 막지 않음
                result["err"] = e

        t = threading.Thread(target=_run, daemon=True, name=f"turso-sync-{what}")
        t.start()
        t.join(_sync_timeout())
        if t.is_alive():
            logger.warning("Turso sync(%s) %.0fs 타임아웃 — 미완료 (감시 스레드 유기)",
                           what, _sync_timeout())
            return "timeout"
        if "err" in result:
            logger.warning("Turso sync(%s) 실패 — 로컬 레플리카로 진행: %s",
                           what, result["err"])
            return "error"
        logger.debug("Turso sync(%s) OK", what)
        return "ok"

    def sync(self) -> None:
        """로컬 변경을 클라우드로 push. 쓰기 배치 후 호출. 로컬 sqlite3 면 no-op.

        push 가 타임아웃되면(유기 스레드가 conn 을 계속 참조) 같은 conn 으로
        추가 sync 를 겹쳐 돌리지 않도록 이후 push 는 스킵 — 프로세스 재시작이
        복구 수단 (systemd 봇/타이머 구조상 자연 복구).
        """
        if not self.is_turso:
            return
        if getattr(self, "_sync_degraded", False):
            logger.debug("Turso push 스킵 — 이전 sync 타임아웃으로 강등 상태")
            return
        if self._safe_sync(self._conn, "push") == "timeout":
            self._sync_degraded = True
            logger.warning("Turso push 타임아웃 — 이 프로세스의 이후 push 는 스킵 "
                           "(로컬 레플리카에는 반영됨, 재시작 시 재동기화)")

    def _resolve_path(self, db_path: Path | None) -> Path:
        self._db_path_was_injected = db_path is not None
        if db_path is not None:
            return Path(db_path)
        env = os.getenv("QUANT_BOT_DB_PATH")
        if env:
            return Path(env).expanduser().resolve()
        return settings.data_dir / "quant_bot.db"

    # ---------------- 내부 공통 ----------------

    def _get_payload(self, namespace: str, key: str, max_age: timedelta) -> str | None:
        try:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM cache WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
        except sqlite3.Error:
            logger.warning("캐시 읽기 실패 (%s/%s) — miss 취급", namespace, key, exc_info=True)
            return None
        if row is None:
            return None
        payload, fetched_at = row
        age = _utcnow() - datetime.fromisoformat(fetched_at)
        if age > max_age:
            logger.debug("캐시 만료 %s/%s (age=%s > ttl=%s)", namespace, key, age, max_age)
            return None
        logger.debug("캐시 적중 %s/%s (age=%s)", namespace, key, age)
        return payload

    def _put_payload(self, namespace: str, key: str, kind: CacheKind, payload: str) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (namespace, key, kind, payload, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (namespace, key, kind, payload, _utcnow().isoformat()),
            )
            self._conn.commit()
        except sqlite3.Error:
            logger.warning("캐시 쓰기 실패 (%s/%s) — 무시", namespace, key, exc_info=True)

    # ---------------- DataFrame / Series / JSON ----------------

    @staticmethod
    def _normalize_datetimes(df: pd.DataFrame) -> pd.DataFrame:
        """JSON 복원 후 datetime 해상도를 ns 로 통일.

        pandas 2.x 의 read_json(orient="table") 은 ISO 문자열을
        datetime64[us] 로 복원 → 캐시 적중 여부에 따라 dtype 이 달라지는
        비결정성을 막기 위해 원본과 같은 ns 로 정규화.
        """
        if isinstance(df.index, pd.DatetimeIndex):
            df.index = df.index.as_unit("ns")
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].astype("datetime64[ns]")
        return df

    def put_dataframe(self, namespace: str, key: str, df: pd.DataFrame) -> None:
        self._put_payload(namespace, key, "dataframe", df.to_json(orient="table", date_format="iso"))

    def get_dataframe(self, namespace: str, key: str, max_age: timedelta) -> pd.DataFrame | None:
        payload = self._get_payload(namespace, key, max_age)
        if payload is None:
            return None
        try:
            return self._normalize_datetimes(pd.read_json(StringIO(payload), orient="table"))
        except ValueError:
            logger.warning("캐시 payload 손상 (%s/%s) — miss 취급", namespace, key)
            return None

    def put_series(self, namespace: str, key: str, s: pd.Series) -> None:
        df = s.to_frame(name=s.name if s.name is not None else "value")
        self._put_payload(namespace, key, "series", df.to_json(orient="table", date_format="iso"))

    def get_series(self, namespace: str, key: str, max_age: timedelta) -> pd.Series | None:
        payload = self._get_payload(namespace, key, max_age)
        if payload is None:
            return None
        try:
            df = self._normalize_datetimes(pd.read_json(StringIO(payload), orient="table"))
            return df.iloc[:, 0].rename(df.columns[0])
        except (ValueError, IndexError):
            logger.warning("캐시 payload 손상 (%s/%s) — miss 취급", namespace, key)
            return None

    def put_json(self, namespace: str, key: str, obj: Any) -> None:
        self._put_payload(namespace, key, "json", json.dumps(obj, ensure_ascii=False))

    def get_json(self, namespace: str, key: str, max_age: timedelta) -> Any | None:
        payload = self._get_payload(namespace, key, max_age)
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("캐시 payload 손상 (%s/%s) — miss 취급", namespace, key)
            return None

    # ---------------- 영속 상태 (TTL 없음 — 신호 엔진의 실행 간 비교용) ----------------

    def put_state(self, namespace: str, key: str, obj: Any) -> None:
        """상태 저장. 캐시와 달리 만료 없음 — 다음 실행에서 '이전 값' 으로 사용."""
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO state (namespace, key, payload, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (namespace, key, json.dumps(obj, ensure_ascii=False), _utcnow().isoformat()),
            )
            self._conn.commit()
        except sqlite3.Error:
            logger.warning("상태 쓰기 실패 (%s/%s) — 무시", namespace, key, exc_info=True)

    def get_state(self, namespace: str, key: str) -> Any | None:
        """상태 조회. 없거나 손상 시 None (= 첫 실행 취급)."""
        try:
            row = self._conn.execute(
                "SELECT payload FROM state WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, json.JSONDecodeError):
            logger.warning("상태 읽기 실패 (%s/%s) — 첫 실행 취급", namespace, key, exc_info=True)
            return None

    # ---------------- 관리 ----------------

    def purge(self, older_than: timedelta | None = None) -> int:
        """오래된 캐시 행 삭제. older_than=None 이면 전체 삭제. 삭제 행 수 반환."""
        if older_than is None:
            cur = self._conn.execute("DELETE FROM cache")
        else:
            cutoff = (_utcnow() - older_than).isoformat()
            cur = self._conn.execute("DELETE FROM cache WHERE fetched_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def stats(self) -> dict[str, int]:
        """namespace 별 행 수."""
        rows = self._conn.execute(
            "SELECT namespace, COUNT(*) FROM cache GROUP BY namespace"
        ).fetchall()
        return dict(rows)

    @property
    def conn(self) -> sqlite3.Connection:
        """원시 connection 접근 — universe.py 등 자체 테이블을 관리하는 모듈용.

        cache/state 외의 영속 테이블(예: screened 유니버스)은 소유 모듈이
        이 connection 으로 자기 스키마를 생성·관리합니다.
        """
        return self._conn

    def close(self) -> None:
        self._conn.close()


def add_column_if_missing(conn, table: str, column: str, coltype: str) -> None:
    """ALTER TABLE ADD COLUMN — Turso 안전 멱등 마이그레이션.

    libsql 임베디드 레플리카는 읽기(PRAGMA)는 로컬, 쓰기(ALTER)는 원격에서 일어난다.
    로컬 레플리카가 stale 하면 PRAGMA 엔 컬럼이 없어 보여도 원격엔 이미 있어, ALTER 가
    'duplicate column' 으로 깨질 수 있다 (실제 운영 크래시) → 그 오류만 삼켜 멱등 보장.
    """
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except Exception as e:  # noqa: BLE001 — libsql: 원격엔 이미 존재할 수 있음(로컬 PRAGMA stale)
        if "duplicate column" in str(e).lower():
            logger.debug("%s.%s 이미 존재 — 마이그레이션 스킵 (stale 로컬 PRAGMA)", table, column)
        else:
            raise


# ---------------- 프로세스 싱글톤 ----------------

_storage: Storage | None = None


def get_storage() -> Storage:
    """표준 캐시 인스턴스 (싱글톤). DB 는 data/quant_bot.db."""
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage


# ---------------- fetch 함수용 데코레이터 ----------------

_GETTERS = {"dataframe": "get_dataframe", "series": "get_series", "json": "get_json"}
_PUTTERS = {"dataframe": "put_dataframe", "series": "put_series", "json": "put_json"}


def _is_empty(result: Any) -> bool:
    if isinstance(result, (pd.DataFrame, pd.Series)):
        return result.empty
    return not result


def cached(namespace: str, ttl: timedelta, kind: CacheKind):
    """fetch 함수에 투명 캐싱을 입히는 데코레이터.

    캐시 키는 함수 시그니처에 인자를 바인딩해 정규화 — 같은 호출이
    positional/keyword 어느 쪽으로 와도 같은 키가 됩니다.

    빈 결과는 캐시하지 않음 (8단계 빈 데이터 컨벤션과 충돌 방지).
    캐시 계층의 어떤 실패도 원본 fetch 를 막지 않음.
    """

    def decorator(fn: Callable):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            mode = _cache_mode()
            if mode == "off":
                return fn(*args, **kwargs)

            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            key = ":".join(f"{k}={v}" for k, v in bound.arguments.items())

            try:
                store = get_storage()
            except StorageError:
                logger.warning("캐시 DB 사용 불가 — 캐싱 없이 진행", exc_info=True)
                return fn(*args, **kwargs)

            if mode != "refresh":
                hit = getattr(store, _GETTERS[kind])(namespace, key, ttl)
                if hit is not None:
                    return hit

            result = fn(*args, **kwargs)
            if not _is_empty(result):
                getattr(store, _PUTTERS[kind])(namespace, key, result)
            return result

        return wrapper

    return decorator
