"""End-to-end hook dispatch: the exact path Cursor exercises."""

import json
from pathlib import Path

import pytest

from archrev.hooks import run_hook
from archrev.storage import SessionStore


@pytest.fixture()
def in_repo(repo: Path, monkeypatch):
    """Hooks run with cwd at the project root, like Cursor does."""
    monkeypatch.chdir(repo)
    return repo


def _payload(**kwargs) -> str:
    return json.dumps({"conversation_id": "conv-42", **kwargs})


def test_full_hook_lifecycle(in_repo: Path):
    assert run_hook("prompt", _payload(prompt="Add a feature")) == {}
    assert run_hook(
        "edit", _payload(file_path=str(in_repo / "app" / "main.py"))
    ) == {}

    # Gate: protected path pauses for approval.
    out = run_hook(
        "gate",
        _payload(
            tool_name="Write",
            tool_input={"file_path": "db/migrations/0002_x.sql"},
        ),
    )
    assert out["permission"] == "ask"
    assert "approval" in out["user_message"].lower()

    # Gate: normal path passes silently.
    out = run_hook(
        "gate", _payload(tool_name="Write", tool_input={"file_path": "app/main.py"})
    )
    assert out == {"permission": "allow"}

    assert run_hook("finalize", _payload(status="completed")) == {}

    session = SessionStore(in_repo).session("conv-42")
    types = [e["type"] for e in session.events()]
    assert types[0] == "prompt"
    assert "edit" in types and "gate" in types and "session_stop" in types
    assert session.manifest() is not None
    assert (session.dir / "report.md").exists()


def test_read_tools_not_gated_by_edit_rules(in_repo: Path):
    """Regression: a Read of a protected path must not trigger edit rules
    (observed live: Cursor's StrReplace pipeline reads the file first)."""
    out = run_hook(
        "gate",
        _payload(
            tool_name="Read",
            tool_input={"file_path": "db/migrations/0001_init.sql"},
        ),
    )
    assert out == {"permission": "allow"}
    # The same path via an edit tool still gates.
    out = run_hook(
        "gate",
        _payload(
            tool_name="StrReplace",
            tool_input={"file_path": "db/migrations/0001_init.sql"},
        ),
    )
    assert out["permission"] == "ask"
    # Unknown/empty tool names err toward protection.
    out = run_hook(
        "gate",
        _payload(tool_name="", tool_input={"file_path": "db/migrations/0001_init.sql"}),
    )
    assert out["permission"] == "ask"


def test_gate_without_paths_allows(in_repo: Path):
    out = run_hook("gate", _payload(tool_name="Shell", tool_input={"command": "ls"}))
    assert out == {"permission": "allow"}


def test_hooks_never_raise_on_garbage(in_repo: Path):
    assert run_hook("gate", "not json at all") == {"permission": "allow"}
    assert run_hook("prompt", "[1,2,3]") == {}
    assert run_hook("finalize", "") == {}


def test_unrecognized_payloads_leave_debug_evidence(in_repo: Path):
    """Field-name drift must be diagnosable from the session record."""
    # Edit payload without any known path key.
    run_hook("edit", _payload(some_new_field="x.py"))
    session = SessionStore(in_repo).session("conv-42")
    debug = [e for e in session.events() if e.get("type") == "payload_debug"]
    assert debug and debug[0]["hook"] == "edit"
    assert debug[0]["payload"]["some_new_field"] == "x.py"

    # Payload without a recognizable conversation id lands in 'unknown'
    # together with a trimmed copy of what actually arrived.
    run_hook("prompt", json.dumps({"chatId": "c9", "prompt": "hi"}))
    unknown = SessionStore(in_repo).session("unknown")
    debug = [e for e in unknown.events() if e.get("type") == "payload_debug"]
    assert debug and debug[0]["payload"]["chatId"] == "c9"


def test_tab_edits_tagged_with_human_origin(in_repo: Path):
    """Tab completions are human-driven; the audit log must say so."""
    run_hook(
        "edit",
        _payload(file_path="app/main.py", hook_event_name="afterTabFileEdit"),
    )
    run_hook("edit", _payload(file_path="app/other.py", tool_name="Write"))
    events = [
        e for e in SessionStore(in_repo).session("conv-42").events()
        if e["type"] == "edit"
    ]
    assert events[0]["origin"] == "tab"
    assert events[1]["origin"] == "agent"


def _shell_write_migration(repo: Path, command: str = "./migrate.sh") -> None:
    """A protected file changed by this session's shell command, not by an
    edit tool: the gate never saw it."""
    run_hook("shell", _payload(command=command))
    (repo / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT, extra TEXT);\n", encoding="utf-8"
    )
    run_hook("exec_end", _payload(command=command))


def test_final_check_notifies_agent_once(in_repo: Path):
    """A gate bypass surfaces as a follow-up message at session end - once."""
    run_hook("prompt", _payload(prompt="do something"))
    _shell_write_migration(in_repo)
    out = run_hook("finalize", _payload(status="completed"))
    assert "followup_message" in out
    assert "db/migrations/0001_init.sql" in out["followup_message"]
    assert "during shell: ./migrate.sh" in out["followup_message"]

    # Same findings again: fingerprint guard suppresses a second follow-up.
    assert run_hook("finalize", _payload(status="completed")) == {}

    events = SessionStore(in_repo).session("conv-42").events()
    final = [e for e in events if e["type"] == "final_check"]
    assert len(final) == 2
    assert final[0]["notified"] is True
    assert final[1]["notified"] is False


def test_acknowledged_findings_are_not_re_raised(in_repo: Path):
    """`archrev ack` resolves a finding: audited, and no more follow-ups."""
    run_hook("prompt", _payload(prompt="do something"))
    _shell_write_migration(in_repo)
    out = run_hook("finalize", _payload(status="completed"))
    assert "followup_message" in out

    session = SessionStore(in_repo).session("conv-42")
    session.append_event(
        "ack", target="db/migrations/0001_init.sql", note="approved by user"
    )
    out = run_hook("finalize", _payload(status="completed"))
    assert out == {}
    final = [e for e in session.events() if e["type"] == "final_check"]
    assert final[-1]["findings"] == []  # resolved, not merely deduplicated


def test_ack_of_unrelated_target_does_not_suppress(in_repo: Path):
    run_hook("prompt", _payload(prompt="do something"))
    _shell_write_migration(in_repo)
    session = SessionStore(in_repo).session("conv-42")
    session.append_event("ack", target="some/other/file.py", note="unrelated")
    out = run_hook("finalize", _payload(status="completed"))
    assert "followup_message" in out


def test_final_check_clean_session_is_silent(in_repo: Path):
    run_hook("prompt", _payload(prompt="hi"))
    run_hook("edit", _payload(file_path="app/main.py"))
    assert run_hook("finalize", _payload(status="completed")) == {}


def test_final_check_can_be_disabled(in_repo: Path):
    (in_repo / ".archrev" / "config.yaml").write_text(
        "final_check: false\n", encoding="utf-8"
    )
    run_hook("prompt", _payload(prompt="do something"))
    _shell_write_migration(in_repo)
    out = run_hook("finalize", _payload(status="completed"))
    assert "followup_message" not in out


def test_edit_paths_from_nested_edits(in_repo: Path):
    out = run_hook(
        "gate",
        _payload(
            tool_name="MultiEdit",
            tool_input={"edits": [{"file_path": "db/migrations/0009_z.sql"}]},
        ),
    )
    assert out["permission"] == "ask"


def test_change_outside_session_commands_is_not_raised(in_repo: Path):
    """Regression (parallel windows): a protected file changed while none of
    this session's commands ran belongs to someone else - no follow-up."""
    run_hook("prompt", _payload(prompt="do something"))
    run_hook("shell", _payload(command="pytest"))
    run_hook("exec_end", _payload(command="pytest"))
    (in_repo / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT, by_other_window TEXT);\n", encoding="utf-8"
    )
    assert run_hook("finalize", _payload(status="completed")) == {}


def test_checkpoint_failure_never_changes_a_gate_decision(in_repo: Path, monkeypatch):
    """Fail-open is for ArchRev crashes, not for attribution hiccups: a
    broken checkpoint must not turn an 'ask' into the generic 'allow'."""
    import archrev.hooks as hooks

    def boom(*_args, **_kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(hooks, "record_checkpoint", boom)
    (in_repo / ".archrev" / "rules" / "shell.yaml").write_text(
        '- id: push\n  kind: shell\n  match_command: ["\\\\bgit\\\\s+push\\\\b"]\n'
        "  action: block\n",
        encoding="utf-8",
    )
    run_hook("prompt", _payload(prompt="ship it"))
    out = run_hook("shell", _payload(command="git push origin main"))
    assert out["permission"] == "ask"
    assert run_hook("exec_end", _payload(command="git push origin main")) == {}
    log = (in_repo / ".archrev" / "hook-errors.log").read_text(encoding="utf-8")
    assert "checkpoint:exec_start" in log and "git exploded" in log


def test_cursor_after_shell_event_closes_the_window(in_repo: Path):
    """`archrev hook auto` with Cursor's afterShellExecution name routes to
    exec_end, the same handler `archrev hook exec_end` uses."""
    run_hook("prompt", _payload(prompt="go"))
    run_hook("shell", _payload(command="make"))
    assert run_hook(
        "auto", _payload(hook_event_name="afterShellExecution", command="make")
    ) == {}
    phases = [
        e.get("phase")
        for e in SessionStore(in_repo).session("conv-42").events()
        if e["type"] == "worktree"
    ]
    assert phases == ["start", "exec_start", "exec_end"]
