"""CLI output: readable, ASCII-safe, and actionable."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bewit.cli import main
from bewit.config import load_config
from bewit.drift import compute_view
from bewit.planning import register_plan
from bewit.rules import load_rules
from bewit.storage import SessionStore
from conftest import git  # pytest puts the tests dir on sys.path (no __init__.py)

SID = "3f2a9c01-1111-2222-3333-444455556666"


@pytest.fixture()
def cli(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    runner = CliRunner()

    def invoke(*args: str):
        return runner.invoke(main, list(args), catch_exceptions=False)

    return invoke


def _session(repo: Path, finalize: bool = True):
    session = SessionStore(repo).session(SID)
    session.ensure_meta(git(repo, "rev-parse", "HEAD").strip(), branch="main", runtime="claude")
    session.append_event("prompt", text='<pasted_content id="1">\n  Add   a greeting\n</pasted_content>')
    register_plan(session, "Edit `app/main.py`.", repo)
    (repo / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    session.append_event("edit", path="app/main.py", tool="Write")
    session.append_event("gate", kind="read", target=".env", permission="deny",
                         paths=[], hits=[{"rule_id": "no-env", "action": "deny", "path": ".env"}])
    if finalize:
        compute_view(repo, load_config(repo), load_rules(repo), session, finalize=True)
    return session


def _ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def test_sessions_is_a_readable_table(cli, repo: Path):
    _session(repo)
    SessionStore(repo).session("human").append_event("human_changes", files=["x"])
    out = cli("sessions").output
    assert out.splitlines()[0].split()[:3] == ["ID", "STARTED", "STATUS"]
    row = next(line for line in out.splitlines() if line.startswith(SID[:8]))
    assert "claude" in row and "Add a greeting" in row and "<pasted_content" not in row
    assert "other records: human" in out
    assert _ascii(out)


def test_show_leads_with_review_and_keeps_targets_on_one_line(cli, repo: Path):
    _session(repo)
    out = cli("show", SID[:8]).output
    assert out.index("REVIEW") < out.index("RULES") < out.index("PROMPTS") < out.index("FILES")
    assert "Rule refused a file read  [no-env]  .env" in out
    assert _ascii(out)


def test_check_plan_names_the_fix(cli, repo: Path):
    session = SessionStore(repo).session(SID)
    session.ensure_meta(None)
    session.append_event("prompt", text="go")
    result = cli("check", "plan", "--session", SID)
    assert result.exit_code == 1
    assert "no plan is registered" in result.output
    assert "--attest api-rate-limit=pass|fail|n/a" in result.output


def test_check_diff_fails_on_a_changed_deny_path(cli, repo: Path):
    """A deny path changed outside the gate must fail CI, not only block paths."""
    (repo / ".bewit" / "rules" / "deny.yaml").write_text(
        "- id: never-edit-app\n  kind: path\n  match: ['app/**']\n  action: deny\n",
        encoding="utf-8",
    )
    (repo / "app" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    result = cli("check", "diff", "--no-quality")
    assert result.exit_code == 1
    assert "FAILED: 1 protected path(s) changed" in result.output


def test_rules_output_has_no_python_reprs(cli):
    out = cli("rules").output
    assert "['" not in out and "\\\\" not in out and "=False" not in out
    assert "Config (.bewit/config.yaml): enforcement on" in out


def test_rules_explain_covers_edits_and_reads(cli, repo: Path):
    (repo / ".bewit" / "rules" / "env.yaml").write_text(
        "- id: no-env\n  kind: read\n  match: ['.env']\n  action: deny\n", encoding="utf-8"
    )
    out = cli("rules", "explain", ".env").output
    assert ".env  (edit)" in out and ".env  (read)" in out and "refused outright" in out


def test_rules_explain_agrees_with_the_gate_on_exempt_paths(cli, repo: Path):
    from bewit.gate import evaluate_read

    (repo / ".bewit" / "rules" / "env.yaml").write_text(
        "- id: no-env\n  kind: read\n  match: ['.env']\n  action: deny\n", encoding="utf-8"
    )
    config = repo / ".bewit" / "config.yaml"
    before = config.read_text(encoding="utf-8") if config.exists() else ""
    config.write_text(before + "\nexempt:\n  - fixtures/**\n",
                      encoding="utf-8")
    fake = "fixtures/corpus/.env"
    assert evaluate_read(load_config(repo), load_rules(repo), [fake], repo).permission == "allow"
    out = cli("rules", "explain", fake, "--kind", "read").output
    assert "exempt (config `exempt: fixtures/**`)" in out
    assert "no-env [deny]" in out and "refused outright" not in out
    # The real secret is still refused.
    assert "refused outright" in cli("rules", "explain", ".env", "--kind", "read").output


def test_verify_ends_with_a_summary(cli, repo: Path):
    _session(repo)
    out = cli("verify", "--all").output
    assert out.strip().splitlines()[-1].startswith("1 verified")


def test_trace_file_works_from_the_manifest_alone(cli, repo: Path):
    """In audit scope a clone has only manifest.json; trace must still find the session."""
    session = _session(repo)
    for name in ("events.jsonl", "meta.json"):
        (session.dir / name).unlink()
    out = cli("trace", "app/main.py").output
    assert SID[:8] in out and "Add a greeting" in out and "claude" in out
