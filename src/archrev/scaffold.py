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

# Additional glob patterns the gate never blocks (extends built-in exemptions
# for .archrev/**, .cursor/**, and *.plan.md).
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

- id: example-api-rate-limit
  kind: prompt
  policy: "Every new API endpoint must specify rate limiting and authorization."
  enabled: false
"""

_AGENT_RULE = """\
---
alwaysApply: true
description: ArchRev session provenance and architecture-rule compliance
---

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

_GIT_HOOK = """\
#!/bin/sh
# ArchRev: link commits to agent sessions via trailers. Fails open by design.
command -v archrev >/dev/null 2>&1 || exit 0
archrev git-trailer "$1" 2>/dev/null || exit 0
"""

#: Hook events wired by init: (event, archrev subcommand, matcher or None).
_HOOK_SPECS: tuple[tuple[str, str, str | None], ...] = (
    ("beforeSubmitPrompt", "prompt", None),
    ("afterFileEdit", "edit", None),
    ("preToolUse", "gate", "Write|StrReplace|Edit|MultiEdit|SearchReplace"),
    ("stop", "finalize", None),
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


def _merge_hooks_json(path: Path, result: InitResult) -> None:
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
    for event, subcommand, matcher in _HOOK_SPECS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            result.warnings.append(f"{path}: '{event}' is not a list; skipped.")
            continue
        command = f"archrev hook {subcommand}"
        if any(
            isinstance(e, dict) and str(e.get("command", "")).startswith("archrev hook")
            for e in entries
        ):
            continue
        entry: dict = {"command": command}
        if matcher:
            entry["matcher"] = matcher
        entries.append(entry)
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


def _install_git_hook(root: Path, result: InitResult) -> None:
    git_dir = root / ".git"
    if not git_dir.is_dir():
        result.warnings.append(
            "No .git directory: commit trailer hook not installed. "
            "Run `archrev init` again after `git init`."
        )
        return
    hook_path = git_dir / "hooks" / "prepare-commit-msg"
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        if "archrev" in content:
            result.skipped.append(str(hook_path))
        else:
            result.warnings.append(
                f"{hook_path} already exists and is not ArchRev's. Append this "
                "line manually to keep commit linking:\n"
                '  archrev git-trailer "$1" 2>/dev/null || true'
            )
        return
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(_GIT_HOOK, encoding="utf-8", newline="\n")
    # Git-for-Windows runs hooks through sh and ignores the execute bit, but
    # POSIX systems need it.
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    result.created.append(str(hook_path))


def init_repo(root: Path) -> InitResult:
    """Install ArchRev into ``root``; returns what was created/skipped."""
    result = InitResult()
    base = root / ARCHREV_DIRNAME
    _write_if_absent(base / "config.yaml", _CONFIG_TEMPLATE, result)
    _write_if_absent(base / "rules" / "00-starter-rules.yaml", _STARTER_RULES, result)
    # Keep the sessions directory present so its purpose is discoverable.
    sessions_dir = base / "sessions"
    if not sessions_dir.exists():
        sessions_dir.mkdir(parents=True)
        (sessions_dir / ".gitkeep").write_text("", encoding="utf-8")
        result.created.append(str(sessions_dir))
    _merge_hooks_json(root / ".cursor" / "hooks.json", result)
    _write_if_absent(root / ".cursor" / "rules" / "archrev.mdc", _AGENT_RULE, result)
    _install_git_hook(root, result)
    return result
