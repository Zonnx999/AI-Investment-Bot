"""
src/utils.py
============
모듈 경계에 속하지 않는 작은 공용 헬퍼 모음 (4단계 리팩토링 결과물).

여기 들어올 자격: 두 개 이상의 모듈/스크립트에서 똑같이 반복되던 코드.
도메인 로직(거시/리스크/스크리닝)은 각자의 모듈에 — 여기는 순수 유틸만.
"""

from __future__ import annotations

import pandas as pd

from src.exceptions import DataValidationError

# 연환산 기준 거래일 수. 암호화폐(365일 거래)도 혼합 패널 일관성을 위해 동일 적용.
TRADING_DAYS_PER_YEAR = 252


def clip(v: float, lo: float, hi: float) -> float:
    """v 를 [lo, hi] 구간으로 자름 (기존 screener/universe 의 `_clip` 통합).

    주의: NaN 입력은 ``min(hi, nan) == hi`` 특성 때문에 hi(만점)로 새어나감 —
    호출부가 NaN 을 미리 걸러야 함 (screener._safe 참고).
    """
    return max(lo, min(hi, v))


def close_series(df: pd.DataFrame) -> pd.Series:
    """OHLCV DataFrame 에서 종가 시리즈 추출.

    yfinance 는 옵션에 따라 'Adj Close' 가 있기도 없기도 함 →
    'Adj Close' 우선, 없으면 'Close' fallback. (기존에 macro_analyzer /
    risk_engine / hello_world 세 곳에 복붙되어 있던 로직)

    Raises
    ------
    DataValidationError
        둘 다 없는 경우 (가격 데이터 스키마 위반).
    """
    if "Adj Close" in df.columns:
        series = df["Adj Close"]
    elif "Close" in df.columns:
        series = df["Close"]
    else:
        raise DataValidationError(
            "가격 데이터에 'Adj Close'/'Close' 컬럼이 모두 없음", source="yfinance"
        )
    return series.squeeze()  # 혹시 2D 로 와도 1D 로 평탄화


def pick_first(row: pd.Series, candidates: list[str]):
    """행(Series)에서 후보 컬럼명들 중 처음으로 '존재하고 NaN 아닌' 값을 반환.

    FMP 와 yfinance 가 같은 항목을 다른 이름으로 주기 때문에
    ("revenue" vs "Total Revenue") 후보 리스트로 흡수. 전부 없으면 None.
    """
    for name in candidates:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return None


# ----------------------------------------------------------------------
# 텔레그램 legacy Markdown 안전 헬퍼 (digest / notifier 공용 — CLAUDE.md §4.10 #10)
# ----------------------------------------------------------------------
# legacy Markdown(parse_mode="Markdown") 은 `_ * \` [` 네 글자로 엔티티를 열고, 백슬래시
# 이스케이프를 지원하지 않는다. 동적 문자열(회사명·에러 문구)은 렌더 경계 한 곳에서
# md_safe() 를 통과시켜 '비슷하게 생긴 다른 유니코드' 로 바꾼다 — 짝이 안 맞아도 파서가
# 엔티티를 열지 않으므로 메시지 전체가 통째로 실패하는 사고(MSG-06)를 막는다.
_MD_LOOKALIKE = str.maketrans({"*": "∗", "_": "＿", "`": "ʼ", "[": "［"})
MD_SAFE_NAME_LEN = 24


def md_safe(text: object, max_len: int | None = None) -> str:
    """동적 문자열을 legacy Markdown 안전 문자열로 (`*`→`∗`, `_`→`＿`, `` ` ``→`ʼ`, `[`→`［`).

    max_len 을 주면 그 길이로 절단 (회사명은 MD_SAFE_NAME_LEN=24). None/빈 값은 "".
    백슬래시는 그대로 둔다 — 엔티티 문자가 모두 치환돼 이스케이프로 해석될 짝이 없다.
    """
    if text is None:
        return ""
    s = str(text).translate(_MD_LOOKALIKE)
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s


def strip_md(text: str) -> str:
    """Markdown 서식 문자를 제거한 평문 — sendMessage 400(파싱 실패) 폴백 재전송용.

    굵게(`*`)·코드(`` ` ``)·링크 괄호(`[ ]`)만 걷어낸다. `_` 는 유저명(john_doe)처럼 내용의
    일부일 수 있어 유지 — 평문에서는 밑줄이 그대로 보여도 무해하다.
    """
    return "".join(ch for ch in str(text) if ch not in "*`[]")
