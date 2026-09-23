"""Audit-tier storage, cross-session spillover, explain, prune, syntax check."""

import os
import time
from pathlib import Path

from archrev.cli import explain_path
from archrev.config import load_config
from archrev.drift import compute_view
from archrev.gitutil import Git
from archrev.quality import run_check
from archrev.rules import Rule, load_rules
from archrev.scaffold import init_repo
from archrev.storage import SessionStore
from conftest import git


def test_init_audit_scope_gitignores_event_logs(tmp_path: Path):
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    init_repo(tmp_path)
    assert load_config(tmp_path).record_scope == "audit"
    assert ".archrev/sessions/*/events.jsonl" in (
        tmp_path / ".gitignore"
    ).read_text(encoding="utf-8")


def test_invalid_record_scope_falls_back_to_full(repo: Path):
    (repo / ".archrev" / "config.yaml").write_text(
        "record_scope: everywhere\n", encoding="utf-8"
    )
    assert load_config(repo).record_scope == "full"


def test_other_session_edits_are_not_a_bypass(repo: Path):
    """A second window must not be told it bypassed the gate for the first."""
    store = SessionStore(repo)
    owner = store.session("window-a")
    owner.ensure_meta(Git(repo).head_sha())
    owner.append_event("edit", path="db/migrations/0001_init.sql")
    (repo / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT, extra TEXT);\n", encoding="utf-8"
    )
    other = store.session("window-b")
    other.ensure_meta(Git(repo).head_sha())
    other.append_event("prompt", text="unrelated")
    view = compute_view(repo, load_config(repo), load_rules(repo), other)
    assert not any(
        c["path"] == "db/migrations/0001_init.sql" for c in view["other_changes"]
    )
    assert any(
        c["path"] == "db/migrations/0001_init.sql" and "window-a" in c["sessions"]
        for c in view["other_sessions"]
    )
    assert not any(
        f["path"] == "db/migrations/0001_init.sql" and not f["via_gate"]
        for f in view["protected_findings"]
    )


def test_shell_bypass_still_raises_when_no_session_owns_it(repo: Path):
    session = SessionStore(repo).session("solo")
    session.ensure_meta(Git(repo).head_sha())
    session.append_event("prompt", text="hi")
    (repo / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT, extra TEXT);\n", encoding="utf-8"
    )
    view = compute_view(repo, load_config(repo), load_rules(repo), session)
    assert any(
        c["path"] == "db/migrations/0001_init.sql" for c in view["other_changes"]
    )
    assert any(
        f["path"] == "db/migrations/0001_init.sql" and not f["via_gate"]
        for f in view["protected_findings"]
    )


def test_explain_names_the_rule_or_says_none(repo: Path):
    hit = explain_path(repo, "db/migrations/0001_init.sql", "edit")
    assert "protect-migrations" in hit
    assert "paused" in hit
    miss = explain_path(repo, "app/main.py", "edit")
    assert "no rule matches" in miss


def test_prune_drops_old_finalized_logs_only(tmp_path: Path):
    store = SessionStore(tmp_path)
    old = store.session("old-one")
    old.ensure_meta(None)
    old.append_event("prompt", text="ancient")
    old.write_manifest({"id": "old-one", "chain_head": "abc"})
    log = old.dir / "events.jsonl"
    stamp = time.time() - 40 * 86400
    os.utime(log, (stamp, stamp))

    live = store.session("live-one")
    live.ensure_meta(None)
    live.append_event("prompt", text="still going")
    live_log = live.dir / "events.jsonl"
    os.utime(live_log, (stamp, stamp))

    removed = store.prune_event_logs(30)
    assert removed == ["old-one"]
    assert not log.exists()
    assert old.manifest()["chain_head"] == "abc"
    assert live_log.exists()


def test_syntaxcheck_does_not_need_pycache(repo: Path):
    rule = Rule(
        id="syn",
        kind="check",
        action="block",
        message="parse",
        command="python -m archrev.syntaxcheck {files}",
        match=("**/*.py",),
    )
    ok = run_check(repo, rule, ["app/main.py"])
    assert ok["ok"] is True
    broken = repo / "app" / "broken.py"
    broken.write_text("def (\n", encoding="utf-8")
    bad = run_check(repo, rule, ["app/broken.py"])
    assert bad["ok"] is False
    assert not (repo / "app" / "__pycache__").exists()
