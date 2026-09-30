"""Per-session working-tree checkpoints: which changes belong to *this* session.

Several agent sessions (Cursor windows, Claude/Codex terminals) and the
human routinely share one checkout, so the git diff since a session
started mixes this session's work with everybody else's. Edit events
attribute tool edits exactly. Everything else in the diff (shell writes,
MCP side effects, hand edits, other windows) used to be charged to every
open session, so a change made in one window was raised as a gate bypass
in all of them.

Checkpoints narrow that down. A ``worktree`` event records content
fingerprints of the dirty files at:

- ``start``        session creation (baseline: changes that pre-date it),
- ``exec_start``   before each of this session's shell / MCP commands,
- ``exec_end``     after it (``afterShellExecution`` / ``PostToolUse``),
- ``turn_start`` / ``turn_end``  prompt / stop, closing any window whose
  after-hook never arrived.

Only content that changed while at least one of this session's commands
was running is attributed to the session. Changes between its commands,
while it is idle, or before it started are *background*: shown in the
review, never raised as this session's bypass. Without after-hooks a
window stays open until the next checkpoint, so attribution errs toward
more review for the session, never toward silence.

Residual ambiguity: a hand edit saved while one of this session's
commands is running cannot be told apart from the command's own write and
is attributed to the session.

Event shape (additive; sessions without checkpoints keep the previous
since-start attribution)::

    {"type": "worktree", "phase": "exec_start", "label": "shell: pytest",
     "changed": {"db/x.sql": "3f2a9c...", "gone.py": "-"}}

``changed`` is a delta against the session's previous checkpoint state, so
a checkpoint where nothing moved costs a few bytes. The ``start`` event
holds the full baseline.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from bewit.gitutil import Git
from bewit.storage import Session, utc_now_iso

EVENT_TYPE = "worktree"

PHASE_START = "start"
PHASE_EXEC_START = "exec_start"
PHASE_EXEC_END = "exec_end"
PHASE_TURN_START = "turn_start"
PHASE_TURN_END = "turn_end"

#: Fingerprint of a path that no longer exists on disk.
DELETED = "-"

#: Files above this size are fingerprinted by size + mtime instead of
#: content, bounding hook latency on large binaries.
_HASH_LIMIT_BYTES = 32 * 1024 * 1024

#: Bewit's own session records change on every hook; never attribute them.
_BOOKKEEPING_PREFIX = ".bewit/sessions/"


def is_bookkeeping(path: str) -> bool:
    return path.replace("\\", "/").lower().startswith(_BOOKKEEPING_PREFIX)


def is_runtime_noise(path: str) -> bool:
    """Bookkeeping and Bewit runtime files are not session changes.

    Do not use ``str.lstrip("./")`` here: it eats the leading dot of
    ``.bewit/...`` and the bookkeeping prefix stops matching.
    """
    norm = path.replace("\\", "/").lower()
    while norm.startswith("./"):
        norm = norm[2:]
    return (
        is_bookkeeping(norm)
        or norm == ".bewit/fingerprint-cache.json"
        or norm.startswith(".bewit/locks/")
    )


_FP_CACHE_NAME = "fingerprint-cache.json"
_FP_CACHE_MAX = 4096


@dataclass
class _FpCache:
    entries: dict[str, str] = field(default_factory=dict)
    dirty: bool = False


def _fp_cache_path(root: Path) -> Path:
    return root / ".bewit" / _FP_CACHE_NAME


def _load_fp_cache(root: Path) -> _FpCache:
    try:
        raw = json.loads(_fp_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _FpCache()
    if not isinstance(raw, dict):
        return _FpCache()
    entries = {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    return _FpCache(entries=entries)


def _save_fp_cache(root: Path, cache: _FpCache) -> None:
    if not cache.dirty:
        return
    if len(cache.entries) > _FP_CACHE_MAX:
        overflow = len(cache.entries) - _FP_CACHE_MAX // 2
        for old in list(cache.entries)[:overflow]:
            del cache.entries[old]
    path = _fp_cache_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cache.entries, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        return


def _fingerprint(
    path: Path, cache: _FpCache | None = None, cache_key: str | None = None
) -> str | None:
    """Content fingerprint; ``None`` for non-regular or unreadable paths."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return DELETED
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None  # submodule checkouts and other directories
    if info.st_size > _HASH_LIMIT_BYTES:
        return f"size:{info.st_size}:{info.st_mtime_ns}"
    token = None
    if cache is not None and cache_key:
        token = f"{cache_key}\0{info.st_size}\0{info.st_mtime_ns}"
        hit = cache.entries.get(token)
        if hit:
            return hit
    digest = hashlib.blake2b(digest_size=8)
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    if cache is not None and token:
        cache.entries[token] = value
        cache.dirty = True
    return value


def fingerprint_file(root: Path, rel: str) -> str | None:
    """Content fingerprint of one repo file, comparable with checkpoints."""
    if not rel or is_runtime_noise(rel):
        return None
    return _fingerprint(root / rel)


def snapshot(root: Path, carried: Iterable[str] = ()) -> dict[str, str] | None:
    """Fingerprints of every dirty path plus every ``carried`` path.

    Carrying the paths of earlier checkpoints keeps a file observable after
    it becomes clean again (reverted or committed), so its later changes
    are still seen. ``None`` when git cannot list the working tree.
    """
    dirty = Git(root).dirty_paths()
    if dirty is None:
        return None
    cache = _load_fp_cache(root)
    result: dict[str, str] = {}
    for rel in dict.fromkeys([*dirty, *carried]):
        if not rel or is_runtime_noise(rel):
            continue
        fingerprint = _fingerprint(root / rel, cache, rel.replace("\\", "/"))
        if fingerprint is not None:
            result[rel] = fingerprint
    _save_fp_cache(root, cache)
    return result


@dataclass(frozen=True)
class Window:
    """One interval during which this session's command(s) were running.

    ``label`` names every command open in the interval; ``latest`` is the
    most recently started one, the likeliest author of a change. Runtimes
    without after-hooks leave commands open until the turn ends, so
    ``label`` can list the whole turn while ``latest`` stays specific.
    """

    label: str
    start: str | None
    end: str | None
    latest: str = ""
    #: Every open command here is one whose end was never recorded although
    #: a later-started command's end was: its after-hook was most likely
    #: lost (e.g. wiring from before PostToolUse covered shells), so the
    #: change may not be this session's at all.
    uncertain: bool = False

    def contains(self, ts: str) -> bool:
        # Event timestamps are second-resolution ISO-8601 UTC strings, so
        # lexicographic order is chronological order.
        return (self.start is None or ts >= self.start) and (
            self.end is None or ts <= self.end
        )


@dataclass
class Replay:
    """A session's checkpoints folded into attribution facts."""

    checkpoints: int = 0
    state: dict[str, str] = field(default_factory=dict)
    open_labels: list[str] = field(default_factory=list)
    last_ts: str | None = None
    #: path -> windows of this session during which its content changed
    windows: dict[str, list[Window]] = field(default_factory=dict)
    #: paths whose content changed while no command of this session ran
    background: set[str] = field(default_factory=set)
    #: path -> every observed content change: (log index, ts, fingerprint,
    #: window or None). The index orders changes against other events
    #: exactly; timestamps only have one-second resolution.
    changes: dict[str, list[tuple[int, str | None, str, Window | None]]] = field(
        default_factory=dict
    )
    #: Parallel to ``open_labels``: True for an open command whose end was
    #: likely lost (a later-started command already ended). Per instance,
    #: not per label, so identical labels (every command is just ``shell``
    #: without full capture) do not taint later commands.
    open_suspect: list[bool] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.checkpoints > 0

    @property
    def is_open(self) -> bool:
        return bool(self.open_labels)

    def delta(self, current: dict[str, str]) -> dict[str, str]:
        return {p: fp for p, fp in current.items() if self.state.get(p) != fp}

    def apply(
        self,
        changed: dict,
        ts: str | None,
        *,
        baseline: bool = False,
        index: int = 1 << 62,
    ) -> None:
        for path, fingerprint in changed.items():
            if not isinstance(path, str) or not isinstance(fingerprint, str):
                continue
            window: Window | None = None
            if self.open_labels and not baseline:
                window = Window(
                    "; ".join(self.open_labels),
                    self.last_ts,
                    ts,
                    latest=self.open_labels[-1],
                    uncertain=all(self.open_suspect),
                )
                self.windows.setdefault(path, []).append(window)
            else:
                self.background.add(path)
            if not baseline:
                self.changes.setdefault(path, []).append((index, ts, fingerprint, window))
            self.state[path] = fingerprint


def replay(events: Iterable[dict]) -> Replay:
    """Fold a session's ``worktree`` events in log order."""
    result = Replay()
    for index, event in enumerate(events):
        if event.get("type") != EVENT_TYPE:
            continue
        changed = event.get("changed")
        ts = event.get("ts") if isinstance(event.get("ts"), str) else None
        result.apply(
            changed if isinstance(changed, dict) else {},
            ts,
            baseline=not result.enabled,
            index=index,
        )
        phase = event.get("phase")
        if phase == PHASE_EXEC_START:
            result.open_labels.append(str(event.get("label") or "command"))
            result.open_suspect.append(False)
        elif phase == PHASE_EXEC_END:
            # Parallel commands may finish in any order. Close the command
            # this end names when known; otherwise the oldest. The count of
            # open commands is what decides attribution either way.
            label = event.get("label")
            if label in result.open_labels:
                # The most recent identical command is the one ending; an
                # older identical one whose end was lost stays open.
                pos = len(result.open_labels) - 1 - result.open_labels[::-1].index(label)
                # Commands started before this one and still open: their
                # after-hooks were most likely lost (or they run long).
                for earlier in range(pos):
                    result.open_suspect[earlier] = True
                result.open_labels.pop(pos)
                result.open_suspect.pop(pos)
            elif result.open_labels:
                result.open_labels.pop(0)
                result.open_suspect.pop(0)
        else:
            result.open_labels.clear()
            result.open_suspect.clear()
        result.last_ts = ts
        result.checkpoints += 1
    return result


def fold_live_tail(root: Path, result: Replay) -> None:
    """Attribute changes made since the last checkpoint (live views).

    An open window (a command still running, or an after-hook that never
    fired) claims them for the session; otherwise they are background.
    """
    current = snapshot(root, result.state)
    if current is not None:
        result.apply(result.delta(current), utc_now_iso())


def record_checkpoint(
    session: Session, root: Path, phase: str, label: str | None = None
) -> dict | None:
    """Append one checkpoint when it carries information; returns the event.

    - ``start`` only establishes a baseline for a session that has none.
    - Sessions recorded before checkpoints existed never get any, keeping
      their attribution consistent (since-start) for their whole lifetime.
    - Closing phases are skipped when no window is open: background
      changes need no marker. If the tree cannot be read, a closing
      checkpoint is skipped too, leaving the window open (conservative);
      an opening one is still recorded so the window starts on time.
    """
    state = replay(session.events())
    if phase == PHASE_START:
        if state.enabled:
            return None
    elif not state.enabled:
        return None
    elif phase != PHASE_EXEC_START and not state.is_open:
        return None

    current = snapshot(root, state.state)
    data: dict[str, object] = {"phase": phase}
    if current is None:
        if phase != PHASE_EXEC_START:
            return None
        data["changed"] = {}
        data["partial"] = True
    else:
        data["changed"] = state.delta(current)
    if label:
        data["label"] = label
    return session.append_event(EVENT_TYPE, **data)
