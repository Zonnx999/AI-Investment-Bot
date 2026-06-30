# Current State — 2026-06-30 기준

> 새 에이전트는 **이 파일 + `CLAUDE.md`(작업 규칙) + `ROADMAP.md`(다음 작업)** 만 읽으면 됩니다.
> 봇 서버 운영은 **`docs/DEPLOYMENT.md`** (Oracle 호스팅 런북).
> 완료 작업의 **상세 변경 내역은 `git log`** 에 있습니다 — 여기엔 현재 상태·핸드오버·API 참조만 둡니다.

---

## 1. 한눈에 (지금 동작하는 것)

- **리팩토링 1–8단계 + Phase 0–10 완료.** 결정론적 신호·스크리닝 봇 (LLM 챗봇 아님 — `CLAUDE.md §1`).
- 매일 아침 **텔레그램 다이제스트** (국면 + 변화 알림 + 팩터 표 + 선행지표 예측). 박스 systemd timer
  로 한국(08:30 KST)·미국(09:00 ET) 장 30분 전 2회 (GitHub Actions 예약발송은 비활성).
- **전 종목 유니버스 DB** (US 2190 / KR 517 / CRYPTO 68) → **오프라인 전수 스캔(API 0콜)** 으로 저평가 발굴.
- **점수**: 4팩터(모멘텀·밸류·퀄리티·로우볼) + health/value 스코어카드(구성요소 분해, `detail` JSON).
- **Turso(libSQL) 클라우드 호스팅** — 노트북 이동성 + 클라우드 풀유니버스.
- **시장별 다이제스트**: 한국 창→KR 종목, 미국 창→US 종목 (cron 이 창에 따라 `--market` 자동 전달).
- **Phase 11a 멀티유저(소유자 승인제)** + **11b 상시 인터랙티브 봇** — Oracle 서버에 systemd
  `quant-bot` 으로 **배포·가동 중**(`docs/DEPLOYMENT.md`). `/start`·`/approve`·`/stock`·`/scan`·`/help`.
- **Phase 12 대시보드 — 라이브** 🟢 https://zonnx999.github.io/AI-Investment-Bot/
  `dashboard/index.html`(5탭) + `scripts/export_dashboard.py` + `.github/workflows/dashboard-export.yml`
  (매일 09:30 KST JSON 갱신 + **Actions 로 Pages 배포**). Pages Source = "GitHub Actions" (설정 완료).
- **서버 자동 배포 (라이브, 06-30 확인)**: `scripts/server_autopull.sh` + systemd timer(15분) → `main` push 시
  ~15분 내 박스 자동 반영(pull→reset→재시작). `dashboard/*.json`·docs 만 바뀐 커밋은 재시작 스킵.
  설정·운영은 `docs/DEPLOYMENT.md §6.1`. 즉시 반영: `sudo systemctl start quant-autopull.service`.
- **(2026-06-29)** 코드리뷰 버그 5건 수정 + 다이제스트 UX(회사명·범례·위계·예측) + `/announce` 소유자 공지 + 문서 정리(CODE_REVIEW·UPGRADE_PLAN 제거).
- **MINIMAX_API_KEY** `.env` 에 추가됨 — LLM 한 줄 요약용(MiniMax-M3/NVIDIA). 구현은 대기(`ROADMAP §2.1`).
- **다음 작업**: 11b 잔여(인라인 승인버튼·`/news`) 또는 LLM 한 줄 요약. 코드 개선 backlog는 `ROADMAP §2.2`.

---

## 2. 열린 이슈 / 핸드오버 (가장 먼저 읽기)

- ✅ **Turso 전체 재점수 느림 — 해결 (2026-06-15)**. 측정으로 근본원인 규명 후 배치 쓰기로 수정.
  - **측정**: libsql 임베디드 레플리카는 쓰기 statement 마다 클라우드(한국→us-west-2) 왕복 ~1s.
    `executemany` 도 내부 statement 루프라 효과 없음(976ms/row). `commit` 매번은 2.5s/row.
    → 240분의 주범은 `detail` blob(평균 395B, 작음)이 아니라 **2164번의 개별 왕복**이었음.
  - **해결**: `executescript` 가 다중 statement 를 **한 요청으로 배치** 전송(실측 ~40ms/row, ~20배).
    `universe.enrich`/`enrich_kr` 의 per-row 쓰기를 모았다가 `chunk_size`(기본 200)마다
    `_flush_updates`(=executescript)로 흘려보냄. executescript 는 파라미터 바인딩 불가 →
    `_sql_lit` 로 값 직렬화(아포스트로피·JSON 따옴표·한글·None/NaN 안전).
  - **실측 검증(실 Turso)**: enrich(limit=20) 1.5s(12.9 rows/s), 다중 청크(5+5+2) 손실 0,
    fresh 레플리카에서 클라우드 영속 확인. US 전수 **~240분 → ~2-3분**.
  - **주의(libsql)**: 레플리카는 자기 쓰기를 `sync()` 전엔 로컬에 안 비춤 → 루프 도중 방금 쓴
    행 재읽기 금지. 최종 반영은 build_universe 끝의 `get_storage().sync()` 담당.
- 점수 공식이 4팩터 + 음수배수/배당 수정으로 바뀜 → **DB 재점수 필요**:
  `python scripts/build_universe.py --enrich --force` (이제 배치로 ~2-3분). 안 돌리면 옛 점수 잔존.
- 작업 리듬(사용자 합의): Phase 단위로 끝낼 때마다 그 부분 리뷰. scoring/enrich/fetch 등
  '정확성 직결' 코드는 작성 직후 한 번 더 검토 + 실데이터 스모크 (`CLAUDE.md §4.10`).

### 운영 현황 / 인계 (2026-06-22~23)
- ✅ **봇·다이제스트 Oracle 서버에 배포·가동 중** — 운영 전반은 **`docs/DEPLOYMENT.md`** (런북).
  - `quant-bot`(systemd) = 실시간 명령 폴링 / `quant-digest-kr|us`(systemd timer) = 평일 자동 발송.
  - 친구 승인·수신 실제 동작 확인됨. 봇 명령: `/start`·`/stop`·(소유자)`/approve`·`/deny`·`/pending`·`/subscribers` /
    (구독자)`/stock`·`/scan`·`/help`·`/menu`(버튼 메뉴).
- ⏳ **남은 사용자 작업**:
  - GitHub Secrets — 이제 GitHub 예약 발송은 끔(박스가 담당). workflow_dispatch 수동 발송용으로만 의미.
  - 박스 `.env` 에 `FRED_API_KEY`·`FMP_API_KEY` (US 다이제스트 팩터·국면용). KR 은 키 없이도 동작.
- ⏳ **다음 기능 후보** (`ROADMAP.md §1`): 인라인 `[승인][거절]` 버튼(승인 부담↓), `/news`, `/regime`·`/predict` 즉답.

### 인계 (2026-06-30)
- ✅ **이번 세션 전부 배포·검증**: 코드리뷰 버그 5건 + 다이제스트 UX(회사명·범례·위계·예측) +
  `/announce` 소유자 공지 + Phase 12 대시보드 **라이브**(Actions Pages 배포) + **서버 자동 배포** + 문서 5개로 정리.
- ✅ **서버 자동 배포 end-to-end 확인**: `[autopull] update … -> … restarted quant-bot` 로그로 검증,
  sudoers 무비번 재시작 OK. push → ~15분 내(또는 `sudo systemctl start quant-autopull.service` 즉시) 박스 반영.
- 📌 **다음 1순위 후보**: 인라인 `[승인][거절]` 버튼(11b), 또는 **LLM 한 줄 요약**(MiniMax-M3,
  `MINIMAX_API_KEY` 준비됨, 설계 `ROADMAP §2.1`). 코드 개선 backlog는 `ROADMAP §2.2`.
- 📌 **필독 문서는 5개**: `CLAUDE.md` / `README.md` / `docs/{CURRENT_STATE,ROADMAP,DEPLOYMENT}.md`
  (CODE_REVIEW·UPGRADE_PLAN 은 정리하며 삭제 — 미해결 항목은 ROADMAP §2 로 이전).

---

## 3. 폴더 구조 (요약)

```
AI-Investment-Bot/
├── CLAUDE.md, docs/{CURRENT_STATE,ROADMAP}.md   # 핸드오버 3종
├── pyproject.toml                                # pip install -e ".[dev]" (+[hosting]=libsql)
├── .env (gitignore)                              # API 키 전부
├── src/                                          # §4 참조
├── scripts/                                      # 실행 진입점 (아래)
├── dashboard/index.html                          # 가치주 대시보드 (Phase 12 에서 확장 예정)
├── tests/                                        # 오프라인 140개 — python -m pytest (~2.5s)
├── data/ (gitignore, 캐시·유니버스 DB), logs/, notebooks/
└── .github/workflows/daily-digest.yml            # cron 다이제스트
```

**주요 스크립트**: `check_market_regime.py`(매일 아침 메인) · `daily_update.py`(수집 오케스트레이터,
cron 진입점) · `check_signals.py` · `check_predictions.py` · `build_universe.py`(주1회 배치,
`--discover`/`--enrich`/`--force`) · `scan.py`(오프라인 발굴, `--check 티커`) · `send_digest.py`
(텔레그램, `--dry-run`) · `telegram_setup.py` · `turso_setup.py` · `screen_value.py`.

---

## 4. 모듈 API 빠른 참조

> 책임 분리 원칙은 `CLAUDE.md §4.4`. 여기엔 함수 시그니처만 (코드 안 읽고 호출 지점 찾기용).

### 예외 계층 (`src/exceptions.py`)
```
QuantBotError
├── ConfigError → MissingApiKeyError(key_name)
├── DataFetchError(source)
│   ├── ApiHttpError(status_code) → ApiAuthError(401) / ApiAuthorizationError(403) / RateLimitError(429)
│   ├── ApiTimeoutError / ApiConnectionError / DataValidationError
└── AnalysisError → InsufficientDataError(n_points, required)
```

### `src/data_fetcher.py` — 모든 외부 API (전 함수 투명 캐싱, `QUANT_BOT_CACHE` 제어)
| 함수 | 소스 |
|---|---|
| `fetch_prices` / `fetch_fundamentals` / `fetch_financials_yf` | yfinance |
| `fetch_macro` / `fetch_macro_dashboard` / `fetch_korea_trade` | FRED |
| `fetch_crypto` / `fetch_crypto_top` | CoinGecko |
| `fetch_quote` / `fetch_profile` / `fetch_company_screener` | FMP |
| `fetch_financial_statements` / `fetch_key_metrics` / `fetch_ratios` | FMP |
| `fetch_krx_daily` / `fetch_krx_base_info` | KRX (AUTH_KEY 헤더) |
| `fetch_dart_corp_codes` / `fetch_dart_financials` | DART (crtfc_key) |
| `fetch_wikipedia_pageviews` | Wikimedia |

상수: `FMP_BASE_URL=".../stable"`(v3 아님), `FRED_SERIES`, `KOREA_TRADE_SERIES`.

### 기타 모듈 핵심 함수
- **`http.py`**: `get_http_session()`(retry/timeout 강제 싱글톤), `mask_secrets()`, `SecretMaskingFilter`,
  `is_timeout(exc)`(⚠️ retry 시 read timeout 이 ConnectionError 로 감싸여 나옴).
- **`storage.py`**: `Storage`(@cached 데코레이터, TTL, best-effort) + Turso 백엔드(`TURSO_*` 있으면 libSQL
  임베디드 레플리카) + `put_state/get_state`(TTL 없는 영속) + `sync()`(쓰기 배치 후 클라우드 push).
- **`macro_analyzer.py`**: `market_summary` · `classify_regime()`(`RegimeReport.signals`≠`.failures` 분리) ·
  `correlation_matrix` · `rolling_correlation` · `current_drawdown`(ffill 보정) · `sharpe_ratio`.
- **`risk_engine.py`**: `historical_var`/`parametric_var`/`expected_shortfall`(수익률만 받음) ·
  `max_drawdown` · `monte_carlo_simulation`(격리 `default_rng`) · `scenario_impact` · `risk_report`.
- **`signals.py`**: 순수 판정 함수(`factor_scores` 4팩터 / `apply_screen_rules` / 알림 룰) +
  `generate_signal_report()` 오케스트레이터(fetch+state 담당). 알림은 `put_state` 비교로 '변화'에만 발화.
- **`screener.py`**: `ScoreCard`/`Component`(점수 분해), `health_scorecard`/`value_scorecard`,
  `latest_fundamentals`(key-metrics+ratios 병합, 종목당 FMP 2콜·7일 캐시), `calculate_*` int wrapper.
- **`universe.py`**: `discover`/`enrich`(US, 배치 쓰기)/`enrich_kr`(DART, 배치)/`scan`/`lookup`/
  `lookup_detail`/`top_symbols`/`stats`. `screened` 테이블 복합 PK(symbol,market).
- **`predictors.py`**: `analyze_lead_lag`(순수, lag 1~12 OLS) + `predict_*`(fetch). `PREDICTORS` 레지스트리 7개.
- **`notifier.py`** / **`digest.py`**: Telegram POST(의존성 0) / `format_digest` 순수 포매터.
- **`utils.py`**: `close_series`(Adj Close/Close fallback), `pick_first`, `TRADING_DAYS_PER_YEAR`.

---

## 5. 완료 이력 (요약 — 상세는 `git log`)

| 단계/Phase | 요약 | 상태 |
|---|---|---|
| 리팩토링 1–8 | 로깅→예외→HTTP견고성→DRY→패키지화→테스트→결정론→API정합성 | ✅ (~06-13) |
| Phase 4 | Storage & Daily Pipeline (SQLite 캐시, daily_update) | ✅ |
| Phase 5 | Signal Engine (팩터/스크리닝/변화알림, 순수함수+상태비교) | ✅ |
| Phase 6 | 선행지표 예측 (lead-lag, M2→BTC·한국수출→SOXX 등 7관계 + 위키) | ✅ |
| Phase 7 | Telegram 알림 봇 + GitHub Actions cron 다이제스트 | ✅ |
| Phase 8 | 전 종목 유니버스 DB + 오프라인 전수 스캔 | ✅ |
| Phase 9a/9b | KRX 한국 발굴(507종목) + DART 펀더멘털 점수(ROE/PER/PBR) | ✅ (06-14) |
| Phase 10 | 데이터 호스팅 (Turso/libSQL 임베디드 레플리카) | ✅ (06-13) |
| 점수 엔진 정밀화 | ScoreCard 분해, 4팩터, 필드 교정(ROIC/PBR/GP마진), `detail` 저장 | ✅ (06-15) |
| 코드리뷰 대응 | 데코레이터 오배치·음수배수·배당누락 등 실버그 수정 | ✅ (06-15) |
| Turso 재점수 배치 | per-row 왕복 → executescript 배치 (240분→~2-3분) | ✅ (06-15) |
| Phase 12 대시보드 | export_dashboard.py + index.html 재작성(5탭·lazy load·sort/filter·₩표기) + CI | ✅ (06-23) |
| 시장별 다이제스트 | 한국창→KR 종목 / 미국창→US 종목 (cron `--market` 자동) | ✅ (06-15) |
| Phase 11a 멀티유저 | 소유자 승인제(`subscribers`, pending/active/inactive), getUpdates 폴링+offset | ✅ (06-22) |
| Phase 11b 인터랙티브 봇 | `bot_commands`+`scripts/bot.py` 폴링 워커, `/stock`·`/scan`·`/help`, rate limit | ✅ (06-22) |
| 텔레그램 견고화 | Markdown 평문 폴백, 조회 구독자 게이팅, reply 키보드 버튼, `/subscribers` | ✅ (06-22) |
| 서버 배포 | Oracle Always Free(systemd `quant-bot`) + 다이제스트 systemd timer | ✅ (06-22) `docs/DEPLOYMENT.md` |
| 코드리뷰 버그수정 | `_safe` NaN/문자열·company_screener 에러dict·DART 계정명·KOSDAQ 발굴 격리 (+테스트8, 커밋 `beac21e`) | ✅ (06-29) |
| 다이제스트 UX + 공지 | 회사명·범례·시각적 위계·예측 가독성 / `/announce` 소유자 공지 브로드캐스트 (+테스트10) | ✅ (06-29) |

> ⚠️ 한계 기록: FMP 엔 실제 KOSPI/KOSDAQ 없음(→ KRX/DART 로 해결). 크립토 점수는 주식과 비교
> 불가(스캔에서 시장별 분리). 봇/다이제스트는 **서로 다른 libsql 레플리카 파일** 필수(WAL 충돌).
> 다이제스트 cron 은 `--no-sync`(봇이 getUpdates 단독 소유). libsql 함정은 `CLAUDE.md §4.10 #9~#11`.

---

## 6. 환경 셋업 (새 머신/새 에이전트)

```
cd /Users/leom/Developer/AI-Investment_Bot/AI-Investment-Bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # 클라우드 호스팅까지: ".[dev,hosting]"
cp .env.example .env             # FRED_API_KEY, FMP_API_KEY, (KRX/DART/TELEGRAM/TURSO) 채우기
python -m pytest                 # 오프라인 140개, ~2.5초
python scripts/check_market_regime.py   # 메인 대시보드
```

---

## 7. Git

- Default branch `main`. Remote `origin = https://github.com/Zonnx999/AI-Investment-Bot.git`.
- push 여부 확인: `git log origin/main..main`.
- 커밋 메시지는 사용자 지시가 있을 때만 (`CLAUDE.md` 의 커밋 규칙 준수).
