# Roadmap

> 현재 상태·완료 상세는 `CURRENT_STATE.md` + `git log`. 이 파일은 **앞으로 할 일**에 집중합니다.
> 작업 규칙(phase-gate·리뷰 톤·결정 시 옵션 제시)은 `CLAUDE.md §4`.

**권장 순서**: Phase 4–12 ✅ + 11b 잔여·LLM 요약·백테스트·§2.2 백로그 ✅ (07-05 개선 브랜치)
→ **① 브랜치 라이브 스모크·머지** (`CURRENT_STATE.md` 인계 체크리스트) → **② Phase 13 포트폴리오 레이어** (§1.1) → 선택 백로그(§2).
운영 런북은 `docs/DEPLOYMENT.md`. 대시보드: https://zonnx999.github.io/AI-Investment-Bot/

---

## 1. 다음 작업 (액티브)

### 1.0 브랜치 머지 게이트 — 라이브 스모크 (사용자 작업, 최우선)
개선 브랜치(`claude/project-improvement-exploration-cf70v4`)는 오프라인 세션 산출물 —
머지 전 `CURRENT_STATE.md` "인계 (2026-07-05)" 체크리스트 5개 항목(FMP 뉴스 엔드포인트,
MiniMax 모델 id, 인라인 버튼 실 텔레그램, 실 Turso 스모크, DB 재점수) 필수.

### 1.1 Phase 13 — 포트폴리오 레이어 (다음 구현 대상)
> 아키텍처 리뷰(2026-07-06, §2.3) 채택 항목. 지금까지는 "이 종목 살만한가?"에 답했다면,
> 이제 "**얼마나** 살 것인가 — 이미 들고 있는 것과의 상관·리스크를 감안해"에 답한다.
> All Weather(자산군 분산 검증) + 소프(파산 회피 사이징) 철학의 실행 단계.
> 전제조건인 백테스트 프레임워크(`src/backtest.py`)는 완료 — 사이징 규칙을 주장이 아니라 검증으로.

- [ ] **13a. 구조화 리서치 결과** — 다이제스트/대시보드가 소비하는 공통 결과 dataclass
      (`score, summary, evidence, confidence` 꼴). 국면·팩터·예측 섹션이 각자 dict 를 주는
      현 구조를 한 shape 로 통일 (digest/export_dashboard 2개 파일 리팩토링 수준, LLM 무관).
- [ ] **13b. 포지션 사이징 엔진** (`src/portfolio.py`, 순수 함수) — 입력: 후보 종목 점수 +
      가격 시계열 + 현재 보유(선택). 출력: 목표 비중. 규칙 후보: 변동성 역가중(risk parity 유사)
      / 상관 페널티(기존 보유와 상관 높으면 감액) / Kelly fraction 상한(soft cap). 음수·0·결측
      가드는 §4.10 #5 사상. 백테스트로 각 규칙의 MDD·Sharpe 를 동일가중 대비 검증 후 채택.
- [ ] **13c. 다이제스트 연동** — 발굴 종목에 "제안 비중" 열 추가 (정보 제공용 — 주문 집행 없음).
- [ ] (선택) 13d. All Weather 자산군 비중 검증 — 자산군 레벨 risk parity 를 backtest 로 리플레이.

### Phase 11a — 멀티유저 브로드캐스트 — ✅ 완료 (소유자 승인제, 배포·가동 중)
`src/subscribers.py`. `subscribers` 테이블(status pending/active/inactive), getUpdates+offset, `/start`·`/stop`·
소유자 `/approve`·`/deny`·`/pending`·`/subscribers`. 친구 승인·수신 실동작 확인.

### Phase 11b — 실시간 인터랙티브 봇 — ✅ 대부분 완료 (배포·가동 중), 일부 잔여
`src/bot_commands.py` + `scripts/bot.py`(폴링 워커, systemd `quant-bot`). `docs/DEPLOYMENT.md`.
- [x] 폴링 워커(getUpdates long-poll) — Oracle Always Free(E2.1.Micro). 무거운 분석은 사전계산, 봇은 DB 읽기 위주
- [x] `/stock <티커>`(점수+근거, `lookup_detail`) · `/scan [us|kr]` · `/help`·`/menu` · 유저별 rate limit
- [x] 조회는 **active 구독자(+소유자)만** (게이팅), reply 키보드 버튼(타이핑↓), Markdown 평문 폴백
- [x] **`/announce` 소유자 공지** — active 구독자 전원에 평문 브로드캐스트(업데이트·정정 알림). 소유자 게이트 (06-29)
- [x] **다이제스트 UX** — 회사명·점수 범례·시각적 위계·예측 가독성 (06-29)
- [x] **인라인 `[승인][거절]` 버튼** — callback_query 처리, 소유자 게이트, 이중탭 멱등, 처리 후 버튼 제거 (07-05, 브랜치. ⚠️ 실 텔레그램 스모크 후 배포)
- [x] `/news <티커>` — FMP `/news/stock` 30분 캐시 + rate limit, 평문 포맷 (07-05, 브랜치. ⚠️ 엔드포인트·필드 라이브 스모크 필요. Haiku 센티먼트는 미포함)
- [ ] (선택) `/regime`·`/predict`·`/me`(내 상태) 즉답 — 단 국면/예측은 라이브 fetch라 봇에 약간 무거움

### Phase 12 — 대시보드 통합 ✅ (2026-06-23, Pages 라이브 06-29)
`dashboard/index.html` 재작성 + `scripts/export_dashboard.py` 신규.
- [x] 탭: 🇰🇷 한국 / 🇺🇸 미국 / ₿ 크립토 / 📊 시장 국면 / 📈 선행지표 (아시아 탭 제거)
- [x] Turso DB → 시장별 별도 JSON (kr/us/crypto_data.json) lazy load
- [x] sort/filter 구현, ₩ 가격 표기, 범례
- [x] 시장 국면 탭 (regime_data.json), 선행지표 탭 (predictions_data.json)
- [x] CI: `.github/workflows/dashboard-export.yml` (매일 09:30 KST 커밋·푸시)
- [x] **Actions 기반 Pages 배포** — `dashboard-export.yml` 에 configure/upload/deploy-pages 추가 (06-29)
- [ ] **사용자 작업 대기 (1회)**: GitHub Settings → Pages → Source = "GitHub Actions"
      (⚠️ branch 배포는 `/`·`/docs` 만 → `/dashboard` 서빙 불가, 그래서 Actions 방식)

---

## 2. 부록 — 선택 기능 / 백로그

### 2.1 다음 후보 기능
- ✅ **LLM 요약 한 줄** — 구현 완료 (07-05, 브랜치): `src/llm.py`(summarize/summarize_safe/킬스위치 `QUANT_BOT_LLM`),
  합의 설계 그대로 — 표현 레이어만, 실패 시 요약 생략 폴백, `--no-llm` 플래그.
  ⚠️ 활성화 전 모델 id(기본 `minimaxai/minimax-m2`)·응답 스키마 라이브 스모크 필수(§4.10 #3, `.env.example` 참조).
- ✅ **백테스트 프레임워크** — 구현 완료 (07-05, 브랜치): `src/backtest.py`(순수 엔진: run_backtest/walk_forward_topn/
  evaluate_lead_lag_oos) + `scripts/check_backtest.py`(실데이터 대시보드). 예측 R² 우려는 OOS 방향 적중률로 정량화.
- **지표 확장** (구 UPGRADE_PLAN P2) — VIX/DXY → `classify_regime` 보강, earnings revision, short interest 알림
- **포트폴리오 최적화 엔진** (구 P3) — 자산군 비중 산출 (All Weather 비중 검증과 연결)
- **알파 신호** (구 P4) — insider trading 알림, earnings-call NLP(LLM), 한국 BOK ECOS API(`ECOS_API_KEY`)
- **미구현(의존성 대기)**: Google Trends(pytrends, 불안정), SEC EDGAR 13F(파싱·45일 지연)
- **Streamlit** — Phase 12 대시보드로 대체됨, skip

### 2.2 코드 개선 backlog — ✅ 대부분 완료 (07-05, 브랜치)
- [x] **점수 정확성**: KR PBR 공식 완화(`_kr_pbr_points`: ≤0.5 만점, 4.5에서 0점 선형), 빈 fundamentals skip
  (`has_fundamentals`, scan 랭킹 제외 — **DB 재점수 필요**), 음수 earningsYield→PER `None`(signals + check_fundamentals)
- [x] **DRY/구조**: `_clip`→`utils.clip`, vol 단일화(`annualized_vol_pct`+`FactorScores.vol_pct`),
  `subscribers` 공개 API(subscriber_status/get·set_updates_offset)+스키마 init 메모이즈(메시지당 Turso 왕복 제거),
  `run()→NoReturn`, `symbols_needing_enrichment(market)`. `_MARKET_LABEL` 은 digest 단독 사용이라 이동 안 함
- [x] **견고성**: Markdown 폴백 `status_code==400` 기반, `drawdown_alerts` 단일 호출
- [ ] **잔여**: KR 배당 DART 연동(현재 0 고정 — DART API 필드 라이브 검증 필요해 보류)

### 2.3 아키텍처 리뷰 트리아지 (2026-07-06 — 외부 AI 아키텍트 리뷰 15개 항목)
> 재논의 방지용 결정 기록. 원칙: **1인 사용자·무료 티어 박스·오프라인 테스트**라는 실제 제약
> 기준으로 판정. 추상적으로 옳아도 이 프로젝트 규모에 비용>효익이면 기각.

**채택 (2건)**
- **포트폴리오 레이어** → Phase 13 (§1.1). 유일한 진짜 공백 — 기존 §2.1 후보와도 일치.
- **구조화 리서치 결과** → 13a. 다이제스트·대시보드가 소비하는 공통 dataclass — 저비용 고효익.

**이미 반영됨 (해당 없음)**
- 결정론/LLM 분리(창립 원칙), 불변 결과 객체(ScoreCard·FactorScores·BacktestResult),
  플러그인 레지스트리(PREDICTORS), 레이어 경계(§4.4 순수함수/오케스트레이터 분리).

**기각 (사유 기록)**
- **플랫폼 레이어링·전면 DI**: src ~5.5k 줄 1인 코드베이스에 간접층 2배 — 현 module 함수 +
  settings 싱글톤 + monkeypatch 로 테스트 13초면 충분. 테스트가 아파질 때 국소 채택.
- **AI 애널리스트 위원회**: Phase 3 말 비용·결정론 이유로 명시 폐기한 방향과 정면 충돌.
  '위원회'가 필요하면 **결정론 모델 N개 투표 + LLM 은 회의록만** 형태로.
- **이벤트 드리븐**: push 이벤트 소스가 없음(FRED 는 웹훅 없음) — 타이머 폴링에 이벤트 버스
  이름만 붙는 꼴. §4.10 #11 교훈대로 schedule-driven 유지. 실제 push 소스 생기면 재고.
- **피처 스토어**: @cached SQLite + 사전 enrich 유니버스 DB(스캔 API 0콜)가 이미 그 역할.
  별도 스토어는 제2의 진실원천 + Turso staleness 문제만 추가.
- **data_fetcher 분할**: '외부 호출 한 파일' 은 의도된 규칙(§4.4) — 데코레이터 오배치 사고를
  잡아낸 것도 grep 한 방이 가능했기 때문. ~1.2k 줄로 아직 임계 미달, 막히면 그때 패키지화.

---

## 3. 완료 이력 (요약 — 상세는 `CURRENT_STATE.md §5` + `git log`)

| 항목 | 상태 |
|---|---|
| 리팩토링 1–8단계 (로깅·예외·HTTP·DRY·패키지화·테스트·결정론·API정합성) | ✅ |
| Phase 4 Storage & Daily Pipeline | ✅ |
| Phase 5 Signal Engine (친구 C 봇의 진화 버전) | ✅ |
| Phase 6 Alternative Data & Predictive Models | ✅ |
| Phase 7 Telegram 알림 봇 + cron 다이제스트 | ✅ |
| Phase 8 전 종목 유니버스 DB + 오프라인 스캔 | ✅ |
| Phase 9 KRX 발굴 + DART 펀더멘털 점수 | ✅ |
| Phase 10 데이터 호스팅 (Turso/libSQL) | ✅ |

---

## 4. 사용자 작업 스타일 (요약 — 전문은 `CLAUDE.md §4`)
- **Strict phase-gate**: "다음 단계로" 명시 승인 전까지 다음 단계 코드 미리 짜지 말 것.
- **시니어 엔지니어 리뷰 톤**: 의심스러운 코드는 비판적으로 짚고 대안 제시.
- **버그 발견 시 즉시 fix 는 OK** (phase-gate 예외) — 단 새 기능 추가가 아니라 기존 버그 수정일 때.
- **결정 사항은 옵션(A/B/C) 제시 후 명시적 승인 요청.** 한국어 응답.
