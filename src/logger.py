"""
src/logger.py
=============
프로젝트 전체 표준 로거 설정.

사용:
    from src.logger import get_logger
    logger = get_logger(__name__)
    logger.info("...")

처음 `get_logger` 가 불릴 때 `setup_logging()` 이 자동으로 한 번 실행됩니다.
- 파일 핸들러: `logs/quant_bot.log` (회전, 10MB × 5 백업, UTF-8, DEBUG 이상 모두 기록)
- 콘솔 핸들러: stderr (INFO 이상)

설정은 idempotent — 여러 번 import 해도 핸들러 중복 등록 없음.

환경변수:
    QUANT_BOT_LOG_LEVEL   콘솔 레벨 override (예: DEBUG, INFO, WARNING)
    QUANT_BOT_LOG_DIR     로그 디렉토리 override (기본: 프로젝트 루트의 logs/)
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

# config 에 의존하지 않도록 직접 PROJECT_ROOT 계산 (순환 import 회피).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 포맷 — %z 가 +0900 같은 오프셋을 자동으로 붙임
_FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_CONSOLE_FORMAT = "%(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"

# 기본 타임존 (한국). 환경변수 QUANT_BOT_LOG_TZ 로 override 가능.
_DEFAULT_TZ_NAME = "Asia/Seoul"


class TZFormatter(logging.Formatter):
    """타임존이 명시된 timestamp 를 찍는 Formatter.

    표준 logging.Formatter 는 호스트 로컬 시각으로 찍어서 머신마다 다르게
    보이는 문제가 있음. 이 Formatter 는 명시한 zone (기본 KST) 으로 변환 후
    `+0900` 같은 오프셋을 같이 출력 → 어떤 환경에서 실행해도 일관됨.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        tz: ZoneInfo | None = None,
    ):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.tz = tz or ZoneInfo(os.getenv("QUANT_BOT_LOG_TZ", _DEFAULT_TZ_NAME))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """record.created (epoch seconds) → 명시된 tz 의 datetime → 포맷팅."""
        ct = dt.datetime.fromtimestamp(record.created, tz=self.tz)
        if datefmt:
            return ct.strftime(datefmt)
        # ISO 8601 (예: 2026-05-10T12:34:56.789+09:00)
        return ct.isoformat(timespec="milliseconds")

# 기본값
DEFAULT_LOG_FILE = "quant_bot.log"
DEFAULT_LEVEL_FILE = logging.DEBUG
DEFAULT_LEVEL_CONSOLE = logging.INFO
DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
DEFAULT_BACKUP_COUNT = 5

# 너무 시끄러운 외부 라이브러리는 WARNING 으로 누름
_NOISY_LOGGERS = (
    "urllib3",
    "urllib3.connectionpool",
    "matplotlib",
    "yfinance",
    "fredapi",
    "requests",
    "peewee",
    "asyncio",
)

# idempotent 보장 플래그
_CONFIGURED = False


def _resolve_log_dir() -> Path:
    """환경변수 우선, 없으면 프로젝트 루트의 logs/."""
    env_dir = os.getenv("QUANT_BOT_LOG_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return PROJECT_ROOT / "logs"


def _resolve_console_level() -> int:
    """환경변수로 콘솔 레벨 override 가능."""
    name = os.getenv("QUANT_BOT_LOG_LEVEL", "").upper().strip()
    if not name:
        return DEFAULT_LEVEL_CONSOLE
    return getattr(logging, name, DEFAULT_LEVEL_CONSOLE)


def setup_logging(
    log_dir: Path | None = None,
    log_file: str = DEFAULT_LOG_FILE,
    file_level: int = DEFAULT_LEVEL_FILE,
    console_level: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    capture_warnings: bool = True,
) -> Path:
    """루트 로거를 설정하고 로그 파일 경로를 반환.

    Idempotent — 두 번째 호출부터는 즉시 반환합니다.

    Returns
    -------
    Path
        실제로 사용 중인 로그 파일의 절대 경로.
    """
    global _CONFIGURED

    log_dir_resolved = (log_dir or _resolve_log_dir()).resolve()
    log_path = log_dir_resolved / log_file

    if _CONFIGURED:
        return log_path

    log_dir_resolved.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # 루트는 가장 낮은 레벨로 두고, 핸들러별로 필터링.
    root.setLevel(logging.DEBUG)

    # 기존 핸들러 제거 (이전 setup, basicConfig, Jupyter 등에서 등록된 것).
    for h in list(root.handlers):
        root.removeHandler(h)

    # ---- 파일 핸들러 (verbose) ----
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(TZFormatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(file_handler)

    # ---- 콘솔 핸들러 (간결, 시간 표기 없음 → 터미널 자체 시간으로 충분) ----
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(console_level if console_level is not None else _resolve_console_level())
    console_handler.setFormatter(TZFormatter(_CONSOLE_FORMAT))
    root.addHandler(console_handler)

    # ---- 시끄러운 라이브러리 누르기 ----
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # yfinance 는 실패를 자기 로거에 ERROR 로 직접 찍어서 WARNING 누름을 우회.
    # 모든 yfinance 실패는 data_fetcher 가 도메인 예외로 변환해 우리가 직접
    # 로깅하므로, 원본 ERROR 는 중복 노이즈 → CRITICAL 로 차단 (3단계 fix).
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    # ---- warnings.warn() 도 로깅으로 흡수 ----
    if capture_warnings:
        logging.captureWarnings(True)

    _CONFIGURED = True

    # 첫 진입 표식
    logging.getLogger(__name__).debug(
        "Logging initialized. file=%s file_level=%s console_level=%s",
        log_path,
        logging.getLevelName(file_level),
        logging.getLevelName(console_handler.level),
    )

    return log_path


def get_logger(name: str) -> logging.Logger:
    """이름이 부여된 로거를 반환. 첫 호출 시 자동으로 setup_logging 실행."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


# 모듈 임포트만으로 로깅이 활성화되도록 — 스크립트가 명시 호출 안 해도 OK.
# (단, side-effect 라는 점은 문서에 명시해두었음. 2단계에서 명시적 호출로 분리 가능.)
