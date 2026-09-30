import json
import threading
from pathlib import Path

import pytest

from bewit.storage import GENESIS, SESSION_ENV_VARS, SessionStore, sanitize_session_id


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
    assert result["ok"] is True
    assert (result["checked"], result["legacy"], result["break_at"]) == (3, 0, None)
    assert (result["duplicates"], result["forks"], result["reason"]) == (0, 0, None)


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


def test_chain_survives_events_larger_than_the_old_tail_window(tmp_path: Path):
    """A prompt bigger than 16 KiB must not break the hash of the next event."""
    session = SessionStore(tmp_path).session("s")
    session.append_event("prompt", text="x" * 50_000)
    session.append_event("edit", path="a.py")
    events = session.events()
    assert events[1]["prev"] == events[0]["hash"]
    assert session.verify_chain()["ok"] is True


def test_parallel_appends_stay_one_chain(tmp_path: Path):
    store = SessionStore(tmp_path)

    def worker(index: int) -> None:
        session = store.session("s")
        for n in range(15):
            session.append_event("edit", path=f"t{index}-{n}.py")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    session = store.session("s")
    assert len(session.events()) == 60
    assert session.verify_chain()["ok"] is True


def test_resolve_prefix_and_latest(tmp_path: Path):
    store = SessionStore(tmp_path)
    store.session("session-alpha").append_event("prompt", text="1")
    store.session("session-beta").append_event("prompt", text="2")
    assert store.resolve("session-a").id == "session-alpha"
    assert store.resolve("session-") is None  # ambiguous
    assert store.resolve("latest") is not None
    assert store.resolve("nope") is None


@pytest.fixture()
def no_session_env(monkeypatch):
    for var in SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_caller_refuses_to_guess_between_live_sessions(tmp_path: Path, no_session_env):
    """Reported from real use: with two agents in one repo, `plan register`
    bound to the other agent's session and replaced its plan."""
    store = SessionStore(tmp_path)
    store.session("agent-a").append_event("prompt", text="a")
    store.session("agent-b").append_event("prompt", text="b")
    session, how = store.resolve_caller("latest", "plan register")
    assert session is None and "2 sessions" in how
    session, _ = store.resolve_caller("agent-b", "plan register")
    assert session.id == "agent-b"


def test_caller_binds_through_the_runtime_env_var(tmp_path: Path, no_session_env, monkeypatch):
    store = SessionStore(tmp_path)
    store.session("agent-a").append_event("prompt", text="a")
    store.session("agent-b").append_event("prompt", text="b")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "agent-a")
    session, how = store.resolve_caller("latest", "check plan")
    assert session.id == "agent-a" and "CLAUDE_CODE_SESSION_ID" in how
    # An id from another repository's conversation is ignored.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "elsewhere")
    assert store.resolve_caller("latest", "check plan")[0] is None


def test_caller_binds_through_a_hook_claim_once(tmp_path: Path, no_session_env):
    store = SessionStore(tmp_path)
    store.session("agent-a").append_event("prompt", text="a")
    store.session("agent-b").append_event("prompt", text="b")
    store.claim_command("agent-b", "plan register")
    session, _ = store.resolve_caller("latest", "check plan")  # other verb
    assert session is None
    session, _ = store.resolve_caller("latest", "plan register")
    assert session.id == "agent-b"
    assert store.resolve_caller("latest", "plan register")[0] is None  # consumed

    store.claim_command("agent-a", "ack")
    store.claim_command("agent-b", "ack")
    session, how = store.resolve_caller("latest", "ack")
    assert session is None and "at once" in how


def test_single_live_session_still_resolves(tmp_path: Path, no_session_env):
    store = SessionStore(tmp_path)
    store.session("solo").append_event("prompt", text="a")
    store.session("human").append_event("human_changes", paths=[])
    assert store.resolve_caller("latest", "plan register")[0].id == "solo"
