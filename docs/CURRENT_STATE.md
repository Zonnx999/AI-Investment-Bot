# Current State — 2026-10-09 기준

> 새 에이전트는 **이 파일 + `CLAUDE.md`(작업 규칙) + `ROADMAP.md`(다음 작업)** 만 읽으면 됩니다.
> 서버 운영·배포 순서는 **`docs/DEPLOYMENT.md`**. 완료 작업의 상세 변경 내역은 **`git log`** 에 있습니다 —
> 여기엔 현재 상태·핸드오버·API 참조만 둡니다.

---

## 1. 한눈에 (지금 코드가 하는 일)

> 아래는 브랜치 `fix/signal-quality-round` 의 코드 기준입니다. **아직 main 에 반영되지 않았고 서버는 옛 버전**입니다 (§2.1).

- **정체성**: 결정론적 신호·스크리닝 봇 (LLM 챗봇 아님 — `CLAUDE.md §1`). 한국 08:30 KST · 미국 09:00 ET (평일) 하루 2회 텔레그램, Oracle 서버 systemd timer.
- **새 메시지 (다이제스트 v2)** — "읽고 넘길 수 있는" 형태로 재설계:
  - 첫 줄이 **오늘 할 일 판정**: `없음` / `매수 검토 N` / `탈락 N` / `리스크 점검 N` + 사유. 판정은 예측이 아니라 **사실의 변화**(후보 리스트 diff·새 알림·선정 후 하락)에서만 나옴.
  - 둘째 줄 **기준일**: `시세 MM-DD 종가 · 점수 MM-DD (N일 전)`. 점수 8일 이상 → ⚠️, 15일 이상 → 후보 보류(아래).
  - **변화만 펼침**: 후보가 안 바뀐 날은 종목당 1줄, 바뀐 🆕 종목만 3줄(가격·200일선·52주·근거). 새 알림이 없으면 "새 알림 없음" 한 줄.
  - **KR 아침** = 판정·🔔 새 알림·🇰🇷 한국 시장(코스피·코스닥·원/달러 + 간밤 미국 1줄)·🌐 글로벌(국면 1줄 + 경보 유지)·📋 한국 후보·📒 성과·⚙️ 상태.
    **US 저녁** = 판정·🌐 아침 이후 변화·📋 미국 후보·📒 성과·⚙️ 상태. (두 메시지가 같은 글로벌 섹션을 반복하던 문제 해소 — 글로벌은 아침 1회.)
  - **점수는 하나**: 후보 선정(`universe.pick_top`)과 표시가 같은 DB `total_score`(밸류+건전성). 가격 줄(200일선·52주)은 참고용, 점수 미반영. `/stock`·`/scan`·대시보드도 같은 숫자·라벨·기준일.
  - **📒 지난 후보 성과** 1줄 (원장, 아래) + 월요일(현지) 확장 블록. 표본이 쌓이기 전엔 항상 `⏳검증 전 n/6` 을 같이 표시.
  - **⚙️ 상태 줄**: 데이터가 묵었거나 국면 지표가 빠졌을 때만(고정 어휘). 소유자에게는 평문 DM 상세.
  - 선행지표 예측·제안 비중(Kelly)·LLM 한 줄 요약은 **기본 제거**(검증 전) — 환경변수 플래그로만 재활성화, 코드는 보존.
  - **규칙 전환일** (직전 리스트가 v0, 현재 v1): 판정 사유가 `규칙 v1 로 후보 재구성`(편입 N · 탈락 M 은 사유에만) — '매수 검토 N' 으로 세지 않고 새 규칙의 기준선으로 기록. 종목별 '선정 후' 는 새 회차부터 다시 셈.
  - **후보 보류일** (점수 ≥ 15일): 후보 섹션이 `후보 보류 — 점수 N일 경과` 1줄, 매수/탈락/기준 수립 판정 없음(`후보 보류 (점수 N일 경과)`), 원장 미기록. 새 알림(리스크)은 보류와 무관하게 판정에 남음.
  - **선정 후 수익률**은 같은 다운로드(1년 프레임)의 **진입일 Adj Close** 기준 — 분할·배당 소급 재조정과 같은 기준(`CLAUDE.md §4.10 (17)`). 프레임 창 밖이면 저장된 진입가로 폴백. |값| > 60% 는 이상치로 보고 −10% 판정에서 제외 + 소유자 DM.
- **알림 규칙 (재설계)** — 모두 "지난 커밋 상태 대비 변화"만, 상태는 `signals/global:v2` 하나(KR·US 공유):
  낙폭(자산별 고정 임계표·확대 1.75×·해제는 서로 다른 관측일 3회) · 변동성 σ20/σ252 ≥ 2.0 · 신용 HY 20일 +0.50/+1.00%p · 원/달러 20일 ±5% ·
  국면 전환은 '중립 이하' 경계 통과만(서로 다른 KST 날짜 2회 확인) · **시장 충격 하드캡**(하루 |r| ≥ S&P500 3.5% / 코스피 5% / 코스닥 6%, 쿨다운 5영업일) ·
  **선정 후 −10% 최초 하회 1회**(임계 `QUANT_BOT_PICK_DD_ALERT_PCT`, 0 이면 끔).
  국면 지표(3개) 중 일부가 빠지면 `판정 보류 (지표 n/3)` 로 직전 확정 판정 유지·전환 억제 (FRED 502 를 '시리즈 없음'으로 오분류하던 것 수정).
- **신호 원장 (`src/ledger.py`)** — 실제 발송한 후보 리스트를 append-only 로 기록하고 "그 리스트를 그대로 보유했다면" 일별 수익을 벤치(US SPY / KR 069500·229200 홈지수 ETF)와 비교,
  비용 편도 10bp 반영, Adj Close 대칭. 판정은 비중첩 20거래일 블록 6개(≈6개월) 이후 **현지 월요일에만** 전환: `pending → testing → validated(t≥+2) / demoted(t≤−1 ∧ 평균≤−1%p)`.
  규칙 버전(`universe.RULE_VERSION`, 현재 `v1`)별로 끊어 판정. 07-13 발송분은 `scripts/ledger.py backfill` 로 소급(`소급 포함` 표시). 채점은 다이제스트 유닛의 `ExecStartPost`.
- **검증 프로토콜 (`scripts/check_backtest.py`)** — [1] 원장 성과 [2] 유니버스 순위 IC(스냅샷 2개 이상) [3] 선행지표 7개 무선견 워크포워드(베이스라인 2종·부호검정·Holm·전후반 안정성)
  [4] 운영 모멘텀 top-N 진단 [5] SPY 200일선 참고. 현재 결과: **선행지표 통과 0/7**(기존 R² 0.62 예측도 베이스라인 수준 → 메시지에서 제거), 운영 모멘텀 t≈−0.6 판정 불가.
  '미입증'은 '예측력 없음'이 아님(문구 고정). 종료코드 1 은 인프라 실패만.
- **데이터 정확성**: US **보통주 필터**(상장 후순위채·우선주·OTC·셸·저유동 `excluded`, `reclassify_us`) · **TTM 기준 점수**(FMP key-metrics-ttm/ratios-ttm, 불가 시 연간 폴백) ·
  **금융업 50% 중립**(맞지 않는 지표 대치, `flags`에 `fin_neutral`) · 이상치 격리(EY>0.35, FCF>0.40 중립) · KR **지배주주 귀속**(DART `fnlttSinglAcntAll`) ·
  KR 일회성 이익·지주사 상한·위생(ADR/상폐/정지) · 섹터 상한 2(US) / 지주사 상한 2(KR) · 배당 `dividendPerShare` 우선.
- **운영 안전장치**: CI(`pytest` 잡) 통과 커밋만 서버 자동 배포 + 기동 실패 시 자동 롤백 · 헬스 리포트(`src/health.py`) → 소유자 평문 DM · systemd 유닛 원본을 저장소에서 관리(`deploy/systemd/`) ·
  `OnFailure` 알림 · 봇 READY 신호·워치독 · 다이제스트 종료코드 표 · 유니버스 주간 재점수 타이머(`quant-universe.timer`, 토 10:00 KST) · 대시보드 export 가드(빈 DB/강등 시 덮어쓰기 거부).
- **LLM 한 줄 요약**: 기본 OFF (NIM 의 MiniMax 모델 퇴역 HTTP 410). 기본 모델 `deepseek-ai/deepseek-v4.1-flash`, 켜려면 `QUANT_BOT_LLM=1` + 서버 `.env` 의 `MINIMAX_MODEL` 확인.
- **봇 명령** (소유자 승인제 멀티유저): `/stock <티커> [us|kr|crypto]`(점수·근거·기준일·제외 사유, 같은 코드가 여러 시장이면 시장 지정) · `/scan [us|kr] [개수]`(다이제스트와 같은 선정) ·
  `/ledger [us|kr]`(원장 성과, **평문**) · `/news <티커>` · `/menu` · 소유자 `/approve` `/deny` `/pending` `/subscribers` `/announce` · 가입 요청 인라인 [승인][거절] 버튼.
- **대시보드** (https://zonnx999.github.io/AI-Investment-Bot/ ): `dashboard/index.html` 5탭 + payload 에 `data_as_of`·`fund_asof`/`price_asof`·`excluded`·`eligible_count`, 중립 어휘, 예측 탭 '검증 미통과 · 참고용'.
  ⚠️ **GitHub Actions "Dashboard Export" 는 현재 중단 상태** — 07-03 부터 연속 실패, 마지막 성공 07-10, 60일 무활동으로 GitHub 이 자동 비활성화(`disabled_inactivity`). 워크플로 수리는 이 브랜치에 포함, **수동 재활성화 필요**(`DEPLOYMENT.md` 배포 순서).
- **Turso(libSQL) 클라우드 호스팅** (US 2190 / KR 517 / CRYPTO 68 — 07-10 재점수 기준, 이후 재점수 없음 → §2.4): 서버 레플리카는 프로세스별 파일 분리(봇 `quant_bot.db` · 다이제스트 `digest_replica.db` · 재점수 `universe_replica.db`).

---

## 2. 열린 이슈 / 핸드오버 (가장 먼저 읽기)

### 2.1 브랜치·배포 상태
- 작업 브랜치 **`fix/signal-quality-round`** (기준 `main` = `6403d60`, 약 60 커밋 앞섬). **origin 에 push 안 됨** — 사용자 검토 대기. 이번 라운드는 사용자 승인("한번 쫙 고쳐보자, push 는 내가 검토한 다음에")을 받은 **phase-gate 예외 라운드**였고, 검토·배포 뒤에는 `CLAUDE.md §4.1` phase-gate 로 복귀.
- `main` 에 push 하면 서버가 15분 내 자동 반영하므로 **배포 순서가 중요**합니다 → `docs/DEPLOYMENT.md` "배포 순서" (브랜치 push 로 CI 먼저 확인 → main ff 병합 → 유닛 설치 → 원장 소급 → 첫 재점수 → 타이머 → 대시보드 재활성화 → 공지).
- `docs/proposals/2026-10-08-fix-proposal.html`(진단·설계 제안서)은 **커밋하지 않기로 한 사용자 결정**으로 로컬에만 있음.
- 테스트: **1,057 통과 / 3 스킵, 약 35초** (2026-10-09 실측, 오프라인).
- 서버 실제 상태(설치 버전·`.env` 키·타이머 목록)는 이번 라운드에서 접속하지 못해 **미확인**입니다. 배포 전 `DEPLOYMENT.md` 의 점검 명령으로 확인하세요.

### 2.2 사용자 결정 사항 (기본값으로 구현됨 — 바꾸려면 알려주세요)
| # | 질문 | 구현된 기본값 | 바꾸는 방법 |
|---|---|---|---|
| 1 | 미국 저녁 메시지를 매일 보낼까? | **A** 매일, 변화 없으면 짧게(약 750~900자). 4주 뒤 '변화 없음' 비율을 보고 재결정 | 타이머 `quant-digest-us.timer` |
| 2 | 원장 성적 줄을 누구에게? | **A** 구독자 전원 매일 1줄 | `QUANT_BOT_LEDGER_LINE=on\|owner\|off` |
| 3 | 07-13 리스트 소급? | **A** 소급 + `소급 포함` 표시 (스크립트 기본은 미리보기, `--confirm` 해야 반영) | `scripts/ledger.py backfill` |
| 4 | 미국 금융업 점수 | **A** 맞지 않는 지표 50% 중립 → 미국 금융주는 자동 후보에서 거의 빠짐(최고 67점), `/stock` 조회는 그대로 | 금융 전용 점수표는 다음 라운드(B) |
| 5 | LLM 한 줄 요약 | **A** 코드 보존·기본 OFF | `QUANT_BOT_LLM=1` (+ `MINIMAX_MODEL`) |
| 6 | 서버 의존성 | **B** 이번에 테스트한 `requirements.lock` 을 서버에 설치. A(서버 현재 `pip freeze` 를 잠금으로)를 원하면 freeze 결과 필요 | `DEPLOYMENT.md §6.1` |
| 7 | 조용한 날 알림음 | **B** 항상 소리 | `QUANT_BOT_DIGEST_SILENT_QUIET=1` 이면 무음 |
| 8 | 선정 후 하락 1회 알림 기준 | **A** −10% | `QUANT_BOT_PICK_DD_ALERT_PCT` (−15 등, 0 이면 끔) |
| 9 | 원장이 '성적 미달'(demoted) 판정되면 | **A** 후보 섹션 제목만 '검증 미달' 로, 종목은 그대로 — ⚠️ **아직 미구현**(판정은 ≈6개월 뒤부터 가능; 현재 demoted 는 성과 줄의 🔻 표기만). 첫 판정 전에 구현 필요 | `src/digest.py render_candidates` |
| 10 | 상용화 로드맵(셀프 가입·약관·결제) | **A** 신호가 검증될 때까지 **보류** (`ROADMAP.md §1`) | — |

### 2.3 환경변수 (모두 선택, 기본값이 권장)
`QUANT_BOT_LLM`(0) · `QUANT_BOT_DIGEST_PREDICTIONS`(0) · `QUANT_BOT_DIGEST_WEIGHTS`(0) · `QUANT_BOT_UNIVERSE_WARN_DAYS`(8) · `QUANT_BOT_UNIVERSE_HOLD_DAYS`(15) ·
`QUANT_BOT_LEDGER_LINE`(on) · `QUANT_BOT_DIGEST_SILENT_QUIET`(0) · `QUANT_BOT_PICK_DD_ALERT_PCT`(−10) · 기존: `QUANT_BOT_CACHE` · `QUANT_BOT_DB_PATH` · `QUANT_BOT_SYNC_TIMEOUT` · `QUANT_BOT_OFFLINE` · `QUANT_BOT_LOG_*`.
설정 오타는 기본값으로 폴백(+경고 로그)하므로 발송이 막히지 않습니다.

### 2.4 알려진 한계 (의도적으로 남김)
- **재점수 공백**: DB 점수는 07-10 이후 갱신되지 않았을 가능성이 높음(서버에 재점수 타이머가 없었음) → 배포 전에는 후보 보류일(≥15일)이 이어질 수 있음. 첫 재점수 전까지 `/stock`·후보는 7월 연간 기준 점수.
- US 미확인 225행(오늘 스크리너에 없는데 DB 에 남은 행) 원인 미규명 — 자동 제외하지 않고 `seen_at` 기록 + WARNING 만.
- 영구우선주(STRC 류)처럼 이름·형제 규칙이 못 잡는 증권은 후보에 섞일 수 있음 (오프라인 `--reclassify` 는 이름·접미사·셸만 적용; 형제·OTC·거래대금 규칙은 첫 재점수 때). isin/cusip 기반 분류는 ROADMAP.
- US 금융업은 사실상 자동 후보에서 제외(결정 #4). KR 업종 분류는 KRX 소속부 수준, 거래대금은 하루치.
- FMP 플랜 한도 미확인 — 전체 재점수가 주 ~4,000콜 안팎. bulk 엔드포인트는 402.
- **Turso API 캐시 비대(OPS-06) 미해결** — 캐시 쓰기가 원격 왕복이라 `QUANT_BOT_CACHE=off` 로 재점수. 서버 측정이 선행되어야 로컬 분리 설계 가능.
- KRX 일별 데이터의 아침 공개 시각 미확인 → KR 아침 메시지의 시세 기준일이 전일/전전일일 수 있음(기준 줄에 날짜가 그대로 나옴).
- 원장 판정은 **약 6개월 뒤부터** 의미 있음(블록 6개). 그 전의 숫자는 흔들리는 표본 1개 리스트 — 그래서 `검증 전` 표시를 뗄 수 없음.
- 선행지표 예측·제안 비중은 코드만 남음(플래그). 검증 통과 전에는 켜지 말 것.

### 2.5 작업 리듬 (사용자 합의)
Phase 단위로 끝낼 때마다 그 부분 리뷰. scoring/enrich/fetch 등 '정확성 직결' 코드는 작성 직후 한 번 더 검토 + 실데이터 스모크 (`CLAUDE.md §4.10`).
libsql/Turso·실 API 형태·마이그레이션은 단위 테스트로 못 잡으므로 해당 코드를 건드리면 격리 스모크(임시 DB 사본, `TURSO_*`·`TELEGRAM_*` 비움) 필수.

---

## 3. 폴더 구조 (요약)

```
AI-Investment-Bot/
├── CLAUDE.md, README.md, docs/{CURRENT_STATE,ROADMAP,DEPLOYMENT}.md   # 필독 5개
├── pyproject.toml, requirements.lock                  # pip install -e ".[dev,hosting]" / 고정 버전 세트
├── .env (gitignore)                                   # API 키 전부
├── src/                                               # §4 참조
├── scripts/                                           # 실행 진입점 (아래)
├── deploy/systemd/                                    # 서버 유닛·타이머 원본 (11개)
├── dashboard/index.html, *_data.json                  # GitHub Pages 대시보드
├── tests/                                             # 오프라인 1,057개 — python -m pytest (~35s)
├── data/ (gitignore, 캐시·유니버스 DB), logs/, notebooks/
└── .github/workflows/{ci,dashboard-export,daily-digest}.yml
```

**주요 스크립트**: `send_digest.py`(`--dry-run` = 쓰기 0) · `bot.py`(상시 봇) · `build_universe.py`(`--discover/--enrich/--force/--reclassify/--kr-refresh/--markets`) ·
`ledger.py`(`snapshot/backfill/score/report/ic`) · `check_backtest.py`(검증 리포트) · `scan.py`(오프라인 발굴, `--check 티커`) · `export_dashboard.py` ·
`notify_owner.py`(소유자 DM) · `server_autopull.sh`(CI 게이트 배포) · `check_*.py`/`daily_update.py`(수동 점검, 상태 쓰기 없음) · `telegram_setup.py`·`turso_setup.py`(1회성).

---

## 4. 모듈 API 빠른 참조

> 책임 분리 원칙은 `CLAUDE.md §4.4`. 여기엔 함수 시그니처만 (코드 안 읽고 호출 지점 찾기용).

### 예외 계층 (`src/exceptions.py`)
```
QuantBotError
├── ConfigError → MissingApiKeyError(key_name)
├── DataFetchError(source)
│   ├── ApiHttpError(status_code) → ApiAuthError(401) / ApiAuthorizationError(403) / RateLimitError(429) / ModelRetiredError(404·410)
│   ├── ApiTimeoutError / ApiConnectionError / DataValidationError
└── AnalysisError → InsufficientDataError(n_points, required)
```

### `src/data_fetcher.py` — 모든 외부 API (`QUANT_BOT_CACHE` 제어, 새 fetch 추가 후 `grep -n "@cached"` 로 매핑 확인)
| 함수 | 소스 |
|---|---|
| `fetch_prices` / `fetch_fundamentals` / `fetch_financials_yf` / `fetch_close_panel(tickers, start, end=None)`(캐시 없음, 원장 채점용) | yfinance |
| `fetch_macro(series_id, start=None)` / `fetch_macro_dashboard` / `fetch_korea_trade` | FRED REST (`_fred_get`, 상태코드별 예외) |
| `fetch_crypto` / `fetch_crypto_top` | CoinGecko |
| `fetch_quote` / `fetch_profile` / `fetch_company_screener` / `fetch_stock_news` | FMP |
| `fetch_financial_statements` / `fetch_key_metrics` / `fetch_ratios` / `fetch_key_metrics_ttm` / `fetch_ratios_ttm` | FMP |
| `fetch_krx_daily` / `fetch_krx_base_info` | KRX (AUTH_KEY 헤더) |
| `fetch_dart_corp_codes` / `fetch_dart_financials(corp_code, year)` | DART (`fnlttSinglAcntAll`, 지배주주 귀속 dict) |
| `fetch_wikipedia_pageviews` | Wikimedia |

상수: `FMP_BASE_URL=".../stable"`(v3 아님), `FRED_SERIES`, `KOREA_TRADE_SERIES`, `KOREA_EXPORTS_LABEL`.

### 신호·유니버스·원장
- **`signals.py`**: 순수 `factor_scores`/`apply_screen_rules`/`asset_observations`/`drawdown_transitions`/`vol_ratio_transitions`/`credit_transition`/`fx_transition`/`shock_transitions`/`regime_view` +
  `generate_signal_report(tickers=..., screen_tickers=None, market="US", *, include_snapshot=None, today_kst=None) -> SignalReport` (**상태 쓰기 0**) · `commit_signal_state(report) -> bool` (발송 성공 후).
  `SignalReport(generated_at, regime_label, factors, candidates, alerts, first_run, regime: RegimeView|None, kr: KrContext|None, active, cleared, data_issues, baseline_missing, pending_state)`.
- **`macro_analyzer.py`**: `classify_regime() -> RegimeReport`(`available/total/stale/outcomes`, `complete`, `reason_line()`) · `fetch_panel_with_failures` · `current_drawdown` · `sharpe_ratio` · `correlation_matrix`. FRED 일시 장애는 `fetch_macro.stale(...)` 로 최대 7일 폴백(설정·ID 오류는 가리지 않음).
- **`screener.py`**: `latest_fundamentals(ticker, *, basis="ttm") -> dict`(`_basis` 포함) · `health_scorecard(metrics, *, is_fin=False)` · `value_scorecard(quote, metrics, *, is_fin=None)` · `is_balance_sheet_financial(sector, industry)` · `ScoreCard`/`Component`.
- **`universe.py`**: `discover` / `enrich`(US, 배치) / `enrich_kr`(DART) / `reclassify_us` / `rescore_kr_offline` / `refresh_kr_prices` ·
  `scan(...)`(excluded 제외) · `lookup(symbol, market=None)`(제외 행도 반환) · `lookup_markets` · `lookup_detail` ·
  `pick_top(market="US", n=6, *, max_per_sector=2, max_holding=2, min_total=0) -> list[ScanRow]` · `top_symbols` · `is_holding_row(row)` · `asof_status(fund_asof)` · `freshness(market) -> {"fund_asof","price_asof","age_days"}` ·
  `yf_symbol(symbol, market, exchange=None, sector="")` · `classify_us_securities(rows)` · `RULE_VERSION="v1"` · `stats()`.
  `ScanRow(symbol, market, name, sector, price, market_cap, total_score, value_score, health_score, roe, per=None, pbr=None, exchange=None, excluded=None, flags=(), fund_asof=None, price_asof=None)`.
- **`ledger.py`**: `record_send(market, sent_date, picks, *, rule_version, source="live", sent=None, failed=0, degraded=False, score_asof=None, text=None, yf_symbol_fn=None, ...) -> bool` ·
  `score(markets=...)` · `load_summary(market)` · `ledger_gate(summary)` · `format_ledger_line` · `format_weekly_block` · `format_report` · `report_text(market)` · `last_sent_list` · `inclusion_start` · `entry_prices` ·
  `snapshot_universe(source)` · `ensure_pre_snapshot()` · `snapshot_reconstructed()` · `backfill(confirm=False)` · `universe_ic()`.
- **`health.py`**: `HealthReport(market, started_at, storage, universe_as_of, price_as_of, regime_ok, regime_total, regime_failures, llm, optional_failures, notes, state_write_ok, sent, failed, recipients, ...)` ·
  `.problems(now)` · `.status_line(now)` · `.owner_text(now, held)` · `.is_hold(universe_count)` · `.signature(now)` · `.needs_owner_dm(now, prev_state, held)` · `.to_state()` — `optional_failures` 는 **고정 키만**(개수·종목은 `notes`).
- **`digest.py`**: `assemble_daily_digest(market="us", *, now=None, top_n=6) -> DigestBundle`(쓰기 0) · `send_daily_digest(market, ...) -> {"sent","failed","recipients","exit_code"}` · `build_daily_digest` ·
  순수 `decide_actions` / `diff_lists` / `render_*` / `price_metrics_from_frame` / `entry_base_from_frame` / `since_pct_for`. 종료코드 `EXIT_OK=0`·`PARTIAL_FAIL=10`·`ALL_FAIL=11`·`NO_RECIPIENTS=12`·`HOLD_NOTIFIED=20`·`HOLD_DM_FAILED=21`·`CONFIG_MISSING=30`.
- **`backtest.py`**: `run_backtest` / `walk_forward_topn` / `walk_forward_lead_lag` / `judge_lead_lag` / `excess_significance` / `block_bootstrap_ci` / `sign_test_paired` / `holm_adjust`. 판정 상수 `LL_MIN_OOS=60, LL_MIN_DISAGREE=10, LL_MIN_EDGE_PP=3.0, LL_ALPHA=0.05`.
- **`predictors.py`**: `analyze_lead_lag`(순수) + `predict_*`(fetch) · `PREDICTOR_SPECS`(`PredictorSpec`, 7개) · `PREDICTORS`(research_only 제외 운영 레지스트리).

### 인프라·표면
- **`storage.py`**: `Storage`(`@cached`, `put_state/get_state(ns, key, strict=False)`, `init_sync`, `degraded`, `require_remote`, `sync() -> str`, `*_any_age`) · `add_column_if_missing` · 공개 `DB_ERRORS`.
- **`http.py`**: `get_http_session()`(retry/timeout) · `mask_secrets()` · `SecretMaskingFilter` · `is_timeout(exc)`.
- **`notifier.py`**: `send_telegram(text, chat_id=None, parse_mode="Markdown", ..., disable_notification=False)` · `send_safe(text, chat_id=None, parse_mode="Markdown", reply_markup=None, ...) -> bool`(400 시 `strip_md` 평문 재전송) · `fallback_count()` / `reset_fallback_count()`.
  소유자 DM 은 `parse_mode=None`.
- **`bot_commands.py`**: `parse_command` · `handle_stock(arg, arg2=None)` · `handle_scan(arg, arg2=None)` · `handle_ledger(arg, chat_id)` · `handle_news` · `format_stock` / `format_scan`. 라벨·면책은 `findings.py` 에서만 import.
- **`findings.py`**: `Finding` + `LABEL_VALUE`/`LABEL_HEALTH`/`LABEL_CANDIDATES`/`DISCLAIMER`/`score_labels(market)`.
- **`utils.py`**: `close_series`(Adj Close/Close fallback) · `pick_first` · `clip` · `md_safe` · `strip_md` · `TRADING_DAYS_PER_YEAR`.

---

## 5. 완료 이력 (요약 — 상세는 `git log`)

| 단계/Phase | 요약 | 상태 |
|---|---|---|
| 리팩토링 1–8, Phase 4–10 | 로깅→예외→HTTP→DRY→패키지화→테스트→결정론→API정합성 / Storage·신호·예측·Telegram·유니버스·KRX/DART·Turso | ✅ (~06-15) |
| Phase 11a/11b | 소유자 승인제 멀티유저 + 상시 인터랙티브 봇 + 서버 배포·자동 배포 | ✅ (06-22~30) |
| Phase 12 | 대시보드(GitHub Pages) | ✅ (06-23) — export 는 현재 중단, 수리분 브랜치에 포함 |
| 07-05 개선 브랜치 | 백로그 일괄·백테스트 프레임워크·인라인 승인버튼·`/news`·LLM 요약·Phase 13 포트폴리오 레이어·전수 버그헌트 + Turso 행 사고 수정 | ✅ (07-13 main 병합) |
| **신호 품질·검증 라운드** | 점수 하나로 통일·원장·검증 프로토콜·데이터 정확성·알림/국면/FRED 재설계·헬스·CI 게이트 배포·다이제스트 v2·표면 정합 (§1) | ✅ 구현 완료 — **검토·배포 대기** (`fix/signal-quality-round`) |

> 한계 기록: FMP 엔 실제 KOSPI/KOSDAQ 없음(→ KRX/DART). 크립토 점수는 주식과 비교 불가(시장별 분리). 봇/다이제스트/재점수는 **서로 다른 libsql 레플리카 파일** 필수(WAL 충돌, `CLAUDE.md §4.10 (9)`).

---

## 6. 환경 셋업 (새 머신/새 에이전트)

```
cd /Users/deepblue/Developer/AI-Investment-Bot
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev,hosting]"
cp .env.example .env
.venv/bin/python -m pytest
.venv/bin/python scripts/send_digest.py --market kr --dry-run
```
- `.env` 에 `FRED_API_KEY`, `FMP_API_KEY`, (KRX/DART/TELEGRAM/TURSO) 채우기. 운영 키로 `--dry-run` 을 돌려도 텔레그램·상태 쓰기는 없지만, **운영 Turso 에 붙으므로** 격리하려면 `TURSO_DATABASE_URL= TURSO_AUTH_TOKEN= TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= QUANT_BOT_DB_PATH=/tmp/x.db` 를 앞에 붙일 것(`CLAUDE.md §4.10 (1)`).
- 저장소 `.venv` 는 머신 종속입니다(다른 머신 경로로 만든 venv 는 실행 불가) — 새 머신에서는 새로 만들기.
- 로컬 `.claude/settings.local.json` 에 `git push`·실발송 명령이 무확인 허용돼 있으면 지우는 것을 권장.

---

## 7. Git

- Default branch `main`. Remote `origin = https://github.com/Zonnx999/AI-Investment-Bot.git` (public).
- push 여부 확인: `git fetch` 후 `git rev-list --count origin/main..main` (`CLAUDE.md §4.10 (12)`). 현재 `origin/main` = `6403d60`, `origin/fix/signal-quality-round` 없음.
- 커밋 메시지는 사용자 지시가 있을 때만. `main` push = 서버 자동 배포이므로 push 는 사용자 명시 승인 후에만.
