import json
from pathlib import Path

from archrev.storage import GENESIS, SessionStore, sanitize_session_id


def test_event_roundtrip(tmp_path: Path):
    store = SessionStore(tmp_path)
    session = store.session("abc-123")
    session.append_event("prompt", text="hello")
    session.append_event("edit", path="a/b.py")
    events = session.events()
    assert [e["type"] for e in events] == ["prompt", "edit"]
    assert all("ts" in e for e in events)
    assert session.touched_files() == ["a/b.py"]


def test_corrupt_lines_skipped(tmp_path: Path):
    session = SessionStore(tmp_path).session("s")
    session.append_event("prompt", text="ok")
    with open(session.dir / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    session.append_event("edit", path="x.py")
    assert [e["type"] for e in session.events()] == ["prompt", "edit"]


def test_meta_records_runtime_once(tmp_path: Path):
    session = SessionStore(tmp_path).session("s")
    first = session.ensure_meta("sha1", runtime="claude")
    second = session.ensure_meta("sha2", runtime="codex")
    assert first == second
    assert session.meta()["runtime"] == "claude"
    assert session.meta()["head_sha"] == "sha1"


def test_sanitize_session_id():
    assert sanitize_session_id("abc-DEF_1.2") == "abc-DEF_1.2"
    assert sanitize_session_id(None) == "unknown"
    weird = sanitize_session_id("a/b:c")
    assert "/" not in weird and ":" not in weird
    # Distinct raw ids that clean to the same string must not collide.
    assert sanitize_session_id("a/b") != sanitize_session_id("a:b")


def test_events_are_hash_chained(tmp_path: Path):
    session = SessionStore(tmp_path).session("s")
    session.append_event("prompt", text="one")
    session.append_event("edit", path="a.py")
    session.append_event("edit", path="b.py")
    events = session.events()
    assert events[0]["prev"] == GENESIS
    assert events[1]["prev"] == events[0]["hash"]
    assert events[2]["prev"] == events[1]["hash"]
    result = session.verify_chain()
    assert result == {"ok": True, "checked": 3, "legacy": 0, "break_at": None}


def test_verify_detects_tampering(tmp_path: Path):
    session = SessionStore(tmp_path).session("s")
    session.append_event("prompt", text="one")
    session.append_event("edit", path="a.py")
    session.append_event("edit", path="b.py")
    # Rewrite the middle event: change its payload but keep the stored hash.
    log = session.dir / "events.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    doctored = json.loads(lines[1])
    doctored["path"] = "totally/different.py"
    lines[1] = json.dumps(doctored, ensure_ascii=False)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    result = session.verify_chain()
    assert result["ok"] is False
    assert result["break_at"] == 2

    # Deleting an event breaks the chain too (successor's prev mismatches).
    log.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8", newline="\n")
    assert session.verify_chain()["ok"] is False


def test_legacy_unhashed_events_tolerated(tmp_path: Path):
    session = SessionStore(tmp_path).session("s")
    session.dir.mkdir(parents=True, exist_ok=True)
    with open(session.dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": "2026-01-01T00:00:00Z", "type": "prompt"}) + "\n")
    session.append_event("edit", path="a.py")
    result = session.verify_chain()
    assert result["ok"] is True
    assert result["legacy"] == 1
    assert result["checked"] == 1


def test_resolve_prefix_and_latest(tmp_path: Path):
    store = SessionStore(tmp_path)
    store.session("session-alpha").append_event("prompt", text="1")
    store.session("session-beta").append_event("prompt", text="2")
    assert store.resolve("session-a").id == "session-alpha"
    assert store.resolve("session-") is None  # ambiguous
    assert store.resolve("latest") is not None
    assert store.resolve("nope") is None
