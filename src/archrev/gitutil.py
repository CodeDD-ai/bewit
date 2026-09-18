"""Thin, defensive git wrapper.

Every call shells out to ``git`` with list arguments (never ``shell=True``)
and degrades gracefully: when git is missing, the directory is not a
repository, or a command fails, callers receive ``None`` / empty results
instead of exceptions. ArchRev must keep recording sessions even in a
repository with a broken git state.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Commit-message trailer key linking commits to sessions.
TRAILER_KEY = "ArchRev-Session"


@dataclass(frozen=True)
class NumstatEntry:
    """One ``git diff --numstat`` line. ``added``/``removed`` are None for binary files."""

    added: int | None
    removed: int | None
    path: str


class Git:
    """Git operations bound to one repository root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _run(self, *args: str) -> str | None:
        """Run a git command; return stdout or None on any failure."""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def is_repo(self) -> bool:
        return self._run("rev-parse", "--is-inside-work-tree") is not None

    def head_sha(self) -> str | None:
        out = self._run("rev-parse", "HEAD")
        return out.strip() if out else None

    # -- diffs -----------------------------------------------------------

    @staticmethod
    def _parse_numstat(output: str) -> list[NumstatEntry]:
        entries: list[NumstatEntry] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            raw_added, raw_removed, path = parts[0], parts[1], parts[2]
            # Rename lines look like "old\tnew" (with -z) or "a/{x => y}/b";
            # keep the last component which git prints as the current path.
            if len(parts) == 4:
                path = parts[3]
            added = int(raw_added) if raw_added.isdigit() else None
            removed = int(raw_removed) if raw_removed.isdigit() else None
            entries.append(
                NumstatEntry(added=added, removed=removed, path=path.strip())
            )
        return entries

    def numstat(self, base: str | None) -> list[NumstatEntry]:
        """Per-file line changes between ``base`` and the working tree.

        With ``base=None`` falls back to ``HEAD``; in a repository with no
        commits yet this yields no entries and callers degrade to counting
        untracked files directly.
        """
        if base:
            out = self._run("diff", "--numstat", "--find-renames", base)
        else:
            out = self._run("diff", "--numstat", "--find-renames", "HEAD")
        return self._parse_numstat(out) if out else []

    def untracked_files(self) -> list[str]:
        out = self._run("ls-files", "--others", "--exclude-standard")
        return [ln.strip() for ln in out.splitlines() if ln.strip()] if out else []

    def staged_files(self) -> list[str]:
        out = self._run("diff", "--cached", "--name-only")
        return [ln.strip() for ln in out.splitlines() if ln.strip()] if out else []

    # -- commits ----------------------------------------------------------

    def commits_with_session(self, session_id: str, limit: int = 50) -> list[dict]:
        """Commits whose message carries this session's trailer."""
        out = self._run(
            "log",
            f"--grep={TRAILER_KEY}: {session_id}",
            f"--max-count={limit}",
            "--format=%H%x09%ci%x09%s",
        )
        commits: list[dict] = []
        if not out:
            return commits
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                commits.append(
                    {"sha": parts[0], "date": parts[1], "subject": parts[2]}
                )
        return commits

    def commit_message(self, sha: str) -> str | None:
        return self._run("log", "-1", "--format=%B", sha)

    def commit_summary(self, sha: str) -> dict | None:
        out = self._run("log", "-1", "--format=%H%x09%ci%x09%s", sha)
        if not out:
            return None
        parts = out.strip().split("\t", 2)
        if len(parts) != 3:
            return None
        return {"sha": parts[0], "date": parts[1], "subject": parts[2]}
