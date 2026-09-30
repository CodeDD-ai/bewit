"""Session-scoped change attribution with working-tree checkpoints.

Two windows (sessions) and the human share one checkout. A change must
only be raised in the review of the session that could have made it.
"""

import json
from pathlib import Path

import pytest

from bewit.config import load_config
from bewit.drift import compute_view
from bewit.gitutil import Git
from bewit.hooks import run_hook
from bewit.rules import load_rules
from bewit.storage import SessionStore
from bewit.worktree import (
    DELETED,
    EVENT_TYPE,
    PHASE_EXEC_END,
    PHASE_EXEC_START,
    replay,
    snapshot,
)

MIGRATION = Path("db") / "migrations" / "0001_init.sql"
MIGRATION_KEY = "db/migrations/0001_init.sql"


@pytest.fixture()
def in_repo(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    return repo


def _hook(event: str, session: str, **fields) -> dict:
    return run_hook(event, json.dumps({"conversation_id": session, **fields}))


def _view(repo: Path, session_id: str) -> dict:
    session = SessionStore(repo).session(session_id)
    return compute_view(repo, load_config(repo), load_rules(repo), session)


def _paths(entries: list[dict]) -> list[str]:
    return [e["path"] for e in entries]


def _bypasses(view: dict) -> list[str]:
    return [f["path"] for f in view["protected_findings"] if not f["via_gate"]]


def _write(repo: Path, rel: Path, text: str) -> None:
    (repo / rel).write_text(text, encoding="utf-8")


# -- the reported bug ----------------------------------------------------------


def test_hand_edit_in_one_window_does_not_spill_into_another(in_repo: Path):
    """Both sessions are open; the user hand-edits a protected file while no
    command of either session runs. Neither review raises it as a bypass."""
    _hook("prompt", "win-a", prompt="feature A")
    _hook("prompt", "win-b", prompt="feature B")
    _write(in_repo, MIGRATION, "CREATE TABLE t (id INT, by_hand TEXT);\n")

    for sid in ("win-a", "win-b"):
        _hook("finalize", sid, status="completed")
        manifest = SessionStore(in_repo).session(sid).manifest()
        assert manifest["attribution"] == "checkpoints"
        assert MIGRATION_KEY in _paths(manifest["background_changes"])
        assert MIGRATION_KEY not in _paths(manifest["other_changes"])
        assert _bypasses(manifest) == []
        final = [e for e in SessionStore(in_repo).session(sid).events()
                 if e["type"] == "final_check"][-1]
        assert final["findings"] == []


def test_other_windows_shell_write_is_not_this_sessions_bypass(in_repo: Path):
    _hook("prompt", "win-a", prompt="migrate")
    _hook("prompt", "win-b", prompt="unrelated")
    _hook("shell", "win-a", command="python manage.py makemigrations")
    _write(in_repo, MIGRATION, "CREATE TABLE t (id INT, extra TEXT);\n")
    _hook("exec_end", "win-a", command="python manage.py makemigrations")

    view_a = _view(in_repo, "win-a")
    assert _bypasses(view_a) == [MIGRATION_KEY]
    finding = next(f for f in view_a["protected_findings"] if not f["via_gate"])
    assert finding["during"] == ["shell: python manage.py makemigrations"]

    view_b = _view(in_repo, "win-b")
    assert _bypasses(view_b) == []
    assert MIGRATION_KEY in _paths(view_b["background_changes"])


def test_changes_that_predate_the_session_are_background(in_repo: Path):
    _write(in_repo, MIGRATION, "CREATE TABLE t (id INT, earlier TEXT);\n")
    _hook("prompt", "late", prompt="start after the change")
    _hook("shell", "late", command="pytest")
    _hook("exec_end", "late", command="pytest")
    view = _view(in_repo, "late")
    assert MIGRATION_KEY in _paths(view["background_changes"])
    assert _bypasses(view) == []


def test_other_sessions_edit_inside_my_window_stays_theirs(in_repo: Path):
    """Window B's agent edits a file via its edit tool while A runs a shell
    command: the change is B's recorded edit, not A's bypass."""
    _hook("prompt", "win-a", prompt="tests")
    _hook("prompt", "win-b", prompt="migration")
    _hook("shell", "win-a", command="pytest")
    _write(in_repo, MIGRATION, "CREATE TABLE t (id INT, extra TEXT);\n")
    _hook("edit", "win-b", file_path=str(in_repo / MIGRATION))
    _hook("exec_end", "win-a", command="pytest")

    view_a = _view(in_repo, "win-a")
    assert _bypasses(view_a) == []
    owned = {e["path"]: e["sessions"] for e in view_a["other_sessions"]}
    assert owned[MIGRATION_KEY] == ["win-b"]
    view_b = _view(in_repo, "win-b")
    assert _paths(view_b["files"]) == [MIGRATION_KEY]


# -- attribution mechanics -----------------------------------------------------


def test_missing_after_hook_keeps_window_open_until_turn_end(in_repo: Path):
    """Without afterShellExecution wiring, attribution errs toward review."""
    _hook("prompt", "s", prompt="go")
    _hook("shell", "s", command="./script.sh")
    _write(in_repo, MIGRATION, "CREATE TABLE t (id INT, extra TEXT);\n")
    _hook("finalize", "s", status="completed")
    manifest = SessionStore(in_repo).session("s").manifest()
    assert _bypasses(manifest) == [MIGRATION_KEY]

    # The stop hook closed the window: later changes are someone else's.
    _write(in_repo, Path("app") / "main.py", "print('edited later')\n")
    view = _view(in_repo, "s")
    assert "app/main.py" in _paths(view["background_changes"])
    assert "app/main.py" not in _paths(view["other_changes"])


def test_parallel_commands_keep_the_window_open(in_repo: Path):
    _hook("prompt", "s", prompt="go")
    _hook("shell", "s", command="npm run build")
    _hook("shell", "s", command="npm test")
    _hook("exec_end", "s", command="npm test")
    _write(in_repo, MIGRATION, "CREATE TABLE t (id INT, extra TEXT);\n")
    _hook("exec_end", "s", command="npm run build")
    view = _view(in_repo, "s")
    assert _bypasses(view) == [MIGRATION_KEY]
    change = next(c for c in view["other_changes"] if c["path"] == MIGRATION_KEY)
    assert change["during"] == ["shell: npm run build"]


def test_new_and_deleted_files_inside_a_window_are_attributed(in_repo: Path):
    _hook("prompt", "s", prompt="go")
    _hook("shell", "s", command="./gen.sh")
    (in_repo / "db" / "migrations" / "0002_new.sql").write_text(
        "ALTER TABLE t ADD c INT;\nSELECT 1;\n", encoding="utf-8"
    )
    (in_repo / "app" / "main.py").unlink()
    _hook("exec_end", "s", command="./gen.sh")
    view = _view(in_repo, "s")
    changes = {c["path"]: c for c in view["other_changes"]}
    assert changes["db/migrations/0002_new.sql"]["added"] == 2
    assert changes["db/migrations/0002_new.sql"]["untracked"] is True
    assert "app/main.py" in changes
    assert "db/migrations/0002_new.sql" in _bypasses(view)


def test_denied_command_opens_no_window(in_repo: Path):
    (in_repo / ".bewit" / "rules" / "shell.yaml").write_text(
        '- id: no-rm\n  kind: shell\n  match_command: ["\\\\brm\\\\b"]\n'
        "  action: deny\n",
        encoding="utf-8",
    )
    _hook("prompt", "s", prompt="go")
    assert _hook("shell", "s", command="rm -rf db")["permission"] == "deny"
    events = SessionStore(in_repo).session("s").events()
    assert not any(
        e["type"] == EVENT_TYPE and e.get("phase") == PHASE_EXEC_START
        for e in events
    )


def test_checkpoints_store_deltas_and_skip_bookkeeping(in_repo: Path):
    _hook("prompt", "s", prompt="go")
    _hook("shell", "s", command="true")
    _hook("exec_end", "s", command="true")
    events = [
        e for e in SessionStore(in_repo).session("s").events()
        if e["type"] == EVENT_TYPE
    ]
    assert [e["phase"] for e in events] == ["start", PHASE_EXEC_START, PHASE_EXEC_END]
    # Nothing moved during the command: the checkpoints carry no delta,
    # even though the session's own event log changed in between.
    assert events[1]["changed"] == {} and events[2]["changed"] == {}
    for event in events:
        assert not any(p.startswith(".bewit/sessions/") for p in event["changed"])


def test_command_text_is_kept_only_with_full_capture(in_repo: Path):
    (in_repo / ".bewit" / "config.yaml").write_text(
        "prompt_capture: none\n", encoding="utf-8"
    )
    _hook("prompt", "s", prompt="go")
    _hook("shell", "s", command="curl -H 'Authorization: secret' x")
    opening = [
        e for e in SessionStore(in_repo).session("s").events()
        if e["type"] == EVENT_TYPE and e.get("phase") == PHASE_EXEC_START
    ]
    assert opening[0]["label"] == "shell"


def test_snapshot_fingerprints_content_and_deletions(repo: Path):
    _write(repo, MIGRATION, "changed\n")
    (repo / "app" / "main.py").unlink()
    first = snapshot(repo)
    assert first[MIGRATION_KEY] != DELETED
    assert first["app/main.py"] == DELETED
    # A carried path that became clean again is still observed.
    Git(repo)._run("checkout", "--", MIGRATION_KEY)
    second = snapshot(repo, carried=first)
    assert second[MIGRATION_KEY] != first[MIGRATION_KEY]


def test_replay_folds_windows_and_background():
    events = [
        {"type": EVENT_TYPE, "ts": "t1", "phase": "start", "changed": {"a": "1"}},
        {"type": EVENT_TYPE, "ts": "t2", "phase": PHASE_EXEC_START,
         "label": "shell: x", "changed": {"b": "1"}},
        {"type": EVENT_TYPE, "ts": "t3", "phase": PHASE_EXEC_END, "changed": {"c": "1"}},
        {"type": EVENT_TYPE, "ts": "t4", "phase": "turn_end", "changed": {"d": "1"}},
    ]
    result = replay(events)
    assert result.background == {"a", "b", "d"}
    assert list(result.windows) == ["c"]
    window = result.windows["c"][0]
    assert (window.label, window.start, window.end) == ("shell: x", "t2", "t3")
    assert not result.is_open


# -- compatibility -------------------------------------------------------------


def test_sessions_without_checkpoints_keep_since_start_attribution(repo: Path):
    """Records written before checkpoints existed are reviewed as before."""
    session = SessionStore(repo).session("legacy")
    session.ensure_meta(Git(repo).head_sha())
    session.append_event("prompt", text="hi")
    _write(repo, MIGRATION, "CREATE TABLE t (id INT, extra TEXT);\n")
    view = compute_view(repo, load_config(repo), load_rules(repo), session)
    assert view["attribution"] == "since_start"
    assert view["background_changes"] == []
    assert _bypasses(view) == [MIGRATION_KEY]


def test_legacy_session_never_starts_checkpointing(in_repo: Path):
    session = SessionStore(in_repo).session("legacy")
    session.ensure_meta(Git(in_repo).head_sha())
    _hook("prompt", "legacy", prompt="continue")
    _hook("shell", "legacy", command="pytest")
    _hook("exec_end", "legacy", command="pytest")
    assert not any(e["type"] == EVENT_TYPE for e in session.events())


def test_shell_hook_makes_its_session_the_cli_default(in_repo: Path):
    """`bewit plan register` / `ack` bind to the latest session. The shell
    hook right before the command now records a checkpoint, so the agent's
    own session wins even if another window was active a moment earlier."""
    _hook("prompt", "win-a", prompt="a")
    _hook("prompt", "win-b", prompt="b")
    _hook("shell", "win-a", command="bewit plan register --text x")
    assert SessionStore(in_repo).current_session().id == "win-a"
