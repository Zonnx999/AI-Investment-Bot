# Roadmap

> 현재 상태·완료 상세는 `CURRENT_STATE.md` + `git log`. 이 파일은 **앞으로 할 일**에 집중합니다.
> 작업 규칙(phase-gate·리뷰 톤·결정 시 옵션 제시)은 `CLAUDE.md §4`.

**권장 순서**: Phase 10·9·11a·11b ✅ (Oracle 서버 가동 중) → **11b 마무리(인라인 버튼·뉴스)** → 12(대시보드).
운영 런북은 `docs/DEPLOYMENT.md`.

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
- [ ] **인라인 `[승인][거절]` 버튼** — 가입요청 알림에서 탭 승인 (callback_query 처리 필요) ← 다음 1순위
- [ ] `/news <티커>` — `NEWS_API_KEY`/FMP 뉴스 (응답 캐시 + rate limit), (선택) Haiku 센티먼트
- [ ] (선택) `/regime`·`/predict`·`/me`(내 상태) 즉답 — 단 국면/예측은 라이브 fetch라 봇에 약간 무거움

### Phase 12 — 대시보드 통합 (모든 정보 한 화면)
`dashboard/index.html`(현재 스크리너 전용) 을 확장.
- [ ] 국면/리스크/예측/유니버스 스캔/종목 상세 탭 추가
- [ ] 데이터 소스: `daily_update`/`scan` 결과 JSON 출력 → 정적 페이지가 fetch
- [ ] 호스팅: GitHub Pages (정적) — Turso DB 와 연계 (⚠️ public repo 전환 시 시크릿/IP 노출 주의)
- [ ] 역할 분담: 봇 push=요약 알림, 대시보드=심층 탐색

### Phase 12 — 대시보드 통합 (모든 정보 한 화면)
`dashboard/index.html`(현재 스크리너 전용) 을 확장.
- [ ] 국면/리스크/예측/유니버스 스캔/종목 상세 탭 추가
- [ ] 데이터 소스: `daily_update`/`scan` 결과 JSON 출력 → 정적 페이지가 fetch
- [ ] 호스팅: GitHub Pages (정적) — Turso DB 와 연계
- [ ] 역할 분담: 봇 push=요약 알림, 대시보드=심층 탐색

---

## 2. 부록 — 우선순위 낮은 선택 단계
- **LLM 요약 한 줄** — 다이제스트 맨 위 Haiku 한 문단 (월 $1 미만). 11b 뉴스 센티먼트와 묶을 수 있음
- **백테스트 프레임워크** — Phase 5 신호 / Phase 6 예측의 과거 성과 검증 (예측 R² 우려 정량 해소)
- **Streamlit 웹 UI** — Phase 12 대시보드로 대체되면 skip
- **미구현(새 의존성 대기)**: Google Trends(pytrends, 불안정), SEC EDGAR 13F(파싱 부담·45일 지연)

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
