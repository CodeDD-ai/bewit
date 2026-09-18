from pathlib import Path

from archrev.config import Config
from archrev.drift import compute_view
from archrev.gitutil import Git
from archrev.planning import register_plan
from archrev.rules import load_rules
from archrev.storage import SessionStore


def _start_session(repo: Path, sid: str = "s1"):
    session = SessionStore(repo).session(sid)
    session.ensure_meta(Git(repo).head_sha())
    return session


def test_finalize_files_loc_and_drift(repo: Path):
    session = _start_session(repo)
    register_plan(session, "Will touch `app/main.py`.", repo)

    # Tracked edit: recorded by the edit hook AND changed on disk.
    (repo / "app" / "main.py").write_text(
        "print('hello')\nprint('world')\n", encoding="utf-8"
    )
    session.append_event("edit", path="app/main.py")
    # Out-of-plan tracked edit.
    (repo / "app" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    session.append_event("edit", path="app/extra.py")

    view = compute_view(repo, Config(), load_rules(repo), session, finalize=True)

    by_path = {f["path"]: f for f in view["files"]}
    assert by_path["app/main.py"]["in_plan"] is True
    assert by_path["app/main.py"]["added"] == 1  # one line added
    assert by_path["app/extra.py"]["in_plan"] is False
    assert by_path["app/extra.py"]["untracked"] is True
    assert view["drift"]["out_of_plan"] == ["app/extra.py"]
    assert view["drift"]["unrealized"] == []
    assert session.manifest() is not None
    assert session.manifest()["finalized_at"]


def test_protected_scan_catches_gate_bypass(repo: Path):
    """A protected file changed WITHOUT an edit event (e.g. via shell)."""
    session = _start_session(repo)
    (repo / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT, x INT);\n", encoding="utf-8"
    )
    view = compute_view(repo, Config(), load_rules(repo), session)

    findings = view["protected_findings"]
    assert any(
        f["path"] == "db/migrations/0001_init.sql"
        and f["rule_id"] == "protect-migrations"
        and f["via_gate"] is False
        for f in findings
    )
    # And it appears under other_changes, not the session's tracked files.
    assert any(c["path"] == "db/migrations/0001_init.sql" for c in view["other_changes"])
    assert view["files"] == []


def test_no_plan_means_no_out_of_plan_drift(repo: Path):
    session = _start_session(repo)
    session.append_event("edit", path="app/main.py")
    view = compute_view(repo, Config(), load_rules(repo), session)
    assert view["plan"]["registered"] is False
    assert view["drift"]["out_of_plan"] == []


def test_view_without_git_still_works(tmp_path: Path):
    """Session capture degrades gracefully in a non-git directory."""
    session = SessionStore(tmp_path).session("s")
    session.ensure_meta(None)
    session.append_event("prompt", text="hi")
    session.append_event("edit", path="a.py")
    view = compute_view(tmp_path, Config(), load_rules(tmp_path), session)
    assert view["files"][0]["path"] == "a.py"
    assert view["files"][0]["added"] is None  # LOC unknown without git
