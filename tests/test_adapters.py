"""Claude Code and Codex adapters: event map, gate rendering, init wiring."""

import json
from pathlib import Path

from archrev.adapters import (
    detect_runtime,
    patch_paths,
    promote_payload,
    runtime_supports_ask,
)
from archrev.hooks import run_hook
from archrev.scaffold import init_repo
from archrev.storage import SessionStore


def _native(repo: Path, event: str, **extra) -> str:
    payload = {
        "session_id": extra.pop("session_id", "native-sess"),
        "cwd": str(repo),
        "hook_event_name": event,
        **extra,
    }
    return json.dumps(payload)


def _write_shell_rule(repo: Path) -> None:
    (repo / ".archrev" / "rules" / "shell.yaml").write_text(
        """
- id: no-rm
  kind: shell
  match_command: ["\\\\brm\\\\s+-rf\\\\b"]
  action: deny
  message: "rm -rf is denied."
- id: no-mcp-web
  kind: mcp
  match_tool: ["web|search"]
  action: deny
  message: "Internet-facing MCP tools are not permitted."
""",
        encoding="utf-8",
    )


# -- mapping / payload -------------------------------------------------------


def test_detect_runtime_from_payload_shape():
    assert detect_runtime({"hook_event_name": "beforeSubmitPrompt"}) == "cursor"
    assert (
        detect_runtime({"hook_event_name": "PreToolUse", "prompt_id": "p"})
        == "claude"
    )
    assert (
        detect_runtime({"hook_event_name": "PreToolUse", "turn_id": "t1"})
        == "codex"
    )
    assert (
        detect_runtime({"hook_event_name": "PreToolUse", "session_id": "thr_abc"})
        == "codex"
    )
    assert runtime_supports_ask("claude") is True
    assert runtime_supports_ask("codex") is False


def test_apply_patch_paths_extracted():
    body = (
        "*** Begin Patch\n"
        "*** Update File: db/migrations/0001_init.sql\n"
        "*** Add File: app/new.py\n"
        "*** End Patch\n"
    )
    assert patch_paths(body) == [
        "db/migrations/0001_init.sql",
        "app/new.py",
    ]
    promoted = promote_payload({"tool_input": {"command": body}})
    assert promoted["file_path"] == "db/migrations/0001_init.sql"
    assert "app/new.py" in promoted["tool_input"]["paths"]


# -- Claude Code -------------------------------------------------------------


def test_claude_pretool_write_asks_in_native_shape(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    out = run_hook(
        "auto",
        _native(
            repo,
            "PreToolUse",
            prompt_id="p1",
            tool_name="Write",
            tool_input={"file_path": "db/migrations/0002_x.sql"},
        ),
    )
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "ask"
    assert "approval" in spec["permissionDecisionReason"].lower()
    meta = SessionStore(repo).session("native-sess").meta()
    assert meta["runtime"] == "claude"


def test_claude_pretool_bash_runs_shell_rules(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    _write_shell_rule(repo)
    out = run_hook(
        "auto",
        _native(
            repo,
            "PreToolUse",
            prompt_id="p1",
            tool_name="Bash",
            tool_input={"command": "rm -rf /tmp/x"},
        ),
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_pretool_read_not_gated_by_path_rules(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    out = run_hook(
        "auto",
        _native(
            repo,
            "PreToolUse",
            prompt_id="p1",
            tool_name="Read",
            tool_input={"file_path": "db/migrations/0001_init.sql"},
        ),
    )
    assert out == {}


def test_claude_pretool_mcp_gated(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    _write_shell_rule(repo)
    out = run_hook(
        "auto",
        _native(
            repo,
            "PreToolUse",
            prompt_id="p1",
            tool_name="mcp__web__search",
            tool_input={"query": "x"},
        ),
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_post_tool_use_records_edit(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    assert (
        run_hook(
            "auto",
            _native(
                repo,
                "PostToolUse",
                prompt_id="p1",
                tool_name="Edit",
                tool_input={"file_path": "app/main.py"},
            ),
        )
        == {}
    )
    events = SessionStore(repo).session("native-sess").events()
    edits = [e for e in events if e["type"] == "edit"]
    assert edits and edits[0]["path"].endswith("app/main.py")


def _native_bash_write(repo: Path, target: Path, content: str, **extra) -> None:
    """A Bash tool call that writes ``target``, bracketed by its hooks."""
    tool = {"tool_name": "Bash", "tool_input": {"command": "./gen.sh"}, **extra}
    run_hook("auto", _native(repo, "PreToolUse", **tool))
    target.write_text(content, encoding="utf-8")
    run_hook("auto", _native(repo, "PostToolUse", **tool))


def test_claude_stop_followup_uses_decision_block(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    run_hook(
        "auto",
        _native(repo, "UserPromptSubmit", prompt_id="p1", prompt="do it"),
    )
    _native_bash_write(
        repo,
        repo / "db" / "migrations" / "0001_init.sql",
        "CREATE TABLE t (id INT, extra TEXT);\n",
        prompt_id="p1",
    )
    out = run_hook("auto", _native(repo, "Stop", prompt_id="p1"))
    assert out.get("decision") == "block"
    assert "db/migrations/0001_init.sql" in out.get("reason", "")
    # Same findings next turn: debounce, no second continuation.
    assert run_hook("auto", _native(repo, "Stop", prompt_id="p1")) == {}


def test_claude_change_after_bash_finished_is_background(repo: Path, monkeypatch):
    """PostToolUse on Bash closes the window: a later hand edit (or another
    window's write) is not this session's gate bypass."""
    monkeypatch.chdir(repo)
    run_hook(
        "auto",
        _native(repo, "UserPromptSubmit", prompt_id="p1", prompt="do it"),
    )
    _native_bash_write(repo, repo / "app" / "gen.txt", "generated\n", prompt_id="p1")
    (repo / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT, by_hand TEXT);\n", encoding="utf-8"
    )
    assert run_hook("auto", _native(repo, "Stop", prompt_id="p1")) == {}
    manifest = SessionStore(repo).session("native-sess").manifest()
    assert [c["path"] for c in manifest["other_changes"]] == ["app/gen.txt"]
    assert manifest["other_changes"][0]["during"] == ["shell: ./gen.sh"]
    background = [c["path"] for c in manifest["background_changes"]]
    assert "db/migrations/0001_init.sql" in background
    assert manifest["protected_findings"] == []


def test_claude_session_end_finalizes_without_followup(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    run_hook(
        "auto",
        _native(repo, "UserPromptSubmit", prompt_id="p1", prompt="hi"),
    )
    (repo / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT, extra TEXT);\n", encoding="utf-8"
    )
    out = run_hook("auto", _native(repo, "SessionEnd", prompt_id="p1", reason="other"))
    assert out == {}
    session = SessionStore(repo).session("native-sess")
    assert session.manifest() is not None
    final = [e for e in session.events() if e["type"] == "final_check"]
    assert final and final[-1]["notified"] is False


# -- Codex -------------------------------------------------------------------


def test_codex_block_renders_as_deny(repo: Path, monkeypatch):
    """Codex ignores ask and would fail-open; block must become deny."""
    monkeypatch.chdir(repo)
    body = (
        "*** Begin Patch\n"
        "*** Update File: db/migrations/0001_init.sql\n"
        "*** End Patch\n"
    )
    out = run_hook(
        "auto",
        _native(
            repo,
            "PreToolUse",
            session_id="thr_99",
            turn_id="turn-1",
            tool_name="apply_patch",
            tool_input={"command": body},
        ),
    )
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "codex cannot prompt" in spec["permissionDecisionReason"].lower()
    meta = SessionStore(repo).session("thr_99").meta()
    assert meta["runtime"] == "codex"


def test_codex_allow_is_silent(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    out = run_hook(
        "auto",
        _native(
            repo,
            "PreToolUse",
            turn_id="t",
            session_id="thr_ok",
            tool_name="apply_patch",
            tool_input={
                "command": (
                    "*** Begin Patch\n*** Update File: app/main.py\n*** End Patch\n"
                )
            },
        ),
    )
    assert out == {}


# -- fail-open / auto --------------------------------------------------------


def test_auto_garbage_is_empty_not_crash(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    assert run_hook("auto", "not json at all") == {}
    assert run_hook("auto", "") == {}


def test_unknown_native_event_is_ignored(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    assert run_hook("auto", _native(repo, "Notification", prompt_id="p")) == {}
    assert not SessionStore(repo).session("native-sess").exists()


# -- init wiring -------------------------------------------------------------


def test_init_writes_claude_and_codex_hooks(repo: Path):
    init_repo(repo)
    claude = json.loads(
        (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert "UserPromptSubmit" in claude["hooks"]
    assert "PreToolUse" in claude["hooks"]
    assert "SessionEnd" in claude["hooks"]
    cmd = claude["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd == "archrev hook"
    matcher = claude["hooks"]["PostToolUse"][0]["matcher"]
    assert "apply_patch" in matcher
    # Shell/MCP after-hooks close the session's command window.
    assert "Bash" in matcher and "mcp__" in matcher
    assert (repo / ".claude" / "rules" / "archrev.md").exists()

    codex = json.loads((repo / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert "PreToolUse" in codex["hooks"]
    assert (repo / "AGENTS.md").exists()
    assert "ArchRev" in (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_init_runtime_subset_skips_other_runtimes(tmp_path: Path):
    init_repo(tmp_path, runtimes=("claude",))
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".cursor" / "hooks.json").exists()
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_reinit_upgrades_native_shim_without_duplicates(repo: Path):
    init_repo(repo, shim="none")
    init_repo(repo, shim="uvx")
    settings = json.loads(
        (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    for event, groups in settings["hooks"].items():
        ours = [
            h
            for g in groups
            for h in g.get("hooks", [])
            if "archrev" in h.get("command", "")
        ]
        assert len(ours) == 1, event
        assert ours[0]["command"].startswith("uvx archrev hook")
