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
from archrev.storage import HUMAN_SESSION_ID, SessionStore

#: Only sessions active within this window are candidates for linking.
_MAX_SESSION_AGE_DAYS = 14

#: Cap trailers per commit; a commit touching files from many sessions is
#: usually a bulk operation where per-session attribution is meaningless.
_MAX_TRAILERS = 3

_TRAILER_RE = re.compile(rf"^{TRAILER_KEY}:\s*(\S+)", re.MULTILINE)


def attribute_staged(root: Path) -> tuple[list[str], list[str]]:
    """Attribute staged files to sessions.

    Returns ``(session_ids, unattributed_files)``: the recent sessions whose
    touched files intersect the staged set, and the staged files no session
    touched — i.e. human changes (or changes from expired/foreign sessions).
    """
    staged_original = Git(root).staged_files()
    staged = {p.lower(): p for p in staged_original}
    if not staged:
        return [], []
    cutoff = time.time() - _MAX_SESSION_AGE_DAYS * 86400
    matches: list[tuple[float, str]] = []
    attributed: set[str] = set()
    for session in SessionStore(root).list_sessions():
        if session.id == HUMAN_SESSION_ID:
            continue
        activity = session.last_activity()
        if activity < cutoff:
            continue  # list is sorted desc; everything after is older
        touched = {p.lower() for p in session.touched_files()}
        overlap = touched & set(staged)
        if overlap:
            matches.append((activity, session.id))
            attributed |= overlap
    matches.sort(reverse=True)
    unattributed = [staged[p] for p in sorted(set(staged) - attributed)]
    return [sid for _, sid in matches[:_MAX_TRAILERS]], unattributed


def sessions_for_staged(root: Path) -> list[str]:
    """Session ids whose touched files intersect the staged files."""
    return attribute_staged(root)[0]


def record_human_changes(root: Path, unattributed: list[str]) -> None:
    """Record staged files with no agent-session attribution.

    Human edits never pass through Cursor hooks, so commit time is the one
    reliable checkpoint where they can enter the audit log. They accumulate
    in the reserved ``human`` session, keeping the record complete: every
    committed change is attributable to an agent session or explicitly
    marked human. Only active when agent sessions exist, so purely manual
    repositories don't generate noise.
    """
    # ArchRev's own session bookkeeping is written by hooks, not by people;
    # counting it as human changes would be pure noise.
    unattributed = [
        p for p in unattributed
        if not p.lower().startswith(".archrev/sessions/")
    ]
    if not unattributed:
        return
    store = SessionStore(root)
    others = [s for s in store.list_sessions() if s.id != HUMAN_SESSION_ID]
    if not others:
        return
    session = store.session(HUMAN_SESSION_ID)
    session.ensure_meta(Git(root).head_sha())
    session.append_event(
        "human_changes",
        files=unattributed,
        note="staged at commit time without agent session attribution",
    )


def add_trailers(msg_file: Path, root: Path) -> bool:
    """Append missing session trailers to the commit message file.

    Returns True when the file was modified. Existing trailers (e.g. from
    ``--amend``) are preserved and never duplicated.
    """
    try:
        message = msg_file.read_text(encoding="utf-8")
    except OSError:
        return False

    session_ids, unattributed = attribute_staged(root)
    record_human_changes(root, unattributed)

    existing = set(_TRAILER_RE.findall(message))
    to_add = [sid for sid in session_ids if sid not in existing]
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
