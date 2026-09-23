"""Hook adapters (Cursor, Claude Code, Codex).

Each runtime invokes ``archrev hook`` (Cursor: ``archrev hook <event>``;
Claude/Codex: ``archrev hook`` with ``hook_event_name`` on stdin) as a
short-lived process. The process appends to the session log and exits.
There is no daemon: capture is event-driven once the runtime's hook
wiring is in place.

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

from archrev.adapters import (
    detect_runtime,
    fail_open_response,
    is_mcp_tool,
    is_read_tool,
    is_shell_tool,
    map_event,
    merge_hook_outputs,
    patch_paths,
    promote_payload,
    render_response,
    tool_name_of,
)
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
from archrev.worktree import (
    PHASE_EXEC_END,
    PHASE_EXEC_START,
    PHASE_START,
    PHASE_TURN_END,
    PHASE_TURN_START,
    record_checkpoint,
)

#: Characters of a shell command kept as a checkpoint label.
_COMMAND_LABEL_CHARS = 120

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

HOOK_EVENTS = (
    "prompt", "edit", "gate", "shell", "read", "mcp", "exec_end", "finalize"
)


def _session_id(payload: dict) -> str:
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _payload_root(payload: dict) -> Path | None:
    """Resolve the repository root: process cwd, then payload hints."""
    root = find_root()
    if root is not None:
        return root
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        found = find_root(Path(cwd.strip()))
        if found is not None:
            return found
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


def _checkpoint(
    root: Path, session: Session, phase: str, label: str | None = None
) -> None:
    """Record a working-tree checkpoint; never raises.

    Checkpoints only sharpen attribution. A failure here must not turn a
    shell or edit gate decision into the generic fail-open ``allow``, so it
    is logged and swallowed right here.
    """
    try:
        record_checkpoint(session, root, phase, label)
    except Exception as exc:  # noqa: BLE001 — attribution is best-effort
        _log_hook_error(root, f"checkpoint:{phase}", exc)


def _open_session(
    root: Path, payload: dict, hook_event: str, runtime: str | None = None
) -> tuple[Session, bool]:
    """Resolve the payload's session; the flag is True when it was just created."""
    store = SessionStore(root)
    session = store.session(_session_id(payload))
    created = session.meta() is None
    git = Git(root)
    session.ensure_meta(
        git.head_sha(),
        branch=git.branch(),
        runtime=runtime or detect_runtime(payload),
    )
    if session.id == "unknown" and payload:
        # Field-name drift diagnostics: Cursor's payload schema is not under
        # our control. When the conversation id cannot be found, record what
        # actually arrived so the adapter can be fixed from evidence instead
        # of guesses (`archrev show unknown` reveals the real field names).
        session.append_event(
            "payload_debug", hook=hook_event, payload=_trim_payload(payload)
        )
    return session, created


def _ensure_session(
    root: Path, payload: dict, hook_event: str, runtime: str | None = None
) -> Session:
    """Resolve the session; a new one gets its working-tree baseline."""
    session, created = _open_session(root, payload, hook_event, runtime)
    if created:
        _checkpoint(root, session, PHASE_START)
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
    extra = tool_input.get("paths")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str) and item.strip():
                found.setdefault(item.strip(), None)
    command = tool_input.get("command")
    if isinstance(command, str):
        for path in patch_paths(command):
            found.setdefault(path, None)
    return list(found)


# ---------------------------------------------------------------------------
# Handlers — each returns the dict to print as the hook's JSON response.
# ---------------------------------------------------------------------------


#: Characters kept when ``prompt_capture: excerpt`` is configured.
_PROMPT_EXCERPT_CHARS = 200


def handle_prompt(root: Path, config: Config, payload: dict) -> dict:
    """``beforeSubmitPrompt``: open the session and record the prompt.

    Prompts are the most privacy-sensitive artifact ArchRev stores (they
    may contain secrets or half-formed thinking). ``prompt_capture`` in
    config.yaml controls what is kept:

    - ``full``    — the whole prompt text (default).
    - ``excerpt`` — the first 200 characters plus the total length.
    - ``none``    — no text at all; only length and a content hash, so the
      event still proves *a* prompt started the session and can be matched
      against a disclosed prompt later without ArchRev storing it.
    - ``sealed``  — ciphertext for the public keys in recipients.yaml.
      If sealing cannot run, this falls back to ``none`` and records why.
      It never stores the plaintext as a consolation.

    The working-tree checkpoint is taken after the prompt event, so a
    session's record still opens with its prompt.
    """
    session, created = _open_session(root, payload, "prompt")
    _record_prompt(root, config, session, payload)
    _checkpoint(root, session, PHASE_START if created else PHASE_TURN_START)
    return {}


def _record_prompt(root: Path, config: Config, session: Session, payload: dict) -> None:
    text = ""
    for key in ("prompt", "text", "user_prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            text = value
            break
    if config.prompt_capture == "none":
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        session.append_event(
            "prompt", text="", chars=len(text), content_hash=digest,
            capture="none",
        )
    elif config.prompt_capture == "excerpt":
        session.append_event(
            "prompt", text=text[:_PROMPT_EXCERPT_CHARS], chars=len(text),
            capture="excerpt",
        )
    elif config.prompt_capture == "sealed":
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        from archrev.seal import SealUnavailable, seal_text

        try:
            sealed = seal_text(root, text)
        except SealUnavailable as exc:
            session.append_event(
                "prompt",
                text="",
                chars=len(text),
                content_hash=digest,
                capture="none",
                seal_warning=str(exc),
            )
        else:
            session.append_event(
                "prompt",
                text="",
                chars=len(text),
                content_hash=digest,
                capture="sealed",
                sealed=sealed,
            )
    else:
        session.append_event("prompt", text=text)


def handle_edit(root: Path, config: Config, payload: dict) -> dict:
    """``afterFileEdit`` / ``PostToolUse``: record tracked agent edits."""
    session = _ensure_session(root, payload, "edit")
    paths: dict[str, None] = {}
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            paths.setdefault(value, None)
    for item in _paths_from_tool_input(
        payload.get("tool_input") or payload.get("toolInput")
    ):
        paths.setdefault(item, None)
    if not paths:
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
    count = len(edits) if isinstance(edits, list) else None
    for raw_path in paths:
        session.append_event(
            "edit",
            path=relativize(raw_path, root),
            tool=tool,
            origin=origin,
            edit_count=count,
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

    # Read tools: enforce ``read`` rules here. Cursor often routes agent
    # reads only through ``preToolUse`` (not ``beforeReadFile``), so relying
    # on the read hook alone leaves secret denies unwired. Edit/path rules
    # must still not apply to reads (StrReplace pipelines read first).
    if tool_name and is_read_tool(tool_name):
        return handle_read(root, config, payload, via="preToolUse")

    # Path (edit) rules apply only to tools that MODIFY files. An empty
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
    command = _shell_command(payload)
    if not command:
        session.append_event(
            "payload_debug", hook="shell", payload=_trim_payload(payload)
        )
        return {"permission": "allow"}
    decision = evaluate_shell(config, load_rules(root), command)
    _record_gate(session, decision, "shell", command.strip()[:200])
    if decision.permission != "deny":
        _checkpoint(root, session, PHASE_EXEC_START, _exec_label(config, payload))
    return decision.to_hook_output()


def _shell_command(payload: dict) -> str:
    for key in ("command", "cmd", "shell_command"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _mcp_identifier(payload: dict) -> str:
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
    return f"{server}.{tool}" if server else tool


def _exec_label(config: Config, payload: dict) -> str | None:
    """Label of a shell / MCP command window, identical at start and end.

    Commands can carry secrets; their text is kept only when the repository
    already opted into full capture of agent input.
    """
    command = _shell_command(payload)
    if command:
        if config.prompt_capture != "full":
            return "shell"
        return f"shell: {' '.join(command.split())[:_COMMAND_LABEL_CHARS]}"
    identifier = _mcp_identifier(payload)
    return f"mcp: {identifier}" if identifier else None


def handle_read(
    root: Path, config: Config, payload: dict, via: str = "beforeReadFile"
) -> dict:
    """Gate file reads (e.g. secrets) and record every read attempt.

    ``via`` names the runtime hook that reported the read. Every read is
    logged, allowed ones included, so the trail shows which hook fired for
    which file; a read of a known file with no event means the runtime
    never consulted ArchRev.
    """
    session = _ensure_session(root, payload, "read")
    raw_path = ""
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            raw_path = value
            break
    if not raw_path:
        session.append_event(
            "payload_debug", hook="read", via=via, payload=_trim_payload(payload)
        )
        return {"permission": "allow"}
    target = relativize(raw_path, root)
    decision = evaluate_read(config, load_rules(root), [raw_path], root)
    session.append_event(
        "read",
        path=target,
        permission=decision.permission,
        via=via,
        tool=tool_name_of(payload),
        rules=[h.rule_id for h in decision.hits],
    )
    _record_gate(session, decision, "read", target)
    return decision.to_hook_output()


def handle_mcp(root: Path, config: Config, payload: dict) -> dict:
    """``beforeMCPExecution``: gate MCP tool calls by identifier."""
    session = _ensure_session(root, payload, "mcp")
    identifier = _mcp_identifier(payload)
    if not identifier:
        session.append_event(
            "payload_debug", hook="mcp", payload=_trim_payload(payload)
        )
        return {"permission": "allow"}
    decision = evaluate_tool(config, load_rules(root), "mcp", identifier)
    _record_gate(session, decision, "mcp", identifier)
    if decision.permission != "deny":
        _checkpoint(root, session, PHASE_EXEC_START, f"mcp: {identifier}")
    return decision.to_hook_output()


def handle_exec_end(root: Path, config: Config, payload: dict) -> dict:
    """``afterShellExecution`` / ``afterMCPExecution`` / ``PostToolUse``:
    close this session's command window (see :mod:`archrev.worktree`)."""
    session = _ensure_session(root, payload, "exec_end")
    _checkpoint(root, session, PHASE_EXEC_END, _exec_label(config, payload))
    return {}


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
        listed = ", ".join(
            f["path"] + (f" (during {'; '.join(f['during'])})" if f.get("during") else "")
            for f in bypassed[:5]
        )
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
    # Quality gates: the latest result per rule counts; block failures are
    # findings (flag failures are recorded and highlighted, not raised).
    latest: dict[str, dict] = {}
    for q in view.get("quality_checks", []):
        latest[str(q.get("rule_id"))] = q
    for rule_id, q in latest.items():
        if q.get("ok") is False and q.get("action") == "block" and rule_id.lower() not in acked:
            findings.append(
                f"quality check '{rule_id}' FAILED: {q.get('message') or 'see recorded output'}"
            )
    return findings


def handle_finalize(
    root: Path,
    config: Config,
    payload: dict,
    *,
    notify: bool = True,
    quality: str = "always",
) -> dict:
    """Write manifest + report, then optionally run the final rule review.

    ``notify``: when false (Claude/Codex ``SessionEnd``), findings are
    recorded but no follow-up is returned — those events cannot continue
    the agent.

    ``quality``: ``always`` runs check-rules every time (Cursor ``stop``);
    ``if_new_edits`` skips them when nothing has been edited since the
    last quality run (Claude/Codex ``Stop`` fires every turn).
    """
    session = _ensure_session(root, payload, "finalize")
    # The turn is over: close any command window whose after-hook never
    # fired, so later changes by others are not charged to this session.
    _checkpoint(root, session, PHASE_TURN_END)
    if quality == "if_new_edits":
        # Claude/Codex Stop fires every turn. Skip a full finalize when
        # nothing material has happened since the last one, so the log is
        # not flooded with session_stop events. SessionEnd still always
        # runs (quality="always") and catches shell-only bypasses.
        events = session.events()
        last_final_i = max(
            (i for i, e in enumerate(events) if e.get("type") == "final_check"),
            default=-1,
        )
        last_material_i = max(
            (
                i
                for i, e in enumerate(events)
                if e.get("type")
                in (
                    "edit",
                    "gate",
                    "prompt",
                    "ack",
                    "plan_registered",
                    "plan_check",
                )
            ),
            default=-1,
        )
        if last_final_i > last_material_i:
            return {}
    session.append_event(
        "session_stop", status=str(payload.get("status") or payload.get("reason") or "")
    )
    ruleset = load_rules(root)

    run_quality = bool(ruleset.check_rules)
    if run_quality and quality == "if_new_edits":
        events = session.events()
        last_edit = max(
            (i for i, e in enumerate(events) if e.get("type") == "edit"),
            default=-1,
        )
        last_quality = max(
            (i for i, e in enumerate(events) if e.get("type") == "quality_check"),
            default=-1,
        )
        run_quality = last_edit > last_quality

    if run_quality:
        from archrev.drift import _is_outside_repo
        from archrev.quality import run_checks

        in_repo = [
            p for p in session.touched_files() if not _is_outside_repo(p)
        ]
        for result in run_checks(root, ruleset, in_repo):
            session.append_event("quality_check", **result)

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
    should_notify = bool(findings) and config.final_check and notify and not already_raised
    session.append_event(
        "final_check",
        findings=findings,
        fingerprint=fingerprint,
        notified=should_notify,
    )
    if should_notify:
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
    "exec_end": handle_exec_end,
    "finalize": handle_finalize,
}


def handle_pretool(root: Path, config: Config, payload: dict) -> dict:
    """Claude/Codex ``PreToolUse``: one event covers tool, path, shell, read, MCP."""
    payload = promote_payload(payload)
    outputs = [handle_gate(root, config, payload)]
    name = tool_name_of(payload)
    if is_shell_tool(name):
        outputs.append(handle_shell(root, config, payload))
    # Read rules run inside handle_gate for read tools; do not evaluate twice.
    if is_mcp_tool(name):
        outputs.append(handle_mcp(root, config, payload))
    return merge_hook_outputs(outputs)


def _dispatch(
    action: str, root: Path, config: Config, payload: dict
) -> dict:
    """Run one canonical action. ``payload`` is already parsed."""
    if action == "pretool":
        return handle_pretool(root, config, payload)
    if action == "stop":
        return handle_finalize(
            root, config, payload, notify=True, quality="if_new_edits"
        )
    if action == "session_end":
        return handle_finalize(
            root, config, payload, notify=False, quality="always"
        )
    if action == "edit":
        promoted = promote_payload(payload)
        name = tool_name_of(promoted)
        if not (is_shell_tool(name) or is_mcp_tool(name)):
            return handle_edit(root, config, promoted)
        # Native PostToolUse after a shell/MCP call closes its command
        # window. A shell-borne apply_patch also names files: record those
        # as edits, but never log a path-less command as a capture gap.
        handle_exec_end(root, config, promoted)
        if _paths_from_tool_input(
            promoted.get("tool_input") or promoted.get("toolInput")
        ):
            handle_edit(root, config, promoted)
        return {}
    if action == "exec_end":
        return handle_exec_end(root, config, promote_payload(payload))
    if action in ("gate", "shell", "read", "mcp"):
        return _HANDLERS[action](root, config, promote_payload(payload))
    handler = _HANDLERS.get(action)
    if handler is None:
        return {}
    return handler(root, config, payload)


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

    ``event`` is a canonical name (``prompt``, ``gate``, ...) when Cursor
    wires ``archrev hook <event>``, or ``auto`` when Claude/Codex wire a
    bare ``archrev hook`` and the payload carries ``hook_event_name``.

    Returns the JSON-serializable response for stdout. The gate fails open
    (``allow``) on internal errors — protection degrading is preferable to
    an editor that cannot save files; the error log and timeline keep the
    degradation visible.
    """
    root: Path | None = None
    payload: dict = {}
    parse_failed = False
    runtime = "cursor"
    action = event or "auto"
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
        auto = action in ("auto", "")
        if payload:
            runtime = detect_runtime(payload)
        if auto:
            mapped = map_event(
                str(
                    payload.get("hook_event_name")
                    or payload.get("hookEventName")
                    or ""
                )
            )
            if mapped is None:
                return {}
            action = mapped
        else:
            # Explicit CLI event: Cursor-shaped stdout, even if the payload
            # happens to carry a native hook_event_name (keeps existing
            # tests and Cursor wiring stable).
            runtime = "cursor"
        if root is None:
            return fail_open_response(runtime, action)
        if parse_failed or not payload:
            # The payload was empty or not a JSON object: record the raw
            # stdin head so the transport problem is diagnosable from the
            # session record alone.
            SessionStore(root).session("unknown").append_event(
                "payload_debug",
                hook=action,
                stdin_len=len(stdin_text),
                stdin_head=stdin_text[:400],
            )
        config = load_config(root)
        result = _dispatch(action, root, config, payload)
        if auto:
            return render_response(runtime, action, result, payload)
        return result
    except Exception as exc:  # noqa: BLE001 — hooks must never crash the editor
        _log_hook_error(root, action, exc)
        try:
            if root is not None:
                # Attribute the failure to the real session when the id is
                # known, so degradation shows up in that session's timeline.
                SessionStore(root).session(_session_id(payload)).append_event(
                    "hook_error", event=action, error=repr(exc)
                )
        except Exception:  # noqa: BLE001
            pass
        return fail_open_response(runtime, action)
