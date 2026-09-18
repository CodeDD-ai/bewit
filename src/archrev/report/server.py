"""Live session viewer (``archrev serve``) and static HTML export.

The server is intentionally minimal: Python's stdlib HTTP server, one
embedded HTML page, and two JSON endpoints the page polls every few
seconds. It binds to localhost by default, reads only the ``.archrev``
directory, and holds no state — stopping or restarting it never affects
capture, which happens in the hooks.

``archrev export`` renders the very same page with the session data
embedded, producing a single self-contained HTML file suitable for
attaching to a merge request or archiving.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote, urlparse

from archrev import __version__
from archrev.config import load_config
from archrev.drift import compute_view
from archrev.rules import load_rules
from archrev.storage import Session, SessionStore, utc_now_iso

#: Marker in template.html replaced with embedded data on export.
_EMBED_MARKER = "/*__ARCHREV_EMBED__*/"


def _template() -> str:
    return (
        resources.files("archrev.report").joinpath("template.html").read_text(
            encoding="utf-8"
        )
    )


def _session_summary(root: Path, session: Session) -> dict:
    """Cheap summary for the sessions list (no git calls)."""
    events = session.events()
    prompts = [e for e in events if e.get("type") == "prompt"]
    first_prompt = str(prompts[0].get("text", "")) if prompts else ""
    gate_flags = gate_blocks = 0
    for e in events:
        if e.get("type") != "gate":
            continue
        if e.get("permission") in ("ask", "deny"):
            gate_blocks += 1
        elif e.get("hits"):
            gate_flags += 1
    meta = session.meta() or {}
    return {
        "id": session.id,
        "started_at": meta.get("started_at"),
        "finalized": session.manifest() is not None,
        "prompt_excerpt": first_prompt.strip()[:160],
        "edits": sum(1 for e in events if e.get("type") == "edit"),
        "flags": gate_flags,
        "blocks": gate_blocks,
        "plan_registered": any(
            e.get("type") == "plan_registered" for e in events
        ),
        "checked": any(e.get("type") == "plan_check" for e in events),
    }


def build_state(root: Path) -> dict:
    store = SessionStore(root)
    return {
        "root": str(root),
        "version": __version__,
        "generated_at": utc_now_iso(),
        "sessions": [_session_summary(root, s) for s in store.list_sessions()],
    }


def build_session_view(root: Path, ref: str) -> dict | None:
    store = SessionStore(root)
    session = store.resolve(ref)
    if session is None:
        return None
    config = load_config(root)
    ruleset = load_rules(root)
    return compute_view(root, config, ruleset, session)


def export_html(root: Path, ref: str) -> str | None:
    """Self-contained HTML for one session; None when the session is unknown."""
    view = build_session_view(root, ref)
    if view is None:
        return None
    payload = {"session": view, "root": str(root), "version": __version__}
    embed = "window.__ARCHREV_DATA__ = " + json.dumps(payload, ensure_ascii=False).replace(
        "</", "<\\/"  # keep embedded JSON from terminating the script tag
    ) + ";"
    return _template().replace(_EMBED_MARKER, embed)


class _Handler(BaseHTTPRequestHandler):
    """Read-only JSON/HTML handler; the repo root is bound via server."""

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

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        root: Path = self.server.archrev_root  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send(
                    200, "text/html; charset=utf-8", _template().encode("utf-8")
                )
            elif path == "/api/state":
                self._send_json(200, build_state(root))
            elif path.startswith("/api/session/"):
                ref = unquote(path.removeprefix("/api/session/"))
                view = build_session_view(root, ref)
                if view is None:
                    self._send_json(404, {"error": f"unknown session '{ref}'"})
                else:
                    self._send_json(200, view)
            else:
                self._send_json(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 — keep the viewer alive
            self._send_json(500, {"error": repr(exc)})

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence per-request logging; the CLI prints the URL once."""


def serve(root: Path, host: str = "127.0.0.1", port: int = 4177) -> None:
    """Run the viewer until interrupted."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.archrev_root = root  # type: ignore[attr-defined]
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
