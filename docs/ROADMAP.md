# Roadmap

> 현재 상태·완료 상세는 `CURRENT_STATE.md` + `git log`. 이 파일은 **앞으로 할 일**에 집중합니다.
> 작업 규칙(phase-gate·리뷰 톤·결정 시 옵션 제시)은 `CLAUDE.md §4`.

**권장 순서**: Phase 4–12 ✅ (봇·대시보드 라이브) → **상용화 전환 Phase A→E (아래 §1)**.
운영 런북 `docs/DEPLOYMENT.md` · 대시보드 https://zonnx999.github.io/AI-Investment-Bot/ · 수익화 전략 전문 `MARKETING.md`(현재 별도 브랜치, main 편입 예정).

---

## 1. 다음 작업 (액티브) — 상용화 전환: 개인 툴 → 투자 리서치 플랫폼

> **방향 전환 (2026-06-30 합의).** 개인용을 넘어 **개인 투자자용 AI 투자 리서치 플랫폼(SaaS)** 으로 확장.
> 포지셔닝은 "종목 찍어주는 봇"이 아니라 **리서치 플랫폼** — 전 구독자 동일·결정론적·설명가능·비개인화가 신뢰이자
> 법적 안전선(수익화 §6). 시장·가격·법무 근거는 `MARKETING.md`(별도 브랜치) 참조.

**North Star = 30일 리텐션 > 40%.** "매출"이 아니라 "30일 뒤에도 봇을 여는가"가 PMF 신호.
**결제·티어는 리텐션 >40% 검증 *후에만*** 붙인다 (리텐션 전 결제 도입 = 대표적 실수, 획득비만 태움).
시퀀스: **획득(A·B·D) → 리텐션 측정(C) → 인터뷰 → 결제(E)**.

> ❌ **DROP — 인라인 `[승인][거절]` 버튼** (구 11b 잔여 1순위였음). 사람(소유자) 승인은 확장의 *병목*이고,
> 버튼은 그 병목을 *빠르게* 만들 뿐 *없애지* 못함. 대신 셀프서비스 가입으로 병목 자체를 제거(Phase A).
> 단, 그 버튼이 쓰려던 **`callback_query` 처리 인프라는 약관 동의 "[동의합니다]" 버튼에 재사용**한다.

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
- **봇 명령 확장** — `/news <티커>`(`NEWS_API_KEY`/FMP 뉴스, 응답 캐시 + rate limit, 선택 Haiku 센티먼트) ·
  `/regime`·`/predict`·`/me`(내 상태) 즉답(국면/예측은 라이브 fetch라 봇에 다소 무거움). (구 Phase 11b 잔여 — Phase A 이후로 미룸)
- **LLM 요약 한 줄** — 다이제스트 맨 위 한 문단. 후보: **MiniMax-M3(NVIDIA API, `MINIMAX_API_KEY` 이미 .env 에 있음)** 또는 Haiku.
  방향만 합의(2026-06-29, 구현 대기): 결정론적 다이제스트 **위 표현 레이어만** — 숫자·티커 생성/수정 금지,
  API 실패/레이트리밋 시 **요약 생략 폴백**(다이제스트는 그대로 발송, §4.10 #10 사상), 하루 1회 호출.
  배선: `src/llm.py`(호출+폴백, http 세션 재사용) + `config` 에 키 로딩 + http 마스킹 등록. 구현 전 모델 id 검증(§4.10 #3).
- **백테스트 프레임워크** — Phase 5 신호 / Phase 6 예측의 과거 성과 검증 (예측 R² 우려 정량 해소)
- **지표 확장** (구 UPGRADE_PLAN P2) — VIX/DXY → `classify_regime` 보강, earnings revision, short interest 알림
- **포트폴리오 최적화 엔진** (구 P3) — 자산군 비중 산출 (All Weather 비중 검증과 연결)
- **알파 신호** (구 P4) — insider trading 알림, earnings-call NLP(LLM), 한국 BOK ECOS API(`ECOS_API_KEY`)
- **미구현(의존성 대기)**: Google Trends(pytrends, 불안정), SEC EDGAR 13F(파싱·45일 지연)
- **Streamlit** — Phase 12 대시보드로 대체됨, skip

### 2.2 코드 개선 backlog (출처: 코드리뷰 2026-06-23 — [BUG] 5건은 06-29 수정 완료)
- **점수 정확성**: KR PBR 공식 완화(중대형주에 가혹, PBR 2.0→0점), KR 배당 DART 연동(현재 0 고정),
  빈 fundamentals 는 0점 대신 skip(현재 누락=조용한 저점), 음수 earningsYield→PER `None` 명시
- **DRY/구조**: `_clip`·`_MARKET_LABEL` → `utils`, vol 계산 중복 → `FactorScores.vol_pct`,
  `subscribers` private 심볼 public API + 봇 conn 재사용(매 메시지 Turso 왕복↓), `run()→NoReturn`,
  `symbols_needing_enrichment(market)` 파라미터화
- **견고성**: Markdown 폴백을 `status_code==400` 기반으로(현 "parse" 문자열 매칭은 취약), `drawdown_alerts` 단일 호출 리팩토링

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
| Phase 11b 실시간 인터랙티브 봇 (`/stock`·`/scan`·`/announce`) | ✅ |
| Phase 12 대시보드 통합 (GitHub Pages 라이브) | ✅ |

---

## 4. 사용자 작업 스타일 (요약 — 전문은 `CLAUDE.md §4`)
- **Strict phase-gate**: "다음 단계로" 명시 승인 전까지 다음 단계 코드 미리 짜지 말 것.
- **시니어 엔지니어 리뷰 톤**: 의심스러운 코드는 비판적으로 짚고 대안 제시.
- **버그 발견 시 즉시 fix 는 OK** (phase-gate 예외) — 단 새 기능 추가가 아니라 기존 버그 수정일 때.
- **결정 사항은 옵션(A/B/C) 제시 후 명시적 승인 요청.** 한국어 응답.
