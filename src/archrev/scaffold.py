"""``archrev init``: install ArchRev into a repository.

Creates the ``.archrev`` data directory, wires Cursor hooks and the
always-apply agent rule, and installs the git commit-trailer hook. The
scaffold is merge-safe and idempotent: existing files are preserved,
existing ``hooks.json`` entries are extended rather than replaced, and a
foreign ``prepare-commit-msg`` hook is never overwritten (instructions are
printed instead).
"""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from archrev.config import ARCHREV_DIRNAME

_CONFIG_TEMPLATE = """\
# ArchRev configuration. Safe to edit; invalid values fall back to defaults.

# Edit-gate behavior:
#   on      - enforce rules as written (block rules pause for user approval)
#   monitor - record everything, downgrade blocks to flags (never stops)
#   off     - disable the gate entirely (session capture still runs)
enforcement: on

# When true, the first gated edit of a session is denied until the agent has
# registered a plan and run `archrev check plan` ("no validated plan, no code").
strict_plan_check: false

# When true, session finalization scans the full git diff against path rules,
# catching protected files changed outside the edit gate (e.g. via shell).
protected_scan: true

# When true, the end-of-session review sends the agent a follow-up message
# when material findings exist (gate bypasses, failed checks, out-of-plan
# drift), so sessions never end silently with open rule violations.
final_check: true

# Prompt privacy - how much of the user's prompt text enters the record:
#   full    - the whole prompt (default; best provenance)
#   excerpt - first 200 characters plus total length
#   none    - no text; only length and a content hash
# Prompts are the most sensitive artifact ArchRev stores. Decide this
# consciously before a team rollout.
prompt_capture: full

# Additional glob patterns the gate never blocks (extends built-in exemptions
# for .archrev/sessions/** and *.plan.md). Note: ArchRev governance files
# (.archrev/config.yaml, rules, .cursor/hooks.json) are intentionally NOT
# exempt - see rules/90-archrev-self-protection.yaml.
# exempt:
#   - docs/**
"""

_STARTER_RULES = """\
# ArchRev rules. Each YAML file in this directory may hold one rule or a list.
#
# Path rules (machine-enforced at edit time):
#   id:         unique identifier
#   kind:       path
#   match:      list of globs; `**` spans directories, patterns without `/`
#               match at any depth (like .gitignore). Case-insensitive.
#   action:     block - pause the edit for explicit user approval
#               flag  - allow, but record and highlight in the review
#   message:    shown to the user and the agent when the rule fires
#   applies_to: optional globs restricting the rule to a monorepo subtree
#   enabled:    default true
#
# Prompt rules (policies the agent must attest during `archrev check plan`):
#   id:      unique identifier
#   kind:    prompt
#   policy:  the policy text the agent attests with --attest <id>=pass|fail
#
# Further machine-enforced kinds (all support deny | block | flag):
#   kind: read   - globs over files the agent READS (e.g. secrets)
#   kind: shell  - `match_command` regexes over shell commands
#   kind: mcp    - `match_tool` regexes over MCP tool identifiers
#   kind: tool   - `match_tool` regexes over agent tool names
#
# Honesty note: these gate the agent's *attempts* at the tool layer and
# record everything - policy plus audit, not a sandbox. Use containers /
# network isolation underneath when hard containment is required.
#
# The starter rules below are DISABLED examples. Copy, adapt, enable.

- id: example-protect-migrations
  kind: path
  match: ["**/migrations/**"]
  action: block
  message: "Database migrations require explicit approval."
  enabled: false

- id: example-flag-infrastructure
  kind: path
  match: ["Dockerfile*", "docker-compose*.yml", ".gitlab-ci*.yml", "k8s/**"]
  action: flag
  message: "Infrastructure change - will be highlighted for review."
  enabled: false

- id: example-no-secret-reads
  kind: read
  match: ["dev-secrets/**", ".env", ".env.*"]
  action: deny
  message: "Agents may not read secrets or environment files."
  enabled: false

- id: example-no-push
  kind: shell
  match_command: ["\\bgit\\s+push\\b"]
  action: block
  message: "Pushing requires explicit approval."
  enabled: false

- id: example-flag-installs
  kind: shell
  match_command: ["\\b(pip|pip3|uv pip|npm|pnpm|yarn)\\s+(install|add)\\b"]
  action: flag
  message: "Dependency installation - recorded for review."
  enabled: false

- id: example-no-subagents
  kind: tool
  match_tool: ["^Task$"]
  action: deny
  message: "Subagents are not permitted in this repository."
  enabled: false

- id: example-api-rate-limit
  kind: prompt
  policy: "Every new API endpoint must specify rate limiting and authorization."
  enabled: false

# Check rules: quality gates run against the session's changed files at
# session end and by `archrev check diff` (CI). The command's exit code is
# the verdict; {files} is replaced with the matched changed files.
# action: block - failures become final-review findings and fail CI
#         flag  - failures are recorded and highlighted only

- id: example-endpoint-validation
  kind: check
  match: ["api/**/*.py"]
  command: "semgrep scan --config .archrev/checks/endpoints.yaml --error --quiet {files}"
  action: block
  message: "New/changed endpoints must validate input (semgrep)."
  enabled: false

- id: example-eslint
  kind: check
  match: ["src/**/*.{js,jsx,ts,tsx}"]
  command: "npx eslint --max-warnings 0 {files}"
  action: flag
  message: "Lint findings in changed frontend files."
  enabled: false
"""

_AGENT_PROTOCOL = """\
# ArchRev workflow (mandatory in this repository)

This repository records agent sessions with ArchRev. Follow this protocol:

1. **Register your plan before implementing.** After planning and before the
   first file edit, run exactly one of:
   - `archrev plan register <path-to-plan-file>`
   - `archrev plan register --text "<short plan: goal + files you will touch>"`
   Mention every file you expect to change; unplanned files are reported as
   drift in the session review.

2. **Check the plan against the rules.** Run `archrev rules` to see the
   active policies, evaluate each `prompt` rule against your plan honestly,
   then record the verdicts:
   `archrev check plan --attest <rule-id>=pass --attest <other-id>=fail ...`
   If any verdict is `fail`, stop and resolve it with the user before
   implementing.

3. **Respect the edit gate.** If an edit is paused or denied by ArchRev, do
   not work around it (e.g. by writing the file via shell commands) - the
   final diff scan reports bypasses. Ask the user or adjust the plan.

4. Never modify files under `.archrev/sessions/` - they are the audit record.
"""

_AGENT_RULE = (
    "---\n"
    "alwaysApply: true\n"
    "description: ArchRev session provenance and architecture-rule compliance\n"
    "---\n\n"
    + _AGENT_PROTOCOL
)

_AGENTS_MD = "# Agent instructions\n\n" + _AGENT_PROTOCOL

_SELF_PROTECTION_RULES = """\
# ArchRev self-protection (enabled by default - think twice before removing).
#
# The edit gate exempts only .archrev/sessions/** and plan files. These
# governance files stay gated so an agent cannot silently disable capture
# or enforcement by editing them; changes pause for your explicit approval.

- id: archrev-self-protection
  kind: path
  match:
    - ".archrev/config.yaml"
    - ".archrev/rules/**"
    - ".cursor/hooks.json"
    - ".cursor/rules/archrev.mdc"
    - ".claude/settings.json"
    - ".claude/rules/archrev.md"
    - ".codex/hooks.json"
    - "AGENTS.md"
  action: block
  message: >-
    ArchRev governance file - changing capture or enforcement configuration
    requires explicit user approval.
"""

_GIT_HOOK = """\
#!/bin/sh
# ArchRev: link commits to agent sessions via trailers. Fails open by design.
command -v {launcher} >/dev/null 2>&1 || exit 0
{prefix}git-trailer "$1" 2>/dev/null || exit 0
"""

#: Command prefixes per shim mode. "uvx" runs ArchRev through uv's tool
#: runner, which auto-installs it on first use: hooks committed to the
#: repository then work on every teammate's machine with zero setup
#: (uv itself being the only prerequisite).
_SHIM_PREFIX = {"none": "archrev ", "uvx": "uvx archrev "}

#: Recognizes our own entries in hooks.json regardless of shim mode, so
#: re-running init can upgrade between modes without duplicating entries.
_OURS_RE = re.compile(r"\barchrev(\.exe)?\s+(hook|git-trailer)\b")

#: Claude Code / Codex lifecycle events. One bare ``archrev hook`` command;
#: the payload's ``hook_event_name`` selects the handler.
_NATIVE_HOOK_SPECS: tuple[tuple[str, str | None, dict], ...] = (
    ("UserPromptSubmit", None, {}),
    ("PreToolUse", None, {}),
    ("PostToolUse", "Edit|Write|NotebookEdit|apply_patch", {}),
    ("Stop", None, {}),
    ("SessionEnd", None, {"timeout": 60}),
)

SUPPORTED_RUNTIMES = ("cursor", "claude", "codex")

#: Hook events wired by init: (event, archrev subcommand, extra entry keys).
#: preToolUse runs unmatched so `tool` rules can gate any tool; the handler
#: answers in milliseconds when nothing applies. afterTabFileEdit captures
#: human Tab-completion edits (tagged origin=tab in the audit log). The stop
#: hook carries loop_limit so the final rule review can send the agent one
#: follow-up without any risk of a notification loop.
_HOOK_SPECS: tuple[tuple[str, str, dict], ...] = (
    ("beforeSubmitPrompt", "prompt", {}),
    ("afterFileEdit", "edit", {}),
    ("afterTabFileEdit", "edit", {}),
    ("preToolUse", "gate", {}),
    ("beforeShellExecution", "shell", {}),
    ("beforeReadFile", "read", {}),
    ("beforeMCPExecution", "mcp", {}),
    ("stop", "finalize", {"loop_limit": 2}),
)


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _write_if_absent(path: Path, content: str, result: InitResult) -> None:
    rel = str(path)
    if path.exists():
        result.skipped.append(rel)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    result.created.append(rel)


def _merge_hooks_json(path: Path, result: InitResult, shim: str) -> None:
    """Add ArchRev entries to hooks.json, preserving everything else."""
    data: dict = {"version": 1, "hooks": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            result.warnings.append(
                f"{path}: existing file is not valid JSON; left untouched. "
                "Add the ArchRev hooks manually (see README)."
            )
            return
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, subcommand, extras in _HOOK_SPECS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            result.warnings.append(f"{path}: '{event}' is not a list; skipped.")
            continue
        command = f"{_SHIM_PREFIX[shim]}hook {subcommand}"
        desired: dict = {"command": command, **extras}
        existing = next(
            (
                e
                for e in entries
                if isinstance(e, dict)
                and _OURS_RE.search(str(e.get("command", "")))
            ),
            None,
        )
        if existing is None:
            entries.append(desired)
            changed = True
        elif existing != desired:
            # Upgrade our own entry in place (command/matcher may evolve
            # between versions); foreign entries are never touched.
            existing.clear()
            existing.update(desired)
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        result.created.append(str(path))
    else:
        result.skipped.append(str(path))


def _install_git_hook(root: Path, result: InitResult, shim: str) -> None:
    git_dir = root / ".git"
    if not git_dir.is_dir():
        result.warnings.append(
            "No .git directory: commit trailer hook not installed. "
            "Run `archrev init` again after `git init`."
        )
        return
    prefix = _SHIM_PREFIX[shim]
    content_desired = _GIT_HOOK.format(
        launcher=prefix.split()[0], prefix=prefix
    )
    hook_path = git_dir / "hooks" / "prepare-commit-msg"
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        if "archrev" not in content:
            result.warnings.append(
                f"{hook_path} already exists and is not ArchRev's. Append this "
                "line manually to keep commit linking:\n"
                f'  {prefix}git-trailer "$1" 2>/dev/null || true'
            )
            return
        if content == content_desired:
            result.skipped.append(str(hook_path))
            return
        # Ours but stale (e.g. shim mode changed): upgrade in place.
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(content_desired, encoding="utf-8", newline="\n")
    # Git-for-Windows runs hooks through sh and ignores the execute bit, but
    # POSIX systems need it.
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    result.created.append(str(hook_path))


def _merge_native_hooks(
    path: Path, result: InitResult, shim: str, *, kind: str
) -> None:
    """Merge ArchRev entries into Claude ``settings.json`` or Codex ``hooks.json``."""
    if kind == "codex":
        data: dict = {
            "description": "ArchRev lifecycle hooks (trust with /hooks).",
            "hooks": {},
        }
    else:
        data = {"hooks": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            result.warnings.append(
                f"{path}: existing file is not valid JSON; left untouched. "
                "Add the ArchRev hooks manually (see README)."
            )
            return
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        result.warnings.append(f"{path}: 'hooks' is not an object; skipped.")
        return
    command = f"{_SHIM_PREFIX[shim]}hook"
    changed = False
    for event, matcher, extras in _NATIVE_HOOK_SPECS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            result.warnings.append(f"{path}: '{event}' is not a list; skipped.")
            continue
        desired_hook: dict = {"type": "command", "command": command, **extras}
        existing_group: dict | None = None
        existing_hook: dict | None = None
        for group in groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for entry in inner:
                if isinstance(entry, dict) and _OURS_RE.search(
                    str(entry.get("command", ""))
                ):
                    existing_group = group
                    existing_hook = entry
                    break
            if existing_hook is not None:
                break
        if existing_hook is None:
            group = {"hooks": [desired_hook]}
            if matcher:
                group["matcher"] = matcher
            groups.append(group)
            changed = True
            continue
        if existing_hook != desired_hook:
            existing_hook.clear()
            existing_hook.update(desired_hook)
            changed = True
        if matcher and existing_group is not None:
            if existing_group.get("matcher") != matcher:
                existing_group["matcher"] = matcher
                changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        result.created.append(str(path))
    else:
        result.skipped.append(str(path))


def _ensure_agents_md(root: Path, result: InitResult) -> None:
    """Write AGENTS.md, or append the ArchRev protocol if it is missing."""
    path = root / "AGENTS.md"
    if not path.exists():
        _write_if_absent(path, _AGENTS_MD, result)
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.warnings.append(f"{path}: could not read ({exc})")
        return
    if "ArchRev" in text:
        result.skipped.append(str(path))
        return
    path.write_text(text.rstrip() + "\n\n" + _AGENT_PROTOCOL, encoding="utf-8", newline="\n")
    result.created.append(str(path))


def init_repo(
    root: Path,
    shim: str = "none",
    runtimes: tuple[str, ...] | None = None,
) -> InitResult:
    """Install ArchRev into ``root``; returns what was created/skipped.

    ``shim="uvx"`` writes hook commands as ``uvx archrev ...`` so teammates
    need no ArchRev installation at all — uv auto-installs it on first hook
    invocation. The wiring is committed with the repository, making team
    onboarding a plain ``git pull``.

    ``runtimes`` defaults to cursor + claude + codex. Repeat ``init`` with a
    subset to add or upgrade one runtime's wiring without touching others.
    """
    if shim not in _SHIM_PREFIX:
        raise ValueError(f"unknown shim {shim!r} (expected one of {list(_SHIM_PREFIX)})")
    selected = runtimes or SUPPORTED_RUNTIMES
    unknown = [r for r in selected if r not in SUPPORTED_RUNTIMES]
    if unknown:
        raise ValueError(f"unknown runtime(s) {unknown!r}")
    result = InitResult()
    base = root / ARCHREV_DIRNAME
    _write_if_absent(base / "config.yaml", _CONFIG_TEMPLATE, result)
    _write_if_absent(base / "rules" / "00-starter-rules.yaml", _STARTER_RULES, result)
    _write_if_absent(
        base / "rules" / "90-archrev-self-protection.yaml",
        _SELF_PROTECTION_RULES,
        result,
    )
    # Keep the sessions directory present so its purpose is discoverable.
    sessions_dir = base / "sessions"
    if not sessions_dir.exists():
        sessions_dir.mkdir(parents=True)
        (sessions_dir / ".gitkeep").write_text("", encoding="utf-8")
        result.created.append(str(sessions_dir))
    if "cursor" in selected:
        _merge_hooks_json(root / ".cursor" / "hooks.json", result, shim)
        _write_if_absent(root / ".cursor" / "rules" / "archrev.mdc", _AGENT_RULE, result)
    if "claude" in selected:
        _merge_native_hooks(
            root / ".claude" / "settings.json", result, shim, kind="claude"
        )
        _write_if_absent(
            root / ".claude" / "rules" / "archrev.md", _AGENT_PROTOCOL, result
        )
    if "codex" in selected:
        _merge_native_hooks(
            root / ".codex" / "hooks.json", result, shim, kind="codex"
        )
        _ensure_agents_md(root, result)
    _install_git_hook(root, result, shim)
    return result
