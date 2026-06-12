# Current State — 2026-06-12 기준

이 문서는 프로젝트의 현재 작업 상태를 기록합니다. 새 에이전트가 이어서 작업할 때
어디서부터 시작해야 하는지 빠르게 파악할 수 있도록 작성되었습니다.

---

## 1. 폴더 구조

```
AI-Investment-Bot/
├── CLAUDE.md                  # 프로젝트 규칙 (먼저 읽기)
├── .env                       # API 키 (gitignore)
├── .env.example               # 키 템플릿
├── .gitignore
├── README.md
├── requirements.txt
│
├── docs/
│   ├── CURRENT_STATE.md       # 이 파일
│   └── ROADMAP.md             # 다음 작업
│
├── src/
│   ├── __init__.py
│   ├── config.py              # Settings dataclass + load_dotenv
│   ├── logger.py              # TZFormatter (KST+0900) + setup_logging + get_logger
│   ├── exceptions.py          # 도메인 예외 계층
│   ├── http.py                # ★ NEW — 표준 HTTP 세션 (retry/timeout/키 마스킹)
│   ├── data_fetcher.py        # 모든 외부 API 호출
│   ├── macro_analyzer.py      # cross-asset + regime classifier
│   ├── risk_engine.py         # VaR / MDD / MC / Scenario
│   ├── screener.py            # ★ NEW — 가치주 스크리너 (Side Quest)
│   └── bot_interface.py       # ⚠️ Deprecated, 4단계에서 삭제 예정
│
├── scripts/
│   ├── hello_world.py         # CPNG/NVDA/BTC/금 종가
│   ├── check_macro.py         # FRED 거시 대시보드
│   ├── check_crypto.py        # BTC/ETH 동향
│   ├── check_korea.py         # 한국 수출/수입/무역수지
│   ├── check_market_regime.py # ★ 가장 자주 쓰는 매일 아침 시장 조망
│   ├── check_fundamentals.py  # FMP 5년 재무제표 (FMP 실패시 yfinance fallback)
│   ├── check_risk.py          # 종목 종합 리스크 리포트
│   ├── diag_fmp.py            # FMP 엔드포인트 접근 진단
│   ├── demo_exceptions.py     # 예외 체계 검증 데모 (2단계 결과물)
│   ├── demo_http.py           # ★ NEW — HTTP 견고성 검증 데모 (3단계 결과물)
│   └── screen_value.py        # ★ NEW — 가치주 스크리너 실행 (Side Quest)
│
├── dashboard/                 # ★ NEW
│   ├── index.html             # 4탭 가치주 대시보드 (Haiku 작성, 그대로 활용)
│   └── screener_data.json     # 자동 생성 (gitignore)
│
├── tests/
│   └── __init__.py            # 아직 빈 디렉토리 (6단계에서 pytest 도입)
│
├── data/.gitkeep              # 캐시 (gitignore)
├── notebooks/.gitkeep
└── logs/quant_bot.log         # 자동 생성 (gitignore)
```

---

## 2. 모듈별 책임

### `src/config.py`
- `Settings` (frozen dataclass): API 키 + 경로
- `settings.require("key_name")` → 값 반환 또는 `MissingApiKeyError`
- `load_dotenv()` 가 import 시 자동 실행

### `src/logger.py`
- `TZFormatter` 클래스 — 어떤 머신에서 실행해도 KST `+0900` 으로 timestamp 통일
- `get_logger(__name__)` — 첫 호출 시 `setup_logging()` 자동 실행 (idempotent)
- 파일 핸들러: `logs/quant_bot.log` (RotatingFileHandler, 10MB×5, DEBUG+)
- 콘솔 핸들러: stderr, INFO+, timestamp 없음
- 환경변수 override: `QUANT_BOT_LOG_LEVEL`, `QUANT_BOT_LOG_TZ`, `QUANT_BOT_LOG_DIR`
- 시끄러운 외부 로거 자동 누름 (`urllib3`, `yfinance`, `fredapi`, `matplotlib` 등)

### `src/exceptions.py` — 도메인 예외 계층

```
QuantBotError
├── ConfigError
│   └── MissingApiKeyError       (key_name 속성)
├── DataFetchError                (source 속성)
│   ├── ApiHttpError              (status_code 속성)
│   │   ├── ApiAuthError          (401)
│   │   ├── ApiAuthorizationError (403)
│   │   └── RateLimitError        (429)
│   ├── ApiTimeoutError
│   ├── ApiConnectionError
│   └── DataValidationError       (응답이 비었거나 스키마 위반)
└── AnalysisError
    └── InsufficientDataError     (n_points, required 속성)
```

### `src/http.py` — 표준 HTTP 레이어 (3단계 결과물)

| 제공 | 설명 |
|---|---|
| `get_http_session()` | 프로젝트 표준 `requests.Session` 싱글톤. 429/5xx 자동 재시도 (backoff 즉시→2s→4s, 최대 3회), connect 5s / read 25s 타임아웃 강제, keep-alive 풀링 |
| `build_session(...)` | 다른 정책의 세션이 필요할 때 (테스트/데모용) |
| `mask_secrets(text)` | 알려진 API 키를 `***REDACTED***` 로 치환 |
| `SecretMaskingFilter` | 로그 메시지 + traceback 의 키 노출 차단. 모듈 import 시 루트 핸들러에 자동 장착 |
| `is_timeout(exc)` | ⚠️ Retry 개입 시 read timeout 이 `requests.ConnectionError` 로 감싸여 나옴 — 타임아웃 여부는 이 헬퍼로 판별해야 함 |

- Retry 는 `raise_on_status=False` → 재시도 소진 시 마지막 response 반환, 도메인 예외 변환은 호출부 책임
- fredapi 는 urllib 기반이라 세션 주입 불가 — FRED 는 도메인 예외 wrapping 만
- pycoingecko 는 `session`/`request_timeout` 속성 교체로 주입 (`data_fetcher._coingecko_client()`)

### `src/data_fetcher.py` — 외부 API 통합

| 함수 | 소스 | 반환 |
|---|---|---|
| `fetch_prices(ticker, period, interval)` | yfinance | OHLCV DataFrame |
| `fetch_fundamentals(ticker)` ⚠️ deprecated | yfinance .info | dict |
| `fetch_financials_yf(ticker, statement)` | yfinance | 재무제표 DataFrame |
| `fetch_macro(series_id, start)` | FRED | Series |
| `fetch_macro_dashboard(start)` | FRED | 다중 시리즈 DataFrame |
| `fetch_crypto(coin_id, days)` | CoinGecko | price/volume/mcap DataFrame |
| `fetch_korea_trade(start)` | FRED | 한국 무역 DataFrame |
| `_fmp_get(endpoint, params)` (private) | FMP | dict/list |
| `fetch_financial_statements(ticker, ...)` | FMP | 재무제표 시계열 |
| `fetch_key_metrics(ticker, ...)` | FMP | 핵심 비율 시계열 |
| `fetch_ratios(ticker, ...)` | FMP | 광범위 비율 시계열 |

상수:
- `FMP_BASE_URL = "https://financialmodelingprep.com/stable"` (v3 가 아님 — 중요)
- `FRED_SERIES` dict — 7개 핵심 거시 시리즈
- `KOREA_TRADE_SERIES` dict — 한국 무역 3개 (수출/수입/무역수지)

### `src/macro_analyzer.py` — 거시 조망

| 함수 | 용도 |
|---|---|
| `fetch_cross_asset_panel(tickers, period)` | 7자산 패널 (SPY/TLT/LQD/HYG/GLD/DBC/BTC-USD) |
| `daily_returns(prices)` | 일별 수익률 |
| `correlation_matrix(prices)` | 상관관계 매트릭스 |
| `rolling_correlation(prices, a, b, window)` | N일 이동 상관계수 |
| `annualized_volatility(prices)` | 연환산 변동성 |
| `cumulative_returns(prices)` | 누적 수익률 |
| `current_drawdown(prices)` | 현재 낙폭 (ffill 보정 적용) |
| `sharpe_ratio(prices, risk_free_rate)` | 샤프 비율 |
| `classify_regime()` | 거시 지표 3개로 시장 국면 분류 |
| `market_summary(period)` | 모든 통계 한 dict |

`classify_regime()` 은 내부적으로 3개 헬퍼(`_eval_yield_curve`, `_eval_hy_spread`, `_eval_jobless_claims`)를 호출. 각 헬퍼는 `IndicatorOutcome` 을 반환하거나 `DataFetchError` 를 던지고, 상위에서 잡아서 `RegimeReport.failures` 에 분리 기록. **에러 메시지가 사용자용 `signals` 에 섞이지 않음** (2단계 리팩토링의 핵심 개선).

### `src/risk_engine.py` — 리스크 엔진

| 함수 | 용도 |
|---|---|
| `historical_var(returns, confidence)` | 과거 분포 기반 VaR |
| `parametric_var(returns, confidence)` | 정규분포 가정 VaR |
| `expected_shortfall(returns, confidence)` | CVaR (꼬리 평균) |
| `drawdown_series(prices)` | 낙폭 시계열 |
| `max_drawdown(prices) → DrawdownInfo` | 최대 낙폭 + 기간 + 회복 |
| `monte_carlo_simulation(prices, days, n_paths, seed) → MonteCarloResult` | GBM 시뮬레이션 |
| `scenario_impact(price, rev_shock, margin_shock, multi_shock, margin)` | 충격 → 가격 환산 |
| `risk_report(ticker, period, mc_days, mc_paths, seed)` | 종합 dict |

⚠️ 내부 `_clean_returns(s)` 가 `s.min() > 0.01` heuristic 으로 가격/수익률 자동 판별 — 페니스톡에 fragile. 7단계에서 명시적 API 로 정리 예정.

### `src/bot_interface.py` ⚠️ Deprecated
LLM 챗봇 placeholder. Phase 3 종료 시점에 LLM 중심 방향 폐기됨. 4단계 리팩토링에서 삭제 예정.

---

## 3. 가장 최근 완료 작업

### 3단계 리팩토링 (HTTP 견고성) — ✅ 완료, 사인오프 받음 (2026-06-12)
- `src/http.py` 신설 — 표준 세션 (retry/backoff + 타임아웃 강제 + 키 마스킹 + 풀링)
- `data_fetcher._fmp_get` → 표준 세션 사용, 타임아웃/연결실패 메시지에 재시도 정보 포함
- `data_fetcher._coingecko_client()` 신설 — pycoingecko 에 표준 세션 + 타임아웃 주입
  (기존 pycoingecko 기본값: retry 가 502/503/504 만 커버, timeout 120s → 교체)
- `scripts/diag_fmp.py` → 표준 세션 + 출력 메시지 `mask_secrets` 적용
- `scripts/demo_http.py` 신설 — 로컬 HTTP 서버로 4케이스 검증 (외부 네트워크 불필요):
  5xx 자동 재시도 성공 / 재시도 소진 / 타임아웃 강제 / 키 마스킹 (로그 파일 검사 포함)
- 검증: demo_http 4케이스 + demo_exceptions 6케이스 + check_market_regime/check_risk/
  check_fundamentals/check_crypto 실데이터 동작 확인
- **발견된 함정 (문서화 가치)**: Retry 가 개입하면 read timeout 이 `MaxRetryError` 로
  감싸여 `requests.exceptions.ConnectionError` 로 표면화됨 (Timeout 아님).
  → `http.is_timeout()` 헬퍼로 판별. urllib3 2.x 는 첫 재시도 backoff 가 0.

### 3단계 작업 중 발견·수정된 기존 버그 (phase-gate 예외 조항 적용)
- `classify_regime()` 이 `DataFetchError` 만 catch → FRED 키 미설정 시
  `MissingApiKeyError`(ConfigError 계열) 가 뚫고 나가 리포트 전체가 크래시.
  `except (ConfigError, DataFetchError)` 로 수정. demo_exceptions CASE 4 가
  이 버그로 죽고 있었음 (커밋된 코드에서도 재현 확인 — 기존 버그).
- yfinance 가 자기 로거에 ERROR 를 직접 찍어 `_NOISY_LOGGERS` 의 WARNING 누름을
  우회 → 도메인 예외와 중복 노이즈. yfinance 로거만 CRITICAL 로 차단 (logger.py).

### Side Quest: 가치주 스크리너 + HTML 대시보드 — ✅ 완료 (2026-06-12)
사용자가 다른 세션에서 Haiku 가 만든 스크리너 코드를 가져와서, 기존 인프라(`_fmp_get`, `logger`, `exceptions`)에 클린 통합. 8단계 리팩토링 룰북을 정면으로 따름.

- `src/data_fetcher.py` — `fetch_quote`, `fetch_profile`, `fetch_crypto_top` 추가
- `src/screener.py` 신설:
  - `US_WATCHLIST` (~40종목), `KR_WATCHLIST` (~15종목) — 전종목 스캔 대신 핵심 종목만 (API 한도 보호)
  - `calculate_health_score`, `calculate_value_score`, `calculate_crypto_scores`
  - `screen_one`, `screen_watchlist`, `screen_crypto`
- `scripts/screen_value.py` 신설 — `--us-only`, `--skip-kr`, `--crypto-top N` CLI 옵션
- `dashboard/index.html` — Haiku 의 707줄 HTML 그대로 재사용. `fetch('screener_data.json')` 으로 같은 폴더 JSON 읽음.
- Haiku 원본의 치명적 문제 모두 수정:
  - 🔴 v3 → /stable/ 엔드포인트 (`_fmp_get` 재사용)
  - 🔴 25,000 콜 폭주 → 핵심 종목 ~100개로 축소
  - 🔴 절대 경로 하드코딩 제거
  - 🟡 print → logger
  - 🟡 broad except → 도메인 예외 catch
- Google Sheets 의존성(gspread, oauth2client) 완전 제거 — MVP 에 불필요

### 2단계 리팩토링 (예외 체계화) — ✅ 완료, 사용자 사인오프 받음
- `src/exceptions.py` 신설 (10개 클래스 계층)
- `config.Settings.require()` 의 `RuntimeError` → `MissingApiKeyError`
- `data_fetcher.py` 의 모든 외부 호출이 도메인 예외 발생
- `_fmp_get` HTTP 401/403/429 → 각각 `ApiAuthError`/`ApiAuthorizationError`/`RateLimitError`
- `macro_analyzer.classify_regime()` 분리 리팩토링:
  - 3개 헬퍼 + `IndicatorOutcome` dataclass
  - `RegimeReport.signals` (성공) ≠ `RegimeReport.failures` (실패) 완전 분리
- 스크립트들의 broad `except Exception` 을 layered 패턴(specific → broad fallback)으로 전환
- 검증: `scripts/demo_exceptions.py` 6 케이스 모두 통과

### 추가 수정: 로깅 타임존 통일 (2단계 시작 직전)
- 모든 timestamp 가 `2026-05-11 05:43:13+0900` 형식으로 KST + 오프셋 명시
- `TZFormatter` 클래스 추가, 환경변수 `QUANT_BOT_LOG_TZ` 로 zone override 가능

### 1단계 리팩토링 (로깅 통합) — ✅ 완료, 사인오프 받음
- `src/logger.py` 신설
- 모든 진단 `print` → `logger.warning/error/info/exception` 으로 전환
- 사용자 대시보드 출력은 `print()` 유지 (의도된 정책)

### 검증 중 발견된 버그 fix
- `macro_analyzer.current_drawdown()` NaN 버그 — 거래 캘린더가 다른 자산(BTC 24/7 vs 주식 평일)이 패널에 섞이면 마지막 행에 NaN 침투. `ffill()` 한 줄로 해결.

---

## 4. 알려진 잔존 이슈 (각 단계에서 처리 예정)

| 이슈 | 영향 | 처리 단계 |
|---|---|---|
| ~~HTTP 재시도/백오프 없음~~ | ✅ 3단계에서 해결 (src/http.py) | 완료 |
| ~~타임아웃 일관성 부재~~ | ✅ 3단계에서 해결 — 단 fredapi(urllib 기반)와 yfinance 내부 호출은 세션 주입 불가, 타임아웃 미적용 잔존 | 부분 완료 |
| ~~API 키가 URL 쿼리스트링에 노출~~ | ✅ 3단계에서 해결 — 로그/traceback 은 마스킹됨. 단 logging 을 안 거친 stderr 직행 traceback (unhandled crash) 은 여전히 노출 가능 | 부분 완료 |
| ~~yfinance 자체 로거 ERROR 직접 출력~~ | ✅ 3단계에서 해결 (CRITICAL 차단) | 완료 |
| `Adj Close`/`Close` fallback 코드 3곳 중복 | 형식 변경 시 3곳 동시 수정 필요 | 4단계 |
| FMP 후처리 코드 3곳 중복 | 유지보수 부담 | 4단계 |
| `pick()` 헬퍼 inline 정의 | DRY 위반 | 4단계 |
| `bot_interface.py` deprecated 모듈 | 데드 코드 | 4단계 |
| `fetch_fundamentals` 부분적 데드 코드 | 4단계 |
| `sys.path.insert` 보일러플레이트 7곳 중복 | 패키지 구조 부재 신호 | 5단계 |
| 테스트 0건 | 회귀 검출 불가능 | 6단계 |
| `_clean_returns` heuristic fragile | 페니스톡 오작동 가능 | 7단계 |
| `np.random.seed` 글로벌 오염 (Monte Carlo) | 다른 random 연산 영향 | 7단계 |
| 매직 넘버 분산 (regime 임계값, 252 등) | 조정·테스트 어려움 | 7단계 |
| 빈 데이터 반환 컨벤션 불일치 | 호출자 방어 코드 3가지 | 8단계 |
| 반환 타입 불투명 (dict[str, Any] 남발) | IDE 자동완성 부재 | 8단계 |

---

## 5. 환경 셋업 (새 머신/새 에이전트가 시작 시)

```bash
cd /Users/leom/Developer/AI-Investment_Bot/AI-Investment-Bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 에 FRED_API_KEY, FMP_API_KEY 채워넣기

# 동작 확인 (위에서 아래 순서로)
python scripts/hello_world.py         # 가장 기본
python scripts/check_market_regime.py # 메인 대시보드
python scripts/check_risk.py CPNG     # 종목 리스크
python scripts/demo_exceptions.py     # 2단계 검증

# 가치주 스크리너 (Side Quest)
python scripts/screen_value.py --us-only
open dashboard/index.html
```

---

## 6. Git 상태

- Default branch: `main`
- Remote: `origin = https://github.com/Zonnx999/AI-Investment-Bot.git`
- 2026-06-12: 리팩토링 1·2단계 + 가치주 스크리너 + 핸드오버 문서를 로컬에 커밋 (Claude Code 마이그레이션 정리). 같은 커밋에서 `.clinerules` 를 삭제하고 `CLAUDE.md` 를 일반 파일로 전환 (이전에는 `.clinerules` 를 가리키는 심볼릭 링크였음)
- push 여부는 `git log origin/main..main` 으로 확인할 것
