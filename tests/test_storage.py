from pathlib import Path

from archrev.storage import SessionStore, sanitize_session_id


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


def test_meta_written_once(tmp_path: Path):
    session = SessionStore(tmp_path).session("s")
    first = session.ensure_meta("sha1")
    second = session.ensure_meta("sha2")
    assert first == second
    assert session.meta()["head_sha"] == "sha1"


def test_sanitize_session_id():
    assert sanitize_session_id("abc-DEF_1.2") == "abc-DEF_1.2"
    assert sanitize_session_id(None) == "unknown"
    weird = sanitize_session_id("a/b:c")
    assert "/" not in weird and ":" not in weird
    # Distinct raw ids that clean to the same string must not collide.
    assert sanitize_session_id("a/b") != sanitize_session_id("a:b")


def test_resolve_prefix_and_latest(tmp_path: Path):
    store = SessionStore(tmp_path)
    store.session("session-alpha").append_event("prompt", text="1")
    store.session("session-beta").append_event("prompt", text="2")
    assert store.resolve("session-a").id == "session-alpha"
    assert store.resolve("session-") is None  # ambiguous
    assert store.resolve("latest") is not None
    assert store.resolve("nope") is None
