"""Live session viewer (``archrev serve``) and static HTML export.

The server is intentionally minimal: Python's stdlib HTTP server, one
embedded HTML page, and a few JSON endpoints the page polls. It binds to
localhost by default, reads only ``.archrev`` directories, and holds no
state — stopping or restarting it never affects capture, which happens in
the hooks.

One viewer serves one project by default. ``archrev serve --all <dir>``
serves every ArchRev repository under a directory (a hub with a project
switcher); every API call then names its project with ``?project=<id>``.

Starting a viewer never silently shows the wrong project: when the port is
taken, :func:`bind_viewer` asks the listener which projects it serves. The
same project is reused; anything else moves this viewer to the next free
port.

``archrev export`` renders the very same page with the session data
embedded, producing a single self-contained HTML file suitable for
attaching to a merge request or archiving.
"""

from __future__ import annotations

import errno
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from archrev import __version__
from archrev.config import load_config
from archrev.drift import compute_view
from archrev.rules import load_rules
from archrev.storage import HUMAN_SESSION_ID, Session, SessionStore, utc_now_iso

#: Marker in template.html replaced with embedded data on export.
_EMBED_MARKER = "/*__ARCHREV_EMBED__*/"

#: Identifies an ArchRev viewer to another ``archrev serve`` probing the port.
APP_ID = "archrev-viewer"

#: Data contract between this server and template.html. The page is read
#: from disk on every request while this module loads once, so a viewer
#: started before an upgrade serves a new page with old data. The page
#: compares this number with its own and asks for a restart on mismatch.
API_LEVEL = 3

#: Ports tried after the requested one when it is held by something else.
PORT_ATTEMPTS = 20

#: Session ids that are records, not agent sessions.
_UNKNOWN_SESSION_ID = "unknown"


def _template() -> str:
    return (
        resources.files("archrev.report").joinpath("template.html").read_text(
            encoding="utf-8"
        )
    )


def _user_prompt_excerpt(prompts: list[dict]) -> str:
    """Sidebar line: the latest thing the user asked, not the opening prompt.

    ArchRev's own final-review follow-up is also stored as a prompt. It is
    a note to the agent, so it must not become the session's title.
    """
    for event in reversed(prompts):
        text = str(event.get("text") or "").strip()
        if not text or text.startswith("ArchRev final review found issues"):
            continue
        return text[:160]
    if any(event.get("capture") == "sealed" for event in prompts):
        return "(sealed prompt)"
    return ""


def _category(session_id: str) -> str:
    """``agent`` for real sessions; ``human`` / ``unknown`` for ledgers."""
    if session_id == HUMAN_SESSION_ID:
        return "human"
    if session_id == _UNKNOWN_SESSION_ID:
        return "unknown"
    return "agent"


def _session_summary(root: Path, session: Session) -> dict:
    """Cheap summary for the sessions list (no git calls)."""
    events = session.events()
    prompts = [e for e in events if e.get("type") == "prompt"]
    gate_flags = gate_blocks = 0
    for e in events:
        if e.get("type") != "gate":
            continue
        if e.get("permission") in ("ask", "deny"):
            gate_blocks += 1
        elif e.get("hits"):
            gate_flags += 1
    finals = [e for e in events if e.get("type") == "final_check"]
    open_findings = len((finals[-1].get("findings") or [])) if finals else 0
    # "To review" uses the same definition as the session page's review box,
    # evaluated on the manifest (the view as of the last turn end) with the
    # acknowledgments recorded since, so an ack clears the count at once.
    manifest = session.manifest()
    to_review = 0
    if manifest is not None:
        from archrev.review import review_items

        live = {**manifest, "acks": [e for e in events if e.get("type") == "ack"]}
        to_review = len(review_items(live)["open"])
    meta = session.meta() or {}
    last_ts = next(
        (e["ts"] for e in reversed(events) if isinstance(e.get("ts"), str)),
        meta.get("started_at"),
    )
    return {
        "id": session.id,
        "category": _category(session.id),
        "started_at": meta.get("started_at"),
        # Last recorded event: what "Recording now" is judged by. The
        # manifest is rewritten at every Claude/Codex turn end, so
        # "finalized" does not mean the session is over.
        "last_activity": last_ts,
        "runtime": meta.get("runtime"),
        "branch": meta.get("branch"),
        "finalized": session.manifest() is not None,
        "prompt_excerpt": _user_prompt_excerpt(prompts),
        "edits": sum(1 for e in events if e.get("type") == "edit"),
        "flags": gate_flags,
        "blocks": gate_blocks,
        "open_findings": open_findings,
        "to_review": to_review,
        "plan_registered": any(
            e.get("type") == "plan_registered" for e in events
        ),
        "checked": any(e.get("type") == "plan_check" for e in events),
    }


def build_state(root: Path) -> dict:
    from archrev.scaffold import wiring_problems

    store = SessionStore(root)
    return {
        "app": APP_ID,
        "api_level": API_LEVEL,
        "root": str(root),
        "project": root.name,
        "version": __version__,
        "generated_at": utc_now_iso(),
        "warnings": wiring_problems(root),
        "sessions": [_session_summary(root, s) for s in store.list_sessions()],
    }


def _active_rules(ruleset) -> list[dict]:
    """Every enabled rule, for the viewer's "what the rules did" panel."""
    return [
        {
            "id": rule.id,
            "kind": rule.kind,
            "action": rule.action if rule.kind != "prompt" else "",
            "targets": list(rule.match or rule.patterns),
            "applies_to": list(rule.applies_to),
            "message": rule.message,
            "policy": rule.policy,
            "command": rule.command,
            "source": rule.source,
        }
        for rule in ruleset.rules
        if rule.enabled
    ]


def build_session_view(root: Path, ref: str) -> dict | None:
    store = SessionStore(root)
    session = store.resolve(ref)
    if session is None:
        return None
    config = load_config(root)
    ruleset = load_rules(root)
    view = compute_view(root, config, ruleset, session)
    view["active_rules"] = _active_rules(ruleset)
    return view


def hidden_by_read_rule(root: Path, path: str) -> str | None:
    """The read rule that makes ``path`` unservable, if any.

    A diff of an untracked file is its full content, so serving diffs for
    paths covered by a ``deny`` or ``block`` read rule would hand secrets
    to anyone who can reach the viewer, the agent included (``curl``).
    """
    from archrev import globmatch
    from archrev.gate import relativize

    rel = relativize(path, root)
    if not rel or globmatch.matches_any(load_config(root).exempt, rel):
        return None
    for rule in load_rules(root).match_read(rel):
        if rule.action in ("deny", "block"):
            return rule.id
    return None


def build_file_diff(root: Path, ref: str, path: str) -> str | None:
    """Unified diff of one session file vs the session-start base.

    Paths covered by a deny/block read rule are never served (see
    :func:`hidden_by_read_rule`); the caller gets a placeholder instead.
    """
    session = SessionStore(root).resolve(ref)
    if session is None:
        return None
    rule_id = hidden_by_read_rule(root, path)
    if rule_id is not None:
        return (
            f"(diff hidden: read rule '{rule_id}' covers this file. "
            "The viewer never serves its content; open it yourself if needed.)"
        )
    meta = session.meta() or {}
    from archrev.gitutil import Git

    return Git(root).file_diff(meta.get("head_sha"), path)


#: Caps for diffs embedded into exported HTML (bytes of diff text).
_EXPORT_DIFF_FILE_CAP = 100_000
_EXPORT_DIFF_TOTAL_CAP = 1_000_000


def _export_diffs(root: Path, view: dict) -> dict[str, str]:
    """Collect per-file diffs for export, respecting size caps."""
    diffs: dict[str, str] = {}
    total = 0
    paths = [f["path"] for f in view.get("files", [])] + [
        f["path"] for f in view.get("other_changes", [])
    ]
    for path in paths:
        diff = build_file_diff(root, view["id"], path)  # read-rule paths come back hidden
        if not diff or len(diff) > _EXPORT_DIFF_FILE_CAP:
            continue
        if total + len(diff) > _EXPORT_DIFF_TOTAL_CAP:
            break
        diffs[path] = diff
        total += len(diff)
    return diffs


def export_html(root: Path, ref: str) -> str | None:
    """Self-contained HTML for one session; None when the session is unknown."""
    view = build_session_view(root, ref)
    if view is None:
        return None
    payload = {
        "session": view,
        "api_level": API_LEVEL,
        "root": str(root),
        "project": root.name,
        "version": __version__,
        "diffs": _export_diffs(root, view),
    }
    embed = "window.__ARCHREV_DATA__ = " + json.dumps(payload, ensure_ascii=False).replace(
        "</", "<\\/"  # keep embedded JSON from terminating the script tag
    ) + ";"
    return _template().replace(_EMBED_MARKER, embed)


# ---------------------------------------------------------------------------
# Projects served by one viewer
# ---------------------------------------------------------------------------


def project_ids(roots: list[Path]) -> dict[str, Path]:
    """Stable, URL-safe project ids (folder names, de-duplicated)."""
    ids: dict[str, Path] = {}
    for root in roots:
        base = root.name or "project"
        candidate, n = base, 2
        while candidate in ids:
            candidate, n = f"{base}-{n}", n + 1
        ids[candidate] = root
    return ids


def _root_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _same_roots(served: list[str], roots: list[Path]) -> bool:
    return {_root_key(p) for p in served} == {_root_key(r) for r in roots}


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


class _Handler(BaseHTTPRequestHandler):
    """Read-only JSON/HTML handler; project roots are bound via the server."""

    server_version = f"archrev/{__version__}"

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data: dict) -> None:
        self._send(
            status,
            "application/json; charset=utf-8",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        )

    def _host_allowed(self) -> bool:
        """Reject foreign Host headers on a loopback viewer (DNS rebinding).

        A web page on another origin can make the browser resolve its own
        name to 127.0.0.1 and read the viewer's prompts and diffs. Only
        loopback names are valid for a loopback bind; an explicit non-
        loopback ``--host`` is the operator's choice and is not checked.
        """
        if not _loopback(self.server.archrev_host):  # type: ignore[attr-defined]
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host[1 : host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in ("127.0.0.1", "localhost", "::1")

    def _project_root(self, query: dict) -> tuple[str, Path] | None:
        projects: dict[str, Path] = self.server.archrev_projects  # type: ignore[attr-defined]
        wanted = (query.get("project") or [""])[0]
        if not wanted:
            first = next(iter(projects))
            return first, projects[first]
        root = projects.get(wanted)
        return (wanted, root) if root is not None else None

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if not self._host_allowed():
            self._send_json(403, {"error": "host not allowed"})
            return
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        projects: dict[str, Path] = self.server.archrev_projects  # type: ignore[attr-defined]
        try:
            if path == "/":
                self._send(
                    200, "text/html; charset=utf-8", _template().encode("utf-8")
                )
                return
            if path == "/api/projects":
                self._send_json(
                    200,
                    {
                        "app": APP_ID,
                        "version": __version__,
                        "hub": self.server.archrev_hub,  # type: ignore[attr-defined]
                        "projects": [
                            {"id": pid, "name": root.name, "root": str(root)}
                            for pid, root in projects.items()
                        ],
                    },
                )
                return
            resolved = self._project_root(query)
            if resolved is None:
                self._send_json(404, {"error": "unknown project"})
                return
            project_id, root = resolved
            if path == "/api/state":
                self._send_json(200, {**build_state(root), "project_id": project_id})
            elif path.startswith("/api/session/"):
                ref = unquote(path.removeprefix("/api/session/"))
                view = build_session_view(root, ref)
                if view is None:
                    self._send_json(404, {"error": f"unknown session '{ref}'"})
                else:
                    self._send_json(200, view)
            elif path == "/api/diff":
                ref = (query.get("session") or [""])[0]
                file_path = (query.get("path") or [""])[0]
                diff = build_file_diff(root, ref, file_path)
                self._send_json(
                    200, {"path": file_path, "diff": diff or "(no diff available)"}
                )
            else:
                self._send_json(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 — keep the viewer alive
            self._send_json(500, {"error": repr(exc)})

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        """Unseal prompts or acknowledge a finding. Localhost only.

        Both require the ``X-ArchRev`` header: a cross-site form or
        ``fetch`` cannot set it without a CORS preflight, which this server
        never answers, so another web page cannot post here.
        """
        client = self.client_address[0]
        if client not in ("127.0.0.1", "::1") or not self._host_allowed():
            self._send_json(403, {"error": "localhost only"})
            return
        if self.headers.get("X-ArchRev") != "1":
            self._send_json(403, {"error": "missing X-ArchRev header"})
            return
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/unseal", "/api/ack"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("expected an object")
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "expected JSON"})
            return
        resolved = self._project_root({"project": [str(body.get("project") or "")]})
        if resolved is None:
            self._send_json(404, {"error": "unknown project"})
            return
        root = resolved[1]
        if parsed.path == "/api/ack":
            self._ack(root, body)
            return
        ref = str(body.get("session") or "")
        passphrase = body.get("passphrase") or None
        session = SessionStore(root).resolve(ref)
        if session is None:
            self._send_json(404, {"error": f"unknown session '{ref}'"})
            return
        from archrev.seal import SealUnavailable, open_sealed

        prompts = []
        try:
            prompt_index = 0
            for event in session.events():
                if event.get("type") != "prompt":
                    continue
                blob = event.get("sealed")
                if event.get("capture") == "sealed" and isinstance(blob, dict):
                    prompts.append(
                        {
                            "index": prompt_index,
                            "text": open_sealed(
                                blob, passphrase if passphrase else None
                            ),
                        }
                    )
                prompt_index += 1
        except SealUnavailable as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"prompts": prompts})

    def _ack(self, root: Path, body: dict) -> None:
        """Record a reviewer acknowledgment made in the viewer.

        Attribution is the same as for the CLI: an ack made while an agent
        command runs (an agent calling this endpoint) is agent-made.
        """
        from archrev.review import detect_ack_actor

        session = SessionStore(root).resolve(str(body.get("session") or ""))
        target = str(body.get("target") or "").strip().replace("\\", "/")
        note = str(body.get("note") or "").strip()
        if session is None:
            self._send_json(404, {"error": "unknown session"})
            return
        if not target or not note:
            self._send_json(400, {"error": "target and note are required"})
            return
        actor, basis = detect_ack_actor(root, session)
        session.append_event(
            "ack", target=target, note=note[:2000], actor=actor,
            actor_basis=basis, via="viewer",
        )
        self._send_json(200, {"actor": actor, "basis": basis})

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence per-request logging; the CLI prints the URL once."""


class _Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second viewer bind an already-used
    # port, silently splitting requests between old and new processes
    # (observed live). Fail loudly instead; POSIX keeps reuse for TIME_WAIT.
    allow_reuse_address = os.name != "nt"


def make_server(
    roots: list[Path], host: str = "127.0.0.1", port: int = 4177, hub: bool = False
) -> _Server:
    """Bind a viewer for ``roots``; raises ``OSError`` when the port is taken."""
    if not roots:
        raise ValueError("a viewer needs at least one project root")
    httpd = _Server((host, port), _Handler)
    httpd.archrev_projects = project_ids(roots)  # type: ignore[attr-defined]
    httpd.archrev_hub = hub  # type: ignore[attr-defined]
    httpd.archrev_host = host  # type: ignore[attr-defined]
    return httpd


def port_in_use(exc: OSError) -> bool:
    if getattr(exc, "winerror", None) in (10048, 10013):
        return True
    return exc.errno in (errno.EADDRINUSE, errno.EACCES, 10048)


def identify_viewer(host: str, port: int, timeout: float = 1.0) -> list[str] | None:
    """Project roots served by the ArchRev viewer on ``host:port``.

    ``None`` when nothing answers or the listener is not an ArchRev viewer.
    Viewers older than the ``/api/projects`` endpoint are identified by
    the ``root`` field of ``/api/state``.
    """
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    if ":" in probe_host and not probe_host.startswith("["):
        probe_host = f"[{probe_host}]"
    base = f"http://{probe_host}:{port}"
    for endpoint in ("/api/projects", "/api/state"):
        try:
            with urllib.request.urlopen(base + endpoint, timeout=timeout) as res:
                data = json.loads(res.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("projects"), list):
            return [str(p.get("root")) for p in data["projects"] if isinstance(p, dict)]
        if isinstance(data.get("root"), str) and "sessions" in data:
            return [data["root"]]
    return None


@dataclass
class Binding:
    """Outcome of :func:`bind_viewer`."""

    server: _Server | None
    port: int
    #: True when a viewer for exactly these projects already runs on ``port``.
    reused: bool = False
    #: What holds the requested port when this viewer moved elsewhere:
    #: the other viewer's project roots, or ``[]`` for a non-ArchRev listener.
    displaced_by: list[str] | None = None


def bind_viewer(
    roots: list[Path],
    host: str = "127.0.0.1",
    port: int = 4177,
    hub: bool = False,
    attempts: int = PORT_ATTEMPTS,
) -> Binding:
    """Bind a viewer, reusing one that already serves the same projects.

    Tries ``port``, then the following ports. A busy port that serves the
    same projects is reused (nothing is bound); a busy port serving other
    projects, or anything else, is skipped. Raises ``OSError`` when no port
    in the range is usable.
    """
    displaced: list[str] | None = None
    for candidate in range(port, port + attempts):
        try:
            server = make_server(roots, host, candidate, hub=hub)
        except OSError as exc:
            if not port_in_use(exc):
                raise
        else:
            return Binding(server, candidate, displaced_by=displaced)
        served = identify_viewer(host, candidate)
        if served is not None and _same_roots(served, roots):
            return Binding(None, candidate, reused=True, displaced_by=displaced)
        if candidate == port:
            displaced = served if served is not None else []
    raise OSError(
        errno.EADDRINUSE,
        f"no free port in {port}-{port + attempts - 1}; pass --port",
    )


def serve(
    root: Path | list[Path],
    host: str = "127.0.0.1",
    port: int = 4177,
    hub: bool = False,
) -> None:
    """Run a viewer until interrupted (no port negotiation)."""
    roots = root if isinstance(root, list) else [root]
    httpd = make_server(roots, host, port, hub=hub)
    run_server(httpd)


def run_server(httpd: _Server) -> None:
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
