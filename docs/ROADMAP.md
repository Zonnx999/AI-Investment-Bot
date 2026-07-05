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
