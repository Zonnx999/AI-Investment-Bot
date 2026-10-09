# AI-Investment-Bot — 프로젝트 규칙

이 문서는 새 AI 에이전트가 이 프로젝트에 합류할 때 가장 먼저 읽어야 할 파일입니다.
`docs/CURRENT_STATE.md` 와 `docs/ROADMAP.md` 도 함께 읽으세요.

---

## 1. 프로젝트 정체성

**개인용 퀀트 리서치 자동화 봇.** 한국 거주 1인 사용자(Leo)가 매일 아침 시장 상황과 종목별 신호를 받아보기 위한 도구입니다.

**중요: 이것은 챗봇이 아닙니다.** 결정론적 신호 생성 + 자동 알림 시스템입니다. 처음에는 LLM 챗봇으로 설계했지만 Phase 3 종료 시점에 사용자가 비용·결정성을 이유로 방향을 전환했습니다. **LLM 은 선택적 양념 레이어**(뉴스 센티먼트 분류, 일일 요약 한 줄)일 뿐 엔진이 아닙니다.

- GitHub: https://github.com/Zonnx999/AI-Investment-Bot
- 로컬 경로(현재 머신): `/Users/deepblue/Developer/AI-Investment-Bot/`
  - 이전 머신 경로(이력): `/Users/leom/Developer/AI-Investment_Bot/AI-Investment-Bot/` — 문서·메모에 남은 옛 경로는 이쪽.

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
- 핵심 라이브러리: `pandas`, `numpy`, `scipy`, `yfinance`, `pycoingecko`, `requests`, `python-dotenv`, `anthropic`(선택), `streamlit`(선택)
  - `fredapi` 는 코드에서 더 이상 import 하지 않음(FRED 는 REST 직접 호출). `pyproject.toml` 에 일부러 남겨 둠 — 전환 push 를 구 autopull 이 처리할 때 pyproject 변경이 있으면 서버 의존성이 무고정 업그레이드되기 때문. **다음 의존성 변경 때 제거**.
  - 버전 고정: `requirements.lock`(CI·Actions 가 사용, pandas 3.x·yfinance 1.7 등). 서버 적용 방식은 `docs/DEPLOYMENT.md §6.1`.

### 데이터 소스

| 소스 | 라이브러리 / 엔드포인트 | 키 필요 | 용도 |
|---|---|---|---|
| Yahoo Finance | `yfinance` | ❌ | 주가/ETF/선물/환율. 한국 종목 접미사: KOSPI → `.KS`, KOSDAQ → `.KQ` (`universe.yf_symbol`). 원장 채점·수익률은 **Adj Close**(§4.10 17) |
| FRED (St. Louis Fed) | REST `api.stlouisfed.org/fred/series/observations` (`src/http.py` 세션) | ✅ FRED_API_KEY | 거시 지표. 상태코드별 예외(401/403/429/5xx 구분 — 502 를 '시리즈 없음'으로 오분류하던 것 수정), 실패 시 최대 7일 stale-if-error(설정·ID 오류는 캐시로 가리지 않음), 예외 메시지에 키·URL 금지 |
| CoinGecko | `pycoingecko` | ❌ | 암호화폐 |
| FMP | `https://financialmodelingprep.com/stable/...?symbol=...` | ✅ FMP_API_KEY (유료) | 재무제표 시계열, 전 종목 발굴(company-screener), **점수는 TTM**(`/stable/key-metrics-ttm` + `/stable/ratios-ttm`, 최근 4분기·현재 시총 기준; 불가 시 연간 폴백). bulk 엔드포인트(`key-metrics-ttm-bulk`)는 현재 플랜에서 402 |
| 한국 무역 | FRED (OECD 출처) | ✅ FRED_API_KEY | 한국 수출·수입·무역수지 |
| KRX (한국거래소) | `data-dbg.krx.co.kr/svc/apis/sto/*` (AUTH_KEY 헤더) | ✅ KRX_API_KEY | 한국 전 종목 일별매매·기본정보 (Phase 9a, API별 신청 필요) |
| DART (전자공시) | `opendart.fss.or.kr/api/*` (crtfc_key 쿼리) | ✅ DART_API_KEY | 한국 재무제표 → ROE/PER/PBR (Phase 9b). corpCode 로 6자리↔8자리 매핑. 재무는 `fnlttSinglAcntAll`(전체 계정): 연결(CFS) 먼저, status 013 이면 별도(OFS) 1회 재시도, `account_id` 로 **지배주주 귀속** 순이익·자본 추출(총계 기준이던 지주사 PER/PBR 왜곡 수정) |

**FMP 중요 사실 (2026-05 기준):** 2025-08-31 이후 가입자는 `/stable/` 엔드포인트만 사용 가능. `/api/v3/` 는 레거시. URL 패턴은 `?symbol=AAPL&apikey=...` (티커가 쿼리 파라미터). `/stable/key-metrics` 는 P/E 를 직접 안 주고 `earningsYield` 형태로 줌 → 역수 취해 계산.

**LLM 요약 (NVIDIA NIM):** MiniMax 모델(m2·m2.7)은 NIM 에서 퇴역(HTTP 410) → 일일 메시지에서 **기본 OFF**(`QUANT_BOT_LLM=1` 로만 켬). 기본 모델 id 는 `deepseek-ai/deepseek-v4.1-flash`(응답 23~37초). 404/410 은 `ModelRetiredError` 로 분류되어 헬스(소유자 DM)로 올라감. 서버 `.env` 에 `MINIMAX_MODEL` 이 남아 있으면 기본값을 덮어쓰니 켤 때 함께 확인.

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
- **테스트 그린 유지** — 작업 후 `python -m pytest` (오프라인) 통과 필수. 2026-10-09 실측: **1,134개, 약 40초** — libsql 설치 환경(CI·프로젝트 `.venv` — `.[dev,hosting]`)은 전부 통과, 미설치 환경은 libsql 전용 3개가 스킵돼 1,131 통과 / 3 스킵 (autopull 시나리오 테스트가 git/bash 를 실제로 돌려서 예전보다 느림). 새 로직에는 테스트 추가 (네트워크 없는 테스트만 — 합성 데이터/로컬 서버). CI(`.github/workflows/ci.yml`, 잡 이름 `pytest`)가 같은 스위트를 `requirements.lock` 환경에서 돌리고, 서버 autopull 은 이 잡이 녹색인 코드 커밋만 배포한다.

### 4.3 새 기능 짜기 전 — 코드 오딧 우선
영향 받는 파일들 먼저 Read 하고, 영향도 분석 후 작업 시작. 추측 금지.

### 4.4 모듈 책임 분리 원칙
- `src/http.py` — 표준 HTTP 세션 (retry/backoff, 타임아웃 강제, API 키 마스킹, 풀링)
- `src/storage.py` — SQLite/Turso 캐시 (`@cached` 데코레이터, TTL, best-effort — 캐시 장애가 파이프라인을 막지 않음) + state 테이블. 연결 상태 API: `init_sync`("local"|"ok"|"error"|"timeout"|"offline")·`degraded`·`require_remote(what)`·`put_state -> bool`·`sync -> str`
- `src/data_fetcher.py` — **모든** 외부 API 호출 전담 (다른 모듈은 여기서만 데이터 받음). 전 함수 투명 캐싱, `QUANT_BOT_CACHE` 로 제어
- `src/macro_analyzer.py` — 거시 조망, 자산 간 분석, 시장 국면
- `src/risk_engine.py` — 종목/자산 리스크 계산
- `src/signals.py` — 신호 엔진 (팩터 점수/스크리닝 룰/변화 알림). 판정 로직은 순수 함수, fetch 는 오케스트레이터만. `generate_signal_report` 는 **상태를 쓰지 않고**, 발송 성공 후 `commit_signal_state(report)` 만 `signals/global:v2` 를 갱신
- `src/universe.py` — 전 종목 유니버스 DB (discover/enrich/scan). SQLite `screened` 테이블 복합 PK(symbol,market). 스캔은 오프라인 API 0콜. US 보통주 분류기(`excluded`)·`pick_top`(섹터 상한 2·KR 지주사 상한 2·KR 비표준 코드 건너뜀)·`freshness`·`yf_symbol`·`RULE_VERSION`. **재보강 누락 행 제외**: `scan` 은 시장 기준일(보강 행 `updated_at` 날짜의 중앙값)보다 `ROW_LAG_EXCLUDE_DAYS`(7)일 이상 뒤처진 행을 기본으로 뺌 (`include_lagging=True` 로 포함, 진단은 `lagging_rows`)
- `src/predictors.py` — 선행지표 lead-lag 예측 (M2→BTC, 한국수출→반도체). 분석은 순수 함수, fetch 는 predict_* 만. `PredictorSpec` 레지스트리(발표 지연 포함), 검증 안 된 `research_only` 는 운영 `PREDICTORS` 에서 제외
- `src/ledger.py` — **신호 원장**: 발송한 후보 리스트 기록(append-only)·채점(`roll_forward`, 리스트 그대로 보유 vs 벤치)·유니버스 스냅샷·07-13 소급 백필·성과 줄/주간 블록 포매터. universe/digest 를 import 하지 않음(duck typing). CLI 는 `scripts/ledger.py`
- `src/health.py` — 헬스 리포트(순수): 구독자 '⚙️ 상태' 줄(고정 어휘, Markdown 금지 문자 없음)·소유자 평문 DM·발송 보류 판정·signature(DM 중복 억제)
- `src/backtest.py` — 검증 프로토콜(순수): 비용 포함 시뮬레이션, 무선견 선행지표 워크포워드 + 베이스라인 2종·부호검정·Holm 보정, 운영 모멘텀 top-N, 블록 부트스트랩·월별 순위 IC. 실행은 `scripts/check_backtest.py`
- `src/findings.py` — `Finding` 공통 dataclass + **사용자 표시 라벨 상수 단일 출처**(`LABEL_VALUE`/`LABEL_HEALTH`/`LABEL_CANDIDATES`/`DISCLAIMER`, 크립토 라벨). 다이제스트·봇·대시보드·스크립트가 여기서만 import
- `src/digest.py` — 일일 메시지 v2: 렌더러는 순수, `assemble_daily_digest`(읽기 전용) → `send_daily_digest`(발송 성공 후에만 상태·원장 커밋)
- `src/exceptions.py` — 도메인 예외 계층
- `src/logger.py` — 로깅 설정 (KST timestamp + offset)
- `src/config.py` — 환경변수 로딩, Settings dataclass
- `src/utils.py` — 2곳 이상에서 반복되는 순수 헬퍼만 (도메인 로직 금지). `md_safe`/`strip_md` 포함
- `scripts/ledger.py` — 원장 CLI (`snapshot`/`backfill`/`score`/`report`/`ic`). `score` 는 다이제스트 유닛 ExecStartPost 가 호출. 저장소가 강등(초기 pull 타임아웃 → 빈 오프라인 파일)이면 어떤 명령도 실행하지 않고 **exit 3**
- `scripts/notify_owner.py` — 소유자 평문 DM 1통(systemd `OnFailure` 및 autopull 알림용). DB/pandas 를 import 하지 않음, 항상 exit 0. journal 꼬리는 줄 절단 **전에** 마스킹(경계에 걸친 키 접두부 누출 방지)
- `scripts/warm_replica.py` — 레플리카 미리 pull: `QUANT_BOT_DB_PATH` 파일을 열어 초기 sync 만 하고 `init_sync=… elapsed=…s` 1줄 출력. 재점수 유닛 `ExecStartPost` 가 재점수 직후 다이제스트 레플리카에 실행 (월요일 다이제스트의 첫 pull 이 한 주치 델타를 한 번에 받다 강등되는 것 방지)
- `scripts/lock_drift.py` — 서버 `.venv` 설치 버전 ↔ `requirements.lock` 핀 비교(표준 라이브러리만, 설치는 안 함). 불일치면 exit 3 + 요약 1줄. autopull 이 15분마다 호출해 lock 내용당 1회 소유자 DM
- `deploy/systemd/` — 서버 systemd 유닛·타이머 **원본**(저장소 관리). 서버에는 복사해 설치 (`docs/DEPLOYMENT.md §0`)

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
한 업그레이드 계획 문서가 `roic`·`grossProfitMargin`·`priceToBookRatio` 가 key-metrics 에 있다고
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

**(9) libsql/Turso 추가 함정 3종 (Phase 11 실사고 — #1 의 구체 사례).**
테스트는 로컬 sqlite3 라 아래를 못 잡는다. **실제 Turso 레플리카에서, 특히 깨끗하지 않은 상태에서** 검증할 것.
- **마이그레이션 PRAGMA/ALTER 분리**: 임베디드 레플리카는 읽기(PRAGMA table_info)=로컬, 쓰기(ALTER)=원격.
  로컬이 stale 하면 "컬럼 없음"으로 보여 ALTER 실행 → 원격엔 이미 있어 **`duplicate column` 크래시**(운영 다운).
  → 컬럼 추가는 반드시 `storage.add_column_if_missing`(중복 오류 삼킴). 마이그레이션 스모크는 **갓 sync 된
  깨끗한 레플리카 말고 stale 레플리카에서도** 해볼 것 (첫 스모크가 깨끗해서 못 잡았던 게 사고 원인).
- **한 로컬 레플리카 파일 = 한 프로세스**: 두 프로세스가 같은 파일에 동시 쓰기 → **`wal_insert_begin failed`**.
  프로세스마다 별도 `QUANT_BOT_DB_PATH`(각자 로컬 레플리카, 같은 클라우드로 sync). 봇=`quant_bot.db`, 다이제스트=`digest_replica.db`.
- **DB 사이드카(`*.db-wal/-shm/-info`) 커밋 금지**: clone 시 낡은 WAL 이 딸려와 레플리카 손상(`malformed WAL`).
  `.gitignore` 에 `data/*.db-*` (이미 추적 중이면 gitignore 만으론 안 빠짐 → `git rm --cached`). 복구는 로컬 .db* 삭제 후 재싱크.

**(10) 외부 포맷의 이스케이프 규칙을 확인 후 쓸 것.**
Telegram legacy `Markdown` 은 **백슬래시 이스케이프(`\_`)를 지원 안 함**(MarkdownV2 만 됨). 동적 콘텐츠(유저명의
`_` 등)가 불균형 엔티티를 만들면 sendMessage 가 통째 실패 → 메시지 유실. → 안내/관리 메시지는 **평문(parse_mode=None)**,
서식 메시지는 **파싱 실패 시 평문 자동 폴백**(`notifier.send_telegram` 에 구현). 손으로 `\` 이스케이프 하려다 사고.

**(11) 시간 민감 작업은 예약 cron 말고 상시 호스트 / 폴링 루프는 메시지 단위 가드.**
- **GitHub Actions 예약 cron 은 수십 분 지연·드롭이 흔함**. 좁은 시각 게이트와 겹치면 조용히 스킵 → 다이제스트가
  들쭉날쭉. → 상시 호스트의 **systemd timer**(OnCalendar 에 타임존 지정 시 KST/ET·DST 자동) 로 옮김 (`docs/DEPLOYMENT.md`).
- **폴링 루프는 메시지 처리부를 try/except 로 감쌀 것**: 한 메시지 예외로 루프가 죽으면, offset 이 배치 끝에 저장되는
  구조상 **같은 메시지 재수신 → 무한 크래시 루프**(poison message). `scripts/bot.py` 가 조회·구독 처리부를 각각 감쌈.

**(12) (프로세스) push/divergence 는 추측 말고 확인.**
로컬 `origin/main` ref 가 stale 해서 "이미 푸시됨"이라 잘못 단정한 적 있음. 상태는 `git rev-list --count origin/main..main`
(+ `git fetch` 선행) 으로 확인하고 말할 것.

**(13) 점수 체계는 하나 — 선정에 쓴 점수를 그대로 표시.**
사고: CALM 이 DB 종합 92 로 1위 선정됐는데 메시지엔 라이브 4팩터 공식 **48**(모멘텀 0)로 표시 — 사용자는 "왜 48점이 1위?"를
설명할 수 없다. → 후보 선정(`universe.pick_top`)·다이제스트·`/stock`·`/scan`·대시보드가 **DB `total_score` 와 같은 라벨(`findings` 상수)·
같은 기준일**을 쓴다. 표면을 추가하면 `tests/test_surface_consistency.py` 로 정합을 고정.

**(14) 검증 안 된 신호는 메시지에 싣지 않는다. 성과는 원장으로 잰다.**
사고: '신뢰도 R² 0.62' 로 보낸 BTC 선행지표 예측이 무선견 워크포워드 검증(`check_backtest`)에서는 7개 전부 베이스라인 수준(통과 0/7)이었다.
→ 예측·제안 비중은 기본 OFF(플래그로만 재활성화). 보낸 종목의 사후 성과는 신호 원장(`src/ledger.py`)이 기록·채점하고, 판정은 20거래일 블록 6개
(≈6개월) 이후 월요일에만 바뀐다. 표본이 부족하면 '검증 전' 을 항상 함께 표시.

**(15) best-effort 레이어도 실패를 헬스로 올린다 — 조용한 실패 금지.**
사고: LLM 요약이 HTTP 410 으로 몇 달 동안 조용히 빠졌고, 대시보드 export 가 46회 연속 실패했고, 유니버스 재점수 타이머가 아예 없었는데 아무도 몰랐다.
→ "실패해도 파이프라인은 계속" 은 유지하되, 실패는 `HealthReport`(상태 줄·소유자 DM)·종료코드·systemd `OnFailure` 로 반드시 보이게. 새 선택 기능을 넣으면
실패 시 어느 경로로 사람에게 알려지는지 먼저 정할 것.

**(16) 외부 모델·엔드포인트는 퇴역한다 — 기본 OFF + 분류.**
NIM 의 MiniMax 모델 퇴역(HTTP 410)이 일반 오류로 뭉개져 원인 파악이 늦었다. 외부 모델 id 는 하드 의존 금지: 기본 OFF, 404/410 은 `ModelRetiredError`,
`finish_reason=="length"`(잘린 응답)는 실패로 분류, 켜기 전 실호출 1회.

**(17) yfinance Adj Close 는 배당·분할 때 과거 전체가 재조정된다.**
원장 리뷰에서 발견: 저장해 둔 과거 절대가격을 새로 받은 가격으로 나누면 배당이 빠지고 분할일엔 가짜 −50% 수익이 영구 기록될 뻔했다.
→ 수익률은 **같은 다운로드 안의 두 시점**끼리만 계산(원장 채점기, 다이제스트 '선정 후' 도 같은 프레임의 진입일 Adj Close 기준). 저장한 `entry_px` 는 프레임 창
밖일 때의 폴백일 뿐.

**(18) libsql 호출은 GIL 을 쥔 채 블로킹할 수 있다 — 프로세스 경계만 확실.**
2026-07-06 노트북 Turso sync() 무한 행 사고. connect/sync/execute 가 GIL 을 쥔 채 네이티브 블로킹 → 스레드 워치독·시그널 같은 in-process 가드는 무력.
쓰기도 원격 왕복. → 초기 pull 은 자식 프로세스 프로브(타임아웃 시 오프라인 파일로 강등), push 전 소켓 사전점검, 서버는 systemd 워치독
(`Type=notify` + `WatchdogSec=300`, `TimeoutStartSec`)과 `QUANT_BOT_OFFLINE=1` 킬스위치. 새 DB 호출 경로를 만들면 "행이 걸리면 누가 죽이나" 를 먼저 답할 것.

**(19) 병렬 작업의 계약은 실제 코드로 확인 — 문서상 시그니처 가정 금지.**
병렬 구현을 병합할 때 `require_remote` 반환값(None/예외), `format_report` 인자, 원장 진입점이 서로 다르게 가정된 것이 병합 후에야 드러났다.
→ 다른 WP 가 제공할 함수는 병합 후 `grep`/Read 로 시그니처·반환 계약을 확인하고, 둘을 잇는 계약 테스트를 둔다.

**(20) 점수·필터·정렬 규칙을 바꾸면 `universe.RULE_VERSION` 을 올린다.**
원장은 규칙 버전 단위로 성과를 끊어 판정한다(옛 규칙 성과와 섞지 않음). v0 = 2026-07-10 동결 규칙, v1 = 2026-10 수정 라운드. 올리면 전환일 메시지의 판정 사유가
'규칙 v1 로 후보 재구성 (편입 N · 탈락 M)'(후보가 같으면 '규칙 v1 전환 (후보 동일)')이 되고 종목별 '선정 후' 는 새 회차부터 다시 센다.

**(21) dry-run·진단 스크립트는 상태를 쓰지 않는다. 상태 커밋은 발송 성공 후.**
사고: 기존 `generate_signal_report` 가 알림 상태를 직접 갱신해, `--dry-run` 이나 `check_signals` 가 다음 실발송의 '변화' 를 먹어치웠다.
→ 리포트 생성은 읽기 전용, 상태는 `commit_signal_state(report)` / `ledger.record_send` 가 발송 성공(sent ≥ 1) 뒤에만. dry-run 의 상태·원장 무쓰기는 테스트(해시 불변)로 고정.
단 dry-run 도 **스키마 보장**(`CREATE IF NOT EXISTS`·멱등 컬럼 추가)과 **조회 캐시 저장**(`@cached`)은 할 수 있다 — Turso 모드에선 이것도 원격 쓰기 (`scripts/send_digest.py` docstring). 운영 레플리카가 아닌 사본으로 점검.

**(22) company-screener 에 보통주가 아닌 증권이 섞인다 — 이름만으론 못 거른다.**
상장 후순위채(baby bond)·우선주가 US 후보 상위에 올랐다(MGR·AFGE 는 이름이 모회사 이름이라 이름 규칙이 못 잡음). → `universe.classify_us_securities`:
이름 규칙 + 같은 모회사 '형제' 비교(가격·거래대금) + OTC·셸·거래대금 하한. 규칙이 못 잡는 영구우선주(예: STRC 류)는 알려진 한계 — `docs/CURRENT_STATE.md §2`.

**(23) 신선도는 행 단위로도 본다 — 재보강 실패 1행이 시장 전체 판단을 막지 않게.**
다이제스트의 점수 기준일은 후보들 중 가장 오래된 `updated_at`(min) 이다. 그래서 주간 재점수에서 갱신에 실패한 행(상폐·DART 미제출·no_data) 하나가 옛 점수로 후보에 남으면
그 1행이 시장 전체를 '후보 보류'·'주간 갱신 지연' 으로 끌고 갈 수 있었다 (최종 리뷰에서 재현, 수정 후 DB 사본 스모크로 확인).
→ `universe.scan` 이 시장 기준일 — 보강 행 `updated_at` 날짜의 **중앙값** — 보다 7일 이상 뒤처진 행을 후보에서 뺀다. 기준을 MAX 가 아니라 중앙값으로 잡은 이유: 수동 `--enrich --limit 1` 같은
소수 행 갱신이 기준을 끌어올려 나머지 전부를 '뒤처짐' 으로 만들지 않게. 빠진 행은 조용히 버리지 않고 소유자 DM 고정 키 `재보강 누락 종목` 으로 보이게 한다 (#15).
시장 단위 집계(min/max)로 신선도를 판정하는 코드를 쓸 땐 "이상 행 하나가 결과를 뒤집지 않는가" 를 먼저 물을 것.

---

## 5. 폴더 경로 표준

- 로컬 작업 폴더: `/Users/deepblue/Developer/AI-Investment-Bot/` (이전 머신: `/Users/leom/Developer/AI-Investment_Bot/AI-Investment-Bot/`)
- 가상환경: `.venv/`
- 로그: `logs/quant_bot.log`
- 데이터 캐시: `data/` (gitignore)
- 노트북: `notebooks/` (gitignore)
- 핸드오버 문서: `CLAUDE.md`(이 파일), `docs/CURRENT_STATE.md`, `docs/ROADMAP.md`
