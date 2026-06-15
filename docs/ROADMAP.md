# Roadmap

> 현재 상태·완료 상세는 `CURRENT_STATE.md` + `git log`. 이 파일은 **앞으로 할 일**에 집중합니다.
> 작업 규칙(phase-gate·리뷰 톤·결정 시 옵션 제시)은 `CLAUDE.md §4`.

**권장 순서**: Phase 10 ✅ → 9(KRX) ✅ → **11a(멀티유저 브로드캐스트, 다음)** → 11b(실시간 봇) → 12(대시보드).
11a 는 새 인프라 없이 가능해 우선순위 높음. 11b(실시간)는 항상 켜진 호스트 결정 후.

---

## 1. 다음 작업 (액티브)

### Phase 11a — 멀티유저 브로드캐스트 ⭐ — 🟡 구현 완료 (2026-06-15), 검증/사인오프 대기
가입 = **소유자 승인제** (유출될 코드 없음, 소유자가 한 명씩 승인). `src/subscribers.py`.
- [x] Turso `subscribers` 테이블 (chat_id PK, name, status[pending|active|inactive])
- [x] cron 실행마다 `getUpdates(offset)` 로 명령 수거 (offset 은 `state` 영속 → 재처리 방지)
- [x] 명령: `/start`(요청→소유자 알림) · `/stop`(해지) · 소유자 전용 `/approve <id>`·`/deny <id>`·`/pending`
- [x] 권한: approve/deny/pending 은 발신자 == `TELEGRAM_CHAT_ID`(소유자) 일 때만 (비소유자 무시)
- [x] 디제스트 1회 조립 후 active 구독자 브로드캐스트 (`send_daily_digest` + `send_safe(text, chat_id)`)
- [x] 순수 파싱/오케스트레이터 분리 + 오프라인 테스트 14개 + 실 Turso 스모크(마이그레이션·라이프사이클)
- [ ] **사용자 검증 대기**: 친구가 `/start` → 너에게 승인요청 알림 → `/approve <chat_id>` →
      `python scripts/send_digest.py` 로 브로드캐스트 확인 (자세히 `CURRENT_STATE.md §2`)
- 한계: 명령은 다음 cron 폴링 때 반영(실시간 아님) → 실시간 조회는 11b

### Phase 11b — 실시간 인터랙티브 (항상 켜진 호스트 필요)
- [ ] 호스팅 결정: 폴링 워커 (Fly.io 무료 / Oracle Always Free / 집 상시 PC).
      웹훅(서버리스)은 pandas/scipy 무거워 콜드스타트 불리 → **폴링 권장**
- [ ] 분리 설계: 무거운 분석은 cron 이 Turso 에 사전계산 → 봇 응답기는 **DB 읽기 위주**(경량)
- [ ] 명령어: `/stock <티커>`(점수+근거 상세, `lookup_detail` 활용), `/news <티커>`, `/scan [시장]`, `/subscribe`
- [ ] 뉴스: `NEWS_API_KEY` 또는 FMP 뉴스 (응답 캐시 + 유저별 rate limit)
- [ ] (선택) 뉴스에 Haiku 센티먼트 분류 — LLM 의 합리적 자리
- [ ] 남용 방지: 유저별 rate limit, 외부 API 직접 호출 최소화(사전계산 DB 우선)

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
