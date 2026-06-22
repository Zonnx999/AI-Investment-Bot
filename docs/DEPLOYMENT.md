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
  `ExecStart=... send_digest.py --no-sync --market %i`) + 타이머 2개:
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
