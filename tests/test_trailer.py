from pathlib import Path

from archrev.gitutil import Git
from archrev.storage import SessionStore
from archrev.trailer import add_trailers, sessions_for_staged
from tests.conftest import git


def _session_with_edit(repo: Path, sid: str, path: str):
    session = SessionStore(repo).session(sid)
    session.ensure_meta(Git(repo).head_sha())
    session.append_event("edit", path=path)
    return session


def test_sessions_for_staged_matches_overlap(repo: Path):
    _session_with_edit(repo, "sess-one", "app/main.py")
    _session_with_edit(repo, "sess-two", "db/migrations/0001_init.sql")

    (repo / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    git(repo, "add", "app/main.py")

    assert sessions_for_staged(repo) == ["sess-one"]


def test_add_trailers_appends_once(repo: Path):
    _session_with_edit(repo, "sess-one", "app/main.py")
    (repo / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    git(repo, "add", "app/main.py")

    msg = repo / "COMMIT_EDITMSG"
    msg.write_text("feat: change main\n", encoding="utf-8")

    assert add_trailers(msg, repo) is True
    content = msg.read_text(encoding="utf-8")
    assert "ArchRev-Session: sess-one" in content
    # Idempotent: second run adds nothing.
    assert add_trailers(msg, repo) is False
    assert content.count("ArchRev-Session") == 1


def test_trailer_lookup_via_git_log(repo: Path):
    _session_with_edit(repo, "sess-one", "app/main.py")
    (repo / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    git(repo, "add", "app/main.py")
    git(
        repo, "commit", "--quiet",
        "-m", "feat: change main\n\nArchRev-Session: sess-one",
    )
    commits = Git(repo).commits_with_session("sess-one")
    assert len(commits) == 1
    assert commits[0]["subject"] == "feat: change main"


def test_no_staged_files_no_trailer(repo: Path):
    _session_with_edit(repo, "sess-one", "app/main.py")
    msg = repo / "COMMIT_EDITMSG"
    msg.write_text("chore: empty\n", encoding="utf-8")
    assert add_trailers(msg, repo) is False
