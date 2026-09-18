"""Session storage: append-only JSONL event logs plus derived artifacts.

Layout under the repository root::

    .archrev/sessions/<session-id>/
        meta.json      written once at session start (id, started_at, head_sha)
        events.jsonl   append-only event stream (audit truth)
        plan.md        snapshot of the registered plan, if any
        manifest.json  finalized summary written on session stop
        report.md      human-readable report regenerated at finalization

Appends are single ``write()`` calls on files opened in append mode, which
is atomic enough for the hook concurrency model (hooks for one session are
serialized by Cursor; distinct sessions write to distinct directories).
Corrupt lines are skipped on read rather than failing the whole session.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from archrev.config import archrev_dir

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

EVENTS_FILENAME = "events.jsonl"
META_FILENAME = "meta.json"
MANIFEST_FILENAME = "manifest.json"
PLAN_FILENAME = "plan.md"
REPORT_FILENAME = "report.md"


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

    def ensure_meta(self, head_sha: str | None) -> dict:
        """Write ``meta.json`` exactly once; return the (existing) meta."""
        existing = self.meta()
        if existing is not None:
            return existing
        meta = {
            "id": self.id,
            "started_at": utc_now_iso(),
            "head_sha": head_sha,
        }
        _write_json(self.dir / META_FILENAME, meta)
        return meta

    # -- events ---------------------------------------------------------

    def append_event(self, event_type: str, **data: object) -> dict:
        """Append one event line; returns the stored event."""
        event: dict = {"ts": utc_now_iso(), "type": event_type, **data}
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)
        with open(
            self.dir / EVENTS_FILENAME, "a", encoding="utf-8", newline="\n"
        ) as fh:
            fh.write(line + "\n")
        return event

    def events(self) -> list[dict]:
        """All events in append order; corrupt lines are skipped."""
        path = self.dir / EVENTS_FILENAME
        out: list[dict] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return out
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
