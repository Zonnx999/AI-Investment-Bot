# Roadmap

> 현재 상태·완료 상세는 `CURRENT_STATE.md` + `git log`. 이 파일은 **앞으로 할 일**에 집중합니다.
> 작업 규칙(phase-gate·리뷰 톤·결정 시 옵션 제시)은 `CLAUDE.md §4`.

**권장 순서**: Phase 4–12 ✅ (Oracle 서버 가동 중, 대시보드 라이브) → **11b 잔여(인라인 승인버튼·`/news`)** → 선택 백로그(§2).
운영 런북은 `docs/DEPLOYMENT.md`. 대시보드: https://zonnx999.github.io/AI-Investment-Bot/

---

## 1. 다음 작업 (액티브)

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
- [ ] **인라인 `[승인][거절]` 버튼** — 가입요청 알림에서 탭 승인 (callback_query 처리 필요) ← 다음 1순위
- [ ] `/news <티커>` — `NEWS_API_KEY`/FMP 뉴스 (응답 캐시 + rate limit), (선택) Haiku 센티먼트
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

---

## 4. 사용자 작업 스타일 (요약 — 전문은 `CLAUDE.md §4`)
- **Strict phase-gate**: "다음 단계로" 명시 승인 전까지 다음 단계 코드 미리 짜지 말 것.
- **시니어 엔지니어 리뷰 톤**: 의심스러운 코드는 비판적으로 짚고 대안 제시.
- **버그 발견 시 즉시 fix 는 OK** (phase-gate 예외) — 단 새 기능 추가가 아니라 기존 버그 수정일 때.
- **결정 사항은 옵션(A/B/C) 제시 후 명시적 승인 요청.** 한국어 응답.
