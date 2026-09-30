import os
import time
from pathlib import Path

from bewit.gitutil import Git
from bewit.storage import SessionStore
from bewit.trailer import add_trailers, attribute_staged, sessions_for_staged
from conftest import git  # pytest puts the tests dir on sys.path (no __init__.py)


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


def test_expired_sessions_are_not_linked(repo: Path):
    session = _session_with_edit(repo, "sess-old", "app/main.py")
    log = session.dir / "events.jsonl"
    old = time.time() - 20 * 86400
    os.utime(log, (old, old))
    (repo / "app" / "main.py").write_text("print('old')\n", encoding="utf-8")
    git(repo, "add", "app/main.py")
    assert sessions_for_staged(repo) == []


def test_add_trailers_appends_once(repo: Path):
    _session_with_edit(repo, "sess-one", "app/main.py")
    (repo / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    git(repo, "add", "app/main.py")

    msg = repo / "COMMIT_EDITMSG"
    msg.write_text("feat: change main\n", encoding="utf-8")

    assert add_trailers(msg, repo) is True
    content = msg.read_text(encoding="utf-8")
    assert "Bewit-Session: sess-one" in content
    # Idempotent: second run adds nothing.
    assert add_trailers(msg, repo) is False
    assert content.count("Bewit-Session") == 1


def test_trailer_lookup_via_git_log(repo: Path):
    _session_with_edit(repo, "sess-one", "app/main.py")
    (repo / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    git(repo, "add", "app/main.py")
    git(
        repo, "commit", "--quiet",
        "-m", "feat: change main\n\nBewit-Session: sess-one",
    )
    commits = Git(repo).commits_with_session("sess-one")
    assert len(commits) == 1
    assert commits[0]["subject"] == "feat: change main"


def test_no_staged_files_no_trailer(repo: Path):
    _session_with_edit(repo, "sess-one", "app/main.py")
    msg = repo / "COMMIT_EDITMSG"
    msg.write_text("chore: empty\n", encoding="utf-8")
    assert add_trailers(msg, repo) is False


def test_unattributed_staged_files_recorded_as_human(repo: Path):
    """Files committed without any agent session land in the human ledger."""
    _session_with_edit(repo, "sess-one", "app/main.py")
    (repo / "app" / "main.py").write_text("print('agent')\n", encoding="utf-8")
    (repo / "app" / "manual.py").write_text("print('human')\n", encoding="utf-8")
    git(repo, "add", "app/main.py", "app/manual.py")

    session_ids, unattributed = attribute_staged(repo)
    assert session_ids == ["sess-one"]
    assert unattributed == ["app/manual.py"]

    msg = repo / "COMMIT_EDITMSG"
    msg.write_text("feat: mixed commit\n", encoding="utf-8")
    add_trailers(msg, repo)

    human = SessionStore(repo).session("human")
    events = [e for e in human.events() if e["type"] == "human_changes"]
    assert len(events) == 1
    assert events[0]["files"] == ["app/manual.py"]
    # The human ledger never attracts commit trailers itself.
    assert "human" not in sessions_for_staged(repo)


def test_bewit_bookkeeping_never_counts_as_human(repo: Path):
    """Session files are written by hooks; the human ledger must skip them."""
    session = _session_with_edit(repo, "sess-one", "app/main.py")
    git(repo, "add", str(session.dir.relative_to(repo)).replace("\\", "/"))
    msg = repo / "COMMIT_EDITMSG"
    msg.write_text("chore: record session\n", encoding="utf-8")
    add_trailers(msg, repo)
    assert SessionStore(repo).session("human").events() == []


def test_no_human_record_without_agent_sessions(repo: Path):
    """Purely manual repositories must not accumulate human-change noise."""
    (repo / "app" / "manual.py").write_text("print('human')\n", encoding="utf-8")
    git(repo, "add", "app/manual.py")
    msg = repo / "COMMIT_EDITMSG"
    msg.write_text("feat: manual\n", encoding="utf-8")
    add_trailers(msg, repo)
    assert SessionStore(repo).session("human").events() == []
