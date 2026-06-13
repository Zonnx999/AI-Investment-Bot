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
├── pyproject.toml             # ★ 패키지/의존성 정의 (5단계) — pip install -e ".[dev]"
├── requirements.txt           # -e .[dev] 포인터 (호환용)
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
│   ├── http.py                # 표준 HTTP 세션 (retry/timeout/키 마스킹)
│   ├── storage.py             # SQLite 캐시(@cached) + state 테이블 (Phase 4/5)
│   ├── signals.py             # 신호 엔진: 팩터/스크리닝/알림 (Phase 5)
│   ├── universe.py            # ★ NEW — 전 종목 DB + 오프라인 전수 스캔 (Phase 8)
│   ├── predictors.py          # ★ NEW — lead-lag 예측 (M2→BTC, 한국수출→반도체) (Phase 6)
│   ├── data_fetcher.py        # 모든 외부 API 호출 (전 함수 투명 캐싱)
│   ├── macro_analyzer.py      # cross-asset + regime classifier
│   ├── risk_engine.py         # VaR / MDD / MC / Scenario
│   ├── screener.py            # 가치주 스크리너 (Side Quest)
│   └── utils.py               # ★ NEW — 공용 헬퍼 (4단계 결과물)
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
│   ├── daily_update.py        # 일일 수집 오케스트레이터 (Phase 4, cron 진입점)
│   ├── check_signals.py       # 일일 신호 리포트 (Phase 5)
│   ├── check_predictions.py   # ★ NEW — 선행지표 예측 리포트 (Phase 6)
│   ├── demo_exceptions.py     # 예외 체계 검증 데모 (2단계 결과물)
│   ├── demo_http.py           # HTTP 견고성 검증 데모 (3단계 결과물)
│   └── screen_value.py        # ★ NEW — 가치주 스크리너 실행 (Side Quest)
│
├── dashboard/                 # ★ NEW
│   ├── index.html             # 4탭 가치주 대시보드 (Haiku 작성, 그대로 활용)
│   └── screener_data.json     # 자동 생성 (gitignore)
│
├── tests/                     # 오프라인 테스트 85개 — python -m pytest
│   ├── conftest.py            # 픽스처: 합성 가격/OHLCV, API 키 격리
│   ├── test_utils.py
│   ├── test_exceptions.py
│   ├── test_http.py           # 마스킹/is_timeout/retry (로컬 서버)
│   ├── test_risk_engine.py
│   ├── test_macro_analyzer.py
│   ├── test_data_fetcher.py
│   ├── test_storage.py / test_storage_state.py
│   ├── test_signals.py
│   └── test_predictors.py
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
| `fetch_fundamentals(ticker)` (스냅샷용, 키 불필요 — 4단계에서 축소) | yfinance .info | dict |
| `fetch_financials_yf(ticker, statement)` | yfinance | 재무제표 DataFrame |
| `fetch_macro(series_id, start)` | FRED | Series |
| `fetch_macro_dashboard(start)` | FRED | 다중 시리즈 DataFrame |
| `fetch_crypto(coin_id, days)` | CoinGecko | price/volume/mcap DataFrame |
| `fetch_korea_trade(start)` | FRED | 한국 무역 DataFrame |
| `_fmp_get(endpoint, params)` (private) | FMP | dict/list |
| `fetch_financial_statements(ticker, ...)` | FMP | 재무제표 시계열 |
| `fetch_key_metrics(ticker, ...)` | FMP | 핵심 비율 시계열 |
| `fetch_ratios(ticker, ...)` | FMP | 광범위 비율 시계열 |
| `fetch_wikipedia_pageviews(article, days)` | Wikimedia | 일별 페이지뷰 Series (Phase 6) |

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

VaR/ES 는 수익률 시리즈만 받음 (7단계). 가격→수익률은 `returns_from_prices()` 명시 변환, 가격 오입력 시 `AnalysisError`. MC 는 격리된 `default_rng` 사용 (글로벌 오염 없음).

### `src/utils.py` — 공용 헬퍼 (4단계 결과물)
- `close_series(df)` — 'Adj Close' 우선/'Close' fallback + squeeze. 둘 다 없으면 `DataValidationError`
- `pick_first(row, candidates)` — FMP/yfinance 컬럼명 차이를 후보 리스트로 흡수

(`src/bot_interface.py` 는 4단계에서 삭제됨 — LLM 챗봇 방향 폐기에 따른 데드 코드)

---

## 3. 가장 최근 완료 작업

### ▶️ 지금 여기 — 다음 작업 (2026-06-13 사용자 방향 설정)
리팩토링 + Phase 0–8 완료. 사용자가 새 방향 4개를 추가 (상세·우선순위는 ROADMAP §4):
1. **Phase 10 호스팅** ⭐ (블로커) — 노트북 이동 + 클라우드 풀유니버스. Turso/Supabase 등 검토
2. **Phase 9 KRX** ⭐ — 한국 전 종목 전수조사 (KRX_API_KEY 등록됨, AUTH_KEY 헤더 확인.
   단 KRX 포털에서 API 개별 신청 필요 — 현재 401)
3. **Phase 11 인터랙티브 봇** — `/stock` `/news` 명령어, 점수 근거 상세, 뉴스 API
4. **Phase 12 대시보드** — `dashboard/index.html` 에 모든 정보 통합
권장 순서: 10 → 9 → 11 → 12 (호스팅이 나머지의 토대).

---

### Phase 8 — 전 종목 유니버스 DB + 오프라인 전수 스크리닝 — ✅ 완료 (2026-06-13)
고정 40종목 워치리스트 → FMP 전 종목 발굴 + DB화 + 오프라인 전수조사. ($30 FMP 활용)
- **2단계 깔때기**: `company-screener`(서버사이드 시총 필터, 1콜)로 유니버스 발굴 →
  종목별 `key-metrics`로 점수 보강(주1회 배치, ~4.5종목/초) → 이후 스캔은 **API 0콜 오프라인**
- `src/data_fetcher.fetch_company_screener` — 시총>$1B 필터 발굴 (price/섹터/배당 포함)
- `src/universe.py` — `discover`/`enrich`(재개가능)/`scan`/`lookup`/`top_symbols`.
  SQLite `screened` 테이블, **복합 PK (symbol, market)** — 주식 'M'(Macy's) vs 크립토 'M' 충돌 방지
- `scripts/build_universe.py`(주1회 배치) + `scripts/scan.py`(오프라인 발굴/`--check` 조회)
- 점수는 `screener.calculate_*` 재사용. 크립토는 CoinGecko(시총상위, FMP 쿼터 0)로 별도
- 다이제스트 발굴 종목이 유니버스 DB 전수스캔 상위에서 나옴 (DB 비면 라이브 스크리너 폴백)
- **실측**: US 발굴 2189종목 / KR 10(ADR) / 크립토 59. 보강 ~4.5종목/초 (US 전체 ~8분)
- ⚠️ **KR 한계**: FMP엔 실제 KOSPI/KOSDAQ 없음 — 미국상장 ADR 대기업 ~10개만. 진짜 한국
  전수조사는 추후 KRX/pykrx 소스 필요. 크립토 점수는 주식 점수와 비교 불가 → 스캔에서 시장별 분리
- ⚠️ **클라우드 미해결**: 유니버스 DB는 로컬 파일 → GitHub Actions(ephemeral)엔 없음.
  현재 클라우드 다이제스트는 40종목 워치리스트로 폴백. 전체 유니버스를 클라우드에 쓰려면
  DB 영속화 결정 필요 (로컬 실행 vs DB 커밋 vs 외부 저장소)
- 테스트: universe 8개 추가, 전부 오프라인(tmp DB)


### Phase 7 — Telegram 알림 봇 — ✅ 구현 완료 (2026-06-13), GitHub Secrets 등록 대기
디스코드는 사용자 요청으로 제외 (텔레그램 단일 채널).
- `src/notifier.py` — 표준 HTTP 세션으로 Telegram Bot API 직접 POST (python-telegram-bot
  의존성 없음). `send_telegram`(Markdown, 4096 클램프), `get_updates`(chat_id 발견),
  `send_safe`(best-effort — 알림 실패가 배치를 안 죽임). 봇 토큰은 마스킹 대상 등록
- `src/digest.py` — `format_digest` 순수 포매터 (국면+알림+팩터+예측 → 1 메시지).
  팩터 표는 **매일 스크리너 발굴 상위 N개**(`select_screened_tickers`)를 자동 사용 →
  고정 종목이 아니라 그날 저평가 상위 종목이 올라옴 (스크리너 실패 시 기본 종목 폴백)
- `scripts/telegram_setup.py` (chat_id 발견 + --test), `scripts/send_digest.py` (--dry-run)
- config: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. **사용자 연결 완료 + 실제 전송 검증됨**
- 테스트 100개 (notifier 6 + digest 8 + 동적선택 1, 전부 오프라인/로컬서버)
- ✅ `.github/workflows/daily-digest.yml`: GitHub Actions 로 한국(08:30 KST)·미국(09:00 ET)
  장 30분 전 2회. UTC cron 3개 + 뉴욕시각 게이트로 DST 대응. 수동 실행 버튼 포함
- ⏳ 사용자: GitHub Secrets 에 FRED_API_KEY/FMP_API_KEY/TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 등록


### Phase 6 — Alternative Data & Predictive Models — ✅ 완료, 사인오프 받음 (2026-06-13)
선행지표로 'N개월 뒤 방향'을 예측. 사용자 선택으로 **FRED/yfinance 관계 + 위키피디아** 보강.
- `src/predictors.py` — lead-lag 회귀 엔진:
  - `to_monthly` / `yoy_growth` (추세 제거 → 레벨 시리즈 허위 상관 방지)
  - `lagged_correlations` / `analyze_lead_lag` — lag 1~12개월 중 |상관| 최대 lag 선택 후 OLS
  - **순수 함수** 설계 (월간 시리즈 2개 → 오프라인 테스트), fetch 는 predict_* 만
- 구현된 관계 7개 (`PREDICTORS` 레지스트리):
  | 관계 | 선행 | 상관 | R² | 판정 |
  |---|---|---|---|---|
  | 위키관심→BTC | 1개월 | +0.79 | 0.63 | ✅ (단 1개월=거의 동행지표) |
  | 한국수출→SOXX | 10개월 | -0.58 | 0.34 | ✅ 진짜 선행 (평균회귀 해석) |
  | 달러→EEM | 1개월 | -0.59 | 0.34 | ✅ 부호 일치, 거의 동행 |
  | 구리/금→SPY | 1개월 | +0.58 | 0.34 | ✅ 부호 일치, 거의 동행 |
  | M2→BTC / 건축허가→XHB / 소비자심리→XLY | — | — | <0.12 | ⚠️ 약함 |
- **핵심 인사이트**: R² 높은 것들이 대부분 "1개월 선행"=동행지표. 진짜 멀리 선행하며
  유의미한 건 한국수출(10개월)뿐 — 엔진이 선행개월·부호·R²를 다 노출해 이 함정을 간파 가능
- `data_fetcher.fetch_wikipedia_pageviews` 신설 — Wikimedia REST (키 불필요, UA 필수, TTL 1일).
  `_parse_wikipedia_items` 순수 헬퍼로 분리 (오프라인 테스트)
- `scripts/check_predictions.py` + daily_update 섹션 통합
- 테스트 85개 (predictors 9개 + 위키 파서). 합성 데이터로 회귀계수·lag·예측방향 정확 복원
- ⚠️ **미구현 (새 의존성 필요 — 사용자 우선순위 대기)**: Google Trends(pytrends, fragile),
  SEC EDGAR 13F(파싱 부담·45일 지연). 사용자가 위키피디아·FRED 우선 선택.

### Phase 5 — Signal Engine — ✅ 완료, 사인오프 받음 (2026-06-13)
친구 C 봇("저평가 종목 알림")의 진화 버전. 결정론적 신호 생성 (LLM 없음).
- `src/signals.py` 신설 — 3종 신호:
  1. **팩터 점수** — momentum(가격) + value/quality(screener 점수 재사용) 각 0~100 + 종합
  2. **스크리닝 룰** — ROE>10% AND FCF yield 양수 AND P/E ≤ 워치리스트 중간값 (적자종목은 P/E 룰 면제)
  3. **알림 룰** — 국면 전환 / 자산 낙폭 -10% 돌파 / 변동성 ×1.25 급등
- **설계**: 점수·룰·알림 판정은 전부 **순수 함수** (데이터를 인자로 → 오프라인 테스트).
  fetch + 상태 관리는 `generate_signal_report()` 오케스트레이터만 담당
- **상태 비교**: `storage.put_state/get_state` (TTL 없는 영속 테이블 신설) 에 직전 실행값
  보관 → 알림은 '변화'에만 발화 (중복 알림 방지, 회복 알림 포함)
- **첫 실행 일관성**: 비교 기준 없는 첫 실행은 모든 변화 알림 생략 + 상태만 시딩
  (regime/낙폭/변동성 일관 처리 — 작업 중 잡은 버그)
- `scripts/check_signals.py` 신설 (`--screen` 옵션), `daily_update.py` 에 신호 섹션 통합
- 테스트 76개 (signals 순수함수 21개 + state 4개 추가). 실데이터 검증:
  변화없음→알림0, 상태주입→국면전환·낙폭돌파·변동성급등 3종 발화 확인

### Phase 4 — Storage & Daily Pipeline — ✅ 완료, 사인오프 받음 (2026-06-13)
- `src/storage.py` 신설 — SQLite 캐시 (`data/quant_bot.db`, gitignore):
  - `Storage` 클래스: (namespace, key) → DataFrame/Series/JSON, TTL 기반
  - **캐시는 best-effort** — 읽기/쓰기 실패는 경고 후 무시, 파이프라인 절대 안 막음
  - 빈 결과는 캐시 안 함 (8단계 빈 데이터 컨벤션 보호)
  - `@cached(namespace, ttl, kind)` 데코레이터 — 시그니처 바인딩으로 키 정규화
  - 직렬화: JSON `orient="table"` (pickle/pyarrow 회피). **datetime 은 항상 ns 로 복원**
    (pandas 3.x 에서 date_range 기본이 us 로 바뀜 — 캐시 적중/미적중 간 dtype 비결정성 차단)
- `data_fetcher` 전 fetch 함수에 투명 캐싱 적용. TTL: 재무제표 7일(FMP 한도 보호 핵심),
  가격 6h, 거시 12h, 크립토 1h, 시세 30분, 프로필 30일
- `QUANT_BOT_CACHE` env: on(기본)/off/refresh, `QUANT_BOT_DB_PATH` 로 DB 경로 변경
- `fetch_crypto` 인덱스: datetime.date 객체 → 자정 정규화 DatetimeIndex (직렬화 호환,
  소비처는 iloc 만 사용해 무영향)
- `scripts/daily_update.py` 오케스트레이터 — 국면/거시/한국무역/크립토/리스크(+옵션 스크리너)
  를 한 번에 수집, 섹션별 실패 격리, 실패 시 exit 1 (Phase 7 cron 의 진입점)
- 테스트 54개 (storage 14개 추가). 기존 테스트는 autouse fixture 로 캐시 off 격리
- **측정**: daily_update 콜드 12.0s → 웜 2.0s (네트워크 섹션 전부 0.0s)

### 리팩토링 5–8단계 일괄 완료 (2026-06-13, 스피드 모드 — 사용자 승인 하에 phase-gate 한시 해제)

**5단계 — 패키지화**
- `pyproject.toml` 신설 (의존성 + dev/viz/llm/ui optional 그룹), `pip install -e ".[dev]"`
- `requirements.txt` 는 `-e .[dev]` 포인터로 축소
- 11개 스크립트의 `sys.path.insert` 보일러플레이트 전부 제거 — 어느 cwd 에서든 실행 가능

**6단계 — 테스트 인프라**
- pytest 도입, `tests/` 에 40개 테스트 — **전부 오프라인** (합성 데이터 + 로컬 HTTP 서버), ~2.5초
- 픽스처: 합성 가격/OHLCV, API 키 격리 (`no_api_keys`, `fake_fmp_key`)
- 회귀 테스트 포함: current_drawdown NaN 버그, classify_regime 실패 격리, 키 마스킹

**7단계 — 결정론 & 검증**
- `_clean_returns` 휴리스틱 제거: VaR/ES 는 수익률만 받음. 가격→수익률은
  `returns_from_prices()` 명시 변환. 가격 오입력 시 `AnalysisError` raise (조용한 오답 방지)
- 표본 부족 시 `InsufficientDataError` (최소 20개)
- Monte Carlo RNG: 글로벌 `np.random.seed` → 격리된 `default_rng` (글로벌 오염 제거)
  ⚠️ 같은 seed 라도 구버전과 난수열이 달라 MC 분위수 값이 바뀜 (재현성 자체는 유지)
- 매직 넘버 상수화: `utils.TRADING_DAYS_PER_YEAR`, macro_analyzer 국면 임계값 8개

**8단계 — API 정합성**
- TypedDict 반환 스키마: `RiskReport`, `MarketSummary`, `ScreenedStock` (런타임은 dict — 무파손)
- 빈 데이터 컨벤션 명문화 (data_fetcher 모듈 docstring): 단일 대상 → raise /
  폴백 시계열 → 빈 DataFrame / 배치 → 부분 결과 + 경고

### 4단계 리팩토링 (DRY 정리) — ✅ 완료, 사인오프 받음 (2026-06-13)
- `src/utils.py` 신설: `close_series` (Adj Close/Close fallback 3곳 통합),
  `pick_first` (check_fundamentals 의 inline `pick` 이동)
- `data_fetcher._fmp_to_dataframe` 신설 — FMP 후처리 3곳 통합
- `src/bot_interface.py` 삭제 (LLM 챗봇 방향 폐기에 따른 데드 코드)
- `fetch_fundamentals` 를 실사용 필드 5개로 축소 — deprecated 가 아니라
  "키 불필요 스냅샷용" 으로 역할 재정의 (hello_world 가 사용, 본격 분석은 FMP)
- `data_fetcher` 모듈 docstring 현행화, `risk_report` 의 낡은 "Phase 4 LLM" 주석 수정

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
| ~~`Adj Close`/`Close` fallback 3곳 중복~~ | ✅ 4단계 — `utils.close_series` 로 통합 | 완료 |
| ~~FMP 후처리 3곳 중복~~ | ✅ 4단계 — `_fmp_to_dataframe` 으로 통합 | 완료 |
| ~~`pick()` 헬퍼 inline 정의~~ | ✅ 4단계 — `utils.pick_first` 로 이동 | 완료 |
| ~~`bot_interface.py` 데드 코드~~ | ✅ 4단계 — 삭제 | 완료 |
| ~~`fetch_fundamentals` 부분 데드 코드~~ | ✅ 4단계 — 사용 필드 5개로 축소, 스냅샷용으로 재정의 | 완료 |
| ~~`sys.path.insert` 보일러플레이트~~ | ✅ 5단계 — editable install 로 제거 | 완료 |
| ~~테스트 0건~~ | ✅ 6단계 — 오프라인 40개 | 완료 |
| ~~`_clean_returns` heuristic fragile~~ | ✅ 7단계 — 명시 API + 오입력 감지 | 완료 |
| ~~`np.random.seed` 글로벌 오염~~ | ✅ 7단계 — default_rng 격리 | 완료 |
| ~~매직 넘버 분산~~ | ✅ 7단계 — 상수화 | 완료 |
| ~~빈 데이터 컨벤션 불일치~~ | ✅ 8단계 — 3분류 컨벤션 명문화 | 완료 |
| ~~반환 타입 불투명~~ | ✅ 8단계 — TypedDict 3종 | 완료 |
| fredapi 타임아웃 강제 불가 (urllib 기반) | FRED 호출 hang 가능성 (낮음) | 추후 (Phase 4 storage 도입 시 재평가) |
| 패키지 import 이름이 `src` | 외부 배포 시 부적합 (개인용은 무방) | 추후 (필요 시 rename) |

---

## 5. 환경 셋업 (새 머신/새 에이전트가 시작 시)

```bash
cd /Users/leom/Developer/AI-Investment_Bot/AI-Investment-Bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # 5단계부터 pyproject.toml 기반 (editable)
cp .env.example .env
# .env 에 FRED_API_KEY, FMP_API_KEY 채워넣기

# 테스트 (오프라인, ~2.5초)
python -m pytest

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
