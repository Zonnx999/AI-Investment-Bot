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


@dataclass(frozen=True)
class Settings:
    """API 키와 경로를 한 곳에 모아두는 설정 객체."""

    # API keys
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    fred_api_key: str = os.getenv("FRED_API_KEY", "")
    fmp_api_key: str = os.getenv("FMP_API_KEY", "")
    news_api_key: str = os.getenv("NEWS_API_KEY", "")
    krx_api_key: str = os.getenv("KRX_API_KEY", "")  # 한국거래소 정보데이터시스템 OpenAPI

    # 알림 채널 (Phase 7)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # 호스팅 DB (Phase 10) — Turso(libSQL) 임베디드 레플리카. 둘 다 비면 로컬 sqlite3
    turso_database_url: str = os.getenv("TURSO_DATABASE_URL", "")
    turso_auth_token: str = os.getenv("TURSO_AUTH_TOKEN", "")

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
