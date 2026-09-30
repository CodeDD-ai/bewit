"""End-to-end hook dispatch: the exact path Cursor exercises."""

import json
from pathlib import Path

import pytest

from bewit.hooks import run_hook
from bewit.storage import SessionStore


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


def test_cursor_pretool_read_enforces_read_rules(in_repo: Path):
    """Regression: Cursor agent reads often skip beforeReadFile; preToolUse
    must still enforce ``read`` rules (e.g. deny-secret-reads)."""
    (in_repo / ".bewit" / "rules" / "secrets.yaml").write_text(
        """
- id: no-env
  kind: read
  match: [".env", ".env.*"]
  action: deny
  message: "No .env reads."
""",
        encoding="utf-8",
    )
    out = run_hook(
        "gate",
        _payload(tool_name="Read", tool_input={"file_path": ".env"}),
    )
    assert out["permission"] == "deny"
    assert "No .env reads." in out["user_message"]
    events = [
        e
        for e in SessionStore(in_repo).session("conv-42").events()
        if e.get("type") == "gate" and e.get("kind") == "read"
    ]
    assert len(events) == 1
    assert events[0]["permission"] == "deny"


def test_grep_is_gated_as_a_read(in_repo: Path):
    """Grep returns file contents, so a read deny must apply to it."""
    (in_repo / ".bewit" / "rules" / "secrets.yaml").write_text(
        '- id: no-env\n  kind: read\n  match: [".env"]\n  action: deny\n'
        '  message: "No .env reads."\n',
        encoding="utf-8",
    )
    out = run_hook(
        "gate",
        _payload(tool_name="Grep", tool_input={"path": ".env", "pattern": "KEY"}),
    )
    assert out["permission"] == "deny"
    assert "No .env reads." in out["user_message"]


def test_every_read_is_logged_with_reporting_hook(in_repo: Path):
    """Allowed reads are part of the audit trail too, tagged with the hook
    that reported them, so a missing event proves the runtime skipped us."""
    (in_repo / ".bewit" / "rules" / "secrets.yaml").write_text(
        '- id: no-env\n  kind: read\n  match: [".env"]\n  action: deny\n',
        encoding="utf-8",
    )
    assert run_hook(
        "gate", _payload(tool_name="Read", tool_input={"path": "app/main.py"})
    ) == {"permission": "allow"}
    assert run_hook("read", _payload(file_path=str(in_repo / "app" / "main.py"))) == {
        "permission": "allow"
    }
    run_hook("read", _payload(file_path=".env"))

    session = SessionStore(in_repo).session("conv-42")
    reads = [e for e in session.events() if e["type"] == "read"]
    assert [(e["path"], e["permission"], e["via"]) for e in reads] == [
        ("app/main.py", "allow", "preToolUse"),
        ("app/main.py", "allow", "beforeReadFile"),
        (".env", "deny", "beforeReadFile"),
    ]
    assert reads[0]["tool"] == "Read"
    assert reads[2]["rules"] == ["no-env"]
    # Only the policy hit is a gate event; routine reads stay out of gate metrics.
    gates = [e for e in session.events() if e["type"] == "gate"]
    assert [g["target"] for g in gates] == [".env"]

    run_hook("finalize", _payload(status="completed"))
    summary = {r["path"]: r for r in session.manifest()["reads"]}
    assert summary["app/main.py"]["count"] == 2
    assert summary["app/main.py"]["via"] == ["preToolUse", "beforeReadFile"]
    assert summary[".env"]["permission"] == "deny"
    report = (session.dir / "report.md").read_text(encoding="utf-8")
    assert "## Files read" in report and "`.env`" in report


def test_read_without_path_leaves_debug_evidence(in_repo: Path):
    assert run_hook("read", _payload(some_new_field="x")) == {"permission": "allow"}
    debug = [
        e for e in SessionStore(in_repo).session("conv-42").events()
        if e["type"] == "payload_debug"
    ]
    assert debug and debug[0]["hook"] == "read"
    assert debug[0]["via"] == "beforeReadFile"


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
    """`bewit ack` resolves a finding: audited, and no more follow-ups."""
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


def test_final_check_raises_only_new_findings(in_repo: Path):
    """Reported from real use: the same 16-18 findings came back every
    turn. A later turn names only what is new, and counts the rest."""
    from bewit.planning import register_plan

    run_hook("prompt", _payload(prompt="do something"))
    session = SessionStore(in_repo).session("conv-42")
    register_plan(session, "Edit `app/main.py`.", in_repo)
    _shell_write_migration(in_repo)
    first = run_hook("finalize", _payload(status="completed"))
    assert "db/migrations/0001_init.sql" in first["followup_message"]

    (in_repo / "app" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    run_hook("edit", _payload(file_path="app/extra.py", tool_name="Write"))
    second = run_hook("finalize", _payload(status="completed"))["followup_message"]
    assert "app/extra.py" in second
    assert "0001_init.sql" not in second
    assert "1 earlier finding(s) are still open" in second
    final = [e for e in session.events() if e["type"] == "final_check"][-1]
    assert len(final["findings"]) == 2  # the record keeps the full list


def test_resolved_then_reopened_finding_is_new_again(in_repo: Path):
    run_hook("prompt", _payload(prompt="do something"))
    _shell_write_migration(in_repo)
    assert "followup_message" in run_hook("finalize", _payload(status="completed"))
    session = SessionStore(in_repo).session("conv-42")
    session.append_event("ack", target="db/migrations/0001_init.sql", note="ok")
    assert run_hook("finalize", _payload(status="completed")) == {}
    # Changed again after the ack: the finding reopens and is raised again.
    import time
    time.sleep(1.1)  # timestamps have second resolution
    run_hook("shell", _payload(command="./migrate.sh --again"))
    (in_repo / "db" / "migrations" / "0001_init.sql").write_text("CHANGED\n", encoding="utf-8")
    run_hook("exec_end", _payload(command="./migrate.sh --again"))
    out = run_hook("finalize", _payload(status="completed"))
    assert "0001_init.sql" in out.get("followup_message", "")


def test_shell_hook_claims_bewit_commands_for_the_cli(in_repo: Path, monkeypatch):
    from bewit.storage import SESSION_ENV_VARS

    for var in SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    SessionStore(in_repo).session("other-agent").append_event("prompt", text="x")
    run_hook("shell", _payload(command='bewit plan register --text "Edit `a.py`"'))
    session, how = SessionStore(in_repo).resolve_caller("latest", "plan register")
    assert session.id == "conv-42" and "shell hook" in how


def test_final_check_clean_session_is_silent(in_repo: Path):
    run_hook("prompt", _payload(prompt="hi"))
    run_hook("edit", _payload(file_path="app/main.py"))
    assert run_hook("finalize", _payload(status="completed")) == {}


def test_final_check_can_be_disabled(in_repo: Path):
    (in_repo / ".bewit" / "config.yaml").write_text(
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
    """Fail-open is for Bewit crashes, not for attribution hiccups: a
    broken checkpoint must not turn an 'ask' into the generic 'allow'."""
    import bewit.hooks as hooks

    def boom(*_args, **_kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(hooks, "record_checkpoint", boom)
    (in_repo / ".bewit" / "rules" / "shell.yaml").write_text(
        '- id: push\n  kind: shell\n  match_command: ["\\\\bgit\\\\s+push\\\\b"]\n'
        "  action: block\n",
        encoding="utf-8",
    )
    run_hook("prompt", _payload(prompt="ship it"))
    out = run_hook("shell", _payload(command="git push origin main"))
    assert out["permission"] == "ask"
    assert run_hook("exec_end", _payload(command="git push origin main")) == {}
    log = (in_repo / ".bewit" / "hook-errors.log").read_text(encoding="utf-8")
    assert "checkpoint:exec_start" in log and "git exploded" in log


def test_cursor_after_shell_event_closes_the_window(in_repo: Path):
    """`bewit hook auto` with Cursor's afterShellExecution name routes to
    exec_end, the same handler `bewit hook exec_end` uses."""
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
