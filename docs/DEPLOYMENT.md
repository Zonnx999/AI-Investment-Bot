# Deployment — 상시 인터랙티브 봇 호스팅 (Phase 11b)

상시 텔레그램 봇(`scripts/bot.py`)을 돌리는 **서버 운영 런북**. 일일 다이제스트는 여전히
GitHub Actions cron 이 담당 (역할 분담은 §7).

> 민감정보(토큰/키/실IP)는 이 문서에 넣지 않음 — OCI 콘솔·`.env`·GitHub Secrets 에만.

---

## 1. 호스트 사양
- **Oracle Cloud Always Free**, 홈 리전 **Japan Central (Osaka, `ap-osaka-1`)** — 홈 리전은 **영구**(변경 불가)
- 인스턴스 `instance-20260616-0047`, shape **VM.Standard.E2.1.Micro** (AMD, 1 OCPU / **1GB RAM**)
  - ARM A1(4 OCPU/24GB)은 용량 부족으로 못 잡아 E2.1.Micro 사용. A1 잡히면 이전 가능
- OS **Ubuntu 24.04** (Python 3.12), 기본 유저 `ubuntu`
- **swap 2GB** 추가됨 (`/swapfile`, fstab 등록) — 1GB RAM 보완
- Public IP = **ephemeral** (인스턴스 **stop 시 바뀜**; 자주 stop 하면 reserved IP 권장). 현재 값은 OCI 콘솔 → 인스턴스 상세
- 비용 $0 (Always Free)

## 2. 접속 (SSH)
```
chmod 400 <private-key 경로>
ssh -i <private-key 경로> ubuntu@<PUBLIC_IP>
```
- 인스턴스 생성 시 등록한 키페어의 private key 로 접속. 인바운드는 SSH(22)만 필요(폴링 봇=아웃바운드).

## 3. 코드 (private repo → SSH Deploy Key)
박스엔 **읽기 전용 deploy key** 로 클론돼 있음: `~/.ssh/github_deploy` (+ `~/.ssh/config` 의 `github.com` 매핑).
→ `git pull/fetch` 가 이 키로 동작.
- 새 박스라면: `ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""` → 공개키를 GitHub repo **Settings → Deploy keys** 에 등록(write 미체크) → `git clone git@github.com:Zonnx999/AI-Investment-Bot.git`

## 4. 새 박스 부트스트랩
```
sudo apt update && sudo apt install -y python3-venv python3-pip git
# (deploy key 설정 후) git clone git@github.com:Zonnx999/AI-Investment-Bot.git
cd AI-Investment-Bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[hosting]"
cp .env.example .env && nano .env          # §8 키 입력
# swap (1GB 박스):
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 5. systemd 서비스 `quant-bot`
서비스 파일 `/etc/systemd/system/quant-bot.service` — WorkingDirectory=repo, ExecStart=venv python `scripts/bot.py`, `Restart=always`, `RestartSec=5`, enabled(부팅 자동).

**⚠️ 워치독 필수 (2026-07-06 행 사고 재발 방지)**: libsql 쓰기는 GIL 을 쥔 채 원격 왕복하므로
네트워크 블랙홀 시 프로세스가 '살아있지만 얼어붙음' — `Restart=always` 는 못 잡는다.
봇이 매 루프 `WATCHDOG=1` 핑을 보내니 (`scripts/bot.py _sd_notify`) 유닛에 다음을 추가:
```
[Service]
Type=notify
WatchdogSec=300
NotifyAccess=main
```
핑이 300초 끊기면 systemd 가 죽이고 재시작 — offset 은 커밋된 지점부터라 텔레그램이
미확인 업데이트를 재전달하므로 유실 없음. 적용: 유닛 수정 후 `sudo systemctl daemon-reload && sudo systemctl restart quant-bot`.
```
sudo systemctl status quant-bot --no-pager     # 상태
journalctl -u quant-bot -f                      # 실시간 로그
sudo systemctl restart quant-bot                # 재시작
```
- `.env` 는 봇이 WorkingDirectory 에서 load_dotenv 로 자동 로드 (EnvironmentFile 불필요).

### 5.1 일일 다이제스트 (systemd timer)
스케줄 발송은 봇과 **별도 oneshot 프로세스**. libsql 레플리카 동시쓰기 충돌(`wal_insert_begin failed`)
방지로 **전용 레플리카 경로**(`QUANT_BOT_DB_PATH=.../data/digest_replica.db`)를 씀 — 봇과 같은 Turso 로
sync 되어 데이터는 일치.
- 유닛: `quant-digest@.service`(템플릿 `%i`=시장, `Environment=QUANT_BOT_DB_PATH=...digest_replica.db`,
  `ExecStart=... send_digest.py --no-sync --market %i`) + 타이머 2개.
  **⚠️ `TimeoutStartSec=900` 를 [Service] 에 추가할 것** — `Type=oneshot` 의 기본
  타임아웃은 무한이라, libsql 행(위 §5 참조) 시 유닛이 'activating' 에 영원히 붙잡혀
  **이후 매일의 타이머 발화가 전부 조용히 무시됨** (다이제스트 무한 중단). 900초가
  지나면 죽고 다음날 타이머는 정상 발화:
  - `quant-digest-kr.timer`: `OnCalendar=Mon..Fri 08:30 Asia/Seoul`
  - `quant-digest-us.timer`: `OnCalendar=Mon..Fri 09:00 America/New_York`
  - systemd OnCalendar 의 **타임존 지정**으로 KST/ET·DST 자동 처리 (UTC 게이트 불필요), `Persistent=true`.
- 관리:
  ```
  systemctl list-timers 'quant-digest-*'         # 다음 발송 시각
  sudo systemctl start quant-digest@kr.service    # 즉시 1회 발송(테스트)
  journalctl -u quant-digest@kr.service -n 30
  ```
- ⚠️ `.env` 에 `FRED_API_KEY`·`FMP_API_KEY` 필요(US 팩터·국면). KR 은 키 없이도 동작.
- GitHub Actions 예약 schedule 은 **비활성**(중복 발송 방지) — 비상시 workflow_dispatch 수동.

## 6. 업데이트 배포 / 복구
**코드 업데이트:**
```
cd ~/AI-Investment-Bot && git fetch origin && git reset --hard origin/main
sudo systemctl restart quant-bot
```
- editable 설치라 `src/` 변경 자동 반영. **새 의존성**이 추가됐으면 `source .venv/bin/activate && pip install -e ".[hosting]"` 재실행.
- `git reset --hard` 안전: `.env` 는 미추적이라 보존됨.

### 6.1 자동 업데이트 (server auto-pull timer)
위 수동 절차를 systemd timer 로 자동화. `scripts/server_autopull.sh` 가 주기적으로 `git fetch`
후 origin/main 이 앞설 때만 reset + (pyproject 변경 시) pip + 봇 재시작. 변경 없으면 아무 것도
안 함. **ubuntu 유저로 실행**(deploy key 접근), `restart` 만 passwordless sudo.

1) restart 전용 passwordless sudo — `sudo visudo -f /etc/sudoers.d/quant-autopull`:
```
ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart quant-bot
```
(경로는 `which systemctl` 로 확인 — 보통 `/usr/bin/systemctl`)

2) 서비스 `/etc/systemd/system/quant-autopull.service`:
```
[Unit]
Description=quant-bot auto-pull (git fetch + reset + restart on change)
After=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/AI-Investment-Bot
ExecStart=/home/ubuntu/AI-Investment-Bot/scripts/server_autopull.sh
```

3) 타이머 `/etc/systemd/system/quant-autopull.timer` (15분 주기 — 개인 봇엔 충분, 폴링·로그 노이즈↓):
```
[Unit]
Description=Run quant-bot auto-pull every 15 min

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```
(코드 업데이트는 긴급하지 않으므로 5분은 과함 — 하루 288회→96회. 개발 중 즉시 반영은 아래 강제 실행으로.)

4) 활성화:
```
sudo systemctl daemon-reload
sudo systemctl enable --now quant-autopull.timer
```
- 다음 실행 시각: `systemctl list-timers quant-autopull.timer`
- 마지막 실행 로그: `journalctl -u quant-autopull.service -n 20`
- 즉시 1회 실행 (테스트 / 개발 중 강제 동기화): `sudo systemctl start quant-autopull.service`
- 끄기: `sudo systemctl disable --now quant-autopull.timer`
- ⚠️ 이제 `main` 에 push 하면 ~15분 내 박스가 **자동 반영** → 수동 배포 불필요. 단 자동 재시작이
  바로 적용되므로 **DB 스키마/마이그레이션 변경은 안전 확인 후** push (CLAUDE §4.10 #9).

**레플리카 손상 복구** (`malformed WAL` 등 — Turso 임베디드 레플리카 파일 깨짐):
```
sudo systemctl stop quant-bot
rm -f data/quant_bot.db data/quant_bot.db-wal data/quant_bot.db-shm data/quant_bot.db-info
sudo systemctl start quant-bot
```
- **데이터 손실 없음** — Turso(클라우드)가 원본, 로컬은 캐시. 재시작 시 깨끗이 재싱크.

## 7. 역할 분담 (중요 — offset 경합 방지)
- **봇(이 서버)** = `getUpdates` 폴링 **단독 소유**. 구독 명령(/start·/stop·/approve·/deny·/pending) + 조회(/stock·/scan·/help) **실시간** 처리. 조회는 active 구독자(+소유자)만.
- **다이제스트 timer(이 서버, §5.1)** = 일일 발송만: `send_digest.py --no-sync` + 전용 레플리카 경로.
  - ⚠️ `--no-sync` 필수 — 봇과 동시에 getUpdates 폴링하면 offset 경합으로 메시지 분실.
  - ⚠️ 봇과 **다른 레플리카 파일** 필수 — 같은 파일이면 WAL 동시쓰기 충돌.
  - (GitHub Actions 예약 발송은 비활성 — §5.1)
- 무거운 분석(유니버스 빌드·다이제스트 조립)은 cron/로컬이 Turso 에 사전계산 → 봇은 **DB 읽기 위주(경량)**.

## 8. 시크릿 (`.env`)
봇 필수 4개: `TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID`(소유자 겸 승인자) · `TURSO_DATABASE_URL` · `TURSO_AUTH_TOKEN`.
(FRED/FMP/KRX/DART 는 이 박스에서 다이제스트/유니버스 빌드도 돌릴 때만)

## 9. Gotchas
- **1GB RAM**: 첫 `/stock` 때 pandas 로드로 메모리 spike → swap 으로 커버. OOM-kill 보이면 swap/메모리 점검.
- **Turso 일시 장애**: `get_storage()` 실패 → 봇 크래시 → systemd 5s 후 재시작(self-heal). 장기 장애 시 크래시 루프 (로컬 sqlite 폴백은 향후 과제).
- **libsql 마이그레이션**: 컬럼 추가는 `storage.add_column_if_missing`(중복 컬럼 오류 삼킴) 사용 — 로컬 PRAGMA(stale) vs 원격 ALTER 불일치 크래시 방지.
- **WAL 사이드카**(`data/*.db-*`)는 gitignore — 절대 커밋 금지(클론 시 레플리카 손상 원인).
- **초기 레플리카 풀 지연 (노트북/일회성 실행)**: 임베디드 레플리카 초기 동기화가 60-120s+ 걸릴 수 있음 — 20s 기본 프로브 타임아웃이 너무 짧아 오프라인으로 강등됨. 대화형/일회성 실행 시 `QUANT_BOT_SYNC_TIMEOUT=240` 로 늘릴 것. 서버(상시 가동)는 기본값 유지해도 무방.
- **대량 재점수/유니버스 빌드 시 캐시 끄기**: `QUANT_BOT_CACHE=off python scripts/build_universe.py --enrich --force` — 캐시 on 상태에서는 심볼당 2-3회 Turso 원격 쓰기(태평양 왕복)가 발생해 ~0.1 symbol/s. `QUANT_BOT_CACHE=off` 시 ~1.5 symbol/s (~15x).
