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
QUANT_BOT_DB_PATH   DB 파일 경로 override (기본: data/quant_bot.db)
QUANT_BOT_CACHE     "on"(기본) / "off"(읽기·쓰기 모두 끔) /
                    "refresh"(읽기만 끔 — 새로 받아서 캐시 갱신)
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sqlite3
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Storage:
    """SQLite 기반 (namespace, key) → payload 캐시.

    읽기 miss 또는 손상된 payload 는 None 반환 (= 캐시 miss 취급).
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = self._resolve_path(db_path)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except (OSError, sqlite3.Error) as e:
            raise StorageError(f"캐시 DB 초기화 실패: {self.db_path}") from e
        logger.debug("캐시 DB 연결: %s", self.db_path)

    @staticmethod
    def _resolve_path(db_path: Path | None) -> Path:
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

    def close(self) -> None:
        self._conn.close()


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
