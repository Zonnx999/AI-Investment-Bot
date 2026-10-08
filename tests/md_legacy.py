"""
tests/md_legacy.py
==================
텔레그램 legacy Markdown(parse_mode="Markdown") 파서의 tdlib 충실 포팅 — **테스트 전용 검증기**.

원본: scratchpad verify-MSG-06/tdlib_md1.py (tdlib `parse_markdown` v1 을 바이트 단위로 옮김).
규칙 (tdlib 과 동일):
  - 엔티티는 `_` `*` `` ` `` `[` 로 열리고 같은 문자(링크는 `]`)로 닫힌다. 엔티티 안은 리터럴.
  - `\\` 뒤에 네 문자 중 하나가 오면 그 글자를 건너뛴다 (tdlib 내부 동작 — 실제 서버는
    legacy 에서 백슬래시를 보장하지 않으므로 우리 메시지는 백슬래시를 쓰지 않는다).
  - 닫는 문자를 못 찾으면 "Can't find end of the entity" — 텔레그램이 400 을 돌려주는 상황.

사용: ``status, ents = parse(text)`` → status == "OK" 면 전송 가능, ents 는 [(종류, 내용)].
``assert_bold_only(text)`` 는 다이제스트 규칙(굵게 `*` 만, 이탤릭·코드·링크 0) 을 단언한다.
"""

from __future__ import annotations

ENTITY_OPENERS = (b"_", b"*", b"`", b"[")


def parse(text: str) -> tuple[str, list[tuple[str, str]]]:
    """legacy Markdown 파싱 시뮬레이션. ("OK" | "ERROR: ...", [(entity_char, inner_text), ...])."""
    b = text.encode("utf-8")
    n = len(b)
    i = 0
    ents: list[tuple[str, str]] = []
    while i < n:
        c = b[i:i + 1]
        if c == b"\\" and i + 1 < n and b[i + 1:i + 2] in ENTITY_OPENERS:
            i += 2
            continue
        if c not in ENTITY_OPENERS:
            i += 1
            continue
        begin = i
        end = b"]" if c == b"[" else c
        i += 1
        is_pre = False
        if c == b"`" and b[i:i + 2] == b"``":
            i += 2
            is_pre = True
        start = i
        while i < n and (b[i:i + 1] != end or (is_pre and b[i + 1:i + 3] != b"``")):
            i += 1
        if i == n:
            ctx = b[max(0, begin - 40):begin + 40].decode("utf-8", "replace")
            return (f"ERROR: Can't find end of the entity starting at byte offset {begin} "
                    f"({c.decode()}) ctx={ctx!r}"), ents
        ents.append((c.decode(), b[start:i].decode("utf-8", "replace")))
        i += 1
        if is_pre:
            i += 2
        if c == b"[":
            if b[i:i + 1] != b"(":
                continue
            j = b.find(b")", i)
            if j < 0:
                return f"ERROR: Can't find end of a URL at byte offset {i}", ents
            i = j + 1
    return "OK", ents


def assert_bold_only(text: str) -> list[tuple[str, str]]:
    """parse OK + 엔티티는 굵게(`*`)만 + 굵게 내용이 비어 있지 않음. 엔티티 목록 반환."""
    status, ents = parse(text)
    assert status == "OK", status
    kinds = {k for k, _ in ents}
    assert kinds <= {"*"}, f"굵게 외 엔티티 발견: {sorted(kinds)} — {ents}"
    assert all(inner.strip() for _, inner in ents), f"빈 굵게 엔티티: {ents}"
    return ents
