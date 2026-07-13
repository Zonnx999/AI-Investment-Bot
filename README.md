# AI-Investment-Bot

개인용 퀀트 리서치 자동화 봇. 매일 아침 시장 국면·자산군 동향·종목 리스크·가치주 신호를 자동으로 계산해 보여주는 도구입니다.

> **챗봇이 아닙니다.** 결정론적 신호 생성 + 자동 알림 시스템입니다. LLM 은 선택적 양념 레이어 — 다이제스트 맨 위 한 줄 요약(`src/llm.py`, MiniMax/NVIDIA)만 담당하며, 실패 시 요약만 생략되고 다이제스트는 그대로 발송됩니다. 숫자·티커는 절대 LLM 이 만들지 않습니다.

**📊 라이브 대시보드: https://zonnx999.github.io/AI-Investment-Bot/**

- 프로젝트 규칙: [CLAUDE.md](CLAUDE.md) (에이전트가 가장 먼저 읽는 파일)
- 현재 작업 상태: [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md)
- 다음 작업: [docs/ROADMAP.md](docs/ROADMAP.md)
- 서버 운영 런북: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)

## 철학

- **레이 달리오 (All Weather)**: 상관관계 낮은 자산군 — 주식·장기채·회사채·하이일드·금·원자재·암호화폐 — 을 한 화면에서 동시에 보고, 분산 효과가 실제로 작동하는지 검증.
- **에드워드 소프 (리스크 관리)**: "이 종목 얼마나 떨어질 수 있나"를 통계적으로 사전 계산. VaR / CVaR / MDD / Monte Carlo / 시나리오 분석.
- **신호 자동화**: 매일 한 번 데이터 받고 분석 돌리고 결과 push. 사용자가 명령어 칠 필요 없게.
- **대체 데이터**: 가격뿐 아니라 거시지표·한국 수출·M2 같은 선행지표로 펀더멘털 예측.
- **주장 말고 검증**: 신호·예측은 백테스트(`src/backtest.py`)로 과거 성과를 확인하고 나서 믿는다.

## 현재 동작 (2026-07)

- **매일 아침 텔레그램 다이제스트** — 시장 국면 + 변화 알림 + 발굴 종목(US 는 제안 비중 포함) + 선행지표 예측 (+선택 LLM 한 줄 요약). 한국(08:30 KST)·미국(09:00 ET) 장 30분 전, 박스 systemd timer.
- **포지션 사이징** — "얼마나 살 것인가": 역변동성 가중 → 보유자산 상관 페널티 → half-Kelly 상한. 규칙은 가중 백테스트로 동일가중 대비 검증(`check_portfolio.py`).
- **상시 인터랙티브 봇** (Oracle 서버, systemd) — 소유자 승인제 멀티유저. 가입 요청은 인라인 `[✅ 승인][❌ 거절]` 버튼으로 탭 처리. `/stock` `/scan` `/news` `/help`, 소유자 `/approve` `/deny` `/announce`.
- **전 종목 유니버스 스캔** — US/KR/크립토 (US 2190 / KR 517 / CRYPTO 68), Turso(libSQL) 클라우드 DB, 오프라인 전수 스캔(API 0콜). 4팩터 + health/value 스코어카드(구성요소 분해).
- **백테스트 프레임워크** — 거래비용·무선견편향 백테스터 + 모멘텀 top-N 워크포워드 + lead-lag 예측의 아웃오브샘플 방향 적중률 검증.
- **라이브 대시보드** — GitHub Pages 5탭, Actions 자동 배포(매일 갱신).
- **서버 자동 배포** — `main` push 시 ~15분 내 박스 자동 반영 (autopull systemd timer).
- 자세한 모듈 맵·현재 상태는 [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md).

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
│   ├── ROADMAP.md         # 다음 작업 계획
│   └── DEPLOYMENT.md      # Oracle 서버 운영 런북
│
├── src/                   # 핵심 코드 (모듈 책임 분리는 CLAUDE.md §4.4)
│   ├── config.py          # 환경변수 로딩, Settings dataclass
│   ├── logger.py          # 로깅 설정 (KST timestamp)
│   ├── exceptions.py      # 도메인 예외 계층
│   ├── http.py            # 표준 HTTP 세션 (retry/timeout, API 키 마스킹)
│   ├── storage.py         # SQLite/Turso 캐시 (@cached, TTL) + state 테이블
│   ├── data_fetcher.py    # 모든 외부 API 호출 전담 (yfinance/FRED/CoinGecko/FMP/KRX/DART)
│   ├── macro_analyzer.py  # 자산 간 상관관계, 시장 국면 분류
│   ├── risk_engine.py     # VaR / MDD / Monte Carlo / 시나리오
│   ├── screener.py        # 가치주 스크리너 (health/value 스코어카드)
│   ├── signals.py         # 신호 엔진 (4팩터 점수 / 스크리닝 룰 / 변화 알림)
│   ├── universe.py        # 전 종목 유니버스 DB (discover/enrich/scan)
│   ├── predictors.py      # 선행지표 lead-lag 예측 (7개 관계 레지스트리)
│   ├── backtest.py        # 백테스트 엔진 (순수 함수 — 비용/워크포워드/lead-lag OOS)
│   ├── portfolio.py       # 포지션 사이징 (역변동성→상관 페널티→Kelly 상한) + 가중 백테스트
│   ├── findings.py        # Finding 공통 리서치 결과 dataclass + 어댑터
│   ├── digest.py          # 텔레그램 다이제스트 포매터 (순수)
│   ├── notifier.py        # Telegram API (send/edit/answerCallback, 평문 폴백)
│   ├── subscribers.py     # 멀티유저 구독 관리 (소유자 승인제)
│   ├── bot_commands.py    # 봇 명령 (/stock /scan /news, 인라인 버튼 콜백)
│   ├── llm.py             # LLM 한 줄 요약 (실패 시 생략 폴백, 킬스위치)
│   └── utils.py           # 공용 순수 헬퍼 (clip, 종가 추출 등)
│
├── scripts/               # 실행 진입점 (얇은 오케스트레이터)
│   ├── check_market_regime.py # ★ 매일 아침 시장 조망 ★
│   ├── daily_update.py        # ★ 일일 수집 오케스트레이터 (cron 진입점) ★
│   ├── check_signals.py       # ★ 일일 신호 리포트 (팩터/발굴/알림) ★
│   ├── check_predictions.py   # ★ 선행지표 예측 (M2→BTC 등) ★
│   ├── check_backtest.py      # ★ 백테스트 리포트 (신호·예측 과거 성과 검증) ★
│   ├── check_portfolio.py     # ★ 제안 비중 + All Weather 사이징 검증 ★
│   ├── send_digest.py         # ★ 텔레그램 다이제스트 발송 (--dry-run / --no-llm) ★
│   ├── bot.py                 # ★ 상시 폴링 봇 워커 (systemd quant-bot) ★
│   ├── build_universe.py      # 주 1회 유니버스 배치 (--discover/--enrich/--force)
│   ├── scan.py                # 오프라인 전수 발굴 (--check 티커)
│   ├── export_dashboard.py    # Turso → dashboard/*.json (Actions 매일)
│   ├── check_risk.py          # 종목 리스크 리포트 (VaR/MDD/몬테카를로)
│   ├── screen_value.py        # 가치주 스크리너
│   ├── check_macro.py / check_crypto.py / check_korea.py / check_fundamentals.py
│   ├── telegram_setup.py / turso_setup.py / diag_fmp.py   # 1회성 설정·진단
│   └── server_autopull.sh     # 서버 자동 배포 (systemd timer)
│
├── dashboard/                 # 정적 대시보드 (GitHub Pages, Actions 자동 배포)
│   ├── index.html             # 5탭: 한국 / 미국 / 크립토 / 시장국면 / 선행지표
│   └── *_data.json            # export_dashboard.py 가 Turso 에서 생성
│
├── data/                  # 데이터 캐시·유니버스 DB (gitignore)
├── notebooks/             # 실험용 노트북 (gitignore)
├── logs/                  # quant_bot.log (gitignore)
└── tests/                 # pytest — 오프라인 테스트 350개, ~13초 (python -m pytest)
```

## 첫 실행 (Quick Start)

```bash
# 1. 가상환경 만들기 (한 번만)
python3.12 -m venv .venv

# 2. 가상환경 켜기 (터미널 켤 때마다)
source .venv/bin/activate

# 3. 패키지 설치 (한 번만, 또는 pyproject.toml 바뀔 때 — editable install)
pip install -e ".[dev]"
pip install -e ".[dev,hosting]"   # Turso 클라우드 호스팅까지 쓰면 이쪽

# 4. 환경변수 파일 만들기
cp .env.example .env
# .env 열어서 FRED_API_KEY, FMP_API_KEY (선택: KRX/DART/TELEGRAM/TURSO/MINIMAX) 채워넣기

# 5. 테스트 + 첫 스크립트
python -m pytest                        # 오프라인 350개, ~13초
python scripts/check_market_regime.py   # 메인 대시보드
```

## 사용법

```bash
# ★ 매일 아침 1회 — 모든 데이터 수집 + 캐시 워밍 (이후 다른 스크립트는 캐시 적중)
python scripts/daily_update.py
python scripts/daily_update.py --refresh   # 캐시 무시하고 새로 수집

# ★ 시장 조망: 자산군 + 상관관계 + 변동성 + 국면 분류
python scripts/check_market_regime.py

# ★ 일일 신호 리포트 — 팩터 점수 + 발굴 종목 + 변화 알림
python scripts/check_signals.py
python scripts/check_signals.py --screen   # 미국 워치리스트 발굴 포함

# ★ 선행지표 예측 — M2/한국수출/달러/구리·금/위키관심 → 자산 (7개 관계)
python scripts/check_predictions.py

# ★ 백테스트 — 신호·예측이 과거에 실제로 통했는지 (거래비용 포함)
python scripts/check_backtest.py

# ★ 포트폴리오 — 워치리스트 제안 비중 + All Weather 사이징 검증
python scripts/check_portfolio.py

# ★ 유니버스 발굴 (주 1회 배치 + 오프라인 스캔)
python scripts/build_universe.py --discover --enrich   # API 로 수집·점수화
python scripts/scan.py                                 # 오프라인 전수 스캔 (API 0콜)
python scripts/scan.py --check NVDA                    # 한 종목 점수 근거 상세

# ★ 텔레그램 다이제스트 (서버에선 systemd timer 가 자동 발송)
python scripts/send_digest.py --dry-run                # 발송 없이 미리보기
python scripts/send_digest.py --market kr --no-llm     # LLM 요약 없이

# 종목 리스크 리포트: VaR / MDD / Monte Carlo / 시나리오
python scripts/check_risk.py CPNG
python scripts/check_risk.py NVDA --days 60

# 기타: check_macro / check_crypto / check_korea / check_fundamentals CPNG / screen_value
```

`data_fetcher.py` 주요 API (전 함수 투명 캐싱 — `QUANT_BOT_CACHE` 로 제어):

```python
from src.data_fetcher import (
    fetch_prices,                # 주식·ETF·선물·환율 (yfinance)
    fetch_macro,                 # FRED 단일 시계열
    fetch_macro_dashboard,       # FRED 여러 지표 한 번에
    fetch_korea_trade,           # 한국 월간 수출/수입/무역수지 (FRED 경유)
    fetch_crypto,                # CoinGecko 코인 시계열
    fetch_crypto_top,            # CoinGecko 시총 상위 N
    fetch_quote,                 # FMP 실시간 시세
    fetch_company_screener,      # FMP 전 종목 발굴
    fetch_key_metrics,           # FMP P/E(earningsYield 역산), ROE 등
    fetch_ratios,                # FMP 광범위한 재무 비율
    fetch_financial_statements,  # FMP 손익/재무/현금흐름 시계열
    fetch_stock_news,            # FMP 종목 뉴스 헤드라인 (/news 명령)
    fetch_krx_daily,             # KRX 한국 전 종목 일별매매
    fetch_dart_financials,       # DART 한국 재무제표 (ROE/PER/PBR)
    fetch_wikipedia_pageviews,   # 위키피디아 일별 페이지뷰 (대체 데이터)
)
```

## 로드맵

현재 위치: **Phase 0–12 완료 + 백테스트·LLM 요약·인라인 버튼·`/news` (2026-07 개선 브랜치). 다음: Phase 13 포트폴리오 레이어.**

- [x] Phase 0–3 — 구조 / data_fetcher / 거시·국면 / 리스크 엔진 (+가치주 스크리너)
- [x] 리팩토링 1–8단계 — 로깅 → 예외 → HTTP → DRY → 패키지화 → 테스트 → 결정론 → API 정합성
- [x] Phase 4–6 — Storage & 일일 파이프라인 / Signal Engine / 선행지표 예측
- [x] Phase 7 — Telegram 다이제스트 (박스 systemd timer, 한·미 장 30분 전)
- [x] Phase 8 — 전 종목 유니버스 DB + 오프라인 전수 스크리닝
- [x] Phase 9 — KRX 한국 전수조사 + DART 펀더멘털 점수
- [x] Phase 10 — 데이터 호스팅 (Turso/libSQL 임베디드 레플리카)
- [x] Phase 11 — 멀티유저(소유자 승인제) + 상시 인터랙티브 봇 (`/stock` `/scan` `/news`, 인라인 승인 버튼)
- [x] Phase 12 — 대시보드 통합 (GitHub Pages 5탭, Actions 자동 배포)
- [x] 백테스트 프레임워크 / LLM 한 줄 요약 (폴백 우선) / 코드 개선 백로그
- [x] **Phase 13 — 포트폴리오 레이어** — 포지션 사이징(역변동성·상관 페널티·Kelly 상한) + 구조화 리서치 결과(Finding) + 다이제스트 제안 비중 + All Weather 검증(`check_portfolio.py`)
- [ ] (선택) KR 배당 DART 연동 / `/regime` 즉답 / Google Trends / SEC EDGAR

세부 내용과 아키텍처 결정 기록(채택/기각 사유)은 [docs/ROADMAP.md](docs/ROADMAP.md) 참고.
