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

### Phase 5 — Signal Engine ⭐ (친구 C 봇의 진화 버전)
- **스크리닝 룰**: "P/E < 업종 중간값, FCF 양수, ROE > 10%" 같은 필터로 종목 발굴
- **알림 룰**: 리스크 점수 급변, 시장 국면 전환, 자산 낙폭 임계 초과
- **팩터 신호**: momentum, value, quality 세 가지 기본
- 이 단계가 사용자가 영감 받은 친구의 C 봇 자리

### Phase 6 — Alternative Data & Predictive Models ⭐⭐
사용자가 가장 중요하게 강조한 부분. 다양한 무료 대체 데이터로 가격·매출을 선행 예측:
- **M2 → BTC** 회귀 (FRED `M2SL` 시리즈)
- **한국 수출 → 반도체株** 선행 회귀 (이미 `fetch_korea_trade` 사용 가능)
- **Google Trends → 소비재 매출** 선행 (`pytrends` 라이브러리, 무료)
- **위키피디아 페이지뷰 → 소비자 관심** (무료)
- **SEC EDGAR 13F → 기관 보유 변동** (무료, 분기별)
- ❌ 신용카드 결제 데이터 (2nd Measure 등) 는 월 수천 달러부터 시작 — 비현실적. 위 6-7가지 무료 alt-data 조합으로 70~80% 효과를 노리는 것이 합리적.

### Phase 7 — Telegram/Discord 알림 봇
- 스케줄러 (cron 또는 GitHub Actions) 로 매일 아침 7시 KST 자동 실행
- Phase 5 신호 + Phase 6 예측을 텔레그램으로 push
- 친구분 C 봇의 정신을 잇되 데이터 풍부함과 신호 정교함을 한 단계 끌어올린 형태
- 라이브러리: `python-telegram-bot`

### Phase 8 (선택) — LLM 요약 한 줄
- Phase 7 push 메시지에 Claude Haiku 호출 한 번 추가
- "오늘 신호: X종목 저평가, Y거시 위험" 같은 자연어 한 단락
- 비용 월 $1 미만
- **LLM 의 첫 번째 합리적 자리** (챗봇이 아닌 thin layer)

### Phase 9 (선택) — 뉴스 센티먼트
- NewsAPI 로 종목별 뉴스 수집 → Haiku 분류 → 신호 가중치 반영
- **LLM 의 두 번째 합리적 자리** (단순 룰로 안 풀리는 영역)

### Phase 10 (선택) — 백테스트 프레임워크
- Phase 5 신호의 과거 성과 검증
- 신호 → 백테스트 → 검증 → 운영 순서 정착

### Phase 11 (선택) — Streamlit 웹 UI
- 봇 push 만으로 충분하면 skip 가능

---

## 4. 사용자의 작업 스타일 (주의사항)

- **Strict phase-gate 엄수** — 사용자가 "다음 단계로" 명시 승인하기 전까지 다음 단계 코드 미리 짜지 말 것. 사용자가 이전에 명시적으로 요청한 사항.
- **시니어 엔지니어 리뷰 톤** 유지. 의심스러운 코드는 비판적으로 짚고 대안 제시.
- **버그 발견 시 즉시 fix** 는 OK (phase-gate 예외) — Step 1 검증 중 발견된 `current_drawdown` NaN 버그가 그 예. 단, 새 기능을 추가하는 게 아니라 기존 버그를 잡는 경우에만.
- **결정 사항이 있으면 옵션 제시 후 명시적 승인 요청** (A/B/C 식).
- 사용자의 한국어 자연스러움을 유지하며 응답.
