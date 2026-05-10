# AI-Investment-Bot

개인용 퀀트 리서치 비서. 펀더멘털 분석 + 거시 지표 + 다운사이드 리스크 + 뉴스 센티먼트를 통합해 LLM이 자연어로 답해주는 챗봇.

## 철학

- **레이 달리오**: 자산 간 상관관계가 낮은 포트폴리오. 주식 / 채권 / 금 / 원자재 / 암호화폐를 한 화면에서 동시에 본다.
- **에드워드 소프**: 악재가 터졌을 때 통계적으로 얼마나 떨어질 수 있는지 사전에 계산. VaR / MDD / 시나리오 분석.
- **모듈 분리**: 데이터, 분석, 리스크, UI를 각각 독립된 모듈로. 한 파일 안에 다 욱여넣지 않는다.

## 폴더 구조

```
AI-Investment-Bot/
├── README.md              # 이 파일
├── .gitignore             # git이 무시할 파일들
├── .env.example           # API 키 템플릿 (실제 .env는 절대 커밋 안 함)
├── requirements.txt       # 파이썬 패키지 목록
│
├── src/                   # 핵심 코드
│   ├── __init__.py
│   ├── config.py          # 환경변수 로딩, 공통 설정
│   ├── data_fetcher.py    # 주가/재무제표/거시지표/암호화폐 수집 전담
│   ├── macro_analyzer.py  # 자산간 상관관계, 시장 국면 분석
│   ├── risk_engine.py     # VaR, MDD, 시나리오 다운사이드 분석
│   └── bot_interface.py   # 챗봇 인터페이스 (Streamlit/CLI)
│
├── scripts/               # 실행 가능한 스크립트
│   ├── hello_world.py         # 주식 + 암호화폐 + 금 종가
│   ├── check_macro.py         # FRED 거시 지표 대시보드
│   ├── check_crypto.py        # 비트코인 · 이더리움 동향
│   ├── check_korea.py         # 한국 수출/수입/무역수지
│   ├── check_fundamentals.py  # FMP 5년 재무제표 추세 (FMP 키 필요)
│   └── check_market_regime.py # ★ 매일 아침 시장 조망 ★
│
├── notebooks/             # 탐색·실험용 주피터 노트북
│
├── data/                  # 캐시된 데이터 (git 무시)
│
└── tests/                 # 테스트 코드
    └── __init__.py
```

## 첫 실행 (Quick Start)

```bash
# 1. 가상환경 만들기 (한 번만)
python3 -m venv .venv

# 2. 가상환경 켜기 (터미널 켤 때마다)
source .venv/bin/activate

# 3. 패키지 설치 (한 번만, 또는 requirements.txt 바뀔 때)
pip install -r requirements.txt

# 4. 환경변수 파일 만들기
cp .env.example .env
# 그리고 .env 파일 열어서 API 키들 채워넣기

# 5. 첫 스크립트 실행
python scripts/hello_world.py
```

## 개발 단계 (Roadmap)

- [x] Phase 0 — 폴더 구조 + Hello World
- [x] Phase 1 — `data_fetcher.py` 완성 (yfinance + FRED + CoinGecko + 한국 무역통계)
- [x] Phase 2 — `macro_analyzer.py` 상관관계 + 시장 국면 분류
- [ ] Phase 3 — `risk_engine.py` VaR / MDD 모듈
- [ ] Phase 4 — LLM 통합 (Claude API tool calling)
- [ ] Phase 5 — 뉴스 인테이크 + 센티먼트
- [ ] Phase 6 — Streamlit 챗 UI

## Phase 1 사용법

```bash
# 주식 + 암호화폐 + 금 (FRED 키 불필요)
python scripts/hello_world.py

# 암호화폐만 자세히 (FRED 키 불필요)
python scripts/check_crypto.py

# 거시 대시보드 (FRED 키 필요 — .env 에 FRED_API_KEY 설정)
python scripts/check_macro.py

# 한국 수출입 통계 (FRED 키 사용)
python scripts/check_korea.py

# 종목 5년 재무제표 추세 (FMP 키 필요)
python scripts/check_fundamentals.py CPNG
python scripts/check_fundamentals.py NVDA

# 시장 조망 (Phase 2): 자산군 + 상관관계 + 변동성 + 국면
python scripts/check_market_regime.py
```

`data_fetcher.py` API:

```python
from src.data_fetcher import (
    fetch_prices,            # 주식·ETF·선물·환율 (yfinance)
    fetch_fundamentals,      # 기업 펀더멘털 요약
    fetch_macro,             # FRED 단일 시계열
    fetch_macro_dashboard,   # FRED 여러 지표 한 번에
    fetch_crypto,            # CoinGecko 코인 시계열
    fetch_korea_trade,       # 한국 월간 수출/수입/무역수지 (FRED 경유)
    fetch_financial_statements,  # FMP 손익/재무/현금흐름 시계열
    fetch_key_metrics,           # FMP P/E, ROE, 부채비율 등
    fetch_ratios,                # FMP 광범위한 재무 비율
)
```
