# AI-Investment-Bot

개인용 퀀트 리서치 자동화 봇. 매일 아침 시장 국면·자산군 동향·종목 후보를 자동으로 계산해 텔레그램으로 보내고, **보낸 후보가 실제로 어땠는지 스스로 채점**하는 도구입니다.

> **챗봇이 아닙니다.** 결정론적 신호 생성 + 자동 알림 시스템입니다. LLM 은 선택적 양념 레이어이며 **기본 OFF** 입니다(켜면 메시지 맨 위 한 줄 요약만 담당, 실패 시 생략). 숫자·티커는 절대 LLM 이 만들지 않습니다.

**📊 라이브 대시보드: https://zonnx999.github.io/AI-Investment-Bot/** (갱신 재활성화 대기 중 — `docs/CURRENT_STATE.md`)

- 프로젝트 규칙: [CLAUDE.md](CLAUDE.md) (에이전트가 가장 먼저 읽는 파일)
- 현재 작업 상태: [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md)
- 다음 작업: [docs/ROADMAP.md](docs/ROADMAP.md)
- 서버 운영·배포 런북: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)

## 철학

- **레이 달리오 (All Weather)**: 상관관계 낮은 자산군 — 주식·장기채·회사채·하이일드·금·원자재·암호화폐 — 을 한 화면에서 동시에 보고, 분산 효과가 실제로 작동하는지 검증.
- **에드워드 소프 (리스크 관리)**: "이 종목 얼마나 떨어질 수 있나"를 통계적으로 사전 계산. VaR / CVaR / MDD / Monte Carlo / 시나리오 분석.
- **신호 자동화**: 매일 한 번 데이터 받고 분석 돌리고 결과 push. 사용자가 명령어 칠 필요 없게.
- **주장 말고 검증**: 보낸 신호는 **신호 원장**에 기록되어 사후 성과로 채점되고, 선행지표 같은 예측은 무선견 워크포워드 검증(`scripts/check_backtest.py`)을 통과하기 전엔 메시지에 싣지 않습니다.

## 현재 동작 (2026-10 기준, 브랜치 `fix/signal-quality-round` — 검토·배포 대기)

- **하루 2회 텔레그램 다이제스트** — 한국 08:30 KST(글로벌 국면 + 한국 시장 + 한국 후보), 미국 09:00 ET(아침 이후 변화 + 미국 후보). 서버 systemd timer.
  - 첫 줄이 **"오늘 할 일"**(없음 / 매수 검토 / 탈락 / 리스크 점검) — 읽고 넘길 수 있게, **변화가 있는 날만 길어집니다**.
  - 후보는 선정에 쓴 **하나의 점수**(밸류+건전성)를 그대로 표시, 기준일·종목별 "선정 후" 수익률 포함.
  - 낙폭·변동성·신용·환율·국면 전환·시장 충격 알림은 모두 "지난번 대비 변화"만 울리고, 데이터가 묵거나 지표가 빠지면 상태 줄/소유자 DM 으로 **눈에 보이게** 알립니다.
- **신호 원장 (`src/ledger.py`)** — 발송한 후보 리스트를 기록하고, "그대로 보유했다면" 벤치(SPY / 코스피·코스닥 ETF) 대비 성과를 비용 포함으로 매일 채점해 메시지에 1줄(`📒 지난 후보 성과`, 월요일엔 2줄 블록)로 보여 줍니다. 판정은 20거래일 블록 6개(≈6개월) 뒤부터 — 그 전엔 `⏳검증 전 (4주 구간 n/6)`, 성적 미달로 판정되면 후보 제목에 `검증 미달`.
- **검증 프로토콜 (`scripts/check_backtest.py`)** — 원장 성과 · 유니버스 순위 IC · 선행지표 7개 무선견 워크포워드(베이스라인·부호검정·Holm 보정) · 운영 모멘텀 진단 · SPY 200일선. 현재 선행지표는 통과 0/7.
- **전 종목 유니버스 스캔** — US/KR/크립토 (US 2190 / KR 517 / CRYPTO 68), Turso(libSQL) 클라우드 DB, 오프라인 전수 스캔(API 0콜). 4팩터 + health/value 스코어카드(구성요소 분해).
  US 보통주 필터(상장 후순위채·우선주 제외), FMP TTM 기준 점수, 금융업 50% 중립, KR 지배주주 귀속(DART), 섹터/지주사 상한, 재보강 누락(1주 이상 뒤처진) 행은 후보 제외. 주 1회 재점수 타이머.
- **상시 인터랙티브 봇** (Oracle 서버, systemd) — 소유자 승인제 멀티유저, 가입 요청은 인라인 `[✅ 승인][❌ 거절]` 버튼.
  `/stock <티커> [us|kr|crypto]` · `/scan [us|kr] [개수]` · `/ledger [us|kr]` · `/news <티커>` · `/help`, 소유자 `/approve` `/deny` `/announce`.
- **운영 안전장치** — CI(pytest) 통과 커밋만 서버 자동 배포 + 기동 실패 시 자동 롤백, 서버 `.venv` ↔ `requirements.lock` 불일치 감시(소유자 DM), 헬스 리포트 → 소유자 DM, 실패 유닛 `OnFailure` 알림, 재점수 직후 다이제스트 레플리카 미리 pull, systemd 유닛 원본을 저장소(`deploy/systemd/`)에서 관리.
- **라이브 대시보드** — GitHub Pages 5탭(한국 / 미국 / 크립토 / 시장국면 / 선행지표). ⚠️ Dashboard Export 워크플로가 7월 이후 중단 상태(GitHub 자동 비활성화) — 수리분은 이 브랜치에 포함, 배포 때 재활성화.
- 선행지표 예측·제안 비중(Kelly 사이징)·LLM 요약은 코드를 보존하되 **기본 제거**(검증 전) — 환경변수 플래그로만 켭니다.
- 자세한 모듈 맵·현재 상태·열린 이슈는 [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md).

## 폴더 구조

```
AI-Investment-Bot/
├── CLAUDE.md              # 프로젝트 규칙 (먼저 읽기)
├── README.md              # 이 파일
├── .env.example           # API 키 템플릿 (실제 .env 는 절대 커밋 안 함)
├── pyproject.toml         # 패키지/의존성 정의 (pip install -e ".[dev]")
├── requirements.lock      # 고정 버전 세트 (CI·Actions·서버가 같은 버전으로 테스트/실행)
│
├── docs/
│   ├── CURRENT_STATE.md   # 현재 작업 상태 (핸드오버용)
│   ├── ROADMAP.md         # 다음 작업 계획
│   └── DEPLOYMENT.md      # Oracle 서버 운영 런북 + 배포 순서
│
├── src/                   # 핵심 코드 (모듈 책임 분리는 CLAUDE.md §4.4)
│   ├── config.py          # 환경변수 로딩, Settings dataclass
│   ├── logger.py          # 로깅 설정 (KST timestamp)
│   ├── exceptions.py      # 도메인 예외 계층
│   ├── http.py            # 표준 HTTP 세션 (retry/timeout, API 키 마스킹)
│   ├── storage.py         # SQLite/Turso 캐시 (@cached, TTL) + state 테이블 + 연결 상태
│   ├── data_fetcher.py    # 모든 외부 API 호출 전담 (yfinance/FRED/CoinGecko/FMP/KRX/DART)
│   ├── macro_analyzer.py  # 자산 간 상관관계, 시장 국면 분류
│   ├── risk_engine.py     # VaR / MDD / Monte Carlo / 시나리오
│   ├── screener.py        # 스코어카드 (health/value, TTM·금융 중립)
│   ├── signals.py         # 신호 엔진 (4팩터 / 스크리닝 룰 / 변화 알림, 상태 커밋은 발송 후)
│   ├── universe.py        # 전 종목 유니버스 DB (discover/enrich/scan/pick_top)
│   ├── ledger.py          # 신호 원장 (발송 기록·채점·스냅샷·성과 줄)
│   ├── backtest.py        # 검증 프로토콜 (비용·워크포워드·무선견 선행지표·부트스트랩)
│   ├── predictors.py      # 선행지표 lead-lag 예측 (7개 관계 레지스트리, 검증 전)
│   ├── portfolio.py       # 포지션 사이징 (역변동성→상관 페널티→Kelly 상한, 기본 OFF)
│   ├── findings.py        # Finding dataclass + 사용자 표시 라벨 상수 (단일 출처)
│   ├── digest.py          # 일일 다이제스트 v2 (판정·후보·성과·상태)
│   ├── health.py          # 헬스 리포트 (상태 줄·소유자 DM·보류 판정)
│   ├── notifier.py        # Telegram API (send/edit/answerCallback, 평문 폴백)
│   ├── subscribers.py     # 멀티유저 구독 관리 (소유자 승인제)
│   ├── bot_commands.py    # 봇 명령 (/stock /scan /ledger /news, 인라인 버튼 콜백)
│   ├── llm.py             # LLM 한 줄 요약 (기본 OFF, 퇴역 모델 분류)
│   └── utils.py           # 공용 순수 헬퍼 (clip, 종가 추출, md_safe 등)
│
├── scripts/               # 실행 진입점 (얇은 오케스트레이터)
│   ├── send_digest.py         # ★ 텔레그램 다이제스트 발송 (--dry-run = 상태·원장 쓰기 0) ★
│   ├── bot.py                 # ★ 상시 폴링 봇 워커 (systemd quant-bot) ★
│   ├── ledger.py              # ★ 신호 원장 CLI (snapshot/backfill/score/report/ic) ★
│   ├── check_backtest.py      # ★ 검증 리포트 (원장·IC·선행지표·모멘텀·200일선) ★
│   ├── build_universe.py      # 유니버스 배치 (--discover/--enrich/--force/--reclassify/--kr-refresh)
│   ├── scan.py                # 오프라인 전수 발굴 (--check 티커)
│   ├── export_dashboard.py    # Turso → dashboard/*.json (Actions 평일)
│   ├── notify_owner.py        # 소유자 평문 DM (systemd OnFailure / autopull)
│   ├── warm_replica.py        # 레플리카 미리 pull (재점수 유닛 ExecStartPost)
│   ├── lock_drift.py          # 서버 .venv ↔ requirements.lock 불일치 점검 (autopull 이 호출)
│   ├── server_autopull.sh     # 서버 자동 배포 (CI 게이트 + 롤백, systemd timer)
│   ├── check_market_regime.py / check_signals.py / check_predictions.py / check_portfolio.py   # 수동 점검 (상태 쓰기 없음)
│   ├── check_risk.py / check_macro.py / check_crypto.py / check_korea.py / check_fundamentals.py / screen_value.py
│   ├── daily_update.py        # 수집 오케스트레이터 (캐시 워밍)
│   └── telegram_setup.py / turso_setup.py / diag_fmp.py   # 1회성 설정·진단
│
├── deploy/systemd/            # 서버 systemd 유닛·타이머 원본 (봇·다이제스트·재점수·알림·autopull)
├── .github/workflows/         # ci.yml (pytest 게이트), dashboard-export.yml, daily-digest.yml (수동)
├── dashboard/                 # 정적 대시보드 (GitHub Pages)
│   ├── index.html             # 5탭: 한국 / 미국 / 크립토 / 시장국면 / 선행지표
│   └── *_data.json            # export_dashboard.py 가 Turso 에서 생성
│
├── data/                  # 데이터 캐시·유니버스 DB (gitignore)
├── notebooks/             # 실험용 노트북 (gitignore)
├── logs/                  # quant_bot.log (gitignore)
└── tests/                 # pytest — 오프라인 테스트 1,132개, 약 40초 (python -m pytest)
```

## 첫 실행 (Quick Start)

가상환경 만들기 (한 번만):
```bash
uv venv --python 3.12 .venv
```
패키지 설치 (한 번만, 또는 의존성이 바뀔 때):
```bash
uv pip install --python .venv/bin/python -e ".[dev,hosting]"
```
환경변수 파일 만들기 — 복사한 `.env` 를 열어 `FRED_API_KEY`, `FMP_API_KEY` (선택: KRX/DART/TELEGRAM/TURSO) 를 채웁니다:
```bash
cp .env.example .env
```
테스트(오프라인 1,132개, 약 40초)와 첫 실행(발송 없는 미리보기):
```bash
.venv/bin/python -m pytest
.venv/bin/python scripts/send_digest.py --market kr --dry-run
```

`uv` 가 없으면 `python3.12 -m venv .venv` → `source .venv/bin/activate` → `pip install -e ".[dev,hosting]"` 도 됩니다.
CI·서버와 같은 고정 버전으로 맞추려면 `pip install -r requirements.lock` 후 `pip install --no-deps -e .`.
⚠️ 운영 `.env`(Turso 키 포함)로 실행하면 운영 DB 에 붙습니다 — 실험할 땐 `TURSO_DATABASE_URL= TURSO_AUTH_TOKEN= TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= QUANT_BOT_DB_PATH=/tmp/x.db` 를 명령 앞에 붙이세요.

## 사용법

| 명령 | 하는 일 |
|---|---|
| `python scripts/send_digest.py --market kr --dry-run` | ★ 다이제스트 미리보기 (`--market us` 도 동일). 상태·원장 쓰기 0. 서버에선 systemd timer 가 자동 발송 |
| `python scripts/ledger.py report us` | ★ 신호 원장 평문 성적표 (`kr` 도 가능) |
| `python scripts/ledger.py score` | 채점 (서버에선 다이제스트 직후 자동) |
| `python scripts/ledger.py backfill` | 07-13 소급 미리보기 (`--confirm` 을 붙여야 기록) |
| `python scripts/check_backtest.py` | ★ 검증 리포트 — 신호·예측이 입증 가능한 수준으로 유효했는지 (거래비용 포함). `-v` 로 선행지표 상세 |
| `python scripts/build_universe.py` | 유니버스 발굴 + 전체 보강 (서버에선 토요일 타이머). `--reclassify` 는 분류 규칙만 오프라인 재적용 |
| `python scripts/scan.py` | 오프라인 전수 스캔 (API 0콜). `--check NVDA` 는 한 종목 점수 근거 상세 |
| `python scripts/check_market_regime.py` | 수동 점검: 자산군 + 상관관계 + 국면 (상태를 쓰지 않음) |
| `python scripts/check_signals.py` | 수동 점검: 팩터 점수 + 알림 후보 |
| `python scripts/check_predictions.py` | 선행지표 예측 (참고용 — 검증 전) |
| `python scripts/check_portfolio.py` | 제안 비중 + All Weather 사이징 검증 |
| `python scripts/check_risk.py CPNG` | 종목 리스크: VaR / MDD / Monte Carlo |

기타: `check_macro` · `check_crypto` · `check_korea` · `check_fundamentals CPNG` · `screen_value`.

`data_fetcher.py` 주요 API (전 함수 투명 캐싱 — `QUANT_BOT_CACHE` 로 제어):

```python
from src.data_fetcher import (
    fetch_prices,                # 주식·ETF·선물·환율 (yfinance)
    fetch_close_panel,           # 여러 티커 종가 패널 (원장 채점용, 캐시 없음)
    fetch_macro,                 # FRED 단일 시계열 (REST)
    fetch_macro_dashboard,       # FRED 여러 지표 한 번에
    fetch_korea_trade,           # 한국 월간 수출/수입 (FRED 경유)
    fetch_crypto,                # CoinGecko 코인 시계열
    fetch_crypto_top,            # CoinGecko 시총 상위 N
    fetch_quote,                 # FMP 실시간 시세
    fetch_company_screener,      # FMP 전 종목 발굴
    fetch_key_metrics_ttm,       # FMP TTM 지표 (점수의 기준)
    fetch_ratios_ttm,            # FMP TTM 비율
    fetch_financial_statements,  # FMP 손익/재무/현금흐름 시계열
    fetch_stock_news,            # FMP 종목 뉴스 헤드라인 (/news 명령)
    fetch_krx_daily,             # KRX 한국 전 종목 일별매매
    fetch_dart_financials,       # DART 한국 재무제표 (지배주주 귀속)
    fetch_wikipedia_pageviews,   # 위키피디아 일별 페이지뷰 (대체 데이터)
)
```

## 로드맵

현재 위치: **Phase 0–13 완료 + 신호 품질·검증 라운드 구현 완료(검토·배포 대기).** 다음은 코드가 아니라 **운영 관찰** — 원장이 6개월치 블록을 쌓을 때까지. 상용화(Phase A→E)는 신호 검증 전까지 **보류**.

- [x] Phase 0–3 — 구조 / data_fetcher / 거시·국면 / 리스크 엔진 (+가치주 스크리너)
- [x] 리팩토링 1–8단계 — 로깅 → 예외 → HTTP → DRY → 패키지화 → 테스트 → 결정론 → API 정합성
- [x] Phase 4–6 — Storage & 일일 파이프라인 / Signal Engine / 선행지표 예측
- [x] Phase 7 — Telegram 다이제스트 (박스 systemd timer, 한·미 장 30분 전)
- [x] Phase 8–9 — 전 종목 유니버스 DB + 오프라인 전수 스크리닝 / KRX·DART 한국 전수조사
- [x] Phase 10 — 데이터 호스팅 (Turso/libSQL 임베디드 레플리카)
- [x] Phase 11 — 멀티유저(소유자 승인제) + 상시 인터랙티브 봇
- [x] Phase 12 — 대시보드 통합 (GitHub Pages 5탭)
- [x] Phase 13 — 포트폴리오 레이어 (사이징·Finding) / 백테스트 프레임워크 / LLM 요약
- [x] **신호 품질·검증 라운드** — 점수 하나로 통일, 신호 원장, 검증 프로토콜, 데이터 정확성, 알림·국면 재설계, 헬스·CI 게이트 배포, 다이제스트 v2
- [ ] 운영 관찰 → 다음 라운드 후보(시점 일관 하네스, 캐시 로컬 분리, 금융 전용 점수표 …)
- [ ] 상용화 전환 Phase A→E — 보류

세부 내용과 아키텍처 결정 기록(채택/기각 사유)은 [docs/ROADMAP.md](docs/ROADMAP.md) 참고.
