"""What of a session record git sees, per record_scope, and reading it back."""

from __future__ import annotations

from pathlib import Path

from bewit.config import load_config
from bewit.drift import compute_view, load_view
from bewit.planning import register_plan
from bewit.report.render import render_markdown
from bewit.rules import load_rules
from bewit.scaffold import COMMITTED_SESSION_FILES, init_repo, records_gitignore_block
from bewit.storage import SessionStore
from conftest import git  # pytest puts the tests dir on sys.path (no __init__.py)


def _finalized_session(repo: Path, sid: str = "s1"):
    session = SessionStore(repo).session(sid)
    session.ensure_meta(git(repo, "rev-parse", "HEAD").strip(), branch="main", runtime="claude")
    session.append_event("prompt", text="add a greeting")
    register_plan(session, "Edit `app/main.py` to greet.", repo)
    (repo / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    session.append_event("edit", path="app/main.py", tool="Write")
    compute_view(repo, load_config(repo), load_rules(repo), session, finalize=True)
    session.write_report("# report")
    return session


def _visible_session_files(repo: Path, sid: str) -> set[str]:
    """Session files git would commit (untracked, not ignored)."""
    out = git(repo, "status", "--porcelain", "--untracked-files=all", f".bewit/sessions/{sid}")
    return {line[3:].rsplit("/", 1)[-1] for line in out.splitlines() if line.strip()}


def _set_scope(repo: Path, scope: str) -> None:
    cfg = repo / ".bewit" / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    for old in ("record_scope: audit", "record_scope: full"):
        text = text.replace(old, f"record_scope: {scope}")
    cfg.write_text(text, encoding="utf-8")


def test_audit_scope_commits_only_the_manifest(repo: Path):
    init_repo(repo, runtimes=("claude",), record_store="tree")
    assert load_config(repo).record_scope == "audit"
    _finalized_session(repo)
    assert _visible_session_files(repo, "s1") == {"manifest.json"}


def test_full_scope_commits_the_evidence_not_derived_files(repo: Path):
    init_repo(repo, runtimes=("claude",), record_store="tree")
    _set_scope(repo, "full")
    init_repo(repo, runtimes=("claude",))  # rewrites the managed block
    _finalized_session(repo)
    assert _visible_session_files(repo, "s1") == set(COMMITTED_SESSION_FILES["full"])


def test_managed_block_replaces_legacy_lines_and_is_idempotent(repo: Path):
    (repo / ".gitignore").write_text(
        "node_modules/\n\n# Bewit audit tier: the full event log stays on the authoring machine.\n"
        "# Manifests, plans, and reports under .bewit/sessions/ stay committed.\n"
        ".bewit/sessions/*/events.jsonl\n",
        encoding="utf-8",
    )
    init_repo(repo, runtimes=("claude",), record_store="tree")
    first = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in first
    assert "stay committed" not in first
    assert first.count(".bewit/sessions/*/events.jsonl") == 1
    assert records_gitignore_block("audit") in first
    init_repo(repo, runtimes=("claude",))
    assert (repo / ".gitignore").read_text(encoding="utf-8") == first
    # Switching to the ref store replaces the block; nothing is left behind.
    init_repo(repo, runtimes=("claude",), record_store="ref")
    switched = (repo / ".gitignore").read_text(encoding="utf-8")
    assert records_gitignore_block("audit", "ref") in switched
    assert ".bewit/sessions/*/events.jsonl" not in switched


def test_session_records_are_marked_generated_once(repo: Path):
    init_repo(repo, runtimes=("claude",))
    init_repo(repo, runtimes=("claude",))
    attrs = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert attrs.count(".bewit/sessions/** linguist-generated=true gitlab-generated=true") == 1
    check = git(repo, "check-attr", "linguist-generated", "--", ".bewit/sessions/s/manifest.json")
    assert check.strip().endswith("true")


def test_clone_with_manifest_only_keeps_metadata_prompts_and_acks(repo: Path):
    """trace, the sidebar, and diffs work from the manifest in a clone."""
    from bewit.report.server import _session_summary, build_file_diff

    session = _finalized_session(repo)
    session.append_event("ack", target="app/main.py", note="fine", actor="human")
    compute_view(repo, load_config(repo), load_rules(repo), session, finalize=True)
    for name in ("events.jsonl", "meta.json", "plan.md", "report.md"):
        (session.dir / name).unlink()
    assert session.meta()["started_at"] and session.meta()["runtime"] == "claude"
    assert session.prompts()[0]["text"] == "add a greeting"
    summary = _session_summary(repo, session)
    assert summary["prompt_excerpt"] == "add a greeting"
    assert summary["edits"] == 1 and summary["runtime"] == "claude"
    diff = build_file_diff(repo, session.id, "app/main.py")
    assert diff and "+print('hi')" in diff  # base commit comes from the manifest


def test_manifest_alone_renders_the_review_and_plan(repo: Path):
    """A colleague or CI in audit scope has only manifest.json."""
    session = _finalized_session(repo)
    for name in ("events.jsonl", "meta.json", "plan.md", "report.md"):
        (session.dir / name).unlink()
    view = load_view(repo, load_config(repo), load_rules(repo), session)
    assert view["from_manifest"] is True
    assert view["plan"]["text"].startswith("Edit `app/main.py`")
    markdown = render_markdown(view)
    assert "add a greeting" in markdown and "## Review" in markdown
