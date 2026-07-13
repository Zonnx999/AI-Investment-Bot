# Roadmap

> 현재 상태·완료 상세는 `CURRENT_STATE.md` + `git log`. 이 파일은 **앞으로 할 일**에 집중합니다.
> 작업 규칙(phase-gate·리뷰 톤·결정 시 옵션 제시)은 `CLAUDE.md §4`.

**권장 순서**: Phase 4–13 ✅ (봇·대시보드 라이브 + 포트폴리오 레이어 — 07-05 개선 브랜치, 라이브 스모크 후 머지 07-13) → **상용화 전환 Phase A→E (아래 §1)**.
운영 런북 `docs/DEPLOYMENT.md` · 대시보드 https://zonnx999.github.io/AI-Investment-Bot/ · 수익화 전략 전문 `MARKETING.md`(현재 별도 브랜치, main 편입 예정).

---

## 1. 다음 작업 (액티브) — 상용화 전환: 개인 툴 → 투자 리서치 플랫폼

> **방향 전환 (2026-06-30 합의).** 개인용을 넘어 **개인 투자자용 AI 투자 리서치 플랫폼(SaaS)** 으로 확장.
> 포지셔닝은 "종목 찍어주는 봇"이 아니라 **리서치 플랫폼** — 전 구독자 동일·결정론적·설명가능·비개인화가 신뢰이자
> 법적 안전선(수익화 §6). 시장·가격·법무 근거는 `MARKETING.md`(별도 브랜치) 참조.

**North Star = 30일 리텐션 > 40%.** "매출"이 아니라 "30일 뒤에도 봇을 여는가"가 PMF 신호.
**결제·티어는 리텐션 >40% 검증 *후에만*** 붙인다 (리텐션 전 결제 도입 = 대표적 실수, 획득비만 태움).
시퀀스: **획득(A·B·D) → 리텐션 측정(C) → 인터뷰 → 결제(E)**.

> (07-13 머지) 개선 브랜치의 Phase 13 포트폴리오 레이어·11b 잔여(인라인 버튼·`/news`)·LLM 요약·백테스트는
> 전부 완료되어 §3 완료 이력으로 이동. 머지 게이트 라이브 스모크 7항목은 `CURRENT_STATE.md` 인계 참조.

> ℹ️ **인라인 `[승인][거절]` 버튼** — 06-30 에 DROP 결정했으나 개선 브랜치(07-05)에서 이미 구현·라이브 검증됨(07-13).
> DROP 의 논리(사람 승인은 확장의 병목 — 셀프서비스 가입으로 병목 자체를 제거)는 Phase A 에 그대로 유효하며,
> 이때 **`callback_query` 처리 인프라(구현 완료)를 약관 동의 "[동의합니다]" 버튼에 재사용**한다.

### Phase A — 가입 모델 전환 + 약관/고지 (획득의 전제) ← 다음 1순위
오너 수동 승인 → **셀프서비스 가입**. 가입과 약관 동의는 **법적으로 분리 불가 → 한 묶음**(수익화 §6).
- [ ] **가입 모델 = 옵션 B(토글)**: `BOT_OPEN_SIGNUP` 플래그(`src/config.py`).
      `false`(기본=현 베타)=소유자 승인 유지 / `true`=`/start`→약관 동의→자동 `active`.
      코드 재배포 없이 플래그로 비공개↔공개 전환. 상태모델(pending/active/inactive) 그대로, 오픈 모드는 pending 건너뜀.
- [ ] **약관/고지 동의 단계**: `/start` 에 disclaimer + 동의 게이트(`callback_query` "[동의합니다]" 버튼, 폴백 `/agree`).
      동의 전엔 active 안 됨. → `src/subscribers.py` · `scripts/bot.py` · `src/notifier.py`(callback answer/edit API).
- [ ] **고지 문구 상시 노출**: 다이제스트·`/stock` 응답 하단 한 줄("투자 자문 아님, 모든 결정은 본인 책임"). `src/digest.py` · `bot_commands`.
- [ ] **스키마 선반영**(`storage.add_column_if_missing`): `tier`(기본 'free') · `terms_accepted_at` · (후일)`expiry_date`.
      지금 넣어도 비용 0, 나중 결제 도입 시 Turso 마이그레이션 충돌 회피(§4.10 #9).

### Phase B — 설명가능성 자연어 레이어 (킬러 피처 · 신뢰)
점수의 "왜"를 자연어로. **신규 API 0콜 · LLM 0** — 기존 `detail` JSON 재사용 + 결정론적 규칙.
- [ ] `format_stock()`(`src/bot_commands.py`) 재작성: 숫자 분해 → "✅수익성 탁월(ROIC156%) · ✅밸류 매력적 · ⚠️배당 거의 없음" 식 사유.
      규칙 예: GP>40%→"수익성 탁월", NetDebt/EBITDA<1→"레버리지 양호", earningsYield<0→"적자/밸류 경고".
- [ ] 동일 규칙을 §5 마케팅 콘텐츠(종목 분석 글)로 재활용 — 신뢰 쇼케이스.
- 결정성·설명가능·비개인화를 동시 충족 → 법적 안전선 유지.

### Phase C — 활동 로깅 (리텐션 측정 시작 — 지금부터 쌓아야 함)
North Star 측정 인프라. **데이터는 소급 백필 불가** → A·B 와 함께 일찍 넣는다.
- [ ] `last_active` 컬럼 + 일별 오픈/명령 이벤트(`src/subscribers.py`, `add_column_if_missing`).
- [ ] 30일 코호트 집계 스크립트(`scripts/`) — 리텐션 % 산출.

### Phase D — 획득 (랜딩 + 무료 콘텐츠)
- [ ] 정적 랜딩(`dashboard/landing.html`) — 공유·홍보용 URL + "무료 시작"(텔레그램 딥링크).
- [ ] 교육 콘텐츠 파이프라인(주간 국면 리포트 등) — 신뢰 = 획득 엔진(수익화 §5).
- [ ] (선택) 워치리스트(`src/watchlists.py`) — "추적·정보 도구"(비개인화, 무료) = 매일 돌아올 이유.

### Phase E — 결제·티어 (리텐션 >40% 검증 후에만)
- [ ] `tier` 분기 + `/upgrade` + 티어별 기능 게이팅(무료/Basic/Pro).
- [ ] Toss Payments + Cloudflare Worker(웹훅 분리 — Oracle OOM 회피) + `expiry_date` 만료 관리.
- [ ] 통신판매업 신고·사업자등록·부가세(수익화 §6). 개인화 기능 도입 전 **한국 변호사 자문 필수**.

> 완료된 Phase 11a(승인제 멀티유저)·11b(인터랙티브 봇)·12(대시보드)는 `CURRENT_STATE.md §5` 참조.

---

## 2. 부록 — 선택 기능 / 백로그

### 2.1 다음 후보 기능
- **봇 명령 확장** — `/regime`·`/predict`·`/me`(내 상태) 즉답(국면/예측은 라이브 fetch라 봇에 다소 무거움 — Phase A 이후로 미룸).
  `/news <티커>` 는 ✅ 완료 (07-05 브랜치 — FMP 뉴스, 30분 캐시 + rate limit, 평문 포맷. Haiku 센티먼트는 미포함).
- ✅ **LLM 요약 한 줄** — 구현 완료 (07-05, 브랜치): `src/llm.py`(summarize/summarize_safe/킬스위치 `QUANT_BOT_LLM`),
  합의 설계 그대로 — 표현 레이어만, 실패 시 요약 생략 폴백, `--no-llm` 플래그.
  모델 id 라이브 스모크 완료 (07-11): 기본 `minimaxai/minimax-m2.7` (m2 는 NIM 퇴역, m3 는 타임아웃 초과).
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
| Phase 11a 멀티유저 브로드캐스트 (소유자 승인제) | ✅ |
| Phase 11b 실시간 인터랙티브 봇 (`/stock`·`/scan`·`/announce`·인라인 승인버튼·`/news`) | ✅ |
| Phase 12 대시보드 통합 (GitHub Pages 라이브) | ✅ |
| Phase 13 포트폴리오 레이어 (Finding·사이징 엔진·다이제스트 제안 비중·All Weather 검증) | ✅ |

---

## 4. 사용자 작업 스타일 (요약 — 전문은 `CLAUDE.md §4`)
- **Strict phase-gate**: "다음 단계로" 명시 승인 전까지 다음 단계 코드 미리 짜지 말 것.
- **시니어 엔지니어 리뷰 톤**: 의심스러운 코드는 비판적으로 짚고 대안 제시.
- **버그 발견 시 즉시 fix 는 OK** (phase-gate 예외) — 단 새 기능 추가가 아니라 기존 버그 수정일 때.
- **결정 사항은 옵션(A/B/C) 제시 후 명시적 승인 요청.** 한국어 응답.
