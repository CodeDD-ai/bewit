"""record_store: ref - session records on refs/bewit/records."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bewit.config import load_config
from bewit.drift import compute_view, load_view
from bewit.hooks import run_hook
from bewit.planning import register_plan
from bewit.refstore import (
    FROM_REF_MARKER,
    RECORDS_REF,
    RefStoreError,
    default_remote,
    hydrate,
    publish,
    publish_quietly,
    ref_sha,
    sync,
)
from bewit.rules import load_rules
from bewit.scaffold import init_repo
from bewit.storage import SessionStore


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def _new_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "dev@example.test")
    _git(path, "config", "user.name", "dev")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    init_repo(path, runtimes=("claude",))  # new repositories default to the ref store
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "bewit")
    return path


def _record(root: Path, sid: str, prompt: str = "change app") -> None:
    session = SessionStore(root).session(sid)
    session.ensure_meta(_git(root, "rev-parse", "HEAD").strip(), runtime="claude")
    session.append_event("prompt", text=prompt)
    register_plan(session, "Edit `app.py`.", root)
    session.append_event("edit", path="app.py")
    compute_view(root, load_config(root), load_rules(root), session, finalize=True)


def _ref_files(root: Path) -> set[str]:
    return set(_git(root, "ls-tree", "-r", "--name-only", RECORDS_REF).split())


def test_new_repositories_default_to_the_ref_store(tmp_path: Path):
    repo = _new_repo(tmp_path / "r")
    assert load_config(repo).record_store == "ref"
    assert ".bewit/sessions/\n" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert "sync --remote" in (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")


def test_publish_writes_the_ref_and_never_the_tree_or_index(tmp_path: Path):
    repo = _new_repo(tmp_path / "r")
    _record(repo, "s-one")
    assert publish(repo, load_config(repo)) is not None
    assert _ref_files(repo) == {"sessions/s-one/manifest.json"}  # audit scope: one file
    assert _git(repo, "status", "--porcelain") == ""  # sessions/ is ignored, tree clean
    assert _git(repo, "diff", "--cached") == ""  # the user's index is untouched
    assert publish(repo, load_config(repo)) is None  # nothing changed: no new commit
    message = _git(repo, "log", "-1", "--format=%an %s", RECORDS_REF).strip()
    assert message == "Bewit bewit: session records"


def test_turn_end_publishes_automatically(tmp_path: Path, monkeypatch):
    repo = _new_repo(tmp_path / "r")
    monkeypatch.chdir(repo)
    run_hook("prompt", json.dumps({"conversation_id": "live-1", "prompt": "hello"}))
    run_hook("finalize", json.dumps({"conversation_id": "live-1", "status": "completed"}))
    assert "sessions/live-1/manifest.json" in _ref_files(repo)


def test_tree_store_never_touches_refs(repo: Path):
    SessionStore(repo).session("t").append_event("prompt", text="x")
    assert publish_quietly(repo, load_config(repo)) is None
    assert ref_sha(repo) is None


def test_two_developers_share_records_through_a_remote(tmp_path: Path):
    origin = _new_repo(tmp_path / "origin")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(origin), str(bare))
    alice, bob = tmp_path / "alice", tmp_path / "bob"
    for clone in (alice, bob):
        _git(tmp_path, "clone", "-q", str(bare), str(clone))
        _git(clone, "config", "user.email", f"{clone.name}@example.test")
        _git(clone, "config", "user.name", clone.name)

    _record(alice, "alice-1", "alice's change")
    first = sync(alice, load_config(alice))
    assert first["pushed"] and first["published"]

    # A clone with nothing of its own fast-forwards: no empty merge commit.
    carol = tmp_path / "carol"
    _git(tmp_path, "clone", "-q", str(bare), str(carol))
    assert sync(carol, load_config(carol), push=False)["published"] is None
    assert ref_sha(carol) == ref_sha(alice)

    _record(bob, "bob-1", "bob's change")
    second = sync(bob, load_config(bob))
    assert second["pushed"] and second["hydrated"] >= 1
    assert _ref_files(bob) == {"sessions/alice-1/manifest.json", "sessions/bob-1/manifest.json"}

    # Bob reads Alice's session from the hydrated summary...
    hydrated = SessionStore(bob).session("alice-1")
    assert (hydrated.dir / FROM_REF_MARKER).exists()
    view = load_view(bob, load_config(bob), load_rules(bob), hydrated)
    assert view["from_manifest"] and view["prompts"][0]["text"] == "alice's change"

    # ...and Alice gets Bob's; her own session is never overwritten.
    sync(alice, load_config(alice))
    assert SessionStore(alice).session("bob-1").manifest() is not None
    assert not (SessionStore(alice).session("alice-1").dir / FROM_REF_MARKER).exists()

    # Alice's later update reaches Bob; Bob never republishes a stale copy.
    SessionStore(alice).session("alice-1").append_event("prompt", text="follow-up")
    s = SessionStore(alice).session("alice-1")
    compute_view(alice, load_config(alice), load_rules(alice), s, finalize=True)
    sync(alice, load_config(alice))
    sync(bob, load_config(bob))
    texts = [p["text"] for p in SessionStore(bob).session("alice-1").manifest()["prompts"]]
    assert "follow-up" in texts


def test_sync_uses_the_remote_git_push_would_use(tmp_path: Path):
    """A repository whose remote is not called origin syncs without --remote."""
    origin = _new_repo(tmp_path / "origin")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(origin), str(bare))
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", "--origin", "codedd_app", str(bare), str(dev))
    _git(dev, "config", "user.email", "dev@example.test")
    _git(dev, "config", "user.name", "dev")
    assert default_remote(dev) == "codedd_app"  # the branch's upstream, and the only remote

    _record(dev, "s1")
    result = sync(dev, load_config(dev))
    assert result["remote"] == "codedd_app" and result["pushed"]
    assert "refs/bewit/records" in _git(bare, "for-each-ref", "--format=%(refname)")

    # A second remote does not change the choice: the upstream wins...
    _git(dev, "remote", "add", "mirror", str(bare))
    assert default_remote(dev) == "codedd_app"
    # ...and an explicit push default beats the upstream, as for git push.
    _git(dev, "config", "remote.pushDefault", "mirror")
    assert default_remote(dev) == "mirror"


def test_sync_refuses_to_guess_between_remotes(tmp_path: Path):
    repo = _new_repo(tmp_path / "r")
    assert default_remote(repo) is None
    with pytest.raises(RefStoreError, match="no git remote"):
        sync(repo, load_config(repo))
    _git(repo, "remote", "add", "a", str(tmp_path / "a.git"))
    _git(repo, "remote", "add", "b", str(tmp_path / "b.git"))
    with pytest.raises(RefStoreError, match="pass --remote"):
        sync(repo, load_config(repo))


def test_hydrate_never_overwrites_a_session_recorded_here(tmp_path: Path):
    repo = _new_repo(tmp_path / "r")
    _record(repo, "mine")
    publish(repo, load_config(repo))
    session = SessionStore(repo).session("mine")
    session.append_event("prompt", text="local only")  # newer than the ref
    before = (session.dir / "events.jsonl").read_bytes()
    hydrate(repo, force=True)
    assert (session.dir / "events.jsonl").read_bytes() == before
    assert not (session.dir / FROM_REF_MARKER).exists()


@pytest.mark.skipif(shutil.which("bewit") is None, reason="needs the bewit CLI on PATH")
def test_git_push_carries_the_records_ref(tmp_path: Path):
    origin = _new_repo(tmp_path / "origin")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(origin), str(bare))
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", str(bare), str(dev))
    _git(dev, "config", "user.email", "dev@example.test")
    _git(dev, "config", "user.name", "dev")
    init_repo(dev, runtimes=("claude",))  # hooks are not cloned: install them
    _record(dev, "pushed-1")
    (dev / "app.py").write_text("x = 2\n", encoding="utf-8")
    _git(dev, "commit", "-qam", "change")
    _git(dev, "push", "-q", "origin", "HEAD")
    remote_refs = _git(bare, "for-each-ref", "--format=%(refname)")
    assert RECORDS_REF in remote_refs
