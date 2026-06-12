"""
scripts/demo_http.py
====================
3단계(HTTP 견고성) 검증용 데모 스크립트.

로컬 HTTP 서버를 띄워 외부 네트워크 없이 검증합니다:
  CASE 1  의도적 5xx → 표준 세션이 backoff (1s → 2s) 로 자동 재시도 → 성공
  CASE 2  계속 5xx → 재시도 소진 → 마지막 response 반환 (도메인 예외 변환은 호출부 몫)
  CASE 3  응답 지연 → 타임아웃 강제 적용 확인
  CASE 4  API 키 마스킹 — 로그 메시지/traceback 에 키가 ***REDACTED*** 로 보임

실행:
    python scripts/demo_http.py
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src.config import settings  # noqa: E402
from src.http import (  # noqa: E402
    REDACTED,
    build_session,
    get_http_session,
    is_timeout,
    mask_secrets,
)
from src.logger import get_logger, setup_logging  # noqa: E402

log = get_logger("demo.http")

# 데모용 가짜 키 주입 — 실제 키 없이도 마스킹 검증 가능.
# (frozen dataclass 우회는 demo_exceptions.py 와 같은 패턴)
FAKE_KEY = "demo_fake_fmp_key_abcdef123456"
object.__setattr__(settings, "fmp_api_key", FAKE_KEY)


class _DemoHandler(BaseHTTPRequestHandler):
    """경로별로 다른 시나리오를 연기하는 로컬 서버 핸들러."""

    flaky_hits = 0  # /flaky 가 몇 번 호출됐는지 (재시도 횟수 증거)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 시그니처
        cls = type(self)
        if self.path == "/flaky":
            cls.flaky_hits += 1
            if cls.flaky_hits < 3:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"boom")
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
        elif self.path == "/always500":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"down")
        elif self.path == "/slow":
            time.sleep(3)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"too late")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # noqa: ANN001 — 시그니처 그대로
        log.debug("local-server: " + fmt, *args)


def main() -> None:
    log_path = setup_logging()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _DemoHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    log.info("=" * 60)
    log.info("3단계 데모 시작 — HTTP 견고성 검증 (로컬 서버 포트 %d)", port)
    log.info("=" * 60)

    # ----- CASE 1: 5xx 두 번 → 자동 재시도 → 성공 -----
    log.info("")
    log.info("[CASE 1] /flaky : 500, 500 후 200 — 자동 재시도로 살아나는지")
    session = get_http_session()
    t0 = time.monotonic()
    resp = session.get(f"{base}/flaky")
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200, f"기대 200, 실제 {resp.status_code}"
    assert _DemoHandler.flaky_hits == 3, f"기대 3회 호출, 실제 {_DemoHandler.flaky_hits}"
    log.info(
        "  ✅ 서버가 3번 맞고 (500→500→200) 최종 200 — 총 %.1fs (backoff 0s+2s 포함)",
        elapsed,
    )

    # ----- CASE 2: 계속 5xx → 재시도 소진 → 마지막 response 반환 -----
    log.info("")
    log.info("[CASE 2] /always500 : 재시도 소진 후 마지막 503 response 반환")
    fast = build_session(backoff_factor=0.1)  # 데모가 7초 기다리지 않도록 backoff 만 단축
    resp = fast.get(f"{base}/always500")
    assert resp.status_code == 503
    log.info("  ✅ 예외 대신 status=%d response — 호출부가 도메인 예외로 변환하는 구조", resp.status_code)

    # ----- CASE 3: 타임아웃 강제 -----
    log.info("")
    log.info("[CASE 3] /slow (3s 지연) vs read timeout 1s — 타임아웃이 강제되는지")
    impatient = build_session(timeout=(2.0, 1.0), retry_total=0)
    t0 = time.monotonic()
    try:
        impatient.get(f"{base}/slow")
        raise AssertionError("타임아웃이 발생했어야 함")
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        # Retry 개입 시 read timeout 은 ConnectionError 로 감싸여 나옴 → is_timeout 으로 판별
        assert is_timeout(e), f"타임아웃으로 판별돼야 함: {type(e).__name__}"
        log.info(
            "  ✅ %.1fs 만에 끊김 + is_timeout()=True — 명시 안 해도 세션이 강제",
            time.monotonic() - t0,
        )

    # ----- CASE 4: API 키 마스킹 -----
    log.info("")
    log.info("[CASE 4] 로그 메시지 + traceback 에서 키가 %s 로 가려지는지", REDACTED)
    masked = mask_secrets(f"https://fmp.example/quote?symbol=AAPL&apikey={FAKE_KEY}")
    assert FAKE_KEY not in masked and REDACTED in masked
    log.info("  mask_secrets() → %s", masked)

    try:
        # 실제 leak 시나리오 재현: 예외 메시지에 키 포함 URL 이 박힘
        raise requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool: Max retries exceeded with url: /quote?apikey={FAKE_KEY}"
        )
    except requests.exceptions.ConnectionError:
        log.exception("CASE 4 의도적 예외 — 아래 traceback 의 apikey 가 가려져야 함")

    # 파일 로그를 직접 읽어 증명
    tail = log_path.read_text(encoding="utf-8")[-3000:]
    assert FAKE_KEY not in tail, "로그 파일에 키 원문이 남으면 안 됨"
    assert REDACTED in tail, "로그 파일에 REDACTED 마킹이 있어야 함"
    log.info("  ✅ 로그 파일(%s) 검사: 키 원문 없음, %s 존재", log_path.name, REDACTED)

    server.shutdown()
    log.info("")
    log.info("=" * 60)
    log.info("데모 완료 — 4 케이스 모두 통과")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
