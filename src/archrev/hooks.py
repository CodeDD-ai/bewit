"""Cursor hook adapters.

Cursor invokes ``archrev hook <event>`` as a short-lived process with a
JSON payload on stdin; the process appends to the session log and exits.
There is no daemon: capture is event-driven and always on once
``.cursor/hooks.json`` is in place.

Robustness contract:

- Handlers parse payload fields defensively (Cursor's exact field names
  may evolve); unknown shapes degrade to recording less, never to crashing.
- Any unexpected exception is logged to ``.archrev/hook-errors.log`` and
  the hook fails **open** (gate answers ``allow``): a broken ArchRev must
  never lock the user out of their own editor. The error is also recorded
  as a session event so silent failure still shows up in the timeline.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

from archrev.config import Config, archrev_dir, find_root, load_config
from archrev.drift import compute_view
from archrev.gate import evaluate_edit, relativize
from archrev.gitutil import Git
from archrev.rules import load_rules
from archrev.storage import Session, SessionStore, utc_now_iso

#: Keys under which Cursor tool inputs may carry file paths.
_PATH_KEYS = (
    "file_path",
    "path",
    "target_file",
    "filePath",
    "targetFile",
    "file",
    "notebook_path",
)

HOOK_EVENTS = ("prompt", "edit", "gate", "finalize")


def _session_id(payload: dict) -> str:
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _payload_root(payload: dict) -> Path | None:
    """Resolve the repository root: cwd first, then workspace hints."""
    root = find_root()
    if root is not None:
        return root
    roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    if isinstance(roots, list):
        for candidate in roots:
            if isinstance(candidate, str):
                found = find_root(Path(candidate))
                if found is not None:
                    return found
    return None


def _trim_payload(value: object, depth: int = 0) -> object:
    """Bounded copy of a payload for diagnostics (no huge file contents)."""
    if depth > 3:
        return "..."
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_trim_payload(v, depth + 1) for v in value[:10]]
    if isinstance(value, dict):
        return {
            str(k): _trim_payload(v, depth + 1)
            for k, v in list(value.items())[:30]
        }
    return repr(value)[:200]


def _ensure_session(root: Path, payload: dict, hook_event: str) -> Session:
    store = SessionStore(root)
    session = store.session(_session_id(payload))
    session.ensure_meta(Git(root).head_sha())
    if session.id == "unknown" and payload:
        # Field-name drift diagnostics: Cursor's payload schema is not under
        # our control. When the conversation id cannot be found, record what
        # actually arrived so the adapter can be fixed from evidence instead
        # of guesses (`archrev show unknown` reveals the real field names).
        session.append_event(
            "payload_debug", hook=hook_event, payload=_trim_payload(payload)
        )
    return session


def _paths_from_tool_input(tool_input: object) -> list[str]:
    """Extract every plausible file path from a tool input mapping."""
    if not isinstance(tool_input, dict):
        return []
    found: dict[str, None] = {}
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            found.setdefault(value.strip(), None)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                for key in _PATH_KEYS:
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        found.setdefault(value.strip(), None)
    return list(found)


# ---------------------------------------------------------------------------
# Handlers — each returns the dict to print as the hook's JSON response.
# ---------------------------------------------------------------------------


def handle_prompt(root: Path, config: Config, payload: dict) -> dict:
    """``beforeSubmitPrompt``: open the session and record the prompt."""
    session = _ensure_session(root, payload, "prompt")
    text = ""
    for key in ("prompt", "text", "user_prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            text = value
            break
    session.append_event("prompt", text=text)
    return {}


def handle_edit(root: Path, config: Config, payload: dict) -> dict:
    """``afterFileEdit``: record one tracked agent edit."""
    session = _ensure_session(root, payload, "edit")
    raw_path = ""
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            raw_path = value
            break
    if not raw_path:
        # An edit we could not attribute to a file is a capture gap; keep
        # the evidence so the parser can be fixed from real payloads.
        session.append_event(
            "payload_debug", hook="edit", payload=_trim_payload(payload)
        )
        return {}
    edits = payload.get("edits")
    session.append_event(
        "edit",
        path=relativize(raw_path, root),
        tool=str(payload.get("tool_name") or payload.get("tool") or ""),
        edit_count=len(edits) if isinstance(edits, list) else None,
    )
    return {}


def handle_gate(root: Path, config: Config, payload: dict) -> dict:
    """``preToolUse`` (edit tools): decide allow / ask / deny."""
    session = _ensure_session(root, payload, "gate")
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    paths = _paths_from_tool_input(
        payload.get("tool_input") or payload.get("toolInput")
    )
    if not paths:
        # Not a file edit we can reason about — never obstruct. The gate's
        # matcher targets edit tools only, so a missing path here usually
        # means field-name drift: keep the evidence.
        session.append_event(
            "payload_debug", hook="gate", payload=_trim_payload(payload)
        )
        return {"permission": "allow"}

    ruleset = load_rules(root)
    decision = evaluate_edit(config, ruleset, session, paths, root)
    if decision.permission != "allow" or decision.hits:
        session.append_event(
            "gate",
            tool=tool_name,
            permission=decision.permission,
            paths=[relativize(p, root) for p in paths],
            hits=[
                {"rule_id": h.rule_id, "action": h.action, "path": h.path}
                for h in decision.hits
            ],
        )
    return decision.to_hook_output()


def handle_finalize(root: Path, config: Config, payload: dict) -> dict:
    """``stop``: write the manifest and regenerate the session report."""
    session = _ensure_session(root, payload, "finalize")
    session.append_event(
        "session_stop", status=str(payload.get("status") or "")
    )
    ruleset = load_rules(root)
    view = compute_view(root, config, ruleset, session, finalize=True)
    # Local import keeps hot hooks (gate/edit) free of report imports.
    from archrev.report.render import render_markdown

    session.write_report(render_markdown(view))
    return {}


_HANDLERS = {
    "prompt": handle_prompt,
    "edit": handle_edit,
    "gate": handle_gate,
    "finalize": handle_finalize,
}


def _log_hook_error(root: Path | None, event: str, exc: BaseException) -> None:
    """Best-effort error trail; failures here are swallowed by design."""
    try:
        if root is None:
            return
        log_dir = archrev_dir(root)
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "hook-errors.log", "a", encoding="utf-8") as fh:
            fh.write(
                f"{utc_now_iso()} event={event} "
                f"{''.join(traceback.format_exception(exc)).strip()}\n"
            )
    except OSError:
        pass


def run_hook(event: str, stdin_text: str) -> dict:
    """Dispatch one hook event; guaranteed not to raise.

    Returns the JSON-serializable response for stdout. The gate fails open
    (``allow``) on internal errors — protection degrading is preferable to
    an editor that cannot save files; the error log and timeline keep the
    degradation visible.
    """
    root: Path | None = None
    payload: dict = {}
    try:
        parsed = json.loads(stdin_text) if stdin_text.strip() else {}
        if isinstance(parsed, dict):
            payload = parsed
    except json.JSONDecodeError:
        payload = {}
    try:
        root = _payload_root(payload)
        if root is None:
            return {"permission": "allow"} if event == "gate" else {}
        config = load_config(root)
        handler = _HANDLERS.get(event)
        if handler is None:
            return {}
        return handler(root, config, payload)
    except Exception as exc:  # noqa: BLE001 — hooks must never crash Cursor
        _log_hook_error(root, event, exc)
        try:
            if root is not None:
                # Attribute the failure to the real session when the id is
                # known, so degradation shows up in that session's timeline.
                SessionStore(root).session(_session_id(payload)).append_event(
                    "hook_error", event=event, error=repr(exc)
                )
        except Exception:  # noqa: BLE001
            pass
        return {"permission": "allow"} if event == "gate" else {}
