"""
scripts/lock_drift.py
=====================
서버 .venv 의 설치 버전이 requirements.lock 과 같은지 점검 (RVOPS-03).

CI·워크플로는 requirements.lock 으로 설치·테스트하지만, 서버 .venv 는 lock 이 '바뀐 커밋' 에서만
autopull 이 설치한다 — lock 이 처음 들어온 배포나 수동 설치본은 lock 과 어긋난 채 남을 수 있고,
그러면 'CI 녹색 = 서버에서 돌 코드가 검증됨' 전제가 깨진다. 이 스크립트는 그 어긋남을 **보여주기만**
한다 (설치·업그레이드는 하지 않음 — 일괄 업그레이드 여부는 소유자 결정, docs/DEPLOYMENT.md).

사용: .venv/bin/python scripts/lock_drift.py requirements.lock
종료코드: 0 = 일치(또는 lock 을 못 읽음 — 경고만), 3 = 불일치 (stdout 에 요약 1줄 + 항목들)
표준 라이브러리만 사용 (src·pandas import 금지 — autopull 이 15분마다 부르는 가벼운 점검).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from importlib import metadata

EXIT_MATCH = 0
EXIT_DRIFT = 3
MAX_SHOWN = 8

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\#]+)")


def normalize(name: str) -> str:
    """PEP 503 이름 정규화 (libsql_experimental == libsql-experimental)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> dict[str, str]:
    """lock 본문 → {정규화 이름: 버전}. 주석·'# via' 줄·옵션 줄은 무시 (순수)."""
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        m = _PIN_RE.match(line)
        if m:
            pins[normalize(m.group(1))] = m.group(2)
    return pins


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def find_drift(pins: dict[str, str],
               installed: Callable[[str], str | None] | None = None) -> list[tuple[str, str, str | None]]:
    """lock 핀과 다른 패키지 → [(이름, lock 버전, 설치 버전 또는 None)] (이름순).

    installed: 이름 → 설치 버전 (None=미설치). 기본은 importlib.metadata (현재 인터프리터의 .venv)."""
    installed = installed or _installed_version
    out = []
    for name in sorted(pins):
        have = installed(name)
        if have != pins[name]:
            out.append((name, pins[name], have))
    return out


def format_drift(drift: list[tuple[str, str, str | None]], total: int) -> str:
    """불일치 요약 (평문 — 소유자 DM 에 그대로 실림)."""
    head = f"lock 불일치 {len(drift)}/{total}개"
    items = [f"{n} lock {want} / 설치 {have or '없음'}" for n, want, have in drift[:MAX_SHOWN]]
    more = [f"외 {len(drift) - MAX_SHOWN}개"] if len(drift) > MAX_SHOWN else []
    return " · ".join([head, *items, *more])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else "requirements.lock"
    try:
        with open(path, encoding="utf-8") as f:
            pins = parse_lock(f.read())
    except OSError as e:
        print(f"lock 을 읽을 수 없음: {e}", file=sys.stderr)
        return EXIT_MATCH
    drift = find_drift(pins)
    if not drift:
        return EXIT_MATCH
    print(format_drift(drift, len(pins)))
    return EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
