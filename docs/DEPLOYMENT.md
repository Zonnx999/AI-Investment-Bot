# Deployment — 서버 운영 런북 (봇·다이제스트·재점수·자동 배포)

Oracle 서버의 systemd 가 **상시 봇, 일일 다이제스트 발송, 신호 원장 채점, 유니버스 주간 재점수, 코드 자동 배포**를 모두 담당한다.
GitHub Actions 는 세 가지만 한다: **CI**(`ci.yml`, 푸시마다 pytest — 서버 배포의 게이트), **Dashboard Export**(`dashboard-export.yml`, 평일 09:30 KST 예약 — 대시보드 JSON 갱신 + Pages 배포),
**Daily Digest**(`daily-digest.yml`, 예약 비활성 — 비상시 `workflow_dispatch` 수동 발송 전용, 권한 `contents: read`·시장 입력은 `us|kr` 만 허용).
저장소는 **public** 이다(서버 clone 은 읽기 전용 deploy key, CI 결과 조회는 무인증 공개 API — private 로 바꾸면 자동 배포가 멈추니 §6.1 경고 참고).

> 민감정보(토큰/키/실IP)는 이 문서에 넣지 않음 — OCI 콘솔·`.env`·GitHub Secrets 에만.
> 유닛 파일의 **원본은 저장소 `deploy/systemd/`** 이다. 서버의 `/etc/systemd/system/` 은 그 복사본 — 바꿀 때는 저장소를 먼저 고치고 다시 복사한다.
> ⚠️ `deploy/` 만 바뀐 커밋은 autopull 이 CI 없이 반영하지만 **서버 유닛을 자동 설치하지는 않는다** (최소 권한 — 설치는 §5 절차로 수동).

---

## 0. 누가 무엇을 언제 돌리나 (실행 주체 표)

| 무엇 | 어디서 | 언제 | 로컬 레플리카 | 실패 시 누가 아나 |
|---|---|---|---|---|
| 상시 봇 `quant-bot.service` | 서버 | 상시 (getUpdates 폴링 단독 소유) | `data/quant_bot.db` | `WatchdogSec=300` 이 얼어붙은 프로세스를 재시작. 반복 재시작은 알림 없음 → `systemctl show quant-bot -p NRestarts` |
| 한국 다이제스트 `quant-digest-kr.timer` → `quant-digest@kr.service` | 서버 | 평일 08:30 KST | `data/digest_replica.db` | 종료코드 11/12/21/30 → `OnFailure` 소유자 DM. 10(부분 실패)·보류·경고는 앱이 직접 소유자 DM (§5.3) |
| 미국 다이제스트 `quant-digest-us.timer` → `quant-digest@us.service` | 서버 | 평일 09:00 ET (DST 자동) | `data/digest_replica.db` | 위와 같음 |
| 신호 원장 채점 (`scripts/ledger.py score`) | 서버 | 다이제스트 직후 `ExecStartPost` (flock 을 60초 안에 못 잡으면 포기) | 다이제스트와 같은 레플리카 | 실패는 무시(`-`) — 다음 런이 이어서 채점. 3일 넘게 못 갱신하면 성과 줄이 숨겨지고 `성적표 N일째 미갱신` 푸터 |
| 유니버스 주간 재점수 `quant-universe.timer` | 서버 | 토 10:00 KST | `data/universe_replica.db` | `OnFailure` 소유자 DM + 종료코드(§5.4) |
| 레플리카 미리 pull (`scripts/warm_replica.py`) | 서버 | 재점수 직후 `ExecStartPost` | `data/digest_replica.db` (다이제스트와 같은 flock) | 실패는 무시(`-`) — 다음 다이제스트의 첫 pull 이 이어받음. 소요 시간은 journal 의 `init_sync=… elapsed=…s` (§5.4) |
| (선택) KR 가격 갱신 `quant-kr-refresh.timer` | 서버 | 평일 08:10 KST (KR 다이제스트 20분 전) | `data/universe_replica.db` | `OnFailure` 소유자 DM |
| 코드 자동 배포 `quant-autopull.timer` | 서버 | 15분마다 | (DB 없음) | `OnFailure` DM + CI 실패/대기/fetch 연속 실패/pip 실패/lock 불일치 알림 (§6.1) |
| Dashboard Export | GitHub Actions | 평일 09:30 KST (cron `30 0 * * 1-5` UTC, 수 시간 지연 가능) | 러너 임시 파일 | Actions 실패. **2026-07 이후 비활성(disabled_inactivity)** — 배포 순서에서 재활성화. 이제 `init_sync!="ok"`·빈 DB 이면 `::error::` 로 실패(덮어쓰기 거부) |
| CI (`pytest` 잡) | GitHub Actions | 푸시·PR 마다 (docs/dashboard/README/CLAUDE.md 변경만이면 건너뜀) | 없음 (오프라인) | GitHub UI. autopull 이 결과를 조회해 배포 여부 결정 |
| Daily Digest 워크플로 | GitHub Actions | 수동 전용 | 러너 임시 파일 | 사람이 직접 실행할 때만 |

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

## 3. 코드 (SSH Deploy Key)
박스엔 **읽기 전용 deploy key** 로 클론돼 있음: `~/.ssh/github_deploy` (+ `~/.ssh/config` 의 `github.com` 매핑). → `git pull/fetch` 가 이 키로 동작.
- 새 박스라면: `ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""` → 공개키를 GitHub repo **Settings → Deploy keys** 에 등록(write 미체크) → `git clone git@github.com:Zonnx999/AI-Investment-Bot.git`

## 4. 새 박스 부트스트랩
```
sudo apt update && sudo apt install -y python3-venv python3-pip git
git clone git@github.com:Zonnx999/AI-Investment-Bot.git
cd AI-Investment-Bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
cp .env.example .env
nano .env
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```
- `.env` 키는 §8. 이후 §5 의 유닛 설치로 이어진다. (`requirements.lock` 은 `[hosting]`=libsql 포함 — 서버와 CI 가 같은 버전 세트.)

## 5. systemd 유닛

### 5.1 설치 (유닛 원본 → 서버)
`deploy/systemd/` 의 11개: `quant-bot.service` · `quant-digest@.service` · `quant-digest-kr.timer` · `quant-digest-us.timer` · `quant-autopull.service` · `quant-autopull.timer` ·
`quant-alert@.service` · `quant-universe.service` · `quant-universe.timer` · (선택) `quant-kr-refresh.service` · `quant-kr-refresh.timer`.
설치는 **백업 → 복사 → daemon-reload → restart → 확인 → 알림 시험** 순서 (배포 순서 §10 에서 한 줄씩 제공). 확인:
```
systemctl status quant-bot --no-pager
systemctl list-timers --all --no-pager
sudo systemctl start quant-alert@autopull.service
```
마지막 줄은 `OnFailure` 알림 경로 시험 — 소유자 텔레그램에 `[quant-bot 실패] quant-autopull.service …` DM 이 오면 정상.

### 5.2 봇 `quant-bot` — 상시 폴링, 워치독
`Type=notify` · `NotifyAccess=main` · `WatchdogSec=300` · `TimeoutStartSec=180` · `Restart=always` · `RestartSec=10` + `RestartSteps=5`/`RestartMaxDelaySec=300`(재시작 백오프) · `OOMScoreAdjust=-200` · `QUANT_BOT_SYNC_TIMEOUT=60`.
- 봇은 스키마 초기화(`ensure_owner`)·offset 읽기가 **끝난 뒤** `READY=1` 을 보내고, 매 루프 `WATCHDOG=1` 핑을 보낸다. 핑이 300초 끊기면 systemd 가 죽이고 재시작 — offset 은 커밋된 지점부터라 텔레그램이 미확인 업데이트를 재전달하므로 유실 없음.
- libsql 쓰기는 GIL 을 쥔 채 원격 왕복하므로 네트워크 블랙홀 시 프로세스가 '살아있지만 얼어붙음' — `Restart=always` 는 못 잡고 워치독이 최종 안전망 (`CLAUDE.md §4.10 (18)`).
- 초기 pull 이 타임아웃으로 오프라인 파일에 강등돼 뜨면 600초 응답만 하다가 `exit 3` 으로 자진 종료 → systemd 가 재시작해 Turso 를 다시 프로브. 강등 중 처리한 구독 요청은 그 파일에만 남아 버려질 수 있음(알려진 한계, `scripts/bot.py` docstring).
- **조회 명령 전 pull**: `/stock`·`/scan`·`/ledger` 가 든 배치는 처리 **전에** `sync()` 1회 — 다이제스트·재점수가 클라우드에 쓴 최신 상태를 봇의 로컬 레플리카가 반영하게 함. 마지막 sync 후 300초 이상일 때만(Turso 왕복 남용 방지),
  강등 중(위 `exit 3` 대기 구간)엔 생략, 실패는 삼키고 로컬 레플리카로 응답. 워치독 300초 예산에 **최대 ~65초**(`QUANT_BOT_SYNC_TIMEOUT=60` + 소켓 사전점검 5초)가 더해질 수 있다 — 여유는 있지만 `WatchdogSec` 을 줄일 때는 이 값을 감안할 것.
```
sudo systemctl status quant-bot --no-pager
journalctl -u quant-bot -f
sudo systemctl restart quant-bot
```
- `.env` 는 봇이 WorkingDirectory 에서 load_dotenv 로 자동 로드 (EnvironmentFile 불필요).

### 5.3 일일 다이제스트 `quant-digest@.service` (템플릿 `%i` = kr | us)
스케줄 발송은 봇과 **별도 oneshot 프로세스**. 전용 레플리카 `data/digest_replica.db`(봇과 같은 Turso 로 sync 되어 데이터는 일치, 파일은 분리 — `wal_insert_begin failed` 방지).
- `ExecStart` = `flock -w 600 data/.digest.lock … send_digest.py --no-sync --market %i` (kr/us 가 같은 레플리카를 쓰므로 직렬화). `--no-sync` 필수 — 봇과 동시에 getUpdates 폴링하면 offset 경합으로 메시지 분실.
- `ExecStartPost` = 같은 레플리카·같은 락으로 `flock -w 60 … timeout 180 … scripts/ledger.py score` — 발송 지연 0, `-` 접두라 실패는 무시. flock 대기는 **60초만**(못 잡으면 포기 — 채점은 다음 런이 이어받음).
- `SuccessExitStatus=10` · `TimeoutStartSec=1500`(libsql 행 시 유닛이 'activating' 에 붙잡혀 이후 타이머가 전부 무시되는 것 방지) · `QUANT_BOT_SYNC_TIMEOUT=180` · `OnFailure=quant-alert@digest-%i.service`.
  **시간 예산**: `TimeoutStartSec`(1500) ≥ ExecStart flock 대기 600 + 본체 여유 300 + ExecStartPost flock 대기 60 + 채점 `timeout` 180 (`tests/test_deploy_units.py` 가 이 관계를 고정 — 값을 바꿀 땐 함께 맞출 것).
- 타이머: `OnCalendar=Mon..Fri 08:30 Asia/Seoul` / `Mon..Fri 09:00 America/New_York` — **타임존 지정**으로 KST/ET·DST 자동 처리, `Persistent=true`.

**종료코드** (`src/digest.py` 상수, `scripts/send_digest.py`):
| 코드 | 의미 | 유닛 | 소유자 알림 |
|---|---|---|---|
| 0 | 전원 발송 (또는 dry-run) | 성공 | 변화·경고 있을 때만 앱이 평문 DM |
| 10 | 일부 전송 실패 | 성공(`SuccessExitStatus`) | 앱이 평문 DM |
| 11 | 전원 전송 실패 | 실패 | `OnFailure` DM |
| 12 | 수신자 0 | 실패 | `OnFailure` DM |
| 20 | 발송 보류 (Turso 강등·후보 0) + 소유자 DM 완료 | 실패 | 앱이 DM → `notify_owner` 가 중복 생략 |
| 21 | 보류 + 소유자 DM 도 실패 | 실패 | `OnFailure` DM |
| 30 | 필수 설정 누락 (`ConfigError`) | 실패 | `OnFailure` DM |
| 1 | 예기치 못한 크래시 | 실패 | `OnFailure` DM |

**헬스 DM**: 정상이고 직전 실행과 문제 목록(`signature`)이 같으면 DM 없음 — 다이제스트 자체가 하트비트. DM 이 가는 경우: 발송 보류 · 발송 실패 · 문제 목록 변화(발굴 데이터 노후 — 점수 기준일 경과 ≥ `QUANT_BOT_UNIVERSE_WARN_DAYS`(8일, 기준 줄 ⚠️·`/scan` 과 같은 경계),
가격 기준일 3영업일 초과 노후, 국면 지표 n/3, Turso 강등·pull 오류, LLM 퇴역, 가격 조회 실패, Markdown 평문 폴백, 원장 기록 실패, 상태 저장 실패, `재보강 누락 종목`, `선정 후 수익률 이상치`).
`재보강 누락 종목` = 주간 재점수에서 갱신 못 한 행이 시장 기준일보다 7일 이상 뒤처져 후보에서 빠졌다는 뜻(DM 본문에 종목·점수·기준일, 원인은 `build_universe` 로그).
`QUANT_BOT_LEDGER_LINE=owner` 이면 성과 줄이 구독자 대신 소유자 DM 에 실리므로 매 런 DM.
**동기화 지연일**: 레플리카 초기 pull 이 빠르게 실패(`init_sync=error`)해 stale 레플리카로 발송하는 날은 직전 리스트(원장)가 낡았을 수 있어 **후보 비교(매수/탈락 판정)를 보류**한다 —
판정 사유 `데이터 동기화 지연 — 후보 비교 보류`, 후보 제목 `(직전 리스트 MM-DD 기준 · 동기화 지연)`, 구독자 상태 줄 `일부 데이터 갱신 지연`, 소유자 DM 에 `init_sync` 값. (타임아웃으로 오프라인 파일에 강등되면 발송 자체를 보류 — 종료코드 20.)
**발송 후 순서**: 브로드캐스트 → (sent ≥ 1 일 때만) `commit_signal_state` → `ledger.record_send`(후보 보류일은 미기록) → 헬스 state → `sync()` → 소유자 DM.

```
systemctl list-timers 'quant-digest-*'
sudo systemctl start quant-digest@kr.service
journalctl -u quant-digest@kr.service -n 30
```
- ⚠️ `.env` 에 `FRED_API_KEY`·`FMP_API_KEY` 필요(US 팩터·국면). KR 은 DART/KRX 키가 재점수에 필요.
- 점검용 미리보기(발송 없음, 상태·원장 쓰기 없음): 서버에서 `QUANT_BOT_DB_PATH=/tmp/chk.db`(사본)로 `scripts/send_digest.py --market kr --dry-run`. 운영 레플리카를 직접 열지 말 것(§9).
  `--dry-run` 도 스키마 보장(`CREATE IF NOT EXISTS`·멱등 컬럼 추가)과 조회 캐시 저장은 할 수 있어서 사본으로 돌리는 것 — Turso 모드에선 이것도 원격 쓰기다.

### 5.4 유니버스 주간 재점수 `quant-universe.service` + `.timer`
`build_universe.py --max-age 6` (discover + enrich, 7일 신선도 6일로 여유) · `QUANT_BOT_CACHE=off`(원격 캐시 쓰기 0) · `QUANT_BOT_SYNC_TIMEOUT=300` · 전용 레플리카 `data/universe_replica.db` + `flock data/.universe.lock` ·
`TimeoutStartSec=4h` · `MemoryMax=600M`(봇보다 먼저 OOM 대상, `OOMScoreAdjust=300`) · `Nice=10` · `IOSchedulingClass=idle` · 타이머 `Sat 10:00 Asia/Seoul`, `Persistent=true`. `[Install]` 이 없으므로 `quant-universe.service` 는 직접 enable 하지 않고 **타이머만 enable**.
- 재점수 직전·직후에 유니버스 스냅샷(`universe_snapshots`)이 저장됨(훅) — 원장의 순위 IC 에 쓰임. 스냅샷은 로컬 레플리카를 읽으므로 **직후 스냅샷 전에 `sync()`** 를 해서 방금 쓴 점수가 클라우드와 합쳐진 뒤에 찍는다.
- **`ExecStartPost` = 레플리카 미리 pull**: `flock -w 900 data/.digest.lock` 아래 `QUANT_BOT_DB_PATH=…/digest_replica.db QUANT_BOT_SYNC_TIMEOUT=1200 … scripts/warm_replica.py`, `-` 접두라 실패는 무시(재점수 자체는 성공).
  이유: 주말엔 `digest_replica.db` 를 여는 유닛이 없어, 안 하면 월요일 08:30 KR 다이제스트의 초기 pull(타임아웃 180초)이 한 주치 델타를 한 번에 받다가 넘기면 오프라인 강등 → 발송 보류(종료코드 20)가 된다.
  다이제스트와 같은 락을 쓰는 건 한 레플리카 파일을 한 프로세스만 열게 하려는 것(`CLAUDE.md §4.10 (9)`). 결과는 journal 에 `init_sync=ok elapsed=NN.Ns db=…` 로 남는다(`timeout`/`error` 면 미리 pull 실패):
  ```
  journalctl -u quant-universe.service --since "7 days ago" --no-pager -o cat | grep init_sync
  ```
- 종료코드: 0 정상 / 2 원격 DB 필요한데 연결 불가 / 5 보강 중단(연속 429 5회·인증 오류)·실패율 20% 초과·`--kr-refresh` 발굴 0건 → 모두 `OnFailure` DM.
- **첫 실행은 수동**으로 소요 시간·메모리를 확인(예상 25~30분): `sudo systemctl start --no-block quant-universe.service` 후 `journalctl -u quant-universe -f`, `systemctl show quant-universe -p MemoryPeak -p ExecMainStatus`. 이때 위 `init_sync … elapsed` 값도 기록해 둘 것(§10 단계 6).
- 재점수 도중엔 autopull 이 코드를 교체하지 않는다(다음 주기로 미룸).

### 5.5 (선택) KR 가격 갱신 `quant-kr-refresh`
평일 08:10 KST — KRX 일별 2콜로 KR 가격·시총 갱신 + 오프라인 PER/PBR 재계산(DART 0콜). KR 아침 메시지의 가격 기준일을 신선하게 하려는 선택 기능. 설치하지 않으면 KR 가격 기준일은 주간 재점수 때만 갱신(`⚙️ 상태` 줄에 가격 노후가 뜰 수 있음).
`Persistent=false`(놓친 날은 건너뜀). 재점수와 같은 레플리카·락이라 서로 직렬화된다.

### 5.6 소유자 알림 `quant-alert@.service`
`OnFailure=quant-alert@<별칭>.service` 대상 (`bot`·`digest-kr`·`digest-us`·`universe`·`kr-refresh`·`autopull`). `scripts/notify_owner.py --failed-unit <별칭>` 이 실패 유닛·종료코드·journal 꼬리 12줄을 **평문**(`parse_mode=None`)으로 소유자에게 1통. 키는 `mask_secrets` 로 가리되 **줄 절단 전에** 먼저 적용(절단 경계에 걸린 키의 앞부분이 새지 않게), 3,800자 절단.
DB/레플리카를 열지 않아(Turso 장애 중에도 알림은 가고, 레플리카 충돌이 없음) 항상 exit 0.

### 5.7 `QUANT_BOT_SYNC_TIMEOUT` 와 유닛 타임아웃 (초기 pull 이 오래 걸리는 정도에 맞춤)
| 대상 | `QUANT_BOT_SYNC_TIMEOUT` | 유닛 `TimeoutStartSec` / 비고 |
|---|---|---|
| 봇 | 60 | 180, `WatchdogSec=300` |
| 다이제스트 | 180 | 1500 (flock 600 + 본체 + 채점 flock 60 + 채점 180 이상, §5.3) |
| 재점수 | 300 | 4h |
| 재점수 직후 레플리카 미리 pull (`warm_replica`) | 1200 | `ExecStartPost` — flock 대기 900, 실패 무시 |
| KR 가격 갱신 | 300 | 1200 |
| GitHub Actions (export·수동 digest) | 300 | 워크플로 `timeout-minutes: 20` |
| 수동 원장 명령 (§10 단계 5) | 240 (명령 앞에 직접) | 강등이면 실행 거부(exit 3) |
| 코드 기본값 (노트북 등) | 20 | 레플리카 초기 풀이 60–120s+ 걸리면 강등됨 → `QUANT_BOT_SYNC_TIMEOUT=240` |

## 6. 업데이트 배포 / 복구

### 6.1 자동 배포 (autopull) — CI 게이트 + 롤백
`scripts/server_autopull.sh` 가 15분마다 `git fetch` 후 `origin/main` 이 앞설 때 배포한다. **ubuntu 유저**로 실행(deploy key 접근), `restart` 만 passwordless sudo:
```
sudo visudo -f /etc/sudoers.d/quant-autopull
```
내용 한 줄: `ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart quant-bot` (경로는 `which systemctl` 로 확인).
- **배포 완료 표식**은 git HEAD 가 아니라 `data/.deployed_sha`. 부분 실패하면 다음 주기에 자동 재시도.
- **코드가 바뀐 커밋**(비코드 경로 = `dashboard/`·`docs/`·`tests/`·`deploy/`·`.github/`·`README.md`·`CLAUDE.md` 외)은 GitHub **check-run `pytest`**(CI 잡)가 `success` 일 때만 배포. **GitHub Actions 앱이 올린 체크만 인정**(같은 이름의 다른 앱 체크는 무시).
  결과를 모르면(API 오류·진행 중·레이트리밋·**응답 형태 이상**) 배포 안 함 — JSON 이 깨졌거나 기대한 형태가 아니면 스크립트가 죽지 않고 `unknown` 으로 보류(DM 없음, 15분 뒤 재확인, `journalctl -u quant-autopull` 에 `CI 상태 'unknown'`).
  실패 → 소유자 DM 1회(sha 당) 후 서버는 직전 버전 유지. 결과가 2시간 넘게 없으면 DM 1회.
  비코드만 바뀐 커밋은 CI 없이 반영하고 재시작도 안 함. `[skip ci]` 데이터 커밋이 끼면 범위 안 '마지막 코드 커밋'의 CI 로 판정.
  ⚠️ **저장소를 private 로 바꾸면** 이 게이트가 쓰는 무인증 공개 API 요청이 거부돼(HTTP 404) 결과가 영구 `unknown` → 코드 커밋이 **DM 도 없이 조용히 영구 보류**된다. 현재 스크립트는 토큰 인증을 지원하지 않으므로
  private 전환 전에 토큰 지원을 먼저 넣어야 한다 (`AUTOPULL_SKIP_CI=1` 은 비상용).
- **의존성**: `requirements.lock` 이 바뀐 커밋이면 코드 교체 **전에** `pip install -r`(실패하면 코드 미반영 + DM 1회, 15분마다 재시도). `pyproject.toml` 변경 시엔 editable 재등록만.
- **lock 불일치 감시** (`scripts/lock_drift.py`): 실행 중 가드(아래)를 통과한 매 주기에, 코드 교체 여부와 무관하게 서버 `.venv` 의 설치 버전을 배포 중인 `requirements.lock` 핀과 비교한다.
  다르면 **lock 내용당 1회** 소유자 DM — `[quant-bot 의존성 점검] 서버 .venv 가 requirements.lock 과 다름 … lock 불일치 N/M개 · 패키지 lock x / 설치 y …`(최대 8개 나열).
  **설치·업그레이드는 하지 않는다** — 일괄 업그레이드 여부는 아래 A/B 로 사용자가 결정. 같은 lock 으로는 불일치가 계속돼도 DM 이 다시 오지 않고, 서버 venv 를 lock 에 맞추면(또는 lock 을 서버에 맞추면) 더는 울리지 않는다.
- **롤백**: `reset --hard` → `sudo systemctl restart quant-bot` → 60초 대기 후 active 이고 `NRestarts` 불변이면 `.deployed_sha` 갱신. 실패하면 직전 sha 로 `reset --hard` + 재시작 + `data/.bad_sha` 에 **실효 코드 커밋**을 기록하고 `exit 1` → `OnFailure` DM.
  `.bad_sha` 코드 커밋은 새 코드 커밋이 올라올 때까지 재배포 안 함(수동 재시도: `rm data/.bad_sha`). 대시보드 커밋이 올라와 remote sha 가 달라져도 같은 불량 코드를 다시 배포하지 않는다.
- **실행 중 가드**: 다이제스트·재점수·KR 가격 갱신이 도는 중에는 코드를 교체하지 않고 다음 주기로 미룸(지연 import 가 새 코드를 섞어 읽는 것 방지).
- **fetch 실패**는 조용히 넘기고 연속 8회(≈2시간)째 1회만 DM. 15분마다 실패 알림이 쌓이지 않게 `notify_once` 로 사건당 1회.
- **비상 수동 반영** (CI 를 건너뜀): `AUTOPULL_SKIP_CI=1 /home/ubuntu/AI-Investment-Bot/scripts/server_autopull.sh`. 즉시 1회 실행: `sudo systemctl start quant-autopull.service`.
- 이 스크립트는 자기 자신을 배포하므로 CI 가 `bash -n scripts/server_autopull.sh` 를 검사한다(문법 오류 = 배포 영구 정지). 동작은 `tests/test_server_autopull.py` 의 시나리오 테스트가 실제 git/bash 로 검증.
- ⚠️ `main` 에 push 하면 ~15분 내 박스가 **자동 반영**된다. DB 스키마/마이그레이션 변경은 안전 확인 후 push (`CLAUDE.md §4.10 (9)`).

**의존성 고정 (`requirements.lock`)** — `uv pip compile pyproject.toml --extra hosting --python-version 3.12 --python-platform x86_64-manylinux_2_28` 로 생성(pandas 3.0.x · numpy 2.5 · yfinance 1.7 · libsql-experimental 0.0.55 등). CI·Actions 가 이 세트로 테스트한다.
**서버 venv 는 06 월 설치본**이라 테스트한 버전과 다를 수 있다 — 아래 중 하나를 사용자 결정(CURRENT_STATE §2.2 #6)으로 정한다. 구 autopull 은 이번 전환 push 에서 lock 을 설치하지 않으므로,
전환 push 뒤 첫 점검(≤15분)에 위 `[quant-bot 의존성 점검]` DM 이 오면 서버 venv 가 lock 과 다르다는 뜻이다 (venv 가 이미 lock 과 같으면 오지 않음). 이 DM 이 A/B 결정의 입력이다.
`requirements.lock` 에는 해시가 없어 버전만 고정·비교한다 (해시 고정은 `ROADMAP.md §2`).
- **B (기본): 테스트한 버전을 서버에 설치.** 먼저 현재 상태를 보관하고 수동으로 설치한다:
  ```
  cd ~/AI-Investment-Bot
  .venv/bin/pip freeze --exclude-editable > ~/freeze-before.txt
  .venv/bin/pip install -r requirements.lock
  .venv/bin/pip install --no-deps -e .
  sudo systemctl restart quant-bot
  ```
  문제가 생기면 `.venv/bin/pip install -r ~/freeze-before.txt` 로 복구.
- **A: 서버 현재 버전을 잠금 파일로.** `~/freeze-before.txt` 내용을 `requirements.lock` 으로 교체해 커밋(사용자가 검토 후). 이 경우 서버 버전은 그대로지만 **CI 가 테스트하는 버전과 달라질 수 있음**을 감수.
- 어느 쪽이든 이후 `requirements.lock` 변경 push 는 autopull 이 자동 설치한다. `fredapi` 는 코드에서 더 이상 쓰지 않지만 `pyproject.toml` 에 남겨 둠(전환 push 때 의존성 무고정 업그레이드 방지) — **다음 의존성 변경 때 함께 제거**.

### 6.2 레플리카 손상 복구 (`malformed WAL` 등)
**데이터 손실 없음** — Turso(클라우드)가 원본이고 로컬 레플리카 파일은 캐시라서, 지우면 다음에 여는 프로세스가 깨끗이 재싱크한다. 레플리카 파일은 3개이고 파일마다 여는 유닛이 달라 **멈출 대상이 다르다**(한 파일 = 한 프로세스, `CLAUDE.md §4.10 (9)`).
서버 `~/AI-Investment-Bot` 에서, 발송 시각(KR 08:30 KST · US 09:00 ET)과 재점수(토 10:00 KST)를 피해 실행한다.
새 파일은 전체를 받아야 해서 초기 pull 이 60–120초 이상 걸릴 수 있다 — 봇(60초)·다이제스트(180초)·재점수(300초)의 타임아웃을 넘기면 오프라인으로 강등되므로, 지운 뒤 `scripts/warm_replica.py` 로 **긴 타임아웃(1200초)으로 먼저 받아 두고**
유닛을 다시 켠다. 그 줄에서 `init_sync=ok elapsed=…` 가 나오면 성공(`timeout`/`error` 면 잠시 뒤 같은 줄을 다시).

| 파일 | 여는 유닛 | 복구 중 멈출 대상 |
|---|---|---|
| `data/quant_bot.db` | `quant-bot.service` | `quant-bot.service` |
| `data/digest_replica.db` | `quant-digest@kr.service` · `quant-digest@us.service` (+ 재점수 끝의 `warm_replica`, 수동 원장 명령) | `quant-digest-kr.timer` · `quant-digest-us.timer` |
| `data/universe_replica.db` | `quant-universe.service` · (설치했다면) `quant-kr-refresh.service` | `quant-universe.timer` · (설치했다면) `quant-kr-refresh.timer` |

봇 `quant_bot.db`:
```
sudo systemctl stop quant-bot
rm -f data/quant_bot.db data/quant_bot.db-wal data/quant_bot.db-shm data/quant_bot.db-info
QUANT_BOT_DB_PATH=data/quant_bot.db QUANT_BOT_SYNC_TIMEOUT=1200 .venv/bin/python scripts/warm_replica.py
sudo systemctl start quant-bot
```
다이제스트 `digest_replica.db` — 두 번째 줄 결과가 셋 다 `inactive` 일 때만 지운다(실행 중이면 끝난 뒤에):
```
sudo systemctl stop quant-digest-kr.timer quant-digest-us.timer
systemctl is-active quant-digest@kr.service quant-digest@us.service quant-universe.service
rm -f data/digest_replica.db data/digest_replica.db-wal data/digest_replica.db-shm data/digest_replica.db-info
QUANT_BOT_DB_PATH=data/digest_replica.db QUANT_BOT_SYNC_TIMEOUT=1200 flock -w 600 data/.digest.lock .venv/bin/python scripts/warm_replica.py
sudo systemctl start quant-digest-kr.timer quant-digest-us.timer
```
재점수 `universe_replica.db` — 두 번째 줄 결과가 둘 다 `inactive` 일 때만 지운다:
```
sudo systemctl stop quant-universe.timer
systemctl is-active quant-universe.service quant-kr-refresh.service
rm -f data/universe_replica.db data/universe_replica.db-wal data/universe_replica.db-shm data/universe_replica.db-info
QUANT_BOT_DB_PATH=data/universe_replica.db QUANT_BOT_SYNC_TIMEOUT=1200 flock -w 600 data/.universe.lock .venv/bin/python scripts/warm_replica.py
sudo systemctl start quant-universe.timer
```
- kr-refresh 를 설치했다면 재점수 블록의 `stop`/`start` 줄에 `quant-kr-refresh.timer` 도 함께 적는다.
- 타이머를 멈춘 사이에 발송·재점수 시각이 지나면, `Persistent=true` 인 타이머(다이제스트·재점수)는 다시 켤 때 그 회차를 곧바로 실행한다 (kr-refresh 는 `Persistent=false` 라 건너뜀).

## 7. 역할 분담 (중요 — offset 경합 방지)
- **봇(이 서버)** = `getUpdates` 폴링 **단독 소유**. 구독 명령(/start·/stop·/approve·/deny·/pending) + 조회(/stock·/scan·/ledger·/news·/help) **실시간** 처리. 조회는 active 구독자(+소유자)만. `/stock`·`/scan`·`/ledger` 배치는 처리 전에 최신 상태 pull (§5.2).
- **다이제스트 timer** = 일일 발송만: `send_digest.py --no-sync` + 전용 레플리카 경로.
  - ⚠️ `--no-sync` 필수 — 봇과 동시에 getUpdates 폴링하면 offset 경합으로 메시지 분실.
  - ⚠️ 봇·다이제스트·재점수는 **서로 다른 레플리카 파일** 필수 — 같은 파일이면 WAL 동시쓰기 충돌 (`CLAUDE.md §4.10 (9)`). 같은 레플리카를 쓰는 명령은 `flock` 으로 직렬화.
- 무거운 분석(유니버스 재점수)은 재점수 타이머가 Turso 에 사전계산 → 봇은 **DB 읽기 위주(경량)**, 다이제스트는 DB 점수 + yfinance 가격 줄.

## 8. 시크릿 (`.env`)
봇 필수 4개: `TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID`(소유자 겸 승인자·알림 수신자) · `TURSO_DATABASE_URL` · `TURSO_AUTH_TOKEN`.
다이제스트·재점수: `FRED_API_KEY` · `FMP_API_KEY` (+ KR 재점수 `KRX_API_KEY` · `DART_API_KEY`). LLM 요약을 켤 때만 `MINIMAX_API_KEY`(+`MINIMAX_MODEL` 확인).
키가 들어 있는지만 보기 (값 출력 안 함): `cut -d= -f1 .env`.

## 9. Gotchas
- **1GB RAM**: 첫 `/stock` 때 pandas 로드로 메모리 spike → swap 으로 커버. 재점수는 `MemoryMax=600M` 로 제한(OOM 시 봇보다 먼저 죽음). OOM-kill 보이면 swap/메모리 점검.
- **Turso 일시 장애**: 다이제스트는 초기 pull 타임아웃 시 오프라인 파일로 강등 → **발송 보류 + 소유자 DM**(종료코드 20). 초기 pull 이 빠르게 오류로 끝나면(`init_sync=error`) stale 레플리카로 발송하되 후보 비교(매수/탈락)만 보류(§5.3 동기화 지연일).
  봇은 600초 뒤 자진 재시작해 재프로브. 장기 장애 시 보류가 이어짐. 원장 CLI(`scripts/ledger.py`)는 강등 상태면 실행을 거부하고 exit 3.
- **libsql 마이그레이션**: 컬럼 추가는 `storage.add_column_if_missing`(중복 컬럼 오류 삼킴) — 로컬 PRAGMA(stale) vs 원격 ALTER 불일치 크래시 방지. 마이그레이션 스모크는 **stale 레플리카에서도**.
- **WAL 사이드카**(`data/*.db-*`)는 gitignore — 절대 커밋 금지(클론 시 레플리카 손상 원인). `data/` 아래 점파일(`.deployed_sha`·`.bad_sha`·`*.lock` 등 런타임 상태)도 gitignore.
- **운영 레플리카를 직접 열지 말 것** — 확인용 조회는 `/tmp/chk/` 로 복사한 사본에서(`digest_replica.db`·`-wal`·`-shm` 세 파일을 함께, 사본 만드는 법은 §10 단계 0). 실행 중 프로세스와 같은 파일을 열면 WAL 충돌.
- **초기 레플리카 풀 지연 (노트북/일회성 실행)**: 60–120s+ 걸릴 수 있음 → `QUANT_BOT_SYNC_TIMEOUT=240`.
- **대량 재점수/유니버스 빌드 시 캐시 끄기**: `QUANT_BOT_CACHE=off` — 켜면 심볼당 2-3회 Turso 원격 쓰기로 ~0.1 symbol/s, 끄면 ~1.5 symbol/s. 재점수 유닛은 이미 off.
- **대시보드 JSON 커밋**: Dashboard Export 가 JSON 을 `main` 에 `[skip ci]` 커밋(autopull 은 비코드 커밋으로 처리)한다. main 이 매일 움직이는 churn 은 알려진 사항 — 로컬에서 push 전엔 `git pull --rebase`.

---

## 10. 배포 순서 (수정 라운드 `fix/signal-quality-round`)

> 순서가 중요합니다. 특히 **원장 소급(backfill)은 첫 재점수 전, 첫 실발송 전**에 해야 합니다 (재점수가 `screened.updated_at` 을 바꾸면 소급 가드가 거부하고, 실발송이 원장에 live 행을 만들면 소급이 거부됨).
> 붙여넣기 명령은 한 줄씩(주석·glob 없음). 서버 명령은 `cd ~/AI-Investment-Bot` 에서.

**0. 사전 점검 (서버)** — 이번 라운드에서 서버를 직접 확인하지 못했습니다. 결과를 확인한 뒤 진행:
```
cd ~/AI-Investment-Bot
git rev-parse --short HEAD
systemctl list-timers --all --no-pager
systemctl cat quant-bot.service | grep -E "Type=|WatchdogSec|TimeoutStartSec|Restart=|ExecStart"
awk -F= '$1=="FRED_API_KEY"||$1=="FMP_API_KEY"||$1=="KRX_API_KEY"||$1=="DART_API_KEY"||$1=="MINIMAX_API_KEY"{print $1, (length($2)>0 ? "SET" : "EMPTY")}' .env
.venv/bin/pip freeze --exclude-editable
free -m
df -h /
mkdir -p /tmp/chk
cp data/digest_replica.db data/digest_replica.db-wal data/digest_replica.db-shm /tmp/chk/
.venv/bin/python -c 'import sqlite3;c=sqlite3.connect("/tmp/chk/digest_replica.db");[print(r) for r in c.execute("select market,count(1),sum(enriched),max(updated_at) from screened group by market")]'
```
확인할 것: 서버 코드가 `6403d60` 인지, 재점수 타이머가 없는지(`quant-universe` 없음), 키가 SET 인지, 마지막 두 줄로 **점수 `max(updated_at)` 이 정말 7월 그대로인지**(최근이면 첫 실발송이 원장에 live 행을 만들어 소급이 거부되므로 단계 2 전에 알려 주세요). `pip freeze` 결과는 의존성 결정 #6 에 필요.

**1. 브랜치를 먼저 올려 CI 확인 (이 Mac)** — 브랜치 push 는 서버에 반영되지 않음(autopull 은 `main` 만 봄):
```
git push origin fix/signal-quality-round
```
GitHub Actions 의 **CI / pytest** 가 녹색인지 확인. (이 단계는 사용자가 직접 — 에이전트는 push 하지 않음.)

**2. main 에 반영 (미국 저녁 메시지 직후 권장)** — 이 push 를 구 autopull 이 15분 안에 서버에 반영한다. `pyproject.toml` 은 바꾸지 않았으므로 의존성은 그대로:
```
git checkout main
git merge --ff-only fix/signal-quality-round
git push origin main
```
서버 반영 확인:
```
cd ~/AI-Investment-Bot
git log -1 --oneline
journalctl -u quant-autopull.service -n 20 --no-pager
```
> 이 시점부터 서버는 **새 코드 + 옛 유닛**. 단계 0 에서 확인한 대로 점수가 7월 그대로(≥15일 노후)라면 다이제스트는 '후보 보류' 로 나가고 원장에 기록하지 않아 소급에 영향 없음. 그래도 3→5 단계는 **다음 다이제스트 시각 전에** 끝낼 것.

**3. 의존성 정렬 (결정 #6)** — §6.1 의 B(설치) 또는 A(freeze 로 lock 교체)를 실행. 단계 2 직후 첫 autopull 점검에 `[quant-bot 의존성 점검]` DM 이 왔다면 서버 venv 가 lock 과 다르다는 뜻 — 이 단계가 그 해소이고, 정렬하면 더는 울리지 않는다.

**4. systemd 유닛 설치** — 기존 유닛을 백업한 뒤 저장소 원본으로 교체:
```
cd ~/AI-Investment-Bot
systemctl show -p User quant-bot quant-digest@kr
sudo chown -R ubuntu:ubuntu /home/ubuntu/AI-Investment-Bot
sudo cp /etc/systemd/system/quant-bot.service /home/ubuntu/quant-bot.service.bak
sudo cp /etc/systemd/system/quant-digest@.service /home/ubuntu/quant-digest@.service.bak
sudo cp /etc/systemd/system/quant-autopull.service /home/ubuntu/quant-autopull.service.bak
sudo cp deploy/systemd/quant-bot.service /etc/systemd/system/quant-bot.service
sudo cp deploy/systemd/quant-digest@.service /etc/systemd/system/quant-digest@.service
sudo cp deploy/systemd/quant-digest-kr.timer /etc/systemd/system/quant-digest-kr.timer
sudo cp deploy/systemd/quant-digest-us.timer /etc/systemd/system/quant-digest-us.timer
sudo cp deploy/systemd/quant-autopull.service /etc/systemd/system/quant-autopull.service
sudo cp deploy/systemd/quant-autopull.timer /etc/systemd/system/quant-autopull.timer
sudo cp deploy/systemd/quant-alert@.service /etc/systemd/system/quant-alert@.service
sudo cp deploy/systemd/quant-universe.service /etc/systemd/system/quant-universe.service
sudo cp deploy/systemd/quant-universe.timer /etc/systemd/system/quant-universe.timer
sudo systemctl daemon-reload
sudo systemctl restart quant-bot
systemctl status quant-bot --no-pager
sudo systemctl start quant-alert@autopull.service
```
봇이 `active (running)` 이고 알림 시험 DM 이 오는지 확인. (KR 가격 갱신을 쓰려면 `quant-kr-refresh.service`·`.timer` 도 같은 방식으로 복사하되, 첫 재점수가 끝난 뒤에 enable.)

**5. 원장 소급 기록 (07-13 발송분)** — 다이제스트 시각을 피해서: KR 08:30 KST, US 09:00 ET (= 22:00 KST 서머타임 / 2026-11-01 이후 23:00 KST). 각 줄 앞의 `QUANT_BOT_SYNC_TIMEOUT=240` 은 레플리카 초기 pull 이 60–120초 이상 걸려도 강등되지 않게 하는 값입니다(셸에서 직접 돌릴 땐 코드 기본 20초가 적용되므로 필수).
미리보기 결과가 텔레그램 07-13 메시지의 종목과 같은지 눈으로 확인한 뒤 `--confirm`:
```
QUANT_BOT_SYNC_TIMEOUT=240 QUANT_BOT_DB_PATH=data/digest_replica.db flock -w 600 data/.digest.lock .venv/bin/python scripts/ledger.py snapshot --source reconstructed --asof 2026-07-10
QUANT_BOT_SYNC_TIMEOUT=240 QUANT_BOT_DB_PATH=data/digest_replica.db flock -w 600 data/.digest.lock .venv/bin/python scripts/ledger.py backfill
QUANT_BOT_SYNC_TIMEOUT=240 QUANT_BOT_DB_PATH=data/digest_replica.db flock -w 600 data/.digest.lock .venv/bin/python scripts/ledger.py backfill --confirm
QUANT_BOT_SYNC_TIMEOUT=240 QUANT_BOT_DB_PATH=data/digest_replica.db flock -w 600 data/.digest.lock .venv/bin/python scripts/ledger.py score
QUANT_BOT_SYNC_TIMEOUT=240 QUANT_BOT_DB_PATH=data/digest_replica.db flock -w 600 data/.digest.lock .venv/bin/python scripts/ledger.py report us
```
소급 가드: 해당 시장에 live 행이 있으면 거부, 07-13 명시 리스트(US CALM HG EOG SM MGR AFGE / KR 009970 007700 284740 041510 073240 003090)와 스냅샷 v0 상위 6 이 다르면 중단.
`report us` 의 `소급 포함` 성과가 보이면 성공(07-13 이후 보유 수익률, 벤치 대비).
**저장소가 강등이면 원장 명령은 실행을 거부합니다** — 240초 안에도 초기 pull 이 끝나지 않아 빈 오프라인 파일이 열리면, 어떤 명령도 하지 않고 stderr 에 `[중단] 저장소 강등 — Turso 초기 pull 타임아웃으로 빈 오프라인 파일이 열렸습니다 …` 를
찍은 뒤 종료코드 3 으로 끝납니다(빈 파일 위에서 `score`/`report` 가 '원장 비어 있음' 으로 성공한 척하는 것을 막음). 이 문구가 보이면 그 줄의 결과는 무효이니 다음 줄로 넘어가지 말고 잠시 뒤 같은 줄을 다시 실행하고, 반복되면 Turso 연결 문제이니 멈추고 알려 주세요.
정상일 때는 초기 동기화 상태를 따로 출력하지 않습니다 — 각 명령의 결과 줄(`US: 저장 …`, `report us` 출력)이 나오는 것으로 확인합니다.

**6. 첫 재점수 수동 실행** — 약 25~30분 예상. 끝나면 소요 시간과 메모리 확인:
```
sudo systemctl start --no-block quant-universe.service
journalctl -u quant-universe -f
systemctl show quant-universe -p MemoryPeak -p ExecMainStatus
```
`ExecMainStatus=0` 이어야 정상(5 = 부분 완료/중단, 2 = 원격 DB 연결 불가). 끝난 뒤 KR/US 후보가 TTM 점수 기준으로 바뀌고 '후보 보류' 가 풀린다 — **규칙 전환일**(판정 사유 `규칙 v1 로 후보 재구성 (편입 N · 탈락 M)`, 후보가 같으면 `규칙 v1 전환 (후보 동일)`) 메시지가 처음 나가는 날.
재점수 직후 레플리카 미리 pull(§5.4)의 소요 시간도 기록해 두세요 — 월요일 다이제스트의 첫 pull(타임아웃 180초)이 충분한지 판단하는 값입니다:
```
journalctl -u quant-universe.service --since "7 days ago" --no-pager -o cat | grep init_sync
```
`init_sync=ok elapsed=NN.Ns db=…digest_replica.db` 가 나오면 정상. `timeout`/`error` 면 미리 pull 이 실패한 것(재점수 자체는 성공) — 월요일 다이제스트의 첫 pull 이 길어질 수 있으니 알려 주세요.

**7. 주간 재점수 타이머 켜기:**
```
sudo systemctl enable --now quant-universe.timer
systemctl list-timers --all --no-pager
```
(선택) `quant-kr-refresh.timer` 도 같은 방식으로 enable.

**8. 대시보드 갱신 다시 켜기 (GitHub 웹)** — 저장소 → Actions → 왼쪽 'Dashboard Export' → 'Enable workflow' → 'Run workflow'(main).
'Export dashboard JSON' 단계가 녹색이고 `us_data.json` 에 `data_as_of` 가 있는지, 대시보드 페이지가 갱신됐는지 확인. 60일 무활동 자동 비활성화는 정상 가동 중엔 재발하지 않는다.

**9. 구독자 공지 1회 (텔레그램 소유자 계정으로 봇에):**
`/announce 메시지 형식이 바뀝니다. 첫 줄 "오늘 할 일"만 보고 넘길 수 있게 했고, 변화가 있는 날만 길어집니다. 종목마다 가격·200일선·선정 후 성과와 봇 후보의 누적 성적을 보여드립니다. 검증 전인 예측과 제안 비중은 뺐습니다.`

**10. 정리 (이 Mac, 권장)** — `.claude/settings.local.json` 에서 `"Bash(git push *)"` 와 `"Bash(.venv/bin/python scripts/send_digest.py)"` 두 줄을 지우면 push 와 실발송이 매번 확인을 거친다.

**롤백**: 코드는 `git push` 로 되돌릴 필요 없이 autopull 의 자동 롤백이 기동 실패를 막는다. 유닛은 4단계에서 만든 `~/*.service.bak` 을 `/etc/systemd/system/` 에 복사 후 `daemon-reload`.
원장 소급을 되돌려야 하면 사용자 수동(`signal_ledger` 의 `source='reconstructed'` 행 삭제) — 되도록 미리보기에서 확인하고 진행.
