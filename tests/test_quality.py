"""Check rules: quality-gate commands over changed files."""

import json
from pathlib import Path

from archrev.hooks import run_hook
from archrev.quality import run_checks
from archrev.rules import load_rules
from archrev.storage import SessionStore

_PASS = 'python -c "import sys; sys.exit(0)"'
_FAIL = 'python -c "import sys; print(\'violation found\'); sys.exit(1)"'


def _write_rule(repo: Path, name: str, body: str) -> None:
    (repo / ".archrev" / "rules" / name).write_text(body, encoding="utf-8")


def test_check_rule_parses_and_lists(repo: Path):
    # Commands are written as plain YAML scalars: quoting them in YAML
    # single quotes breaks on the quotes the commands themselves contain.
    _write_rule(repo, "checks.yaml", f"""
- id: q-pass
  kind: check
  match: ["app/**"]
  command: {_PASS}
  action: block
""")
    ruleset = load_rules(repo)
    assert not ruleset.errors
    assert [r.id for r in ruleset.check_rules] == ["q-pass"]


def test_check_rules_reject_deny_action(repo: Path):
    _write_rule(repo, "checks.yaml", f"""
- id: q-bad
  kind: check
  match: ["app/**"]
  command: {_PASS}
  action: deny
""")
    ruleset = load_rules(repo)
    assert ruleset.check_rules == []
    assert any("q-bad" in e for e in ruleset.errors)


def test_run_checks_verdicts_and_skipping(repo: Path):
    _write_rule(repo, "checks.yaml", f"""
- id: q-pass
  kind: check
  match: ["app/**"]
  command: {_PASS}
  action: flag
- id: q-fail
  kind: check
  match: ["app/**"]
  command: {_FAIL}
  action: block
- id: q-skipped
  kind: check
  match: ["frontend/**"]
  command: {_FAIL}
  action: block
""")
    results = run_checks(repo, load_rules(repo), ["app/main.py"])
    by_id = {r["rule_id"]: r for r in results}
    assert set(by_id) == {"q-pass", "q-fail"}  # q-skipped matched nothing
    assert by_id["q-pass"]["ok"] is True
    assert by_id["q-fail"]["ok"] is False
    assert "violation found" in by_id["q-fail"]["output"]


def test_files_placeholder_substitution(repo: Path):
    _write_rule(repo, "checks.yaml", """
- id: q-echo
  kind: check
  match: ["app/**"]
  command: python -c "import sys; print(sys.argv[1:]); sys.exit(0)" {files}
  action: flag
""")
    results = run_checks(repo, load_rules(repo), ["app/main.py"])
    assert results[0]["ok"] is True
    assert "app/main.py" in results[0]["output"]


def test_unrunnable_check_is_error_not_failure(repo: Path):
    """Fail-open: a missing tool must not fail the session."""
    _write_rule(repo, "checks.yaml", """
- id: q-missing
  kind: check
  match: ["app/**"]
  command: definitely-not-a-real-tool-xyz --flag
  action: block
""")
    results = run_checks(repo, load_rules(repo), ["app/main.py"])
    # Windows shell reports missing commands via exit code, POSIX via
    # OSError; either way the check must not pass silently as ok=True.
    assert results[0]["ok"] is not True


def test_failed_block_check_reaches_final_review(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    _write_rule(repo, "checks.yaml", f"""
- id: q-fail
  kind: check
  match: ["app/**"]
  command: {_FAIL}
  action: block
  message: "Endpoints must validate input."
""")
    payload = json.dumps({"conversation_id": "conv-q", "prompt": "x"})
    run_hook("prompt", payload)
    run_hook(
        "edit",
        json.dumps({"conversation_id": "conv-q", "file_path": "app/main.py"}),
    )
    out = run_hook("finalize", json.dumps({"conversation_id": "conv-q"}))
    assert "followup_message" in out
    assert "q-fail" in out["followup_message"]

    session = SessionStore(repo).session("conv-q")
    events = [e for e in session.events() if e["type"] == "quality_check"]
    assert events and events[-1]["ok"] is False

    # Acknowledging the rule id resolves it.
    session.append_event("ack", target="q-fail", note="accepted for this session")
    assert run_hook("finalize", json.dumps({"conversation_id": "conv-q"})) == {}


def test_flag_check_failure_recorded_but_not_raised(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    _write_rule(repo, "checks.yaml", f"""
- id: q-flag
  kind: check
  match: ["app/**"]
  command: {_FAIL}
  action: flag
""")
    payload = json.dumps({"conversation_id": "conv-q2", "prompt": "x"})
    run_hook("prompt", payload)
    run_hook(
        "edit",
        json.dumps({"conversation_id": "conv-q2", "file_path": "app/main.py"}),
    )
    out = run_hook("finalize", json.dumps({"conversation_id": "conv-q2"}))
    assert out == {}  # flag failures never nag the agent
    session = SessionStore(repo).session("conv-q2")
    events = [e for e in session.events() if e["type"] == "quality_check"]
    assert events and events[-1]["ok"] is False  # but the record is there
