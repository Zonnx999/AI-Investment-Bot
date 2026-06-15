# AI-Investment-Bot — 프로젝트 규칙

이 문서는 새 AI 에이전트가 이 프로젝트에 합류할 때 가장 먼저 읽어야 할 파일입니다.
`docs/CURRENT_STATE.md` 와 `docs/ROADMAP.md` 도 함께 읽으세요.

---

## 1. 프로젝트 정체성

**개인용 퀀트 리서치 자동화 봇.** 한국 거주 1인 사용자(Leo)가 매일 아침 시장 상황과 종목별 신호를 받아보기 위한 도구입니다.

**중요: 이것은 챗봇이 아닙니다.** 결정론적 신호 생성 + 자동 알림 시스템입니다. 처음에는 LLM 챗봇으로 설계했지만 Phase 3 종료 시점에 사용자가 비용·결정성을 이유로 방향을 전환했습니다. **LLM 은 선택적 양념 레이어**(뉴스 센티먼트 분류, 일일 요약 한 줄)일 뿐 엔진이 아닙니다.

- GitHub: https://github.com/Zonnx999/AI-Investment-Bot
- 로컬 경로: `/Users/leom/Developer/AI-Investment_Bot/AI-Investment-Bot/`
  - 부모 폴더는 언더스코어 `AI-Investment_Bot`, repo 는 하이픈 `AI-Investment-Bot` — 헷갈리지 말 것.

---

## 2. 투자 철학 (시스템 설계의 척추)

- **레이 달리오 All Weather** — 상관관계 낮은 자산군(주식·장기채·회사채·하이일드·금·원자재·암호화폐) 동시 조망. 분산 효과가 실제로 작동하는지 시점별 검증.
- **에드워드 소프 리스크 관리** — "이 종목 얼만큼 떨어질 수 있나"를 통계적으로 답함. VaR/CVaR/MDD/Monte Carlo/시나리오 분석.
- **신호 자동화** — 매일 한 번 데이터 받고 분석 돌리고 결과 push. 사용자가 명령어 칠 필요 없음.
- **대체 데이터 통합** — 단순 가격뿐 아니라 거시지표·한국 수출·M2·뉴스 등 다양한 선행지표로 펀더멘털 예측 (Phase 6).

영감: 사용자의 친구가 C 언어로 만든 "저평가 종목 알림" 디스코드 봇 (단순했지만 효과적). 이걸 더 풍부한 데이터 위에서 진화시키는 것이 목표.

---

## 3. 기술 스택

- **Python 3.12** (3.10+ 호환)
- 가상환경: `.venv/` (프로젝트 루트)
- 패키지 관리: `pyproject.toml` + `pip install -e ".[dev]"` (5단계에서 전환 완료. `requirements.txt` 는 호환용 포인터)
- 핵심 라이브러리: `pandas`, `numpy`, `scipy`, `yfinance`, `fredapi`, `pycoingecko`, `requests`, `python-dotenv`, `anthropic`(선택), `streamlit`(선택)

### 데이터 소스

| 소스 | 라이브러리 / 엔드포인트 | 키 필요 | 용도 |
|---|---|---|---|
| Yahoo Finance | `yfinance` | ❌ | 주가/ETF/선물/환율 |
| FRED (St. Louis Fed) | `fredapi` | ✅ FRED_API_KEY | 거시 지표 |
| CoinGecko | `pycoingecko` | ❌ | 암호화폐 |
| FMP | `https://financialmodelingprep.com/stable/...?symbol=...` | ✅ FMP_API_KEY (유료) | 재무제표 시계열, 전 종목 발굴(company-screener) |
| 한국 무역 | FRED (OECD 출처) | ✅ FRED_API_KEY | 한국 수출·수입·무역수지 |
| KRX (한국거래소) | `data-dbg.krx.co.kr/svc/apis/sto/*` (AUTH_KEY 헤더) | ✅ KRX_API_KEY | 한국 전 종목 일별매매·기본정보 (Phase 9a, API별 신청 필요) |
| DART (전자공시) | `opendart.fss.or.kr/api/*` (crtfc_key 쿼리) | ✅ DART_API_KEY | 한국 재무제표 → ROE/PER/PBR (Phase 9b). corpCode 로 6자리↔8자리 매핑 |

**FMP 중요 사실 (2026-05 기준):** 2025-08-31 이후 가입자는 `/stable/` 엔드포인트만 사용 가능. `/api/v3/` 는 레거시. URL 패턴은 `?symbol=AAPL&apikey=...` (티커가 쿼리 파라미터). `/stable/key-metrics` 는 P/E 를 직접 안 주고 `earningsYield` 형태로 줌 → 역수 취해 계산.

API 키는 모두 프로젝트 루트의 `.env` 파일에 저장 — gitignore 됨.

---

## 4. 작업 원칙 (반드시 준수)

### 4.1 Strict Phase-Gate
사용자가 **"테스트 완료. 다음 단계로 넘어가자"** 라고 명시 승인하기 전까지 다음 페이즈의 기능을 미리 구현하거나 제안하지 않습니다. 현재 페이즈의 완벽성에만 집중. 사용자는 속도보다 완벽함을 우선시하며, 시니어 엔지니어 리뷰 스타일을 원합니다.

### 4.2 Definition of Done
모든 코드는 다음을 갖춰야 '완료'된 것:
- **실전 예외 처리** — broad `except Exception` 금지. 단 layered 패턴의 최후 fallback 자리는 예외이며 `# noqa: BLE001` 주석 필수.
- **logging 모듈 사용** — 진단/에러는 `logger.*`. `print()` 는 사용자 대시보드 출력(stdout deliverable)에만 사용.
- **모듈 독립 테스트 가능** — 다른 모듈 없이도 import + 실행 가능해야 함.
- **테스트 그린 유지** — 작업 후 `python -m pytest` (오프라인, ~2.5초) 통과 필수. 새 로직에는 테스트 추가 (네트워크 없는 테스트만 — 합성 데이터/로컬 서버).

### 4.3 새 기능 짜기 전 — 코드 오딧 우선
영향 받는 파일들 먼저 Read 하고, 영향도 분석 후 작업 시작. 추측 금지.

### 4.4 모듈 책임 분리 원칙
- `src/http.py` — 표준 HTTP 세션 (retry/backoff, 타임아웃 강제, API 키 마스킹, 풀링)
- `src/storage.py` — SQLite 캐시 (`@cached` 데코레이터, TTL, best-effort — 캐시 장애가 파이프라인을 막지 않음)
- `src/data_fetcher.py` — **모든** 외부 API 호출 전담 (다른 모듈은 여기서만 데이터 받음). 전 함수 투명 캐싱, `QUANT_BOT_CACHE` 로 제어
- `src/macro_analyzer.py` — 거시 조망, 자산 간 분석, 시장 국면
- `src/risk_engine.py` — 종목/자산 리스크 계산
- `src/signals.py` — 신호 엔진 (팩터 점수/스크리닝 룰/변화 알림). 판정 로직은 순수 함수, fetch+state 는 오케스트레이터만
- `src/universe.py` — 전 종목 유니버스 DB (discover/enrich/scan). SQLite `screened` 테이블 복합 PK(symbol,market). 스캔은 오프라인 API 0콜
- `src/predictors.py` — 선행지표 lead-lag 예측 (M2→BTC, 한국수출→반도체). 분석은 순수 함수, fetch 는 predict_* 만
- `src/exceptions.py` — 도메인 예외 계층
- `src/logger.py` — 로깅 설정 (KST timestamp + offset)
- `src/config.py` — 환경변수 로딩, Settings dataclass
- `src/utils.py` — 2곳 이상에서 반복되는 순수 헬퍼만 (도메인 로직 금지)

### 4.5 예외 처리 규칙
- 외부 라이브러리 예외(`requests.HTTPError`, `ValueError` 등)는 라이브러리 경계에서 **도메인 예외로 변환** (`raise CustomError(...) from original_error` 로 원인 체인 보존)
- Catch 순서: specific (`DataFetchError` 등 도메인) → broad (`Exception` with `# noqa: BLE001`)
- API 키 누락은 `MissingApiKeyError` (subclass of `ConfigError`)
- HTTP 401 / 403 / 429 는 각각 `ApiAuthError` / `ApiAuthorizationError` / `RateLimitError`
- 전체 예외 계층은 `src/exceptions.py` 참고

### 4.6 로깅 규칙
- 모든 모듈은 상단에서 `from src.logger import get_logger; logger = get_logger(__name__)` 패턴 사용
- Timestamp 는 KST + `+0900` 오프셋 (logger.py 의 `TZFormatter` 가 자동 처리)
- 환경변수: `QUANT_BOT_LOG_LEVEL` (DEBUG/INFO/WARNING), `QUANT_BOT_LOG_TZ` (zone 이름), `QUANT_BOT_LOG_DIR`
- 콘솔: INFO 이상만, 시간 표기 없음
- 파일 (`logs/quant_bot.log`): DEBUG 이상 전부, 회전 10MB×5

### 4.7 print vs logger 구분
- **logger** → 진단, 경고, 에러, 진행 상황
- **print** → 사용자에게 보여줄 대시보드 / 표 / 결과 출력 (stdout deliverable)
- 헷갈리면 logger 사용

### 4.8 커뮤니케이션 톤
- 한국어 응답 기본 (코드 주석도 한국어 OK, 식별자는 영어)
- 사용자는 Python 기초 가능 수준 → 너무 deep dive 하지 말 것, 다만 시니어 엔지니어처럼 명확하게
- 결정사항이 있으면 명시적으로 묻기 (옵션 A/B/C 식으로)

### 4.9 보안
- 코드/로그/에러 메시지에 **API 키가 노출되지 않도록** 주의. 3단계에서 `src/http.py` 의 `SecretMaskingFilter` 가 로그/traceback 을 마스킹하도록 했음 — 단 logging 을 안 거친 unhandled crash 의 stderr traceback 은 여전히 노출 가능하니 키가 들어간 문자열을 예외 메시지에 직접 넣지 말 것
- 사용자가 채팅에 키를 붙여넣으면 즉시 회전 권고
- `.env` 는 절대 커밋 금지 (gitignore 됨)

### 4.10 과거 실수에서 얻은 원칙 (반복 금지)
이 프로젝트에서 실제로 발생한 버그들의 **근본 원인**과 재발 방지 규칙. 각 항목은
실제 사고에 기반하므로 추상적 조언이 아니라 체크리스트로 쓸 것.

**(1) 테스트 그린 ≠ 정상. 테스트는 운영과 다른 환경에서 돈다.**
단위테스트는 **로컬 sqlite3 + 캐시 off + 네트워크 없음**으로 돈다. 그래서 다음 부류는
테스트가 절대 못 잡는다 — 실제로 다 사고가 났다:
- **libsql/Turso 전용 동작**: `conn.execute` 에 list 파라미터 → sqlite3 는 통과, libsql 은
  `PyTuple` 에러. → **SQL 파라미터는 항상 tuple**.
- **캐시/데코레이터 배선**: `@cached` 네임스페이스 오배치 (아래 2번).
- **실제 API 필드명·에러 형태**: 외부 문서가 틀릴 수 있음 (아래 3번).
→ 규칙: 위 부류를 건드린 코드는 **실제 백엔드(Turso)·실데이터로 1회 스모크** 필수.
  (Turso 쓰기는 임시 로컬 DB `QUANT_BOT_DB_PATH=/tmp/x.db TURSO_DATABASE_URL=` 로 격리 검증)

**(2) 데코레이터/구조 근처에 코드를 끼운 뒤 결과를 반드시 재확인.**
사고: Phase 8 에서 `fetch_company_screener` 를 `fetch_quote` 바로 위에 추가하다가
`@cached("fmp_quote")` 데코레이터가 quote 가 아니라 screener 에 붙음 → quote 가 무캐시로
매번 실시간 호출(비용 낭비), screener 는 이중캐시. → 규칙: 새 fetch 추가/이동 후
`grep -n "@cached" src/data_fetcher.py` 로 **함수↔네임스페이스 매핑을 눈으로 확인**.

**(3) 외부 계획/문서의 '필드·동작 가정'은 실제 응답으로 검증 후 사용 (§4.3 확장).**
UPGRADE_PLAN 이 `roic`·`grossProfitMargin`·`priceToBookRatio` 가 key-metrics 에 있다고
가정했으나, 실제로는 `returnOnInvestedCapital`(이름 다름)·`ratios` 엔드포인트였음. **검증해서
막았다** — 이게 정답 패턴. 코딩 전 해당 엔드포인트를 1콜 찔러 필드 존재를 확인할 것.

**(4) 성능은 추정 말고 측정. 비용은 한 차원만 보지 말 것.**
사고: "데이터가 캐시라 재점수가 빠를 것" 이라 했으나 실제는 240분 — **API 가 아니라
Turso 쓰기(태평양 왕복)** 가 병목이었다. 읽기(로컬)만 보고 쓰기 비용을 빠뜨림. → 규칙:
성능을 말하기 전 **측정**. 네트워크 경계(원격 DB 쓰기, API)는 읽기/쓰기·로컬/원격을 분리해 따질 것.

**(5) 금융 비율의 부호·경계값을 명시 가드.**
사고: `_clip(25 − evToEBITDA×1.25, 0, 25)` 가 **음수 EV/EBITDA(적자기업)** 에 만점을 줌
("낮을수록 좋음" 공식이 음수에서 의미가 뒤집힘). PER/PBR/EV배수는 음수·0·None·극단값이
정반대 신호일 수 있다. → 점수 함수는 음수/0/None 을 **명시적으로** 0점 또는 별도 처리.

**(6) 어댑터/shim dict 는 대상 함수가 읽는 '모든' 필드를 채워라.**
사고: `enrich` 가 `value_scorecard` 에 넘긴 `quote_like` 에 `lastDividend` 가 빠져 → US 전
종목 배당점수가 항상 0. → 함수가 내부에서 무엇을 읽는지 확인 후 shim 구성. 리팩토링으로
데이터 출처를 쪼갤 때 특히 주의.

**(7) 점수/돈/데이터 정확성 코드는 작성 직후 한 번 더 (Phase 단위 리뷰).**
"여러 Phase 먼저 만들고 끝에 몰아서 버그수정" 은 금지 — 버그 위에 기능이 쌓여 비싸진다
(데코레이터 버그가 Phase 8→9 내내 quote 를 낭비한 게 예). 기능 단위로 끝낼 때 그 부분을
리뷰하고, scoring/enrich/fetch 류는 즉시 재검토 + 실데이터 스모크. UI/포맷팅은 가볍게.

**(8) (운영) 붙여넣기용 셸 명령은 주석·glob 없이 한 줄씩.**
사용자 zsh 는 `#` 주석을 인자로, 매칭 없는 glob(`*`, `^` 등)을 에러로 처리한다(반복 사고).
명령을 안내할 땐 trailing 주석·glob 을 빼고 한 줄씩 제공.

---

## 5. 폴더 경로 표준

- 로컬 작업 폴더: `/Users/leom/Developer/AI-Investment_Bot/AI-Investment-Bot/`
- 가상환경: `.venv/`
- 로그: `logs/quant_bot.log`
- 데이터 캐시: `data/` (gitignore)
- 노트북: `notebooks/` (gitignore)
- 핸드오버 문서: `CLAUDE.md`(이 파일), `docs/CURRENT_STATE.md`, `docs/ROADMAP.md`
