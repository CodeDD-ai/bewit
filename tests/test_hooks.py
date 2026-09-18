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


def test_edit_paths_from_nested_edits(in_repo: Path):
    out = run_hook(
        "gate",
        _payload(
            tool_name="MultiEdit",
            tool_input={"edits": [{"file_path": "db/migrations/0009_z.sql"}]},
        ),
    )
    assert out["permission"] == "ask"
