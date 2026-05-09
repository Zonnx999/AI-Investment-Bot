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

    # 경로
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"

    def require(self, key: str) -> str:
        """필수 키가 비어있으면 친절한 에러 메시지를 띄움."""
        value = getattr(self, key, "")
        if not value:
            raise RuntimeError(
                f".env 파일에 {key.upper()} 가 설정되어 있지 않습니다. "
                f"프로젝트 루트의 .env.example 을 참고해 .env 를 만들어 주세요."
            )
        return value


settings = Settings()
