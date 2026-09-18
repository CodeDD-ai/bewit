"""Diff building and HTML export embedding."""

from pathlib import Path

from archrev.gitutil import Git
from archrev.report.server import build_file_diff, export_html
from archrev.storage import SessionStore
from conftest import git  # pytest puts the tests dir on sys.path (no __init__.py)


def _session(repo: Path, sid: str = "sess-1"):
    session = SessionStore(repo).session(sid)
    session.ensure_meta(Git(repo).head_sha())
    return session


def test_file_diff_for_modified_file(repo: Path):
    session = _session(repo)
    (repo / "app" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    session.append_event("edit", path="app/main.py")

    diff = build_file_diff(repo, session.id, "app/main.py")
    assert diff is not None
    assert "-print('hello')" in diff
    assert "+print('changed')" in diff


def test_file_diff_for_untracked_file_is_synthesized(repo: Path):
    session = _session(repo)
    (repo / "app" / "new.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    session.append_event("edit", path="app/new.py")

    diff = build_file_diff(repo, session.id, "app/new.py")
    assert diff is not None
    assert "+a = 1" in diff and "+b = 2" in diff
    assert "/dev/null" in diff


def test_export_embeds_diffs(repo: Path):
    session = _session(repo)
    (repo / "app" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    session.append_event("edit", path="app/main.py")

    html = export_html(repo, session.id)
    assert html is not None
    assert "__ARCHREV_DATA__" in html
    # The diff itself travels inside the export (JSON-escaped).
    assert "print('changed')" in html
