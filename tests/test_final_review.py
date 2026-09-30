"""Regression tests for the findings of the final whole-codebase review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bewit.config import Config, load_config
from bewit.drift import compute_view
from bewit.gate import evaluate_shell
from bewit.gitutil import Git
from bewit.hooks import run_hook
from bewit.planning import register_plan
from bewit.report.server import build_file_diff
from bewit.rules import load_rules
from bewit.storage import SessionStore
from conftest import git  # pytest puts the tests dir on sys.path (no __init__.py)


def _rule(repo: Path, name: str, text: str) -> None:
    (repo / ".bewit" / "rules" / name).write_text(text, encoding="utf-8")


# 1 ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["dev-secrets", ".", "dev-secret*/key.txt", ":(glob)**/key.txt", "./"])
def test_viewer_diff_cannot_be_widened_to_a_read_ruled_file(repo: Path, path: str):
    (repo / "dev-secrets").mkdir()
    (repo / "dev-secrets" / "key.txt").write_text("old\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "key")
    (repo / "dev-secrets" / "key.txt").write_text("TOPSECRET\n", encoding="utf-8")
    _rule(repo, "s.yaml", "- id: no-dev-secrets\n  kind: read\n  match: ['dev-secrets/**']\n  action: deny\n")
    session = SessionStore(repo).session("s")
    session.ensure_meta(git(repo, "rev-parse", "HEAD").strip())
    assert "TOPSECRET" not in (build_file_diff(repo, "s", path) or "")


# 2 ---------------------------------------------------------------------------


def test_utf16_rules_file_is_read_and_bad_bytes_do_not_disable_gating(repo: Path):
    # PowerShell 5.1 `>` writes UTF-16 LE with a BOM.
    (repo / ".bewit" / "rules" / "ps.yaml").write_bytes(
        "- id: ps-rule\n  kind: path\n  match: ['ps/**']\n  action: deny\n".encode("utf-16")
    )
    (repo / ".bewit" / "rules" / "junk.yaml").write_bytes(b"\x80\x81 not text")
    rules = load_rules(repo)
    assert any(r.id == "ps-rule" for r in rules.rules)
    assert any("junk.yaml" in e for e in rules.errors)
    assert any(r.id == "protect-migrations" for r in rules.rules)  # others still load
    (repo / ".bewit" / "config.yaml").write_bytes(b"\x80\x81")
    assert load_config(repo) == Config()


# 3 ---------------------------------------------------------------------------


def test_codex_patch_through_a_shell_tool_meets_path_rules(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    patch = "*** Begin Patch\n*** Add File: db/migrations/0002.sql\n+x\n*** End Patch"
    out = run_hook("auto", json.dumps({
        "hook_event_name": "PreToolUse", "session_id": "thr_x", "turn_id": "t",
        "tool_name": "exec_command", "tool_input": {"command": f"apply_patch <<'EOF'\n{patch}\nEOF"},
    }))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"  # block on Codex = deny


# 4 ---------------------------------------------------------------------------


def test_check_command_quoting_on_windows(monkeypatch):
    from bewit import quality

    monkeypatch.setattr(quality.os, "name", "nt")
    assert quality._quote_arg("a&whoami&.py") == '"a&whoami&.py"'
    assert not quality._cmd_safe('a"b.py') and not quality._cmd_safe("a%PATH%.py")
    assert quality._cmd_safe("normal file.py")


# 5 ---------------------------------------------------------------------------


def test_git_base_from_a_record_is_never_an_option(repo: Path):
    g = Git(repo)
    head = git(repo, "rev-parse", "HEAD").strip()
    assert g.safe_base("--output=PWNED.txt") == "HEAD"
    assert g.safe_base("3f2a9c01d4e5") == "3f2a9c01d4e5"
    assert g.safe_base(None) == "HEAD"
    # CI passes branch names (--base origin/main); they resolve to a commit.
    git(repo, "branch", "-f", "target")
    assert g.safe_base("target") == head
    assert g.safe_base("no-such-branch") == "HEAD"
    assert not (repo / "PWNED.txt").exists()


# 6 ---------------------------------------------------------------------------


def test_capture_mode_governs_debug_payloads_and_shell_targets(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    (repo / ".bewit" / "config.yaml").write_text("prompt_capture: none\n", encoding="utf-8")
    _rule(repo, "sh.yaml", "- id: flag-echo\n  kind: shell\n  match_command: ['echo']\n  action: flag\n")
    run_hook("prompt", json.dumps({"prompt": "MY SECRET PROMPT"}))  # no session id
    run_hook("shell", json.dumps({"conversation_id": "c1", "command": "echo pw=hunter2"}))
    text = "".join(
        p.read_text(encoding="utf-8")
        for p in (repo / ".bewit" / "sessions").rglob("events.jsonl")
    )
    assert "MY SECRET PROMPT" not in text
    assert "hunter2" not in text
    assert "shell command (sha256" in text


# 7 ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["..", ".", "...hidden"])
def test_session_id_cannot_escape_the_sessions_directory(tmp_path: Path, raw: str):
    store = SessionStore(tmp_path)
    session = store.session(raw)
    assert not session.id.startswith(".")
    assert session.dir.parent == store.sessions_dir


# 8 ---------------------------------------------------------------------------


def test_index_metrics_work_from_manifests(repo: Path):
    from bewit.metrics import collect_repo

    session = SessionStore(repo).session("m1")
    session.ensure_meta(git(repo, "rev-parse", "HEAD").strip())
    register_plan(session, "Edit `app/main.py`.", repo)
    session.append_event("edit", path="app/main.py")
    compute_view(repo, load_config(repo), load_rules(repo), session, finalize=True)
    (session.dir / "events.jsonl").unlink()
    m = collect_repo(repo)
    assert (m.sessions, m.with_plan, m.edits, m.files_touched) == (1, 1, 1, 1)


# shell-read heuristic additions ---------------------------------------------------


@pytest.mark.parametrize("command,dialect", [
    ("Select-String -Path .env -Pattern KEY", "powershell"),
    ("$x = Get-Content .env", "powershell"),
    ("cmd /c type .env", "posix"),
    ("pwsh -Command Get-Content .env", "posix"),
])
def test_more_shell_read_forms_are_refused(repo: Path, command: str, dialect: str):
    _rule(repo, "env.yaml", "- id: no-env\n  kind: read\n  match: ['.env']\n  action: deny\n")
    decision = evaluate_shell(Config(), load_rules(repo), command, repo, dialect=dialect)
    assert decision.permission == "deny", command
