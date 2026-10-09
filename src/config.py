"""
공통 설정과 환경변수 로딩.

다른 모듈은 `from src.config import settings` 만 하면
어디서든 API 키와 경로를 똑같이 쓸 수 있습니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src.exceptions import MissingApiKeyError

# 프로젝트 루트 = 이 파일이 있는 src/ 의 부모 디렉토리
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# .env 파일이 있으면 환경변수로 로딩
load_dotenv(PROJECT_ROOT / ".env")

_TRUTHY = ("1", "on", "true", "yes")


def _env_flag(name: str, default: str = "0") -> bool:
    """환경변수 → bool (1/on/true/yes 만 True). llm.llm_enabled 와 같은 규칙."""
    return os.getenv(name, default).strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    """환경변수 → int. 비어 있거나 숫자가 아니면 기본값 (설정 오타가 발송을 막지 않게)."""
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """환경변수 → 허용값 중 하나 (소문자 비교). 비어 있으면 default, 허용값 밖이면 default + 경고.

    logger 는 지연 import — src.logger 는 config 를 쓰지 않으므로 순환 없음(모듈 상단 import 를
    피하는 건 config 가 가장 먼저 로드되는 모듈이라 로깅 설정 시점을 뒤로 미루기 위함).
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in allowed:
        return raw
    from src.logger import get_logger

    get_logger(__name__).warning("%s=%r 는 허용값 %s 밖 — %r 로 동작", name, raw, allowed, default)
    return default


@dataclass(frozen=True)
class Settings:
    """API 키와 경로를 한 곳에 모아두는 설정 객체."""

    # API keys
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    fred_api_key: str = os.getenv("FRED_API_KEY", "")
    fmp_api_key: str = os.getenv("FMP_API_KEY", "")
    news_api_key: str = os.getenv("NEWS_API_KEY", "")
    krx_api_key: str = os.getenv("KRX_API_KEY", "")  # 한국거래소 정보데이터시스템 OpenAPI
    dart_api_key: str = os.getenv("DART_API_KEY", "")  # DART 전자공시 (한국 재무제표, Phase 9b)

    # LLM 다이제스트 요약 (ROADMAP §2.1, 선택) — NVIDIA NIM (OpenAI chat 호환)
    # 기본 OFF (QUANT_BOT_LLM=1 로 opt-in, src/llm.py). 키 이름 MINIMAX_* 는 하위호환으로 유지.
    # ⚠️ 기본 모델 id 는 배포 전 라이브 스모크로 검증 필수 (§4.10 #3, src/llm.py 참고)
    minimax_api_key: str = os.getenv("MINIMAX_API_KEY", "")
    minimax_base_url: str = os.getenv("MINIMAX_BASE_URL", "https://integrate.api.nvidia.com/v1")
    # 이력: minimax-m2 → m2.7 (2026-07) → m2.7 도 HTTP 410 퇴역 (2026-10).
    # 2026-10 실측(각 1~3콜): deepseek-v4.1-flash 3/3 성공 23~37s·수치 충실 /
    # glm-5.3-flash 는 reasoning 토큰 소진으로 문장 절단 / gemma-4-31b-it 60s 타임아웃.
    # ⚠️ .env 에 MINIMAX_MODEL 이 있으면 이 기본값을 덮어씀 — 퇴역 id 가 남아 있지 않은지 확인.
    minimax_model: str = os.getenv("MINIMAX_MODEL", "deepseek-ai/deepseek-v4.1-flash")

    # 알림 채널 (Phase 7) — telegram_chat_id 는 소유자(Phase 11a 승인자) 겸 항상 수신자
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # 호스팅 DB (Phase 10) — Turso(libSQL) 임베디드 레플리카. 둘 다 비면 로컬 sqlite3
    turso_database_url: str = os.getenv("TURSO_DATABASE_URL", "")
    turso_auth_token: str = os.getenv("TURSO_AUTH_TOKEN", "")

    # ---- 다이제스트 (Stage 2 WP-DIGEST) — 기본값은 '판정·후보·성과만' 인 조용한 메시지 ----
    # 선행지표 예측 섹션 / 제안 비중(Kelly) — 검증 전이라 기본 OFF (코드는 보존, 플래그로만 재활성화)
    digest_predictions: bool = _env_flag("QUANT_BOT_DIGEST_PREDICTIONS")
    digest_weights: bool = _env_flag("QUANT_BOT_DIGEST_WEIGHTS")
    # 점수(유니버스 재점수) 기준일 신선도 — warn 일 이상이면 기준 줄 ⚠️ + 소유자 DM,
    # hold 일 이상이면 후보 섹션을 '후보 보류' 1줄로 대체하고 원장에 기록하지 않음 (>14일 = 15)
    universe_warn_days: int = _env_int("QUANT_BOT_UNIVERSE_WARN_DAYS", 8)
    universe_hold_days: int = _env_int("QUANT_BOT_UNIVERSE_HOLD_DAYS", 15)
    # 원장 성과 줄 표시: on(구독자 메시지) | owner(매 런 소유자 평문 DM 에 성과 줄) | off.
    # 허용값 밖이면 'on' 폴백 + 경고 로그 (오타가 조용히 '성과 줄 없음' 이 되지 않게)
    ledger_line: str = _env_choice("QUANT_BOT_LEDGER_LINE", "on", ("on", "owner", "off"))
    # '오늘 할 일: 없음' 이고 경고가 없는 날 무음 도착(disable_notification) — 기본 꺼짐(항상 소리)
    silent_quiet: bool = _env_flag("QUANT_BOT_DIGEST_SILENT_QUIET")
    # 후보 선정 후 하락 1회 알림 임계(%) — 0 이면 끔 (사용자 옵션 D: 기본 -10 / -15 / 끄기)
    pick_dd_alert_pct: float = _env_float("QUANT_BOT_PICK_DD_ALERT_PCT", -10.0)

    # 경로
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"

    def require(self, key: str) -> str:
        """필수 키가 비어있으면 MissingApiKeyError 를 발생.

        Parameters
        ----------
        key : str
            Settings 의 attribute 이름 (예: "fred_api_key").
            대응되는 환경변수 이름은 자동으로 대문자 변환.
        """
        value = getattr(self, key, "")
        if not value:
            raise MissingApiKeyError(key_name=key.upper())
        return value


settings = Settings()
