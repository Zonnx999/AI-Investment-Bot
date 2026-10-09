"""scripts/server_autopull.sh 시나리오 테스트 (오프라인).

실제 서버 대신 임시 디렉터리에 가짜 환경을 만든다:
  - bare origin + dev clone + server clone (진짜 git)
  - PATH 앞쪽의 shim: curl(GitHub check-runs 응답 흉내) / systemctl / sudo / flock / pip
  - 상태는 $SIMSTATE 디렉터리 파일로 주입 (ci_<sha>, restart_fails, pip_fails ...)

네트워크·systemd·sudo 를 전혀 쓰지 않는다. bash / git 이 없으면 skip.
검증 대상: CI 게이트(success/failure/missing/pending/unknown), 비코드 커밋 무재시작,
skip-ci 폴백, DM 1회 규칙, 크래시루프 롤백, pip 선실패 시 코드 불변, 실행 중 스킵.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REAL_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server_autopull.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="bash/git 필요",
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}

_SHIMS = {
    "curl": r"""#!/bin/bash
url="${@: -1}"; sha=$(echo "$url" | sed -E 's#.*/commits/([0-9a-f]+)/check-runs.*#\1#')
echo "curl $sha" >> $SIMSTATE/calls
f=$SIMSTATE/ci_$sha; if [ -f "$f" ]; then c=$(cat $f); else c=none; fi
case $c in
  none) echo '{"total_count":0,"check_runs":[]}';;
  apierr) exit 22;;
  pending) echo '{"check_runs":[{"id":1,"name":"pytest","status":"in_progress","conclusion":null,"app":{"slug":"github-actions"}}]}';;
  rawbody) cat "$SIMSTATE/body_$sha";;
  otherapp) echo '{"check_runs":[{"id":1,"name":"pytest","status":"completed","conclusion":"success","app":{"slug":"evil-app"}}]}';;
  *) echo "{\"check_runs\":[{\"id\":1,\"name\":\"pytest\",\"status\":\"completed\",\"conclusion\":\"$c\",\"app\":{\"slug\":\"github-actions\"}}]}";;
esac
""",
    "systemctl": r"""#!/bin/bash
D=$SIMSTATE
case "$1" in
  is-active) u="$2"; if [ "$u" = "quant-bot" ]; then cat $D/bot_active 2>/dev/null || echo active; else cat "$D/active_$u" 2>/dev/null || echo inactive; fi ;;
  show) n=$(cat $D/nrestarts 2>/dev/null || echo 0); if [ -f $D/restart_breaks ]; then n=$((n+1)); echo $n > $D/nrestarts; fi; echo $n ;;
  restart) echo "restart $2" >> $D/calls; [ -f $D/restart_fails ] && exit 1; exit 0 ;;
esac
""",
    "sudo": '#!/bin/bash\n"$@"\n',
    "flock": '#!/bin/bash\n[ -f "$SIMSTATE/flock_busy" ] && exit 1\nexit 0\n',
}

_PIP = '#!/bin/bash\necho "pip $*" >> $SIMSTATE/calls; [ -f $SIMSTATE/pip_fails ] && exit 1; exit 0\n'
_NOTIFY = 'import sys, os; open(os.environ["SIMSTATE"] + "/dm", "a").write(" ".join(sys.argv[1:]) + "\\n")\n'


def _exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Sim:
    def __init__(self, root: Path, template: Path | None = None):
        """template 이 있으면 (git clone 8회를 반복하지 않도록) 통째로 복사하고 remote URL 만 고친다."""
        self.root = root
        self.state = root / "state"
        self.shims = root / "shims"
        self.origin = root / "origin.git"
        self.dev = root / "dev"
        self.server = root / "server"
        if template is not None:
            shutil.copytree(template, root, symlinks=True, dirs_exist_ok=True)
            self.git(self.dev, "remote", "set-url", "origin", str(self.origin))
            self.git(self.server, "remote", "set-url", "origin", str(self.origin))
            return
        self.state.mkdir()
        for name, body in _SHIMS.items():
            _exe(self.shims / name, body)
        self.git(root, "init", "--bare", "-q", "-b", "main", str(self.origin))
        self.git(root, "clone", "-q", str(self.origin), str(self.dev))
        self.git(self.dev, "checkout", "-q", "-b", "main")
        self.commit(
            {
                "scripts/server_autopull.sh": REAL_SCRIPT.read_text(),
                "scripts/notify_owner.py": _NOTIFY,
                "src/a.py": "v = 1\n",
                "dashboard/x.json": "{}\n",
                "requirements.lock": "a==1\n",
            }
        )
        self.git(root, "clone", "-q", "-b", "main", str(self.origin), str(self.server))
        (self.server / "scripts" / "server_autopull.sh").chmod(0o755)
        _exe(self.server / ".venv" / "bin" / "pip", _PIP)
        py = self.server / ".venv" / "bin" / "python"
        py.symlink_to(sys.executable)

    # -- git helpers -------------------------------------------------
    def git(self, cwd: Path, *args: str, env_extra: dict | None = None) -> str:
        env = {**os.environ, **_GIT_ENV, **(env_extra or {})}
        r = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
        )
        return r.stdout.strip()

    def commit(self, files: dict[str, str], date: str | None = None) -> str:
        for rel, text in files.items():
            p = self.dev / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git(self.dev, "add", "--", *files)
        extra = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else None
        self.git(self.dev, "commit", "-q", "-m", "c", env_extra=extra)
        self.git(self.dev, "push", "-q", "origin", "main")
        return self.git(self.dev, "rev-parse", "HEAD")

    def head(self) -> str:
        return self.git(self.server, "rev-parse", "HEAD")

    # -- state helpers -----------------------------------------------
    def mark_deployed(self) -> str:
        sha = self.head()
        (self.server / "data").mkdir(exist_ok=True)
        (self.server / "data" / ".deployed_sha").write_text(sha + "\n")
        return sha

    def ci(self, sha: str, result: str) -> None:
        (self.state / f"ci_{sha}").write_text(result)

    def flag(self, name: str, on: bool = True) -> None:
        f = self.state / name
        f.write_text("1") if on else f.unlink(missing_ok=True)

    def set_text(self, name: str, text: str) -> None:
        (self.state / name).write_text(text)

    def read(self, name: str) -> str:
        p = self.state / name
        return p.read_text() if p.exists() else ""

    def data(self, name: str) -> str:
        p = self.server / "data" / name
        return p.read_text().strip() if p.exists() else ""

    def run(self, **env_extra: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            **_GIT_ENV,
            "PATH": f"{self.shims}{os.pathsep}{os.environ['PATH']}",
            "SIMSTATE": str(self.state),
            "AUTOPULL_HEALTH_WAIT": "0",
            **env_extra,
        }
        return subprocess.run(
            ["bash", str(self.server / "scripts" / "server_autopull.sh")],
            cwd=self.server,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )


@pytest.fixture(scope="module")
def _sim_template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("sim_template")
    Sim(root)
    return root


@pytest.fixture
def sim(tmp_path: Path, _sim_template: Path) -> Sim:
    return Sim(tmp_path, template=_sim_template)


def test_script_syntax():
    r = subprocess.run(["bash", "-n", str(REAL_SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_first_run_initializes_deployed_sha(sim):
    base = sim.head()
    r = sim.run()
    assert r.returncode == 0
    assert sim.data(".deployed_sha") == base
    assert "curl" not in sim.read("calls")


def test_up_to_date_is_silent(sim):
    sim.mark_deployed()
    r = sim.run()
    assert r.returncode == 0 and r.stdout == ""
    assert sim.read("calls") == ""


def test_code_change_without_ci_result_waits(sim):
    deployed = sim.mark_deployed()
    sim.commit({"src/a.py": "v = 2\n"})
    r = sim.run()
    assert r.returncode == 0
    assert sim.head() == deployed
    assert "restart" not in sim.read("calls")
    assert sim.read("dm") == ""          # 방금 푸시 → 2시간 이내라 DM 없음


@pytest.mark.parametrize("state", ["pending", "apierr"])
def test_pending_or_unknown_ci_waits(sim, state):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, state)
    r = sim.run()
    assert r.returncode == 0
    assert sim.head() == deployed
    assert "restart" not in sim.read("calls")


@pytest.mark.parametrize("body", [
    "[]",                                              # 최상위가 list
    "null",
    '"x"',
    '{"check_runs": "oops"}',                          # check_runs 가 list 아님
    '{"message": "API rate limit exceeded"}',          # check_runs 없음
    '{"check_runs": [1, "a"]}',                        # 원소가 dict 아님
    "not json at all",
    '{"check_runs": [{"id": "x", "status": "completed", "conclusion": "success"}, 5]}',
])
def test_malformed_ci_response_is_unknown_not_crash(sim, body):
    """SEC-05 — 비정상 JSON 이어도 스크립트가 죽지 않고(exit 0) 배포만 보류."""
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "rawbody")
    sim.set_text(f"body_{sha}", body)
    r = sim.run()
    assert r.returncode == 0, r.stderr
    assert sim.head() == deployed
    assert "restart" not in sim.read("calls")
    assert sim.read("dm") == ""
    assert "Traceback" not in r.stderr


def test_check_from_other_app_is_ignored(sim):
    """다른 앱이 같은 이름('pytest') 으로 올린 success 체크는 게이트로 인정하지 않는다."""
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "otherapp")
    r = sim.run()
    assert r.returncode == 0
    assert sim.head() == deployed                      # 배포 안 됨
    assert "restart" not in sim.read("calls")


def test_ci_success_deploys_and_restarts(sim):
    sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "success")
    r = sim.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert sim.head() == sha
    assert sim.data(".deployed_sha") == sha
    assert "restart quant-bot" in sim.read("calls")
    assert (sim.server / "src" / "a.py").read_text() == "v = 2\n"


def test_dashboard_only_commit_applies_without_restart_or_ci(sim):
    sim.mark_deployed()
    sha = sim.commit({"dashboard/x.json": '{"a": 1}\n'})
    r = sim.run()
    assert r.returncode == 0
    assert sim.head() == sha
    assert sim.data(".deployed_sha") == sha
    calls = sim.read("calls")
    assert "restart" not in calls and "curl" not in calls


def test_skip_ci_data_commit_uses_last_code_commit_ci(sim):
    sim.mark_deployed()
    code = sim.commit({"src/a.py": "v = 2\n"})
    data = sim.commit({"dashboard/x.json": '{"b": 2}\n'})     # [skip ci] 데이터 커밋 — CI 결과 없음
    sim.ci(code, "success")
    r = sim.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert sim.head() == data
    assert "restart quant-bot" in sim.read("calls")


def test_ci_failure_blocks_and_notifies_once(sim):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "failure")
    assert sim.run().returncode == 0
    assert sim.run().returncode == 0
    assert sim.head() == deployed
    assert "restart" not in sim.read("calls")
    assert len(sim.read("dm").splitlines()) == 1        # 같은 사건은 DM 1회


def test_missing_ci_older_than_two_hours_notifies_once(sim):
    deployed = sim.mark_deployed()
    sim.commit({"src/a.py": "v = 2\n"}, date="2020-01-01T00:00:00+00:00")
    assert sim.run().returncode == 0
    assert sim.run().returncode == 0
    assert sim.head() == deployed
    assert len(sim.read("dm").splitlines()) == 1


def test_skip_ci_override_deploys(sim):
    sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    r = sim.run(AUTOPULL_SKIP_CI="1")
    assert r.returncode == 0
    assert sim.head() == sha
    assert "curl" not in sim.read("calls")


def test_crash_loop_rolls_back_and_marks_bad(sim):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "success")
    sim.flag("restart_breaks")                       # NRestarts 가 계속 증가 = 크래시 루프
    r = sim.run()
    assert r.returncode == 1
    assert "ROLLBACK" in r.stdout
    assert sim.head() == deployed
    assert sim.data(".deployed_sha") == deployed
    assert sim.data(".bad_sha") == sha
    assert sim.read("calls").count("restart quant-bot") == 2    # 배포 재시작 + 롤백 재시작

    # 다음 주기: 롤백된 sha 는 새 커밋이 오기 전까지 재시도하지 않음
    sim.flag("restart_breaks", False)
    before = sim.read("calls")
    r2 = sim.run()
    assert r2.returncode == 0
    assert sim.read("calls") == before


def test_rollback_then_dashboard_commit_does_not_redeploy_bad_code(sim):
    """롤백 뒤 [skip ci] 대시보드 커밋이 올라와도 같은 불량 코드를 재배포하지 않는다."""
    deployed = sim.mark_deployed()
    bad = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(bad, "success")
    sim.flag("restart_breaks")
    assert sim.run().returncode == 1
    assert sim.data(".bad_sha") == bad

    sim.flag("restart_breaks", False)
    sim.commit({"dashboard/x.json": '{"c": 3}\n'})       # CI 결과 없는 데이터 커밋
    before = sim.read("calls")
    r = sim.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert sim.read("calls") == before                  # restart 도 CI 조회도 없음
    assert sim.head() == deployed

    # 새 코드 커밋(수정본)이 오면 정상 배포
    fix = sim.commit({"src/a.py": "v = 3\n"})
    sim.ci(fix, "success")
    r2 = sim.run()
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert sim.head() == fix
    assert sim.data(".deployed_sha") == fix
    assert sim.data(".bad_sha") == ""


def test_fetch_failure_is_silent_then_notifies_once_at_threshold(sim):
    deployed = sim.mark_deployed()
    sim.git(sim.server, "remote", "set-url", "origin", str(sim.root / "nope.git"))
    for _ in range(2):
        r = sim.run(AUTOPULL_FETCH_FAIL_NOTIFY="3")
        assert r.returncode == 0                        # 일시 장애는 OnFailure 를 타지 않는다
    assert sim.read("dm") == ""
    assert sim.run(AUTOPULL_FETCH_FAIL_NOTIFY="3").returncode == 0
    assert len(sim.read("dm").splitlines()) == 1        # 3회째에 1회
    assert sim.run(AUTOPULL_FETCH_FAIL_NOTIFY="3").returncode == 0
    assert len(sim.read("dm").splitlines()) == 1        # 이후 반복 없음
    assert sim.head() == deployed and sim.read("calls") == ""

    # 복구되면 카운터 초기화
    sim.git(sim.server, "remote", "set-url", "origin", str(sim.origin))
    assert sim.run().returncode == 0
    assert sim.data(".autopull_fetch_fail") == ""


def test_bot_not_active_after_restart_rolls_back(sim):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "success")
    sim.set_text("bot_active", "failed")
    r = sim.run()
    assert r.returncode == 1
    assert sim.head() == deployed
    assert sim.data(".bad_sha") == sha


def test_restart_command_failure_rolls_back(sim):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "success")
    sim.flag("restart_fails")
    r = sim.run()
    assert r.returncode == 1
    assert sim.head() == deployed
    assert sim.data(".bad_sha") == sha


def test_pip_failure_leaves_code_untouched_then_retries(sim):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n", "requirements.lock": "a==2\n"})
    sim.ci(sha, "success")
    sim.flag("pip_fails")
    r = sim.run()
    assert r.returncode == 0                            # exit 1 이면 매 주기 OnFailure DM 폭주
    assert sim.head() == deployed                       # reset 전에 설치 → 코드 불변
    assert sim.data(".deployed_sha") == deployed
    assert "restart" not in sim.read("calls")
    assert len(sim.read("dm").splitlines()) == 1
    assert sim.run().returncode == 0                    # 지속 실패해도 같은 sha 는 DM 1회
    assert len(sim.read("dm").splitlines()) == 1

    sim.flag("pip_fails", False)                        # 다음 주기에 자동 재시도
    r2 = sim.run()
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert sim.head() == sha
    assert sim.data(".deployed_sha") == sha
    assert "pip install --quiet -r" in sim.read("calls")


def test_digest_running_defers_deploy(sim):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "success")
    sim.set_text("active_quant-digest@kr.service", "active")
    r = sim.run()
    assert r.returncode == 0
    assert sim.head() == deployed
    assert "restart" not in sim.read("calls")
    assert "curl" not in sim.read("calls")


def test_concurrent_run_is_skipped(sim):
    deployed = sim.mark_deployed()
    sha = sim.commit({"src/a.py": "v = 2\n"})
    sim.ci(sha, "success")
    sim.flag("flock_busy")
    r = sim.run()
    assert r.returncode == 0
    assert sim.head() == deployed


# ---------------------------------------------------------------------------
# deploy/systemd 유닛 정합성 (systemd-analyze 없이 구조만 점검)
# ---------------------------------------------------------------------------
UNIT_DIR = REAL_SCRIPT.parents[1] / "deploy" / "systemd"
_EXPECTED_UNITS = {
    "quant-alert@.service",
    "quant-autopull.service",
    "quant-autopull.timer",
    "quant-bot.service",
    "quant-digest-kr.timer",
    "quant-digest-us.timer",
    "quant-digest@.service",
    "quant-kr-refresh.service",
    "quant-kr-refresh.timer",
    "quant-universe.service",
    "quant-universe.timer",
}
_ALERT_ALIASES = {"bot", "digest-kr", "digest-us", "universe", "kr-refresh", "autopull"}


def _unit_lines(name: str) -> list[str]:
    return [
        ln.strip()
        for ln in (UNIT_DIR / name).read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_systemd_unit_set_is_complete():
    assert {p.name for p in UNIT_DIR.iterdir()} == _EXPECTED_UNITS


def test_timers_point_to_existing_services():
    for timer in UNIT_DIR.glob("*.timer"):
        targets = [ln.split("=", 1)[1] for ln in _unit_lines(timer.name) if ln.startswith("Unit=")]
        if timer.name == "quant-autopull.timer":
            assert targets == []                       # Unit= 생략 → 같은 이름의 .service
            assert (UNIT_DIR / "quant-autopull.service").exists()
        else:
            assert len(targets) == 1, timer.name
            # 템플릿 인스턴스(quant-digest@kr.service) 는 quant-digest@.service 로 확인
            tmpl = targets[0].replace("@kr.", "@.").replace("@us.", "@.")
            assert (UNIT_DIR / tmpl).exists(), f"{timer.name} -> {targets[0]}"
        assert any(ln.startswith(("OnCalendar=", "OnBootSec=")) for ln in _unit_lines(timer.name))


def test_service_units_have_valid_alert_alias_and_scripts():
    root = REAL_SCRIPT.parents[1]
    for svc in UNIT_DIR.glob("*.service"):
        lines = _unit_lines(svc.name)
        for ln in lines:
            if ln.startswith("OnFailure="):
                target = ln.split("=", 1)[1]
                assert target.startswith("quant-alert@") and target.endswith(".service")
                alias = target[len("quant-alert@"):-len(".service")]
                if "%i" in alias:                      # digest-%i → digest-kr | digest-us
                    assert alias.replace("%i", "kr") in _ALERT_ALIASES
                    assert alias.replace("%i", "us") in _ALERT_ALIASES
                else:
                    assert alias in _ALERT_ALIASES, f"{svc.name}: {alias}"
        # ExecStart 의 scripts/ 경로는 저장소에 있어야 한다 (notify_owner 는 WP-OPS-CORE 소유)
        for ln in lines:
            if ln.startswith("ExecStart="):
                for tok in ln.split():
                    if "scripts/" in tok:
                        rel = "scripts/" + tok.split("scripts/", 1)[1]
                        if rel.endswith("notify_owner.py"):
                            continue
                        assert (root / rel).exists(), f"{svc.name}: {rel}"


def test_units_respect_one_replica_one_process():
    bot = "\n".join(_unit_lines("quant-bot.service"))
    assert "QUANT_BOT_DB_PATH=/home/ubuntu/AI-Investment-Bot/data/quant_bot.db" in bot
    for name in ("quant-digest@.service", "quant-universe.service", "quant-kr-refresh.service"):
        text = "\n".join(_unit_lines(name))
        assert "quant_bot.db" not in text, name        # §4.10 #9: 한 레플리카 파일 = 한 프로세스
        assert "flock" in text, name                   # 같은 레플리카를 쓰는 유닛끼리 직렬화


# 예정 플래그 없음 — WP-UNIVERSE 병합으로 --kr-refresh 가 실제 정의됨 (일반 검사로 고정).
_PENDING_BUILD_UNIVERSE_FLAGS: set[str] = set()


def test_build_universe_flags_in_units_are_defined():
    """유닛 ExecStart 가 build_universe.py 에 없는 CLI 플래그를 쓰면(argparse exit 2) 매일 DM 이 난다."""
    import re

    src = (REAL_SCRIPT.parent / "build_universe.py").read_text()
    defined = set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', src))
    assert "--discover" in defined                     # 정규식 자체 점검
    for svc in UNIT_DIR.glob("*.service"):
        for ln in _unit_lines(svc.name):
            if ln.startswith("ExecStart=") and "build_universe.py" in ln:
                flags = set(re.findall(r"\s(--[a-z0-9-]+)", ln.split("build_universe.py", 1)[1]))
                missing = flags - defined - _PENDING_BUILD_UNIVERSE_FLAGS
                assert not missing, f"{svc.name}: build_universe.py 에 없는 플래그 {missing}"
