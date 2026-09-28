"""Runtime adapters: Cursor / Claude Code / Codex hook I/O.

Handlers stay runtime-agnostic (canonical events and a Cursor-shaped
decision dict). This module maps each runtime's ``hook_event_name`` onto
those events, promotes nested ``tool_input`` fields so the handlers can
read them, extracts paths from ``apply_patch`` payloads, and renders the
stdout JSON each runtime actually honors.

Honesty notes, encoded as behavior not comments-in-README-only:

- Claude Code ``Stop`` fires every turn; ``SessionEnd`` is the real
  session close. Finalize-with-nudge maps to Stop; durable manifest maps
  to SessionEnd.
- Codex ``permissionDecision: "ask"`` is parsed but not honored (the
  hook is marked failed and the tool proceeds). Block rules therefore
  render as ``deny`` on Codex so they cannot fail-open.
"""

from __future__ import annotations

import os
import re

#: Canonical actions the dispatcher understands, including the two native
#: fan-out modes that have no Cursor CLI equivalent.
CANONICAL = (
    "prompt",
    "edit",
    "gate",
    "shell",
    "read",
    "mcp",
    "exec_end",
    "finalize",
    "pretool",
    "stop",
    "session_end",
)

#: Cursor hook event names (from ``.cursor/hooks.json``).
CURSOR_EVENTS = {
    "beforeSubmitPrompt": "prompt",
    "afterFileEdit": "edit",
    "afterTabFileEdit": "edit",
    "preToolUse": "gate",
    "beforeShellExecution": "shell",
    "afterShellExecution": "exec_end",
    "beforeReadFile": "read",
    "beforeMCPExecution": "mcp",
    "afterMCPExecution": "exec_end",
    "stop": "finalize",
}

#: Claude Code and Codex share these lifecycle names.
NATIVE_EVENTS = {
    "UserPromptSubmit": "prompt",
    "PreToolUse": "pretool",
    "PostToolUse": "edit",
    "Stop": "stop",
    "SessionEnd": "session_end",
}

_EVENT_MAP = {**CURSOR_EVENTS, **NATIVE_EVENTS}

# PowerShell/pwsh: Claude Code on Windows ships a PowerShell tool next to
# Bash. Missing it let every command there bypass shell rules, read rules,
# and command attribution (found in a live read test).
_SHELL_TOOL_RE = re.compile(
    r"^(bash|shell|exec_command|powershell|pwsh)$", re.IGNORECASE
)
_POWERSHELL_TOOL_RE = re.compile(r"^(powershell|pwsh)$", re.IGNORECASE)
# Grep returns file contents, so a secret-read deny must cover it. A grep
# with no path still searches the workspace; that is the documented
# "not a sandbox" limit, not something a path rule can see.
_READ_TOOL_RE = re.compile(r"^(read|read_file|grep)$", re.IGNORECASE)
_MCP_TOOL_RE = re.compile(r"(^mcp__)|(^mcp$)", re.IGNORECASE)
_PATCH_FILE_RE = re.compile(
    r"(?m)^\*\*\* (?:Add|Update|Delete|Move|Rename) File: (.+)$"
)
_PERM_RANK = {"allow": 0, "ask": 1, "deny": 2}


def map_event(name: str | None) -> str | None:
    """Map a runtime hook event name to a canonical action, if known."""
    if not name:
        return None
    return _EVENT_MAP.get(name)


def detect_runtime(payload: dict) -> str:
    """Best-effort runtime id for ``meta.json`` and response rendering.

    Cursor events are unambiguous. Claude Code and Codex share event
    names; Codex is identified by ``turn_id`` / ``thr_`` session ids,
    Claude by ``prompt_id`` or ``CLAUDE_PROJECT_DIR``. Unknown native
    payloads default to Claude (``ask`` works there). Codex detection
    is biased toward false positives rather than false negatives so
    block rules cannot fail-open.
    """
    name = str(
        payload.get("hook_event_name") or payload.get("hookEventName") or ""
    )
    if name in CURSOR_EVENTS:
        return "cursor"
    if name in NATIVE_EVENTS:
        session_id = str(payload.get("session_id") or "")
        if (
            payload.get("turn_id")
            or session_id.startswith("thr_")
            or os.environ.get("CODEX_HOME")
            or os.environ.get("CODEX_THREAD_ID")
        ):
            return "codex"
        if payload.get("prompt_id") or os.environ.get("CLAUDE_PROJECT_DIR"):
            return "claude"
        return "claude"
    return "cursor"


def runtime_supports_ask(runtime: str) -> bool:
    """False on Codex: returning ``ask`` there fails the hook and allows the tool."""
    return runtime != "codex"


def tool_name_of(payload: dict) -> str:
    return str(payload.get("tool_name") or payload.get("tool") or "")


def is_shell_tool(name: str) -> bool:
    return bool(_SHELL_TOOL_RE.match(name.strip()))


def is_powershell_tool(name: str) -> bool:
    return bool(_POWERSHELL_TOOL_RE.match(name.strip()))


def is_read_tool(name: str) -> bool:
    return bool(_READ_TOOL_RE.match(name.strip()))


def is_mcp_tool(name: str) -> bool:
    return bool(_MCP_TOOL_RE.search(name.strip()))


def patch_paths(command: str) -> list[str]:
    """File paths named in an ``apply_patch`` / V4A patch body."""
    if not command or "***" not in command:
        return []
    found: dict[str, None] = {}
    for match in _PATCH_FILE_RE.finditer(command):
        path = match.group(1).strip()
        if path:
            found.setdefault(path, None)
    return list(found)


def promote_payload(payload: dict) -> dict:
    """Copy nested ``tool_input`` fields to the top level handlers already read.

    Cursor puts ``command`` / ``file_path`` at the top level of some events
    and inside ``tool_input`` of others. Claude and Codex always nest them.
    Promoting once keeps the handlers runtime-agnostic.
    """
    out = dict(payload)
    raw_input = payload.get("tool_input") or payload.get("toolInput")
    if not isinstance(raw_input, dict):
        return out
    promoted = dict(raw_input)
    command = raw_input.get("command")
    if isinstance(command, str):
        if "command" not in out:
            out["command"] = command
        paths = patch_paths(command)
        if paths:
            promoted.setdefault("file_path", paths[0])
            existing = promoted.get("paths")
            merged = list(paths)
            if isinstance(existing, list):
                for item in existing:
                    if isinstance(item, str) and item.strip():
                        merged.append(item.strip())
            # de-dupe, preserve order
            seen: dict[str, None] = {}
            for path in merged:
                seen.setdefault(path, None)
            promoted["paths"] = list(seen)
            if "file_path" not in out:
                out["file_path"] = paths[0]
    for key in (
        "file_path",
        "path",
        "target_file",
        "filePath",
        "targetFile",
        "file",
        "notebook_path",
    ):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip() and key not in out:
            out[key] = value.strip()
    out["tool_input"] = promoted
    if "toolInput" in out:
        out["toolInput"] = promoted
    return out


def merge_hook_outputs(outputs: list[dict]) -> dict:
    """Keep the strictest permission; concatenate messages."""
    if not outputs:
        return {}
    best_rank = -1
    best: dict = {}
    user_parts: list[str] = []
    agent_parts: list[str] = []
    saw_permission = False
    for out in outputs:
        if not out:
            continue
        perm = str(out.get("permission") or "allow")
        saw_permission = True
        rank = _PERM_RANK.get(perm, 0)
        if rank > best_rank:
            best = dict(out)
            best_rank = rank
        if out.get("user_message"):
            user_parts.append(str(out["user_message"]))
        if out.get("agent_message"):
            agent_parts.append(str(out["agent_message"]))
    if not saw_permission:
        return {}
    if user_parts:
        best["user_message"] = "\n".join(user_parts)
    if agent_parts:
        best["agent_message"] = "\n".join(agent_parts)
    best.setdefault("permission", "allow")
    return best


def render_response(
    runtime: str, action: str, result: dict, payload: dict
) -> dict:
    """Translate a canonical handler result into runtime-specific stdout JSON."""
    result = result or {}
    if runtime == "cursor":
        return result

    event_name = str(
        payload.get("hook_event_name") or payload.get("hookEventName") or ""
    )

    if action in ("stop", "finalize") and result.get("followup_message"):
        if event_name == "Stop" or action == "stop":
            # Claude/Codex: decision=block on Stop continues the turn with
            # ``reason`` as the next user prompt — the analogue of Cursor's
            # followup_message.
            return {
                "decision": "block",
                "reason": str(result["followup_message"]),
            }
        return {}

    if action in ("session_end", "prompt", "edit"):
        return {}

    if action != "pretool" and event_name != "PreToolUse":
        return result if result else {}

    perm = str(result.get("permission") or "allow")
    # Claude/Codex show a deny reason to the agent and an ask reason to the
    # user, so each gets the message written for its reader. The rule lines
    # (which rule, which path) live in the user message; a deny keeps them
    # after the agent guidance so the agent knows exactly what was refused.
    if perm == "deny":
        reason = "\n".join(
            str(part)
            for part in (result.get("agent_message"), result.get("user_message"))
            if part
        )
    else:
        reason = str(
            result.get("user_message") or result.get("agent_message") or ""
        )
    if perm == "ask" and not runtime_supports_ask(runtime):
        perm = "deny"
        reason = (
            (reason + "\n") if reason else ""
        ) + (
            "Codex cannot prompt for approval, so ArchRev enforces this "
            "block rule as deny. Ask the user, then retry; do not work around it."
        )
    if perm == "allow":
        extra = str(result.get("agent_message") or "")
        if not extra:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": extra,
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": perm,
            "permissionDecisionReason": reason or "ArchRev policy",
        }
    }


def fail_open_response(runtime: str, action: str) -> dict:
    """Permissive answer when the hook itself crashed. Never lock the editor."""
    if runtime == "cursor" and action in (
        "gate",
        "shell",
        "read",
        "mcp",
        "pretool",
    ):
        return {"permission": "allow"}
    return {}
