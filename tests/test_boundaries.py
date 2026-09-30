"""Shell / read / MCP / tool rules and the deny action."""

import json
from pathlib import Path

import pytest

from bewit.config import Config
from bewit.gate import evaluate_read, evaluate_shell, evaluate_tool
from bewit.hooks import run_hook
from bewit.rules import load_rules
from bewit.storage import SessionStore

BOUNDARY_RULES = """
- id: no-secret-reads
  kind: read
  match: ["dev-secrets/**", ".env", ".env.*"]
  action: deny
  message: "No secret reads."
- id: no-push
  kind: shell
  match_command: ["\\\\bgit\\\\s+push\\\\b"]
  action: block
  message: "Push needs approval."
- id: flag-installs
  kind: shell
  match_command: ["\\\\bpip\\\\s+install\\\\b"]
  action: flag
  message: "Install recorded."
- id: no-web-mcp
  kind: mcp
  match_tool: ["web|fetch"]
  action: deny
  message: "No internet MCP."
- id: no-subagents
  kind: tool
  match_tool: ["^Task$"]
  action: deny
  message: "No subagents."
"""


@pytest.fixture()
def bounded_repo(repo: Path) -> Path:
    (repo / ".bewit" / "rules" / "20-boundaries.yaml").write_text(
        BOUNDARY_RULES, encoding="utf-8"
    )
    return repo


def test_boundary_rules_load_cleanly(bounded_repo: Path):
    ruleset = load_rules(bounded_repo)
    assert not ruleset.errors
    assert len(ruleset.shell_rules) == 2
    assert len(ruleset.read_rules) == 1
    assert len(ruleset.mcp_rules) == 1
    assert len(ruleset.tool_rules) == 1


def test_invalid_regex_is_a_load_error(repo: Path):
    (repo / ".bewit" / "rules" / "bad.yaml").write_text(
        "{id: bad-re, kind: shell, match_command: ['[unclosed'], action: deny}",
        encoding="utf-8",
    )
    ruleset = load_rules(repo)
    assert any("invalid regex" in e for e in ruleset.errors)
    assert not ruleset.shell_rules


def test_shell_deny_block_flag(bounded_repo: Path):
    ruleset = load_rules(bounded_repo)
    config = Config()
    assert evaluate_shell(config, ruleset, "git push origin main").permission == "ask"
    flagged = evaluate_shell(config, ruleset, "pip install requests")
    assert flagged.permission == "allow" and flagged.hits
    assert evaluate_shell(config, ruleset, "git status").permission == "allow"


def test_shell_monitor_downgrades(bounded_repo: Path):
    ruleset = load_rules(bounded_repo)
    decision = evaluate_shell(
        Config(enforcement="monitor"), ruleset, "git push origin main"
    )
    assert decision.permission == "allow"
    assert decision.hits and decision.hits[0].action == "flag"


def test_read_deny(bounded_repo: Path):
    ruleset = load_rules(bounded_repo)
    decision = evaluate_read(
        Config(), ruleset, [str(bounded_repo / "dev-secrets" / "db.txt")],
        bounded_repo,
    )
    assert decision.permission == "deny"
    assert "No secret reads." in decision.user_message
    assert evaluate_read(
        Config(), ruleset, ["app/main.py"], bounded_repo
    ).permission == "allow"


def test_tool_and_mcp_deny(bounded_repo: Path):
    ruleset = load_rules(bounded_repo)
    assert evaluate_tool(Config(), ruleset, "tool", "Task").permission == "deny"
    assert evaluate_tool(Config(), ruleset, "tool", "Write").permission == "allow"
    assert (
        evaluate_tool(Config(), ruleset, "mcp", "browser.web_fetch").permission
        == "deny"
    )


def test_hooks_end_to_end(bounded_repo: Path, monkeypatch):
    monkeypatch.chdir(bounded_repo)
    payload = lambda **kw: json.dumps({"conversation_id": "b-1", **kw})  # noqa: E731

    out = run_hook("shell", payload(command="git push --force"))
    assert out["permission"] == "ask"
    out = run_hook("read", payload(file_path=".env.production"))
    assert out["permission"] == "deny"
    out = run_hook("mcp", payload(tool_name="web_search", server="browser"))
    assert out["permission"] == "deny"
    out = run_hook("gate", payload(tool_name="Task", tool_input={}))
    assert out["permission"] == "deny"

    kinds = {
        (e.get("kind"), e.get("permission"))
        for e in SessionStore(bounded_repo).session("b-1").events()
        if e.get("type") == "gate"
    }
    assert kinds == {
        ("shell", "ask"), ("read", "deny"), ("mcp", "deny"), ("tool", "deny"),
    }


def test_bom_prefixed_stdin_still_parses(repo: Path, monkeypatch):
    """Regression: Windows BOM/mojibake before the JSON must not break capture
    (observed in real Cursor payloads)."""
    monkeypatch.chdir(repo)
    garbled = "\ufeffï»¿" + json.dumps(
        {"conversation_id": "bom-1", "prompt": "hello through the BOM"}
    )
    assert run_hook("prompt", garbled) == {}
    session = SessionStore(repo).session("bom-1")
    prompts = [e for e in session.events() if e.get("type") == "prompt"]
    assert prompts and prompts[0]["text"] == "hello through the BOM"
