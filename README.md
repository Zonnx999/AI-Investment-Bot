# AI-Investment-Bot

개인용 퀀트 리서치 자동화 봇. 매일 아침 시장 국면·자산군 동향·종목 리스크·가치주 신호를 자동으로 계산해 보여주는 도구입니다.

> **챗봇이 아닙니다.** 결정론적 신호 생성 + 자동 알림 시스템입니다. LLM 은 나중에 뉴스 센티먼트 분류나 일일 요약 한 줄 정도의 선택적 레이어로만 쓸 계획입니다 (Phase 8+).

- 프로젝트 규칙: [CLAUDE.md](CLAUDE.md) (에이전트가 가장 먼저 읽는 파일)
- 현재 작업 상태: [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md)
- 다음 작업: [docs/ROADMAP.md](docs/ROADMAP.md)

## 철학

- **레이 달리오 (All Weather)**: 상관관계 낮은 자산군 — 주식·장기채·회사채·하이일드·금·원자재·암호화폐 — 을 한 화면에서 동시에 보고, 분산 효과가 실제로 작동하는지 검증.
- **에드워드 소프 (리스크 관리)**: "이 종목 얼마나 떨어질 수 있나"를 통계적으로 사전 계산. VaR / CVaR / MDD / Monte Carlo / 시나리오 분석.
- **신호 자동화**: 매일 한 번 데이터 받고 분석 돌리고 결과 push. 사용자가 명령어 칠 필요 없게.
- **대체 데이터**: 가격뿐 아니라 거시지표·한국 수출·M2 같은 선행지표로 펀더멘털 예측 (Phase 6).

## 폴더 구조

```
AI-Investment-Bot/
├── CLAUDE.md              # 프로젝트 규칙 (먼저 읽기)
├── README.md              # 이 파일
├── .env.example           # API 키 템플릿 (실제 .env 는 절대 커밋 안 함)
├── pyproject.toml         # 패키지/의존성 정의 (pip install -e ".[dev]")
│
├── docs/
│   ├── CURRENT_STATE.md   # 현재 작업 상태 (핸드오버용)
│   └── ROADMAP.md         # 다음 작업 계획
│
├── src/                   # 핵심 코드
│   ├── config.py          # 환경변수 로딩, Settings dataclass
│   ├── logger.py          # 로깅 설정 (KST timestamp)
│   ├── exceptions.py      # 도메인 예외 계층
│   ├── data_fetcher.py    # 모든 외부 API 호출 전담
│   ├── macro_analyzer.py  # 자산 간 상관관계, 시장 국면 분류
│   ├── risk_engine.py     # VaR / MDD / Monte Carlo / 시나리오
│   ├── screener.py        # 가치주 스크리너
│   ├── storage.py         # SQLite 캐시 + state 테이블
│   ├── signals.py         # 신호 엔진 (팩터/스크리닝/알림)
│   ├── predictors.py      # 선행지표 lead-lag 예측 (7개 관계: M2/한국수출/달러/위키 등)
│   └── utils.py           # 공용 헬퍼 (종가 추출, 컬럼 후보 선택)
│
├── scripts/               # 실행 스크립트
│   ├── hello_world.py         # 주식 + 암호화폐 + 금 종가
│   ├── check_macro.py         # FRED 거시 지표 대시보드
│   ├── check_crypto.py        # BTC/ETH 동향
│   ├── check_korea.py         # 한국 수출/수입/무역수지
│   ├── check_fundamentals.py  # FMP 5년 재무제표 추세
│   ├── check_market_regime.py # ★ 매일 아침 시장 조망 ★
│   ├── check_risk.py          # ★ 종목 리스크 리포트 ★
│   ├── screen_value.py        # ★ 가치주 스크리너 ★
│   ├── daily_update.py        # ★ 일일 수집 오케스트레이터 (cron 진입점) ★
│   ├── check_signals.py       # ★ 일일 신호 리포트 (팩터/발굴/알림) ★
│   ├── check_predictions.py   # ★ 선행지표 예측 (M2→BTC 등) ★
│   ├── diag_fmp.py            # FMP 엔드포인트 접근 진단
│   └── demo_exceptions.py     # 예외 체계 검증 데모
│
├── dashboard/
│   ├── index.html             # 가치주 대시보드 (스크리너 결과 시각화)
│   └── screener_data.json     # screen_value.py 가 자동 생성 (gitignore)
│
├── data/                  # 데이터 캐시 (gitignore)
├── notebooks/             # 실험용 노트북 (gitignore)
├── logs/                  # quant_bot.log (gitignore)
└── tests/                 # pytest — 오프라인 테스트 85개 (python -m pytest)
```

## 첫 실행 (Quick Start)

```bash
# 1. 가상환경 만들기 (한 번만)
python3.12 -m venv .venv

# 2. 가상환경 켜기 (터미널 켤 때마다)
source .venv/bin/activate

# 3. 패키지 설치 (한 번만, 또는 pyproject.toml 바뀔 때 — editable install)
pip install -e ".[dev]"

# 4. 환경변수 파일 만들기
cp .env.example .env
# .env 열어서 FRED_API_KEY, FMP_API_KEY 채워넣기

# 5. 첫 스크립트 실행
python scripts/hello_world.py
```

## 사용법

```bash
# ★ 매일 아침 1회 — 모든 데이터 수집 + 캐시 워밍 (이후 다른 스크립트는 캐시 적중)
python scripts/daily_update.py
python scripts/daily_update.py --refresh   # 캐시 무시하고 새로 수집

# ★ 일일 신호 리포트 — 팩터 점수 + 발굴 종목 + 변화 알림 (Phase 5)
python scripts/check_signals.py
python scripts/check_signals.py --screen   # 미국 워치리스트 발굴 포함

# ★ 선행지표 예측 — M2/한국수출/달러/구리·금/위키관심 → 자산 (Phase 6, 7개 관계)
python scripts/check_predictions.py

# 주식 + 암호화폐 + 금 (API 키 불필요)
python scripts/hello_world.py

# 암호화폐만 자세히 (API 키 불필요)
python scripts/check_crypto.py

# 거시 대시보드 (FRED 키 필요)
python scripts/check_macro.py

# 한국 수출입 통계 (FRED 키 사용)
python scripts/check_korea.py

# 종목 5년 재무제표 추세 (FMP 키 필요, 실패 시 yfinance fallback)
python scripts/check_fundamentals.py CPNG

# ★ 시장 조망: 자산군 + 상관관계 + 변동성 + 국면 분류
python scripts/check_market_regime.py

# ★ 종목 리스크 리포트: VaR / MDD / Monte Carlo / 시나리오
python scripts/check_risk.py CPNG
python scripts/check_risk.py NVDA --days 60

# ★ 가치주 스크리너 + HTML 대시보드
python scripts/screen_value.py            # 미국 + 한국 + 크립토
python scripts/screen_value.py --us-only  # 미국만 (빠름)
open dashboard/index.html                 # 결과 시각화 (브라우저)
```

`data_fetcher.py` 주요 API:

```python
from src.data_fetcher import (
    fetch_prices,                # 주식·ETF·선물·환율 (yfinance)
    fetch_financials_yf,         # yfinance 재무제표
    fetch_macro,                 # FRED 단일 시계열
    fetch_macro_dashboard,       # FRED 여러 지표 한 번에
    fetch_crypto,                # CoinGecko 코인 시계열
    fetch_crypto_top,            # CoinGecko 시총 상위 N
    fetch_korea_trade,           # 한국 월간 수출/수입/무역수지 (FRED 경유)
    fetch_quote,                 # FMP 실시간 시세
    fetch_profile,               # FMP 기업 프로필
    fetch_financial_statements,  # FMP 손익/재무/현금흐름 시계열
    fetch_key_metrics,           # FMP P/E, ROE, 부채비율 등
    fetch_ratios,                # FMP 광범위한 재무 비율
    fetch_wikipedia_pageviews,   # 위키피디아 일별 페이지뷰 (대체 데이터)
)
```

## 로드맵

현재 위치: **Phase 0–6 + 가치주 스크리너 + 리팩토링 8단계 완료. 다음은 Phase 7 (알림 봇).**

- [x] Phase 0 — 폴더 구조 + Hello World
- [x] Phase 1 — `data_fetcher.py` (yfinance + FRED + CoinGecko + 한국 무역통계)
- [x] Phase 2 — `macro_analyzer.py` 상관관계 + 시장 국면 분류
- [x] Phase 3 — `risk_engine.py` VaR / MDD / Monte Carlo / 시나리오
- [x] Side Quest — 가치주 스크리너 + HTML 대시보드
- [x] 리팩토링 1–8단계 — 로깅 → 예외 → HTTP → DRY → 패키지화 → 테스트 → 결정론 → API 정합성
- [x] Phase 4 — Storage & Daily Pipeline (SQLite 캐시 + `daily_update.py` 오케스트레이터)
- [x] Phase 5 — Signal Engine (팩터 점수 + 스크리닝 룰 + 변화 알림)
- [x] Phase 6 — 선행지표 예측 (7개 lead-lag 관계 + 위키피디아 대체 데이터)
- [ ] Phase 6 (선택 잔여) — Google Trends / SEC EDGAR (새 의존성·우선순위 낮음)
- [ ] Phase 7 — Telegram/Discord 알림 봇 (매일 아침 7시 KST 자동 push)
- [ ] Phase 8+ (선택) — LLM 요약 한 줄 / 뉴스 센티먼트 / 백테스트 / Streamlit

세부 내용과 단계별 Definition of Done 은 [docs/ROADMAP.md](docs/ROADMAP.md) 참고.
