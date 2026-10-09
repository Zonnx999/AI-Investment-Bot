"""systemd 유닛·워크플로 정적 검증 — 시간 예산 관계 고정 (RVOPS-07), 워크플로 권한 (SEC-04)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST_UNIT = ROOT / "deploy" / "systemd" / "quant-digest@.service"
WORKFLOW = ROOT / ".github" / "workflows" / "daily-digest.yml"


def _unit_lines(key: str) -> list[str]:
    out = []
    for line in DIGEST_UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        m = re.match(rf"^{key}=(.*)$", line.strip())
        if m:
            out.append(m.group(1))
    return out


def _flock_wait(cmd: str) -> int:
    m = re.search(r"flock\s+-w\s+(\d+)", cmd)
    assert m, f"flock -w 없음: {cmd}"
    return int(m.group(1))


def test_digest_unit_time_budget_relation():
    timeout_start = int(_unit_lines("TimeoutStartSec")[0])
    start = _unit_lines("ExecStart")[0]
    post = _unit_lines("ExecStartPost")[0]
    start_wait = _flock_wait(start)
    post_wait = _flock_wait(post)
    post_timeout = int(re.search(r"/timeout\s+(\d+)", post).group(1))
    body_margin = 300          # 본체(send_digest) 여유 — 최소 5분
    assert post_wait <= 60     # 채점은 포기하고 다음 런이 이어받음
    assert timeout_start >= start_wait + body_margin + post_wait + post_timeout
    assert timeout_start == 1500


def test_digest_unit_post_ignores_failure_and_keeps_flock():
    post_raw = [l for l in DIGEST_UNIT.read_text(encoding="utf-8").splitlines()
                if l.startswith("ExecStartPost=")][0]
    assert post_raw.startswith("ExecStartPost=-")      # 채점 실패는 무시
    assert ".digest.lock" in post_raw


def test_workflow_has_least_privilege_permissions():
    text = WORKFLOW.read_text(encoding="utf-8")
    top = text.split("jobs:")[0]
    assert re.search(r"^permissions:\s*\n\s+contents:\s*read\s*$", top, re.M)


def test_workflow_validates_dispatch_market():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "in us|kr) ;;" in text and "exit 1" in text
