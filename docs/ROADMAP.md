# 다음 작업 로드맵

마이그레이션 직후 바로 착수할 작업과 그 이후의 전체 그림.

---

## 0. 사용자 상태 변경 (2026-06-12)

리팩토링 1-2단계 완료 후 사용자가 **방향 전환**:
- 다른 세션에서 Haiku 가 만든 가치주 스크리너 코드(`stock_screener_project.zip`)를 가져와 기존 코드베이스에 클린 통합 → ✅ 완료
- Strict phase-gate 룰을 **잠시 풀고** 실제 동작하는 product 를 먼저 만들어보는 모드로 전환 가능성 있음
- 클로드 코드(Claude Code) 환경으로 마이그레이션 준비 중 — 새 에이전트가 이 문서들을 읽고 이어서 작업

## 1. 즉시 착수 후보 — 사용자에게 우선순위 확인 필요

### A. 스크리너 결과 보고 추가 종목/지표 튜닝 (가벼움)
- `US_WATCHLIST`, `KR_WATCHLIST` 조정
- 점수 공식 미세조정 (현재 Haiku 공식 그대로)
- 새 필드 추가 (`forward_pe`, `peg_ratio` 등)

### B. 리팩토링 — ✅ 8단계 전체 완료 (2026-06-13)
다음 작업은 **Phase 4 (Storage & Daily Pipeline)** 입니다 (사용자 승인 후 착수).

### 목표
`src/http.py` 를 신설하고 `requests.Session` + retry adapter + 타임아웃 일원화. 일일 배치 운영에서 transient 네트워크 에러로 인한 무작위 실패를 근절합니다.

### Definition of Done (3단계)
- [x] `src/http.py` 작성 완료, 타입힌트 + 도큐스트링 포함
- [x] **단일 HTTP 세션** — `get_http_session()` 가 프로젝트 표준 세션 반환
- [x] **Retry/backoff 자동** — 429/5xx 에 exponential backoff (즉시 → 2s → 4s, 최대 3회 — urllib3 2.x 는 첫 재시도 backoff 0). `urllib3.util.retry.Retry` 활용.
- [x] **타임아웃 강제** — connect 5s / read 25s 를 어댑터가 강제. (예외: fredapi 는 urllib 기반이라 미적용)
- [x] **API 키 마스킹** — `SecretMaskingFilter` 가 로그 메시지 + traceback 마스킹. `_fmp_get` 키 leak 해결.
- [x] **연결 풀링** — 단일 세션 keep-alive.
- [x] 마이그레이션 완료:
  - `data_fetcher._fmp_get()` → 표준 세션
  - `fetch_crypto`/`fetch_crypto_top` → `_coingecko_client()` 가 pycoingecko 에 세션 주입 (3.2.0 의 public `session` 속성 확인됨)
  - `scripts/diag_fmp.py` → 표준 세션 + mask_secrets
  - `fetch_macro` (fredapi) → urllib 기반 확인, wrapping 만 유지
- [x] 데모 입증: `scripts/demo_http.py` — 5xx 자동 재시도 / 재시도 소진 / 타임아웃 강제 / 키 마스킹 (4케이스)
- [x] 기존 스크립트 정상 동작 (`check_market_regime.py`, `check_risk.py`, `check_fundamentals.py`, `check_crypto.py`, `demo_exceptions.py`)
- [x] **사용자 사인오프 받음** (2026-06-12 — 사용자가 demo_http 4케이스 직접 실행·확인)

### 시작 시 권장 절차
1. 사용자에게 작업 시작 알림
2. `src/data_fetcher.py` 의 HTTP 호출 지점 모두 파악 (Read)
3. `src/exceptions.py` 의 `ApiTimeoutError`, `ApiConnectionError`, `RateLimitError` 이미 정의되어 있음 — 활용
4. `src/http.py` 초안 작성 후 사용자에게 검토 요청
5. 점진적 마이그레이션 (한 번에 한 호출 지점씩) + 매번 데모로 검증
6. 사인오프 받기 전에 다음 단계로 넘어가지 말 것

---

## 2. 리팩토링 8단계 전체 그림

원래 코드 오딧 리포트의 8단계:

| 단계 | 작업 | 상태 |
|---|---|---|
| 1 | 로깅 통합 (logger.py + print→logging) | ✅ 완료, 사인오프 |
| 2 | 예외 체계화 (exceptions.py + specific catches) | ✅ 완료, 사인오프 |
| 3 | HTTP 견고성 (http.py + Session/retry/timeout/masking) | ✅ 완료, 사인오프 |
| 4 | DRY 정리 (utils.py 신설, 데드 코드 삭제, 중복 제거) | ✅ 완료, 사인오프 |
| 5 | 패키지화 (pyproject.toml + `pip install -e .`, sys.path hack 제거) | ✅ 완료 (2026-06-13) |
| 6 | 테스트 인프라 (pytest + 픽스처 + 오프라인 테스트 40개) | ✅ 완료 (2026-06-13) |
| 7 | 결정론 & 검증 (`_clean_returns` 명시화, MC RNG 격리, 입력 검증, 매직 넘버 → 상수) | ✅ 완료 (2026-06-13) |
| 8 | API 정합성 (빈 데이터 컨벤션 통일, TypedDict 반환 타입) | ✅ 완료 (2026-06-13) |

**리팩토링 8단계 전체 완료 (2026-06-13).** 5–8단계는 사용자 승인 하에 스피드 모드로
일괄 진행 — 최종 사인오프 후 **Phase 4 (Storage & Daily Pipeline)** 진입.

---

## 3. 본격 개발 로드맵 (리팩토링 완료 후)

### Phase 4 — Storage & Daily Pipeline — ✅ 완료, 사인오프 받음 (2026-06-13)
- ✅ `src/storage.py`: SQLite 캐시 레이어 (TTL, best-effort, @cached 데코레이터)
- ✅ `scripts/daily_update.py`: 매일 한 번 모든 데이터 수집 오케스트레이터
- ✅ 같은 데이터 두 번 안 부름 — 콜드 12s → 웜 2s 측정
- ✅ FMP 한도 보호: 재무제표 TTL 7일, 시세 30분
- 사인오프 후 다음: **Phase 5 (Signal Engine)**

### Phase 5 — Signal Engine ⭐ (친구 C 봇의 진화 버전) — ✅ 완료, 사인오프 받음 (2026-06-13)
- ✅ **스크리닝 룰**: ROE>10% AND FCF yield 양수 AND P/E ≤ 워치리스트 중간값 (`apply_screen_rules`)
- ✅ **알림 룰**: 시장 국면 전환, 자산 낙폭 -10% 돌파, 변동성 ×1.25 급등 (상태 비교 기반)
- ✅ **팩터 신호**: momentum / value / quality + 종합 (`factor_scores`)
- ✅ `src/signals.py` + `scripts/check_signals.py` + daily_update 통합
- 사인오프 후 다음: **Phase 6 (Alternative Data & Predictive Models)**

### Phase 6 — Alternative Data & Predictive Models ⭐⭐ — ✅ 완료, 사인오프 받음 (2026-06-13)
사용자가 가장 중요하게 강조한 부분. 다양한 무료 대체 데이터로 가격·매출을 선행 예측.
**이미 가진 데이터로 가능한 핵심 2개를 먼저 견고하게 구현** (`src/predictors.py`):
- ✅ **M2 → BTC** 회귀 (FRED `M2SL`) — 현재 R²=0.06 (약함, 정직하게 플래그)
- ✅ **한국 수출 → 반도체(SOXX)** 선행 회귀 — R²=0.34, 10개월 선행
- ✅ lead-lag 엔진: YoY 변환 → lag 1~12 탐색 → OLS → 신뢰도(R²) 판정. 순수 함수 + 테스트
- ✅ `scripts/check_predictions.py` + daily_update 통합

**보강 완료 (2026-06-13, 사용자 선택)**:
- ✅ **FRED/yfinance 관계 4개 추가**: 건축허가→XHB, 소비자심리→XLY, 달러→EEM, 구리/금→SPY
- ✅ **위키피디아 페이지뷰** fetcher + Bitcoin 관심→BTC 예측 (Wikimedia REST, 키 불필요)
- 총 7개 관계. ⚠️ 발견: R² 높은 것들이 대부분 1개월 선행=동행지표. 진짜 선행은 한국수출(10mo).

**미구현 — 새 의존성 필요 (다음 결정 지점)**:
- ⏳ **Google Trends** (`pytrends`, rate-limit 불안정)
- ⏳ **SEC EDGAR 13F** (무료지만 파싱 부담 + 45일 지연으로 '선행' 의미 약함)
- ❌ 신용카드 결제 데이터 — 월 수천 달러, 비현실적

### Phase 7 — Telegram 알림 봇 — ✅ 구현 완료 (2026-06-13), GitHub Secrets 등록 대기
- ✅ `src/notifier.py` (표준 HTTP POST, 의존성 0) + `src/digest.py` (메시지 조립)
- ✅ `scripts/telegram_setup.py` (chat_id) + `scripts/send_digest.py` (cron 진입점)
- ✅ 디스코드 제외 (사용자 요청, 텔레그램 단일). 봇 연결 + 실제 전송 검증 완료
- ✅ `.github/workflows/daily-digest.yml` — 한국(08:30 KST)·미국(09:00 ET) 장 30분 전 2회.
     UTC cron 3개 + 뉴욕 시각 게이트로 DST 자동 대응
- ⏳ 사용자 작업: GitHub 저장소 Secrets 에 키 4개 등록 (FRED/FMP/TELEGRAM_*)
- ✅ 다이제스트 팩터 표 = 매일 스크리너 발굴 상위 N개 (고정 종목 아님)
- ✅ Node 24 런타임 opt-in (액션 deprecation 경고 제거)
- ⚠️ 클라우드 러너는 ephemeral → 변화 알림 상태는 actions/cache 로 best-effort 유지

### Phase 8 — 전 종목 유니버스 DB + 오프라인 전수 스크리닝 — ✅ 완료 (2026-06-13)
- ✅ `company-screener`(시총 필터) 발굴 → `key-metrics` 보강 → 오프라인 전수 스캔
- ✅ `src/universe.py` + `scripts/build_universe.py` + `scripts/scan.py` (`--check` 종목 조회)
- ✅ SQLite `screened` 테이블, 복합 PK(symbol,market). 다이제스트가 DB 스캔 상위에서 발굴
- ⚠️ 한계: FMP엔 실제 KOSPI/KOSDAQ 없음 → **Phase 9(KRX)로 해결**. DB 로컬 전용 → **Phase 10(호스팅)**

---

## 4. 다음 작업 — 우선순위 TODO (2026-06-13 사용자 방향 설정)

원래 "LLM/뉴스/백테스트/Streamlit"(아래 부록) 위에, 사용자가 새 방향을 추가했습니다.
**권장 순서: 10(호스팅) ✅ → 9(KRX) → 11a(멀티유저 브로드캐스트) → 11b(실시간 봇) → 12(대시보드).**
11a(친구 공유)는 새 인프라 없이 가능해 우선순위 높음. 11b(실시간)는 항상 켜진 호스트 결정 후.

### Phase 9 — KRX 한국 전수조사 ⭐ (FMP 한국 한계 해결)
사용자 결정: **B (DART 펀더멘털로 제대로)**. KRX 발굴(9a) + DART 점수(9b)로 분리.

#### Phase 9a — KRX 발굴 — ✅ 완료, 검증됨 (2026-06-14)
- ✅ `data_fetcher.fetch_krx_daily` / `fetch_krx_base_info` (AUTH_KEY 헤더, basDd, 도메인 예외)
- ✅ `universe._discover_kr`: 코스피+코스닥 일별매매 → 보통주(주권) 필터 → 시총≥5천억 →
  `screened`(market=KR, 가격/시총/섹터/명, enriched=0). 최근 영업일 자동 탐색(walk-back)
- ✅ 검증: 한국 중대형 **507종목** 발굴 (삼성전자 1885조 등), 우선주/ETF 제외 정상
- ✅ FMP 보강 경로는 US 전용으로 분리 (KR 6자리코드는 FMP 미지원 → DART 로)

#### Phase 9b — DART 펀더멘털 점수 (DART 키 발급 후)
- [ ] **사용자 선행 작업**: opendart.fss.or.kr 가입 → 인증키 → `.env` DART_API_KEY (자리 마련됨)
- [ ] DART corpCode.xml(zip) 다운로드 → 6자리 종목코드 ↔ 8자리 corp_code 매핑 테이블
- [ ] `fetch_dart_financials(corp_code, year)` — 재무제표(매출/순이익/자본 등)
- [ ] KR 점수: DART 순이익·자본 + KRX 시총/가격 → ROE / PER / PBR → value/health 점수
- [ ] `enrich_kr()` (DART 배치, 재개가능) → KR 행 enriched=1 → scan/digest 에 한국 등장
- [ ] 오프라인 테스트 (DART 응답 샘플 → 비율 계산 순수 함수)

### Phase 10 — 데이터 호스팅 / 이동성 해결 — ✅ 완료 (Turso, 2026-06-13)
노트북 이동이 잦아 로컬 DB 가 불편 + 클라우드 다이제스트가 풀유니버스를 못 씀.
- [ ] 옵션 검토 후 결정:
  - **A. GitHub 에 DB 커밋** — 무료·단순. 단 SQLite 바이너리 diff 비효율, 커밋 노이즈,
    100MB 제한, 동시쓰기 충돌. 유니버스 테이블만 별도 `.db` 로 분리하면 완화 가능.
  - **B. 매니지드 클라우드 DB** — Turso(libSQL, SQLite 호환·무료티어), Supabase/Neon(Postgres
    무료티어). 코드 변경 최소(Turso), 어디서든 접근, 동시성 OK. **권장.**
  - **C. 오브젝트 스토리지** — Cloudflare R2 / S3 에 `.db` 업로드·다운로드. 단순하지만 수동적.
- [ ] 선택지에 맞게 `storage.py` 백엔드 추상화 (현재 로컬 sqlite3 → 주입 가능하게)
- [ ] GitHub Actions 다이제스트가 호스팅 DB 를 읽도록 → 클라우드에서도 풀유니버스 발굴

### Phase 11 — 멀티유저 + 인터랙티브 텔레그램 봇
친구들과 공유 + 명령어 조회. **핵심 갈림길**: 브로드캐스트(공유)는 지금 인프라로 가능,
실시간 명령어 응답은 항상 켜진 listener 가 필요. 그래서 11a / 11b 로 분리.

#### Phase 11a — 멀티유저 브로드캐스트 (새 인프라 불필요, 먼저)
"내가 일일이 전달 / 친구 chat_id 를 시크릿에 추가" 문제를 해결.
- [ ] Turso `subscribers` 테이블 (chat_id, name, subscribed_at, active)
- [ ] cron(GitHub Actions) 실행마다 `getUpdates` 로 신규 `/start` 수거 → 구독 등록,
      `/stop` → 해지 (항상 켜진 서버 없이 cron 폴링만으로 구독 관리)
- [ ] 디제스트 발송을 단일 chat → **전 구독자 루프**로 (notifier.send_safe 재사용)
- [ ] 접근 제어: 공개 허용 vs allowlist/초대코드 (텔레그램 봇은 누구나 /start 가능)
- [ ] 한계: 명령어는 다음 cron 때 처리됨 (실시간 아님) → 실시간은 11b

#### Phase 11b — 실시간 인터랙티브 (항상 켜진 호스트 필요)
- [ ] 호스팅 결정: 폴링 워커 (Fly.io 무료 / Oracle Always Free / 집 라파·상시 PC).
      웹훅(서버리스)은 pandas/scipy 무거운 의존성 탓에 콜드스타트·용량 불리 → 폴링 권장
- [ ] 분리 설계: 무거운 분석은 cron 이 Turso 에 사전계산 → 봇 응답기는 **DB 읽기 위주**(경량)
- [ ] 명령어: `/stock <티커>` (점수+근거 상세), `/news <티커>`, `/scan [시장]`, `/subscribe`
- [ ] **점수 근거 상세화** — 현재 health/value 는 합산값만 → 항목별 기여도 분해해서 노출
- [ ] 뉴스: `NEWS_API_KEY` 또는 FMP 뉴스 → 종목 뉴스 (응답은 캐시 + 유저별 rate limit)
- [ ] (선택) 뉴스에 Haiku 센티먼트 분류 — LLM 의 합리적 자리
- [ ] 남용 방지: 유저별 rate limit, 외부 API 직접 호출 최소화(사전계산 DB 우선)

### Phase 12 — 대시보드 통합 (모든 정보 한 화면)
`dashboard/index.html` 을 발전시켜 봇의 모든 정보를 웹에서.
- [ ] 현재 스크리너 전용 → 국면/리스크/예측/유니버스 스캔/종목 상세 탭 추가
- [ ] 데이터 소스: `daily_update`/`scan` 결과 JSON 출력 → 정적 페이지가 fetch
- [ ] 호스팅: GitHub Pages (정적) — Phase 10 DB 와 연계
- [ ] 봇 push vs 대시보드 역할 분담 (push=요약 알림, 대시보드=심층 탐색)

---

## 5. 부록 — 원래 선택 단계 (우선순위 낮음, 위 TODO 이후)
- **LLM 요약 한 줄** — 다이제스트 맨 위 Haiku 한 문단 (월 $1 미만). Phase 11 뉴스 센티먼트와 묶을 수 있음
- **백테스트 프레임워크** — Phase 5 신호 / Phase 6 예측의 과거 성과 검증 (예측 R² 우려를 정량 해소)
- **Streamlit 웹 UI** — Phase 12 대시보드로 대체 가능하면 skip

---

## 6. 사용자의 작업 스타일 (주의사항)

- **Strict phase-gate 엄수** — 사용자가 "다음 단계로" 명시 승인하기 전까지 다음 단계 코드 미리 짜지 말 것. 사용자가 이전에 명시적으로 요청한 사항.
- **시니어 엔지니어 리뷰 톤** 유지. 의심스러운 코드는 비판적으로 짚고 대안 제시.
- **버그 발견 시 즉시 fix** 는 OK (phase-gate 예외) — Step 1 검증 중 발견된 `current_drawdown` NaN 버그가 그 예. 단, 새 기능을 추가하는 게 아니라 기존 버그를 잡는 경우에만.
- **결정 사항이 있으면 옵션 제시 후 명시적 승인 요청** (A/B/C 식).
- 사용자의 한국어 자연스러움을 유지하며 응답.
