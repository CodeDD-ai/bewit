"""Session storage: append-only JSONL event logs plus derived artifacts.

Layout under the repository root::

    .archrev/sessions/<session-id>/
        meta.json      written once at session start (id, started_at, head_sha)
        events.jsonl   append-only event stream (audit truth)
        plan.md        snapshot of the registered plan, if any
        manifest.json  finalized summary written on session stop
        report.md      human-readable report regenerated at finalization

Appends are a single ``write()`` of one line, serialized by a per-session
lock so parallel tool calls cannot fork the hash chain. Distinct sessions
lock distinct files. Corrupt lines are skipped on read rather than failing
the whole session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from archrev.config import archrev_dir

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

#: Chain anchor for the first event of a session log.
GENESIS = "genesis"

#: Reserved session id that accumulates human changes with no agent session
#: attribution (recorded at commit time by the git hook).
HUMAN_SESSION_ID = "human"


def event_hash(event: dict) -> str:
    """Deterministic hash over an event's content including ``prev``.

    The ``hash`` field itself is excluded; key order is canonicalized so
    the value survives JSON round-trips.
    """
    content = {k: v for k, v in event.items() if k != "hash"}
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

EVENTS_FILENAME = "events.jsonl"
META_FILENAME = "meta.json"
MANIFEST_FILENAME = "manifest.json"
PLAN_FILENAME = "plan.md"
REPORT_FILENAME = "report.md"


#: Parsed event logs, keyed by path. Invalidated when size or mtime changes.
#: One hook process reads the same log many times (finalize, drift, replay);
#: the viewer polls it. The cached list is shared and must not be mutated.
_EVENT_CACHE: dict[str, tuple[int, int, list[dict]]] = {}
_EVENT_CACHE_MAX = 64

#: In-process companions to the cross-process file lock. Windows locks are
#: per-process, so threads in one hook-host process also need this.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def utc_now_iso() -> str:
    """Current UTC time in second-resolution ISO-8601 (stable, sortable)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_session_id(raw: str | None) -> str:
    """Make a session id filesystem-safe without losing uniqueness.

    IDs come from Cursor's ``conversation_id`` and are normally UUIDs; if an
    unexpected value arrives, unsafe characters are replaced and a short
    digest is appended so distinct raw ids cannot collide after cleaning.
    """
    if not raw:
        return "unknown"
    cleaned = _SAFE_ID.sub("-", raw)[:64]
    if cleaned == raw:
        return cleaned
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


class Session:
    """One agent session's on-disk record."""

    def __init__(self, sessions_dir: Path, session_id: str) -> None:
        self.id = sanitize_session_id(session_id)
        self.dir = sessions_dir / self.id

    # -- lifecycle -----------------------------------------------------

    def exists(self) -> bool:
        return (self.dir / EVENTS_FILENAME).exists() or (
            self.dir / META_FILENAME
        ).exists()

    def ensure_meta(
        self,
        head_sha: str | None,
        branch: str | None = None,
        runtime: str | None = None,
    ) -> dict:
        """Write ``meta.json`` exactly once; return the (existing) meta.

        ``runtime`` is optional and additive (``cursor`` / ``claude`` /
        ``codex``). Older records without it remain valid.
        """
        existing = self.meta()
        if existing is not None:
            return existing
        meta = {
            "id": self.id,
            "started_at": utc_now_iso(),
            "head_sha": head_sha,
            "branch": branch,
        }
        if runtime:
            meta["runtime"] = runtime
        _write_json(self.dir / META_FILENAME, meta)
        return meta

    # -- events ---------------------------------------------------------

    def _lock_path(self) -> Path:
        """Per-session lock under ``.archrev/locks/`` (gitignored runtime noise)."""
        return self.dir.parent.parent / "locks" / f"{self.id}.lock"

    @contextmanager
    def _append_lock(self):
        """Serialize appends. Lock failure yields anyway: losing an event is worse."""
        thread = _thread_lock(str(self.dir.resolve()))
        with thread:
            fh = None
            locked = False
            try:
                path = self._lock_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(path, "a+b")
                if os.name == "nt":
                    import msvcrt

                    if fh.seek(0, 2) == 0:
                        fh.write(b"\0")
                        fh.flush()
                    fh.seek(0)
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                break
                            time.sleep(0.01)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    locked = True
            except OSError:
                fh = None
            try:
                yield
            finally:
                if fh is not None:
                    try:
                        if locked and os.name == "nt":
                            import msvcrt

                            fh.seek(0)
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        elif locked:
                            import fcntl

                            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                    fh.close()

    def _last_hash(self) -> str:
        """Hash of the most recent event, or GENESIS for an empty log.

        The previous implementation read only the last 16 KiB. A single
        event larger than that (a full prompt, a long plan) made the next
        append chain from GENESIS and ``archrev verify`` reported a break.
        """
        path = self.dir / EVENTS_FILENAME
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                if size == 0:
                    return GENESIS
                pos = size
                buf = b""
                while pos > 0:
                    step = min(65536, pos)
                    pos -= step
                    fh.seek(pos)
                    buf = fh.read(step) + buf
                    stripped = buf.rstrip(b"\r\n")
                    parts = stripped.split(b"\n")
                    # The first slice may be a partial line when the read
                    # started mid-line. Every later slice is complete.
                    start = 0 if pos == 0 else 1
                    if pos > 0 and len(parts) <= 1:
                        continue
                    for raw in reversed(parts[start:]):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if isinstance(event, dict):
                            return str(event.get("hash") or GENESIS)
                    if len(buf) > 64 * 1024 * 1024:
                        break
        except OSError:
            return GENESIS
        return GENESIS

    def append_event(self, event_type: str, **data: object) -> dict:
        """Append one hash-chained event line; returns the stored event.

        Each event carries ``prev`` (the previous event's hash) and ``hash``
        (over the event content plus ``prev``), making the log append-only
        in a verifiable sense: any later modification or deletion breaks the
        chain, detectable via ``archrev verify``. Tamper-*evident*, not
        tamper-*proof* — an attacker can rewrite the whole chain, but cannot
        alter history quietly while commits/exports referencing earlier
        hashes exist.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._append_lock():
            prev = self._last_hash()
            event: dict = {
                "ts": utc_now_iso(),
                "type": event_type,
                **data,
                "prev": prev,
            }
            event["hash"] = event_hash(event)
            line = json.dumps(event, ensure_ascii=False)
            with open(
                self.dir / EVENTS_FILENAME, "a", encoding="utf-8", newline="\n"
            ) as fh:
                fh.write(line + "\n")
        return event

    def verify_chain(self) -> dict:
        """Validate the event hash chain.

        Returns ``{"ok", "checked", "legacy", "break_at", "reason",
        "duplicates", "forks"}``. Events written before hashing existed
        count as ``legacy`` and reset the chain start; they are reported,
        not failed.

        Breaks carry a ``reason``:

        - ``modified``: an event's hash does not match its content, or its
          ``prev`` names no earlier event (edited or deleted history).
        - ``fork``: a valid event whose ``prev`` names an *earlier* event, so
          two events chain to one predecessor. Usually two concurrent
          appends; an inserted event looks the same, so it still fails.

        ``duplicates`` (an event byte-identical to its predecessor: one
        hook delivered twice) alter nothing and do not break the chain.
        """
        checked = legacy = duplicates = forks = 0
        break_at: int | None = None
        reason: str | None = None
        prev: str | None = None
        seen: set[str] = set()
        for index, event in enumerate(self.events(), start=1):
            if "hash" not in event:
                legacy += 1
                prev = None  # chain restarts after legacy prefix
                continue
            own = str(event.get("hash"))
            if own != event_hash(event):
                break_at, reason = index, "modified"
                break
            if prev is not None and event.get("prev") != prev:
                if own == prev:
                    duplicates += 1
                    continue
                forks += 1 if event.get("prev") in seen else 0
                break_at = index
                reason = "fork" if event.get("prev") in seen else "modified"
                break
            prev = own
            seen.add(own)
            checked += 1
        return {
            "ok": break_at is None,
            "checked": checked,
            "legacy": legacy,
            "break_at": break_at,
            "reason": reason,
            "duplicates": duplicates,
            "forks": forks,
        }

    def events(self) -> list[dict]:
        """All events in append order; corrupt lines are skipped.

        The parsed list is cached until the file's size or mtime changes.
        Callers must not mutate the returned list or its dicts.
        """
        path = self.dir / EVENTS_FILENAME
        try:
            st = path.stat()
        except OSError:
            return []
        key = str(path.resolve())
        hit = _EVENT_CACHE.get(key)
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        try:
            st_after = path.stat()
        except OSError:
            return out
        if (
            st_after.st_mtime_ns == st.st_mtime_ns
            and st_after.st_size == st.st_size
        ):
            if len(_EVENT_CACHE) >= _EVENT_CACHE_MAX:
                _EVENT_CACHE.pop(next(iter(_EVENT_CACHE)))
            _EVENT_CACHE[key] = (st.st_mtime_ns, st.st_size, out)
        return out

    def has_event(self, event_type: str) -> bool:
        return any(e.get("type") == event_type for e in self.events())

    def last_event(self, event_type: str) -> dict | None:
        """Most recent event of ``event_type``, or None."""
        for event in reversed(self.events()):
            if event.get("type") == event_type:
                return event
        return None

    def last_activity(self) -> float:
        """Modification time of the event log (0.0 when absent)."""
        try:
            return (self.dir / EVENTS_FILENAME).stat().st_mtime
        except OSError:
            try:
                return (self.dir / META_FILENAME).stat().st_mtime
            except OSError:
                return 0.0

    # -- derived artifacts ------------------------------------------------

    def meta(self) -> dict | None:
        return _read_json(self.dir / META_FILENAME)

    def manifest(self) -> dict | None:
        return _read_json(self.dir / MANIFEST_FILENAME)

    def write_manifest(self, manifest: dict) -> None:
        _write_json(self.dir / MANIFEST_FILENAME, manifest)

    def plan_text(self) -> str | None:
        try:
            return (self.dir / PLAN_FILENAME).read_text(encoding="utf-8")
        except OSError:
            return None

    def write_plan(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / PLAN_FILENAME).write_text(text, encoding="utf-8")

    def write_report(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / REPORT_FILENAME).write_text(text, encoding="utf-8")

    # -- convenience -----------------------------------------------------

    def touched_files(self) -> list[str]:
        """Paths recorded by edit events, in first-touch order, deduplicated."""
        seen: dict[str, None] = {}
        for event in self.events():
            if event.get("type") == "edit":
                path = event.get("path")
                if isinstance(path, str) and path:
                    seen.setdefault(path, None)
        return list(seen)

    def prompts(self) -> list[dict]:
        return [e for e in self.events() if e.get("type") == "prompt"]

    def event_log_present(self) -> bool:
        return (self.dir / EVENTS_FILENAME).is_file()


class SessionStore:
    """Access point for all sessions in one repository."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sessions_dir = archrev_dir(root) / "sessions"

    def session(self, session_id: str) -> Session:
        return Session(self.sessions_dir, session_id)

    def list_sessions(self) -> list[Session]:
        """All sessions, most recently active first."""
        if not self.sessions_dir.is_dir():
            return []
        sessions = [
            Session(self.sessions_dir, entry.name)
            for entry in self.sessions_dir.iterdir()
            if entry.is_dir()
        ]
        return sorted(sessions, key=lambda s: s.last_activity(), reverse=True)

    def current_session(self) -> Session | None:
        """The most recently active session, if any.

        CLI commands run *by the agent* (``plan register``, ``check plan``)
        cannot know their own conversation id — only hooks receive it — so
        they bind to the most recently active session. With one agent per
        repository this is exact; parallel agents can override via
        ``--session``.
        """
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    def resolve(self, ref: str) -> Session | None:
        """Resolve ``ref`` to a session: exact id, unique prefix, or 'latest'."""
        if ref == "latest":
            return self.current_session()
        exact = self.session(ref)
        if exact.exists():
            return exact
        matches = [s for s in self.list_sessions() if s.id.startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    def prune_event_logs(self, keep_days: int) -> list[str]:
        """Delete local event logs older than ``keep_days``.

        Only finalized sessions are eligible, so a live session is never
        truncated. Manifests, plans, and reports stay — those are the
        committed audit tier. Returns the session ids whose logs were removed.
        """
        cutoff = time.time() - keep_days * 86400
        removed: list[str] = []
        for session in self.list_sessions():
            if session.id == HUMAN_SESSION_ID:
                continue
            log = session.dir / EVENTS_FILENAME
            if not log.is_file() or session.manifest() is None:
                continue
            if session.last_activity() >= cutoff:
                continue
            log.unlink()
            removed.append(session.id)
        return removed
