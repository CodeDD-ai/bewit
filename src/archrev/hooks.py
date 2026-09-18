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

import hashlib
import json
import re
import traceback
from pathlib import Path

from archrev.config import Config, archrev_dir, find_root, load_config
from archrev.drift import compute_view
from archrev.gate import (
    GateDecision,
    evaluate_edit,
    evaluate_read,
    evaluate_shell,
    evaluate_tool,
    relativize,
)
from archrev.gitutil import Git
from archrev.rules import load_rules
from archrev.storage import Session, SessionStore, utc_now_iso

#: Tool names whose calls modify files and are therefore subject to path
#: (edit) rules in the preToolUse gate.
_EDIT_TOOL_RE = re.compile(r"write|edit|replace|patch|notebook", re.IGNORECASE)

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

HOOK_EVENTS = ("prompt", "edit", "gate", "shell", "read", "mcp", "finalize")


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
    git = Git(root)
    session.ensure_meta(git.head_sha(), branch=git.branch())
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
    # Tab completions are human-driven edits assisted by the editor; tag
    # the origin so the review distinguishes them from agent edits.
    event_name = str(
        payload.get("hook_event_name") or payload.get("hookEventName") or ""
    )
    tool = str(payload.get("tool_name") or payload.get("tool") or "")
    origin = "tab" if "tab" in (event_name + tool).lower() else "agent"
    session.append_event(
        "edit",
        path=relativize(raw_path, root),
        tool=tool,
        origin=origin,
        edit_count=len(edits) if isinstance(edits, list) else None,
    )
    return {}


def _record_gate(
    session: Session,
    decision: GateDecision,
    kind: str,
    target: str,
    paths: list[str] | None = None,
) -> None:
    """Persist a gate decision when it stopped, asked, or flagged something."""
    if decision.permission == "allow" and not decision.hits:
        return
    session.append_event(
        "gate",
        kind=kind,
        target=target,
        permission=decision.permission,
        paths=paths or [],
        hits=[
            {"rule_id": h.rule_id, "action": h.action, "path": h.path}
            for h in decision.hits
        ],
    )


def handle_gate(root: Path, config: Config, payload: dict) -> dict:
    """``preToolUse``: gate tool usage (tool rules) and file edits (path rules)."""
    session = _ensure_session(root, payload, "gate")
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    ruleset = load_rules(root)

    # Tool rules first: "may this tool be used at all in this repo?"
    if tool_name:
        decision = evaluate_tool(config, ruleset, "tool", tool_name)
        if decision.permission != "allow" or decision.hits:
            _record_gate(session, decision, "tool", tool_name)
            if decision.permission != "allow":
                return decision.to_hook_output()

    # Path (edit) rules apply only to tools that MODIFY files. Read-style
    # tools also carry file paths through preToolUse, but gating them here
    # produced false "ask" prompts on plain reads (observed live); reads
    # are governed by `read` rules via beforeReadFile instead. An empty
    # tool name is treated as an edit, erring toward protection.
    if tool_name and not _EDIT_TOOL_RE.search(tool_name):
        return {"permission": "allow"}

    paths = _paths_from_tool_input(
        payload.get("tool_input") or payload.get("toolInput")
    )
    if not paths:
        # Not a file edit we can reason about — never obstruct.
        return {"permission": "allow"}

    decision = evaluate_edit(config, ruleset, session, paths, root)
    _record_gate(
        session,
        decision,
        "edit",
        tool_name,
        paths=[relativize(p, root) for p in paths],
    )
    return decision.to_hook_output()


def handle_shell(root: Path, config: Config, payload: dict) -> dict:
    """``beforeShellExecution``: gate one shell command."""
    session = _ensure_session(root, payload, "shell")
    command = ""
    for key in ("command", "cmd", "shell_command"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            command = value
            break
    if not command:
        session.append_event(
            "payload_debug", hook="shell", payload=_trim_payload(payload)
        )
        return {"permission": "allow"}
    decision = evaluate_shell(config, load_rules(root), command)
    _record_gate(session, decision, "shell", command.strip()[:200])
    return decision.to_hook_output()


def handle_read(root: Path, config: Config, payload: dict) -> dict:
    """``beforeReadFile``: gate file reads (e.g. secrets)."""
    session = _ensure_session(root, payload, "read")
    raw_path = ""
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            raw_path = value
            break
    if not raw_path:
        return {"permission": "allow"}
    decision = evaluate_read(config, load_rules(root), [raw_path], root)
    _record_gate(session, decision, "read", relativize(raw_path, root))
    return decision.to_hook_output()


def handle_mcp(root: Path, config: Config, payload: dict) -> dict:
    """``beforeMCPExecution``: gate MCP tool calls by identifier."""
    session = _ensure_session(root, payload, "mcp")
    tool = ""
    for key in ("tool_name", "tool", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            tool = value
            break
    server = ""
    for key in ("server", "server_name", "provider", "namespace"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            server = value
            break
    identifier = f"{server}.{tool}" if server else tool
    if not identifier:
        session.append_event(
            "payload_debug", hook="mcp", payload=_trim_payload(payload)
        )
        return {"permission": "allow"}
    decision = evaluate_tool(config, load_rules(root), "mcp", identifier)
    _record_gate(session, decision, "mcp", identifier)
    return decision.to_hook_output()


def _final_findings(view: dict) -> list[str]:
    """Material findings worth confronting the agent with at session end.

    Targets acknowledged via ``archrev ack`` are skipped: acknowledgment is
    the review's prescribed resolution for legitimate findings, and the ack
    itself is an audited, hash-chained event — resolved, not erased.
    """
    acked = {
        str(a.get("target", "")).lower()
        for a in view.get("acks", [])
        if a.get("target")
    }
    findings: list[str] = []
    bypassed = [
        f
        for f in view.get("protected_findings", [])
        if not f.get("via_gate") and f["path"].lower() not in acked
    ]
    if bypassed:
        listed = ", ".join(f["path"] for f in bypassed[:5])
        findings.append(
            f"{len(bypassed)} protected path(s) changed OUTSIDE the edit "
            f"gate (shell/manual): {listed}"
        )
    checks = view.get("checks", [])
    if checks and not checks[-1].get("ok") and "plan-check" not in acked:
        findings.append(
            "the latest plan check did NOT pass (failed or unattested "
            "prompt rules)"
        )
    out_of_plan = [
        p
        for p in view.get("drift", {}).get("out_of_plan", [])
        if p.lower() not in acked
    ]
    if view.get("plan", {}).get("registered") and out_of_plan:
        listed = ", ".join(out_of_plan[:5])
        findings.append(
            f"{len(out_of_plan)} file(s) touched but not declared in the "
            f"plan: {listed}"
        )
    return findings


def handle_finalize(root: Path, config: Config, payload: dict) -> dict:
    """``stop``: write manifest + report, then run the final rule review.

    When material findings exist (gate bypasses, failed checks, drift) and
    ``final_check`` is enabled, the agent receives a follow-up message so
    every implementation ends with an explicit review instead of a silent
    manifest. A findings fingerprint prevents follow-up loops: the same
    findings are raised at most once.
    """
    session = _ensure_session(root, payload, "finalize")
    session.append_event(
        "session_stop", status=str(payload.get("status") or "")
    )
    ruleset = load_rules(root)
    view = compute_view(root, config, ruleset, session, finalize=True)
    # Local import keeps hot hooks (gate/edit) free of report imports.
    from archrev.report.render import render_markdown

    session.write_report(render_markdown(view))

    findings = _final_findings(view)
    fingerprint = hashlib.sha256(
        json.dumps(findings, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    previous = session.last_event("final_check")
    already_raised = bool(previous) and previous.get("fingerprint") == fingerprint
    notify = bool(findings) and config.final_check and not already_raised
    session.append_event(
        "final_check",
        findings=findings,
        fingerprint=fingerprint,
        notified=notify,
    )
    if notify:
        return {
            "followup_message": (
                "ArchRev final review found issues in this session:\n- "
                + "\n- ".join(findings)
                + "\nReview them with the user: confirm legitimate ones with "
                "`archrev ack <path|plan-check> --note \"<reason>\"` (audited, "
                "stops re-raising); revert unintended ones. See `archrev "
                "show` for the full record."
            )
        }
    return {}


_HANDLERS = {
    "prompt": handle_prompt,
    "edit": handle_edit,
    "gate": handle_gate,
    "shell": handle_shell,
    "read": handle_read,
    "mcp": handle_mcp,
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
    parse_failed = False
    text = stdin_text.strip()
    # Defense in depth against transport artifacts (BOMs, stray prefix
    # bytes): if direct parsing fails, retry from the first '{'.
    for candidate in (text, text[text.find("{") :] if "{" in text else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parse_failed = True
            continue
        if isinstance(parsed, dict):
            payload = parsed
            parse_failed = False
            break
        parse_failed = True
    try:
        root = _payload_root(payload)
        if root is None:
            return {"permission": "allow"} if event == "gate" else {}
        if parse_failed or not payload:
            # The payload was empty or not a JSON object: record the raw
            # stdin head so the transport problem is diagnosable from the
            # session record alone.
            SessionStore(root).session("unknown").append_event(
                "payload_debug",
                hook=event,
                stdin_len=len(stdin_text),
                stdin_head=stdin_text[:400],
            )
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
