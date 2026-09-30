"""Session records on a git ref (``record_store: ref``).

Hooks keep writing session files to ``.bewit/sessions/`` on disk; that is
fast and needs no git. With ``record_store: ref`` that directory is
gitignored, and the *committed* files of each session (per
``record_scope``; see :data:`bewit.scaffold.COMMITTED_SESSION_FILES`) are
published to their own ref, ``refs/bewit/records``, as plain files in a
commit history of their own:

    refs/bewit/records
      sessions/<id>/manifest.json          (audit and full scope)
      sessions/<id>/events.jsonl, meta.json  (full scope)

So the record travels with the repository (push, fetch, clone) without
ever appearing in a code commit or a merge-request diff, and a hub or CI
job collects it by fetching one ref.

- :func:`publish` writes the ref with git plumbing (a temporary index,
  ``commit-tree``, and a compare-and-swap ``update-ref``). It never
  touches the working tree or the user's index. Records fetched from a
  remote are merged in (union by path; sessions recorded here win).
- :func:`hydrate` materializes records from the ref(s) into
  ``.bewit/sessions/`` for reading. A session recorded on this machine
  (it has its own event log) is never overwritten; hydrated sessions carry
  a ``.from-ref`` marker and are never re-published from here.
- :func:`sync` fetches the remote's records, merges and publishes, then
  pushes (``bewit sync``; also run by the pre-push hook).

Everything here fails soft for the hooks: callers in hot paths catch
:class:`RefStoreError`; capture never depends on the ref.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from bewit.config import Config, bewit_dir

#: The ref that holds the committed session records.
RECORDS_REF = "refs/bewit/records"
#: Where ``bewit sync`` fetches a remote's records (never pushed).
REMOTE_REFS_PREFIX = "refs/bewit-remote/"
#: Marker in a session directory materialized from the ref.
FROM_REF_MARKER = ".from-ref"
#: Record commits are Bewit's, not a person's.
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Bewit",
    "GIT_AUTHOR_EMAIL": "bewit@localhost",
    "GIT_COMMITTER_NAME": "Bewit",
    "GIT_COMMITTER_EMAIL": "bewit@localhost",
}
_ZERO = "0" * 40


class RefStoreError(Exception):
    """A git plumbing step failed (message carries git's stderr)."""


def _git(
    root: Path,
    *args: str,
    input: str | None = None,
    env: dict | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, input=input, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, **(env or {})}, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RefStoreError(f"git {args[0]}: {exc}") from exc
    if check and proc.returncode != 0:
        raise RefStoreError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[:300]}")
    return proc


def ref_sha(root: Path, ref: str = RECORDS_REF) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def remote_refs(root: Path) -> list[str]:
    out = _git(root, "for-each-ref", "--format=%(refname)", REMOTE_REFS_PREFIX, check=False).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _is_ancestor(root: Path, maybe: str, of: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", maybe, of, check=False).returncode == 0


def _tree_entries(root: Path, commit: str) -> dict[str, str]:
    """``{path: blob sha}`` of every file in ``commit``."""
    out = _git(root, "ls-tree", "-r", "-z", commit).stdout
    entries: dict[str, str] = {}
    for item in out.split("\0"):
        if "\t" not in item:
            continue
        meta, path = item.split("\t", 1)
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            entries[path] = parts[2]
    return entries


def _sessions_dir(root: Path) -> Path:
    return bewit_dir(root) / "sessions"


def local_record_files(root: Path, config: Config) -> dict[str, Path]:
    """Committed files of the sessions recorded on this machine.

    Keyed by their path in the ref (``sessions/<id>/<name>``). Sessions
    hydrated from the ref (``.from-ref``) are someone else's and skipped.
    """
    from bewit.scaffold import COMMITTED_SESSION_FILES

    names = COMMITTED_SESSION_FILES.get(config.record_scope, COMMITTED_SESSION_FILES["audit"])
    files: dict[str, Path] = {}
    base = _sessions_dir(root)
    if not base.is_dir():
        return files
    for session_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if (session_dir / FROM_REF_MARKER).exists():
            continue
        for name in names:
            path = session_dir / name
            if path.is_file():
                files[f"sessions/{session_dir.name}/{name}"] = path
    return files


def _hash_files(root: Path, paths: list[Path]) -> list[str]:
    if not paths:
        return []
    # --stdin-paths reads lines; git strips a trailing CR, but pass forward
    # slashes and absolute paths so nothing depends on the platform.
    out = _git(root, "hash-object", "-w", "--stdin-paths",
               input="\n".join(p.resolve().as_posix() for p in paths) + "\n").stdout
    shas = out.split()
    if len(shas) != len(paths):
        raise RefStoreError("git hash-object returned an unexpected number of ids")
    return shas


def publish(root: Path, config: Config, attempts: int = 3) -> str | None:
    """Publish this machine's session records to :data:`RECORDS_REF`.

    Returns the new commit id, or ``None`` when nothing changed. Merges
    any fetched remote records (union by path; files recorded here win).
    Compare-and-swap on the ref: a concurrent publisher makes this retry
    on top of the other's commit, never overwrite it.
    """
    local = local_record_files(root, config)
    local_blobs = dict(zip(local, _hash_files(root, list(local.values()))))
    for _ in range(attempts):
        base = ref_sha(root)
        extra_parents = [
            sha for sha in (ref_sha(root, r) for r in remote_refs(root))
            if sha and sha != base and not (base and _is_ancestor(root, sha, base))
        ]
        entries = _tree_entries(root, base) if base else {}
        for parent in extra_parents:
            entries.update(_tree_entries(root, parent))  # colleagues' records
        entries.update(local_blobs)  # sessions recorded here win
        if base and not extra_parents and entries == _tree_entries(root, base):
            return None
        if not entries and not base:
            return None
        # Nothing to add on top of the remote's records: fast-forward to its
        # commit instead of recording a merge that changes nothing.
        if (
            len(extra_parents) == 1
            and (base is None or _is_ancestor(root, base, extra_parents[0]))
            and entries == _tree_entries(root, extra_parents[0])
        ):
            if _git(root, "update-ref", RECORDS_REF, extra_parents[0], base or _ZERO,
                    check=False).returncode == 0:
                return None
            continue
        with tempfile.TemporaryDirectory(prefix="bewit-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            _git(root, "read-tree", "--empty", env=env)
            # NUL-separated (-z): text-mode stdin on Windows turns "\n" into
            # "\r\n", and git would then reject every path ("path\r").
            index_info = "".join(f"100644 {sha}\t{path}\0" for path, sha in sorted(entries.items()))
            _git(root, "update-index", "-z", "--index-info", input=index_info, env=env)
            tree = _git(root, "write-tree", env=env).stdout.strip()
        parents = [p for p in [base, *extra_parents] if p]
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        commit = _git(root, *args, "-m", "bewit: session records", env=_IDENTITY).stdout.strip()
        swap = _git(root, "update-ref", RECORDS_REF, commit, base or _ZERO, check=False)
        if swap.returncode == 0:
            return commit
    raise RefStoreError("the records ref kept moving; try again")


def _read_blobs(root: Path, shas: list[str]) -> dict[str, bytes]:
    """Blob contents by sha, via one ``git cat-file --batch``."""
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=root, input=("\n".join(shas) + "\n").encode(),
        capture_output=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RefStoreError(f"git cat-file: {proc.stderr.decode(errors='replace')[:300]}")
    data, pos, blobs = proc.stdout, 0, {}
    for sha in shas:
        header_end = data.index(b"\n", pos)
        header = data[pos:header_end].split()
        size = int(header[2]) if len(header) == 3 else 0
        start = header_end + 1
        blobs[sha] = data[start:start + size]
        pos = start + size + 1
    return blobs


def _hydrate_key_path(root: Path) -> Path:
    return bewit_dir(root) / "locks" / "refstore-hydrated.json"


def hydrate(root: Path, force: bool = False) -> int:
    """Materialize records from the ref(s) into ``.bewit/sessions/``.

    Returns the number of files written. Sessions recorded here (an event
    log without a ``.from-ref`` marker) are never touched. Skipped cheaply
    when the refs have not moved since the last hydration.
    """
    refs = [RECORDS_REF, *remote_refs(root)]
    shas = {ref: ref_sha(root, ref) for ref in refs}
    shas = {ref: sha for ref, sha in shas.items() if sha}
    if not shas:
        return 0
    key_path = _hydrate_key_path(root)
    key = json.dumps(shas, sort_keys=True)
    try:
        if not force and key_path.read_text(encoding="utf-8") == key:
            return 0
    except OSError:
        pass
    # Local ref first; a remote ref not yet merged into it is newer news.
    local_sha = shas.get(RECORDS_REF)
    ordered = [sha for ref, sha in shas.items() if ref == RECORDS_REF]
    ordered += [sha for ref, sha in shas.items()
                if ref != RECORDS_REF and not (local_sha and _is_ancestor(root, sha, local_sha))]
    entries: dict[str, str] = {}
    for sha in ordered:
        entries.update(_tree_entries(root, sha))
    wanted: dict[Path, str] = {}
    base = _sessions_dir(root)
    for path, blob in entries.items():
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "sessions" or parts[1] in ("", ".", ".."):
            continue
        session_dir = base / parts[1]
        recorded_here = (session_dir / "events.jsonl").exists() and not (
            session_dir / FROM_REF_MARKER).exists()
        if recorded_here:
            continue
        wanted[session_dir / parts[2]] = blob
    blobs = _read_blobs(root, sorted(set(wanted.values())))
    written = 0
    for target, blob in wanted.items():
        content = blobs.get(blob, b"")
        try:
            if target.is_file() and target.read_bytes() == content:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            (target.parent / FROM_REF_MARKER).write_text("materialized from refs/bewit/records\n",
                                                         encoding="utf-8")
            target.write_bytes(content)
            written += 1
        except OSError:
            continue
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return written


def hydrate_quietly(root: Path) -> None:
    """:func:`hydrate` for read paths: a git problem never breaks a reader."""
    try:
        hydrate(root)
    except (RefStoreError, OSError, ValueError):
        pass


def publish_quietly(root: Path, config: Config) -> str | None:
    """:func:`publish` for hooks: failures are swallowed (capture is on disk)."""
    if config.record_store != "ref":
        return None
    try:
        return publish(root, config)
    except (RefStoreError, OSError, ValueError):
        return None


def default_remote(root: Path) -> str | None:
    """The remote ``git push`` would use from here, as git itself picks it.

    In order: the current branch's ``pushRemote``, ``remote.pushDefault``,
    the branch's upstream remote, then ``origin``, then the only remote.
    None when there is no remote or several without a preference.
    """
    def config(key: str) -> str:
        return _git(root, "config", "--get", key, check=False).stdout.strip()

    remotes = [r for r in _git(root, "remote", check=False).stdout.split() if r]
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    candidates = [
        config(f"branch.{branch}.pushRemote") if branch else "",
        config("remote.pushDefault"),
        config(f"branch.{branch}.remote") if branch else "",
    ]
    for name in candidates:
        if name and name in remotes:  # "." (a local upstream) is not a remote
            return name
    if "origin" in remotes:
        return "origin"
    return remotes[0] if len(remotes) == 1 else None


def sync(root: Path, config: Config, remote: str | None = None, push: bool = True) -> dict:
    """Fetch the remote's records, merge and publish, hydrate, and push.

    ``remote`` defaults to :func:`default_remote`. Returns ``{"remote",
    "fetched", "published", "hydrated", "pushed", "notes"}``. Raises
    :class:`RefStoreError` when the remote does not exist or none can be
    chosen.
    """
    if remote is None:
        remote = default_remote(root)
        if remote is None:
            names = _git(root, "remote", check=False).stdout.split()
            raise RefStoreError(
                "no git remote to sync with" if not names else
                f"several remotes ({', '.join(names)}) and no upstream: pass --remote <name>"
            )
    if _git(root, "remote", "get-url", remote, check=False).returncode != 0:
        raise RefStoreError(f"no git remote named '{remote}'")
    tracking = f"{REMOTE_REFS_PREFIX}{remote}/records"
    result = {"remote": remote, "fetched": False, "published": None, "hydrated": 0,
              "pushed": False, "notes": []}
    for attempt in range(2):
        fetch = _git(root, "fetch", "--quiet", remote, f"+{RECORDS_REF}:{tracking}", check=False)
        result["fetched"] = fetch.returncode == 0
        if not result["fetched"] and attempt == 0:
            result["notes"].append("the remote has no records yet")
        result["published"] = publish(root, config)
        result["hydrated"] = hydrate(root, force=True)
        if not push or ref_sha(root) is None:
            return result
        pushed = _git(root, "push", "--quiet", remote, f"{RECORDS_REF}:{RECORDS_REF}",
                      env={"BEWIT_IN_PRE_PUSH": "1"}, check=False)
        if pushed.returncode == 0:
            result["pushed"] = True
            return result
        result["notes"].append(pushed.stderr.strip().splitlines()[-1] if pushed.stderr.strip()
                               else "push rejected")
    return result
