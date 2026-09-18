"""Commit-message trailers linking commits to the sessions that produced them.

Called by the ``prepare-commit-msg`` git hook. The linkage is computed at
commit time by intersecting the *staged files* with the files each recent
session touched — deliberately not "whichever session is active", because
commits routinely happen after the session ended.

The git hook wrapper fails open (``|| exit 0``), and this module never
raises: a provenance tool that blocks commits would be removed within a day.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from archrev.gitutil import TRAILER_KEY, Git
from archrev.storage import SessionStore

#: Only sessions active within this window are candidates for linking.
_MAX_SESSION_AGE_DAYS = 14

#: Cap trailers per commit; a commit touching files from many sessions is
#: usually a bulk operation where per-session attribution is meaningless.
_MAX_TRAILERS = 3

_TRAILER_RE = re.compile(rf"^{TRAILER_KEY}:\s*(\S+)", re.MULTILINE)


def sessions_for_staged(root: Path) -> list[str]:
    """Session ids whose touched files intersect the staged files."""
    staged = {p.lower() for p in Git(root).staged_files()}
    if not staged:
        return []
    cutoff = time.time() - _MAX_SESSION_AGE_DAYS * 86400
    matches: list[tuple[float, str]] = []
    for session in SessionStore(root).list_sessions():
        activity = session.last_activity()
        if activity < cutoff:
            continue  # list is sorted desc; everything after is older
        touched = {p.lower() for p in session.touched_files()}
        if touched & staged:
            matches.append((activity, session.id))
    matches.sort(reverse=True)
    return [sid for _, sid in matches[:_MAX_TRAILERS]]


def add_trailers(msg_file: Path, root: Path) -> bool:
    """Append missing session trailers to the commit message file.

    Returns True when the file was modified. Existing trailers (e.g. from
    ``--amend``) are preserved and never duplicated.
    """
    try:
        message = msg_file.read_text(encoding="utf-8")
    except OSError:
        return False

    existing = set(_TRAILER_RE.findall(message))
    to_add = [sid for sid in sessions_for_staged(root) if sid not in existing]
    if not to_add:
        return False

    # Git trailers belong in the last paragraph; appending after a blank
    # line keeps them recognized by `git interpret-trailers` consumers.
    body = message.rstrip("\n")
    trailer_block = "\n".join(f"{TRAILER_KEY}: {sid}" for sid in to_add)
    separator = "\n" if _TRAILER_RE.search(body) else "\n\n"
    try:
        msg_file.write_text(body + separator + trailer_block + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
