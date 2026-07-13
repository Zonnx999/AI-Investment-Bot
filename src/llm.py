"""
src/llm.py
==========
ROADMAP §2.1 — 다이제스트 LLM 한 줄 요약 (선택적 양념 레이어).

이미 계산 완료된 결정론적 다이제스트 텍스트를 받아, MiniMax(NVIDIA NIM,
OpenAI chat 호환 API)로 한국어 2~3문장 요약을 생성합니다. **표현 레이어일 뿐**
— 숫자·티커를 생성/수정하지 않도록 프롬프트로 금지하며, 어떤 실패든
(키 미설정·HTTP 에러·타임아웃·레이트리밋·응답 스키마 위반) 요약만 생략되고
다이제스트 발송은 절대 막히지 않습니다 (CLAUDE.md §4.10 #10 사상).

공개 API
--------
- ``summarize(digest_text)`` — 요약 문자열 반환. 실패 시 도메인 예외 raise
  (라이브러리 경계에서 변환 — §4.5).
- ``summarize_safe(digest_text)`` — best-effort 래퍼. 킬스위치 확인 후 호출,
  어떤 실패든 warning 로그만 남기고 ``None`` 반환. 다이제스트 경로는 이것만 사용.
- ``llm_enabled()`` — 킬스위치 (환경변수 ``QUANT_BOT_LLM=0`` 으로 끔).

⚠️ 배포 전 라이브 스모크 필수 (§4.10 #3)
----------------------------------------
기본 모델 id(``minimaxai/minimax-m2.7``, 2026-07 스모크로 확정 — m2 는 NIM 에서 은퇴,
m3 는 ~54s 로 read timeout 초과)와 응답 스키마(choices[0].message.content)는
오프라인에서 검증 불가한 외부 가정입니다. 첫 사용 전 실제 NVIDIA API 에 1콜 찔러
모델 id 유효성·필드 존재를 확인할 것. 틀려도 요약 생략 폴백이라 다이제스트는 안전.
모델/엔드포인트는 ``MINIMAX_MODEL`` / ``MINIMAX_BASE_URL`` 로 교체 가능.
"""

from __future__ import annotations

import os
import re

from src.config import settings
from src.exceptions import (
    ApiAuthError,
    ApiAuthorizationError,
    ApiConnectionError,
    ApiHttpError,
    ApiTimeoutError,
    DataValidationError,
    QuantBotError,
    RateLimitError,
)
from src.http import RETRY_TOTAL, get_http_session, is_timeout
from src.logger import get_logger

logger = get_logger(__name__)

_SOURCE = "NVIDIA-NIM"

# 생성 파라미터 — 요약은 결정성에 가깝게 (낮은 temperature), 분량은 2~3문장이면 충분
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 300

# 최종 요약 최대 길이 (문자) — 초과분은 "…" 로 잘라 다이제스트 상단을 짧게 유지
MAX_SUMMARY_CHARS = 500

# 표현 레이어 계약: 숫자·티커 생성/수정 금지를 프롬프트로 명시
_SYSTEM_PROMPT = (
    "당신은 개인 퀀트 리서치 봇의 다이제스트 요약가입니다. "
    "사용자가 이미 계산 완료된 일일 투자 다이제스트 전문을 줍니다. "
    "그 내용만 근거로 오늘의 시장 분위기와 핵심 포인트를 "
    "한국어 평문 2~3문장으로 쉽게 풀어 요약하세요.\n"
    "규칙:\n"
    "- 다이제스트에 없는 숫자·티커·종목명·사실을 새로 만들지 마세요.\n"
    "- 숫자를 다시 계산하거나 바꾸지 마세요.\n"
    "- 매수/매도 권유 표현을 쓰지 마세요.\n"
    "- 마크다운 서식·이모지·머리말 없이 요약 문장만 출력하세요."
)


def llm_enabled() -> bool:
    """LLM 요약 킬스위치. ``QUANT_BOT_LLM=0`` (또는 off/false) 이면 비활성."""
    return os.getenv("QUANT_BOT_LLM", "1").strip().lower() not in ("0", "off", "false")


def _raise_for_status(status_code: int) -> None:
    """HTTP 상태 코드 → 도메인 예외 변환 (§4.5: 401/403/429 는 전용 예외)."""
    if status_code < 400:
        return
    msg = f"LLM 요약 API 실패 (HTTP {status_code})"
    if status_code == 401:
        raise ApiAuthError(msg + " — MINIMAX_API_KEY 무효", source=_SOURCE)
    if status_code == 403:
        raise ApiAuthorizationError(msg + " — 권한/플랜 부족", source=_SOURCE)
    if status_code == 429:
        raise RateLimitError(msg + " — 호출 한도 초과", source=_SOURCE)
    raise ApiHttpError(msg, status_code=status_code, source=_SOURCE)


def _extract_content(body: object) -> str | None:
    """OpenAI-chat 호환 응답에서 choices[0].message.content 를 방어적으로 추출.

    스키마가 조금이라도 다르면 None — 외부 응답 형태를 신뢰하지 않음 (§4.10 #3).
    """
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


# reasoning 모델(MiniMax 계열 등)의 사고 블록 — 닫는 태그가 없는 미완성 출력까지 방어.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def _normalize(content: str) -> str:
    """reasoning 태그 제거 → Markdown 문자 제거 → 공백 정리 → 길이 절단.

    요약은 '평문 산문' 계약: (1) reasoning 모델의 <think> 블록이 그대로 나오면
    사용자에게 사고과정이 노출되고, (2) *·_·`·[ 가 섞이면 Markdown 다이제스트
    전체가 파싱 실패 → 평문 폴백(전송 2배)이 되므로 여기서 모두 제거.
    """
    text = _THINK_RE.sub("", content)
    text = _THINK_OPEN_RE.sub("", text)          # 잘린(미닫힘) think 블록
    text = text.translate(str.maketrans("", "", "*_`["))
    text = " ".join(text.split())   # 개행·연속 공백 → 한 칸 (한 문단 유지)
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return text


def summarize(digest_text: str) -> str:
    """다이제스트 텍스트 → 한국어 2~3문장 요약. 실패 시 도메인 예외.

    Raises
    ------
    MissingApiKeyError     MINIMAX_API_KEY 미설정 (HTTP 호출 전에 발생)
    ApiAuthError / ApiAuthorizationError / RateLimitError / ApiHttpError
                           NVIDIA API 4xx/5xx
    ApiTimeoutError / ApiConnectionError   네트워크 실패
    DataValidationError    응답이 JSON 이 아니거나 스키마 위반, 또는 내용이 빈 문자열
    """
    import requests

    api_key = settings.require("minimax_api_key")   # raises MissingApiKeyError
    url = f"{settings.minimax_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.minimax_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": digest_text},
        ],
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
    }
    # 키는 헤더로만 전송 — URL/예외 메시지에 넣지 않음 (§4.9). 로그는 http.py 마스킹이 방어.
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        # LLM 생성은 데이터 API 보다 느림 (m2.7 실측 ~7s, 리즈닝 버스트 여유) — read 60s
        response = get_http_session().post(
            url, json=payload, headers=headers, timeout=(5.0, 60.0)
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if is_timeout(e):
            raise ApiTimeoutError(
                f"LLM 요약 타임아웃 (재시도 {RETRY_TOTAL}회 포함)", source=_SOURCE
            ) from e
        raise ApiConnectionError("LLM 요약 API 연결 실패", source=_SOURCE) from e

    _raise_for_status(response.status_code)

    try:
        body = response.json()
    except ValueError as e:
        raise DataValidationError("LLM 요약 응답이 JSON 이 아님", source=_SOURCE) from e

    content = _extract_content(body)
    if not content or not content.strip():
        raise DataValidationError(
            "LLM 요약 응답에 choices[0].message.content 가 없거나 비어 있음",
            source=_SOURCE,
        )
    normalized = _normalize(content)
    if not normalized:
        raise DataValidationError(
            "LLM 요약이 정규화 후 비어 있음 (reasoning-only 응답 추정)", source=_SOURCE
        )
    return normalized


def summarize_safe(digest_text: str) -> str | None:
    """best-effort 요약 — 어떤 실패든 요약만 생략하고 None (다이제스트 발송 불가침).

    킬스위치(``QUANT_BOT_LLM=0``)가 켜져 있거나 키 미설정이면 HTTP 호출 없이 생략.
    """
    if not llm_enabled():
        logger.info("LLM 요약 비활성 (QUANT_BOT_LLM) — 생략")
        return None
    try:
        return summarize(digest_text)
    except QuantBotError as e:
        # 키 미설정 포함 — 요약은 선택 기능이라 warning 으로만 남김
        logger.warning("LLM 요약 실패 — 요약 없이 발송: %s", e)
        return None
    except Exception:  # noqa: BLE001 — 최후 fallback: 요약이 다이제스트를 막으면 안 됨
        logger.exception("LLM 요약 중 예상 밖 오류 — 요약 없이 발송")
        return None
