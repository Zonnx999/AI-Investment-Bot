# HANDOVER — 개선 탐사 브랜치 인수인계 (2026-07-06)

> **Claude CLI 로 이 프로젝트를 이어받는 세션을 위한 파일.**
> 읽는 순서: `CLAUDE.md`(작업 규칙, 필수) → **이 파일** → `docs/CURRENT_STATE.md` → `docs/ROADMAP.md`.
> 이 파일은 브랜치 머지가 끝나면 삭제해도 됨 (내용은 CURRENT_STATE/ROADMAP 에 이관 완료).

---

## 0. 30초 요약

- **브랜치**: `claude/project-improvement-exploration-cf70v4` (origin 에 푸시됨, main 미머지)
- **한 것**: 코드 개선 백로그 전량 + 백테스트 프레임워크 + Phase 11b 잔여(인라인 버튼·`/news`)
  + LLM 한 줄 요약 + **Phase 13 포트폴리오 레이어 전체** + 적대적 리뷰 3라운드로 실버그 21건 수정
  + Turso 무한 행(hang) 사고 근본 수정
- **테스트**: 216 → **420개** (오프라인, `python -m pytest`, ~14초, 전부 그린)
- **다음 단계**: §3 의 머지 게이트(라이브 스모크 7항목) → main 머지 → 재점수 → Phase 14 후보(§4)

```bash
# 새 세션 부트스트랩
git fetch origin claude/project-improvement-exploration-cf70v4
git checkout claude/project-improvement-exploration-cf70v4
pip install -e ".[dev,hosting]"
python -m pytest          # 420 passed 확인
```

---

## 1. 이 브랜치에서 한 것 (17 커밋, ca90f89..HEAD)

### 1.1 기능 (feat)
| 커밋 | 내용 |
|---|---|
| `eef92f1` | ROADMAP §2.2 백로그 전량 — KR PBR 완화, 빈 fundamentals skip, 음수 earningsYield→PER None, `utils.clip` 통합, vol 단일화, subscribers 공개 API + Turso 왕복 제거, Markdown 폴백 400 기준 등 |
| `20b0956` | **백테스트 프레임워크** `src/backtest.py` — run_backtest(비용·무선견편향) / walk_forward_topn / evaluate_lead_lag_oos + `scripts/check_backtest.py` |
| `3713894` | 가입요청 **인라인 [승인][거절] 버튼** (callback_query, 멱등, 소유자 게이트) |
| `a048a11` | **LLM 한 줄 요약** `src/llm.py` — 표현 레이어만, 실패 시 생략 폴백, `--no-llm`/`QUANT_BOT_LLM` 킬스위치 |
| `0489bc4` | **`/news <티커>`** — FMP `/news/stock`, 30분 캐시, 티커 검증, 평문 포맷 |
| `7b08266` | **Phase 13a** `src/findings.py` — Finding 공통 dataclass, 다이제스트·대시보드 공유 shape (렌더 바이트 동일) |
| `975ce9b` | **Phase 13b+13d** `src/portfolio.py` — 역변동성→상관 페널티→Kelly 상한 사이징 + weighted_backtest + `scripts/check_portfolio.py`(All Weather 리플레이) |
| `8647597` | **Phase 13c** — 다이제스트 US 발굴 종목에 "제안 N%" 열 (best-effort, `--no-weights`) |

### 1.2 버그 수정 (적대적 리뷰 3라운드 — 리뷰어 병렬 발굴 → 발견별 반박 검증)
| 커밋 | 확정 발견 |
|---|---|
| `f77d4da` `50b6751` | 1라운드 6건: 백테스트 MDD 초기자본 앵커 누락, lead-lag 발표지연 선견편향(`publication_lag_months`), has_fundamentals 식별자 문자열 오발화, cron 경로 버튼 탭 유실, LLM `<think>`/Markdown 유출, /news 400 폴백 2배 |
| `7c38235` | 2라운드(Phase 13) 3건: 상관 재정규화가 max_weight 상한 위반, "제안 0%" 경계 모호(→`<1%`), 전체 NaN 종가 TypeError |
| `48cf6d3` | 3라운드(전수) 12건 중 금융수식 5: **MC 이중 Ito 보정**(P50 비관 편향), **12-1 모멘텀이 1y 데이터론 영원히 죽은 코드**(→2y), 혼합 캘린더 월요일 수익률 유실, 자본잠식 KR 만점 함정, NDE 적자 만점 + 경계 4: fredapi 소켓 타임아웃, FMP 200 에러 dict 중앙 차단, notifier 기타 requests 예외, discover 의 enrichment 신선도 오염 |

### 1.3 Turso 무한 행 사고 (실사고, 노트북에서 재현됨)
| 커밋 | 내용 |
|---|---|
| `ad7c32c` | 1차 수정 — 스레드 워치독 + `QUANT_BOT_OFFLINE=1` 킬스위치 + 오프라인 파일 강등 |
| `cf2afb6` | **재설계** — 전수 리뷰가 1차 수정의 결함 적발: libsql 은 GIL 을 쥔 채 블로킹 → 스레드 워치독 무력. 초기 pull 을 **자식 프로세스 프로브**로(커널 수준 타임아웃), push 전 소켓 사전점검, bot.py sd_notify 하트비트, `_DB_ERRORS`(libsql 은 ValueError 를 던져 기존 best-effort 가드가 전부 무력이었음) |

핵심 교훈 (CLAUDE.md §4.10 에 추가할 가치 있음 — 아직 안 함):
libsql 호출(connect/sync/execute)은 **GIL 을 쥔 채 무한 블로킹 가능** → in-process 가드(스레드/시그널) 불가,
프로세스 경계(subprocess timeout, systemd watchdog)만 확실. 쓰기도 원격 왕복임을 잊지 말 것.

---

## 2. 지금 상태

- 브랜치 origin 푸시 완료, main 대비 +17 커밋. PR 은 안 만들었음 (사용자 요청 시).
- 문서 최신화 완료: `README.md`(전면), `ROADMAP.md`(Phase 13 체크오프 + §2.3 아키텍처 리뷰 트리아지 — 외부 리뷰 15항목 채택/기각 기록), `CURRENT_STATE.md`(인계 2건), `DEPLOYMENT.md §5`(systemd 워치독).
- 사용자(Leo) 노트북에서 Turso sync 행 재현됨 → 이 브랜치 pull 후엔 20초 강등 or `QUANT_BOT_OFFLINE=1`.

## 3. 다음 단계 ① — 머지 게이트 (라이브 스모크 7항목, 오프라인 세션이라 미검증)

> 상세는 `CURRENT_STATE.md` "인계 (2026-07-05)" 섹션. 순서대로:

1. **FMP 뉴스 필드**: `QUANT_BOT_CACHE=off python -c "from src.data_fetcher import fetch_stock_news; print(fetch_stock_news('AAPL'))"` — 필드 다르면 `fetch_stock_news` 파서만 수정
2. **MiniMax 모델 id**: `python scripts/send_digest.py --dry-run` — 🧠 줄 없으면 로그 확인, `MINIMAX_MODEL` 로 교체 (틀려도 다이제스트는 안전)
3. **인라인 버튼 실텔레그램**: /start → 버튼 탭 승인 → 이중탭 "이미 처리됨" → 48h 경과 메시지 edit 거부 시 로그만 남는지
4. **실 Turso 스모크**: 브랜치 pull 후 아무 스크립트 1회 (subscribers/universe 변경분 + 새 프로브 경로)
5. **DB 재점수** (점수 공식 다수 변경): `python scripts/build_universe.py --enrich --force` (~2-3분)
6. **`python scripts/check_portfolio.py`** 실네트워크 1회 (Phase 13 사이징 + All Weather 리플레이)
7. **서버 systemd 유닛 2개** (`DEPLOYMENT.md §5` 스니펫 그대로): quant-bot `Type=notify`+`WatchdogSec=300`, quant-digest@ `TimeoutStartSec=900`

그 다음: main 머지 → push → 서버 자동 배포(~15분) → `journalctl -u quant-bot -f` 로 확인.
⚠️ 머지 후 다이제스트 점수가 눈에 띄게 달라짐 (12-1 모멘텀 첫 실가동 + 점수 가드들) — 버그 수정이지 회귀 아님.

## 4. 다음 단계 ② — 이후 작업 후보 (우선순위 제안)

1. **관찰 기간 (1~2주, 코드 작성 없음)**: `check_backtest.py` / `check_portfolio.py` 실데이터 결과를 읽고
   어떤 신호·예측이 실제로 통했는지 판단 → 살아남은 것만 신뢰, 죽은 것은 ROADMAP 에서 강등
2. **CLAUDE.md §4.10 에 libsql GIL 교훈 추가** (§1.3 참조 — 5분 작업)
3. **KR 배당 DART 연동** (ROADMAP §2.2 잔여 — DART API 필드 라이브 검증 필요해서 보류했던 것)
4. (선택) `/regime`·`/predict` 즉답, 신호 성과 추적(과거 다이제스트 발굴 종목의 이후 수익률 기록), Phase 13 후속(보유 포트폴리오 입력 UI)

## 5. 이 브랜치의 작업 방식 메모 (다음 에이전트 참고)

- 이 브랜치는 **phase-gate 예외 세션**이었음 (사용자 명시 승인). 머지 후엔 CLAUDE.md §4.1 phase-gate 로 복귀할 것.
- 품질 루프가 유효했음: 구현(병렬 에이전트, 파일 분할) → 적대적 리뷰(발견별 반박 검증) → 확정만 수정.
  특히 **자기 커밋도 리뷰 대상에 넣을 것** — 1차 Turso 수정의 GIL 결함을 그렇게 잡았음.
- 금융 점수/수식 코드는 §4.10 #5 부호 함정 체크리스트(음수/0/None/NaN이 정반대 의미가 되는가)를 매번.
