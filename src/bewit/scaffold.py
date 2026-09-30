"""``bewit init``: install Bewit into a repository.

Creates the ``.bewit`` data directory, wires Cursor hooks and the
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

from bewit.config import BEWIT_DIRNAME, load_config

_CONFIG_TEMPLATE = """\
# Bewit configuration. Safe to edit; invalid values fall back to defaults.

# Edit-gate behavior:
#   on      - enforce rules as written (block rules pause for user approval)
#   monitor - record everything, downgrade blocks to flags (never stops)
#   off     - disable the gate entirely (session capture still runs)
enforcement: on

# When true, the first gated edit of a session is denied until the agent has
# registered a plan and run `bewit check plan` ("no validated plan, no code").
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
#   sealed  - ciphertext for the public keys in .bewit/recipients.yaml
#             (bewit seal keygen). Falls back to `none` if sealing
#             cannot run — never stores plaintext by accident.
# Prompts are the most sensitive artifact Bewit stores. Decide this
# consciously before a team rollout.
prompt_capture: full

# What of the session record is committed with the repository
# (re-run `bewit init` after changing it; it rewrites .gitignore):
#   audit - one file per session: manifest.json (summary, plan, review,
#           and the event-log chain head). The event log stays on the
#           machine that wrote it.
#   full  - events.jsonl + meta.json + manifest.json: the full evidence,
#           including every prompt your capture mode kept.
# plan.md and report.md are never committed; `bewit show` renders them.
record_scope: audit

# Where those committed files live:
#   ref  - on git ref refs/bewit/records, never in a code commit or a
#          merge-request diff. Shared by `bewit sync` (the pre-push hook
#          runs it); CI and hubs fetch that one ref.
#   tree - in .bewit/sessions/, committed with the code.
record_store: ref

# Additional glob patterns the gate never blocks (extends built-in exemptions
# for .bewit/sessions/** and *.plan.md). Note: Bewit governance files
# (.bewit/config.yaml, rules, .cursor/hooks.json) are intentionally NOT
# exempt - see rules/90-bewit-self-protection.yaml.
# exempt:
#   - docs/**
"""

_STARTER_RULES = """\
# Bewit rules. Each YAML file in this directory may hold one rule or a list.
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
# Prompt rules (policies the agent must attest during `bewit check plan`):
#   id:      unique identifier
#   kind:    prompt
#   policy:  the policy text the agent attests with --attest <id>=pass|fail
#
# Further machine-enforced kinds (all support deny | block | flag):
#   kind: read   - globs over files the agent READS (e.g. secrets)
#   kind: shell  - `match_command` regexes over shell commands
#   kind: mcp    - `match_tool` regexes over MCP tool identifiers
#   kind: tool   - `match_tool` regexes over agent tool names
#   Write regexes in 'single quotes': YAML keeps backslashes literal there,
#   while "double quotes" treat \\s, \\d, ... as (invalid) escape sequences.
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

- id: example-protect-secret-files
  kind: path
  match: ["dev-secrets/**", ".env", ".env.*"]
  action: block
  message: "Writing secrets or environment files requires explicit approval."
  enabled: false

- id: example-no-push
  kind: shell
  match_command: ['\\bgit\\s+push\\b']
  action: block
  message: "Pushing requires explicit approval."
  enabled: false

- id: example-flag-installs
  kind: shell
  match_command: ['\\b(pip|pip3|uv pip|npm|pnpm|yarn)\\s+(install|add)\\b']
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
# session end and by `bewit check diff` (CI). The command's exit code is
# the verdict; {files} is replaced with the matched changed files.
# action: block - failures become final-review findings and fail CI
#         flag  - failures are recorded and highlighted only

- id: example-endpoint-validation
  kind: check
  match: ["api/**/*.py"]
  command: "semgrep scan --config .bewit/checks/endpoints.yaml --error --quiet {files}"
  action: block
  message: "New/changed endpoints must validate input (semgrep)."
  enabled: false

- id: example-python-syntax
  kind: check
  match: ["**/*.py"]
  command: python -m bewit.syntaxcheck {files}
  action: flag
  message: "Changed Python files must parse (no bytecode written)."
  enabled: false

- id: example-eslint
  kind: check
  match: ["src/**/*.js", "src/**/*.jsx", "src/**/*.ts", "src/**/*.tsx"]  # no {a,b} braces
  command: "npx eslint --max-warnings 0 {files}"
  action: flag
  message: "Lint findings in changed frontend files."
  enabled: false
"""

_AGENT_PROTOCOL = """\
# Bewit workflow (mandatory in this repository)

This repository records agent sessions with Bewit. Follow this protocol:

1. **Register your plan before implementing.** After planning and before the
   first file edit, run one of:
   - `bewit plan register <path-to-plan-file>`
   - `bewit plan register --text "<short plan: goal + files you will touch>"`
   Mention every file you expect to change by its full repository path;
   unplanned files are reported as drift in the session review. Fix any
   path the command reports as not found. For each later step in the same
   session, register again: the new files are added to the earlier plan
   (`--replace` starts over).

2. **Check the plan against the rules.** Run `bewit rules` to see the
   active policies, evaluate each `prompt` rule against your plan honestly,
   then record the verdicts:
   `bewit check plan --attest <rule-id>=pass --attest <other-id>=fail ...`
   Verdicts carry over for policies whose part of the plan did not change;
   attest the ones the check lists as not attested. If any verdict is
   `fail`, stop and resolve it with the user before implementing.

3. **Respect the edit gate.** If an edit is paused or denied by Bewit, do
   not work around it (e.g. by writing the file via shell commands) - the
   final diff scan reports bypasses. Ask the user or adjust the plan.

4. Never modify files under `.bewit/sessions/` - they are the audit record.
"""

_AGENT_RULE = (
    "---\n"
    "alwaysApply: true\n"
    "description: Bewit session provenance and architecture-rule compliance\n"
    "---\n\n"
    + _AGENT_PROTOCOL
)

_AGENTS_MD = "# Agent instructions\n\n" + _AGENT_PROTOCOL

_SELF_PROTECTION_RULES = """\
# Bewit self-protection (enabled by default - think twice before removing).
#
# The edit gate exempts only .bewit/sessions/** and plan files. These
# governance files stay gated so an agent cannot silently disable capture
# or enforcement by editing them; changes pause for your explicit approval.

- id: bewit-self-protection
  kind: path
  match:
    - ".bewit/config.yaml"
    - ".bewit/rules/**"
    - ".cursor/hooks.json"
    - ".cursor/rules/bewit.mdc"
    - ".claude/settings.json"
    - ".claude/settings.local.json"   # can set disableAllHooks
    - ".claude/rules/bewit.md"
    - ".codex/hooks.json"
    - "AGENTS.md"
  action: block
  message: >-
    Bewit governance file - changing capture or enforcement configuration
    requires explicit user approval.

# Deleting or moving the record itself is a shell act, which path rules do
# not see (they gate edits). This pauses it for approval instead.
- id: bewit-no-record-deletion
  kind: shell
  match_command:
    - '(\\brm\\b|\\brmdir\\b|\\bdel\\b|\\berase\\b|\\brd\\b|remove-item|\\bri\\b|\\bgit\\s+rm\\b|\\bmv\\b|\\bmove\\b|move-item|\\bunlink\\b|shutil\\.rmtree)[^|;&\\n]*\\.bewit'
  action: block
  message: >-
    Deleting or moving .bewit/ (or anything in it) removes Bewit's
    configuration, rules, or audit record and requires explicit approval.

# Acknowledging a finding is the reviewer's decision. An acknowledgment made
# while an agent command runs is recorded as agent-made and does not clear
# bypasses or failed checks; this rule stops the attempt up front. The second
# pattern matches an HTTP client calling the viewer's ack endpoint, not a
# plan or commit message that merely mentions it.
- id: bewit-no-agent-ack
  kind: shell
  match_command:
    - '\\bbewit(\\.exe)?\\s+ack\\b'
    - '(\\bcurl\\b|\\bwget\\b|invoke-webrequest|invoke-restmethod|\\biwr\\b|\\birm\\b|requests\\.|urllib|httpx\\.|aiohttp|\\bfetch\\s*\\()[^|;&\\n]*/api/ack\\b'
  action: deny
  message: >-
    Only the reviewer acknowledges Bewit findings. Report the findings to
    the user; they acknowledge legitimate ones from their own terminal or the
    viewer.
"""

_GIT_HOOK = """\
#!/bin/sh
# Bewit: link commits to agent sessions via trailers. Fails open by design.
command -v {launcher} >/dev/null 2>&1 || exit 0
{prefix}git-trailer "$1" 2>/dev/null || exit 0
"""

#: Command prefixes per shim mode. "uvx" runs Bewit through uv's tool
#: runner, which auto-installs it on first use: hooks committed to the
#: repository then work on every teammate's machine with zero setup
#: (uv itself being the only prerequisite).
_SHIM_PREFIX = {"none": "bewit ", "uvx": "uvx bewit "}

#: Recognizes our own entries in hooks.json regardless of shim mode, so
#: re-running init can upgrade between modes without duplicating entries.
_OURS_RE = re.compile(r"\bbewit(\.exe)?\s+(hook|git-trailer)\b")

#: Claude Code / Codex lifecycle events. One bare ``bewit hook`` command;
#: the payload's ``hook_event_name`` selects the handler. PostToolUse also
#: matches shell and MCP tools: it closes the session's command window,
#: which keeps changes made by other sessions out of this session's review.
_NATIVE_HOOK_SPECS: tuple[tuple[str, str | None, dict], ...] = (
    ("UserPromptSubmit", None, {}),
    ("PreToolUse", None, {}),
    (
        "PostToolUse",
        "Edit|Write|NotebookEdit|apply_patch|Bash|PowerShell|pwsh|shell|exec_command|mcp__.*",
        {},
    ),
    ("Stop", None, {}),
    ("SessionEnd", None, {"timeout": 60}),
)

SUPPORTED_RUNTIMES = ("cursor", "claude", "codex")

#: Hook events wired by init: (event, bewit subcommand, extra entry keys).
#: preToolUse runs unmatched so `tool` rules can gate any tool; the handler
#: answers in milliseconds when nothing applies. afterTabFileEdit captures
#: human Tab-completion edits (tagged origin=tab in the audit log). The stop
#: hook carries loop_limit so the final rule review can send the agent one
#: follow-up without any risk of a notification loop. The after-shell/MCP
#: hooks close the session's command window (see bewit.worktree).
_HOOK_SPECS: tuple[tuple[str, str, dict], ...] = (
    ("beforeSubmitPrompt", "prompt", {}),
    ("afterFileEdit", "edit", {}),
    ("afterTabFileEdit", "edit", {}),
    ("preToolUse", "gate", {}),
    ("beforeShellExecution", "shell", {}),
    ("afterShellExecution", "exec_end", {}),
    ("beforeReadFile", "read", {}),
    ("beforeMCPExecution", "mcp", {}),
    ("afterMCPExecution", "exec_end", {}),
    ("stop", "finalize", {"loop_limit": 2}),
)


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_EVENTS_GITIGNORE_LINE = ".bewit/sessions/*/events.jsonl"

#: Local runtime files. Never part of the audit record.
_RUNTIME_GITIGNORE_LINES = (
    ".bewit/hook-errors.log",
    ".bewit/fingerprint-cache.json",
    ".bewit/locks/",
)


#: Session files committed per ``record_scope``. Everything else in a
#: session directory stays local. ``plan.md`` and ``report.md`` are derived
#: and rendered on demand (``bewit show``), so they are never committed;
#: the manifest carries the plan text and the review. Audit scope commits
#: one small file per session; full scope adds the evidence itself.
COMMITTED_SESSION_FILES: dict[str, tuple[str, ...]] = {
    "audit": ("manifest.json",),
    "full": ("events.jsonl", "meta.json", "manifest.json"),
}
_SESSION_FILES = ("events.jsonl", "meta.json", "manifest.json", "plan.md", "report.md")
_RECORDS_BEGIN = "# >>> bewit session records (managed by `bewit init`; change record_scope, not this block) >>>"
_RECORDS_END = "# <<< bewit session records <<<"
#: Lines written by earlier versions for the audit scope, replaced by the block.
_LEGACY_RECORD_LINES = (
    _EVENTS_GITIGNORE_LINE,
    "# Bewit audit tier: the full event log stays on the authoring machine.",
    "# Manifests, plans, and reports under .bewit/sessions/ stay committed.",
)

#: Session records are collapsed in GitHub and GitLab merge-request diffs.
_GITATTRIBUTES_LINE = ".bewit/sessions/** linguist-generated=true gitlab-generated=true"


def _append_gitignore_lines(
    root: Path,
    lines: tuple[str, ...],
    comment: str,
    result: InitResult,
    filename: str = ".gitignore",
) -> None:
    """Append any of ``lines`` that are not already in ``filename``."""
    path = root / filename
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        result.warnings.append(f"{path}: could not read ({exc})")
        return
    present = set(existing.splitlines())
    missing = [line for line in lines if line not in present]
    if not missing:
        result.skipped.append(str(path))
        return
    block = comment.rstrip() + "\n" + "\n".join(missing) + "\n"
    body = existing.rstrip()
    try:
        path.write_text(
            (body + "\n\n" if body else "") + block, encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        result.warnings.append(f"{path}: could not write ({exc})")
        return
    result.created.append(str(path))


def _ensure_runtime_gitignore(root: Path, result: InitResult) -> None:
    """Keep hook locks, the fingerprint cache, and the error log untracked."""
    _append_gitignore_lines(
        root,
        _RUNTIME_GITIGNORE_LINES,
        "# Bewit runtime files (not part of the session record).",
        result,
    )


def records_gitignore_block(scope: str, store: str = "tree") -> str:
    """The managed ``.gitignore`` block for a record scope and store."""
    keep = COMMITTED_SESSION_FILES.get(scope, COMMITTED_SESSION_FILES["full"])
    if store == "ref":
        return "\n".join([
            _RECORDS_BEGIN,
            f"# record_store: ref. Session records ({', '.join(keep)} per session) are",
            "# published to refs/bewit/records, never committed with code: bewit sync.",
            ".bewit/sessions/",
            _RECORDS_END,
        ])
    ignored = [f".bewit/sessions/*/{name}" for name in _SESSION_FILES if name not in keep]
    return "\n".join([
        _RECORDS_BEGIN,
        f"# record_scope: {scope}. Committed per session: {', '.join(keep)}.",
        *ignored,
        _RECORDS_END,
    ])


def _ensure_records_gitignore(root: Path, result: InitResult) -> None:
    """Write the managed session-records block for the current record scope.

    The block is replaced wholesale on every ``init``, so switching
    ``record_scope`` (either way) takes effect. Lines earlier versions
    wrote for the audit scope are removed; they are superseded.
    Already-tracked files stay tracked: ``.gitignore`` never untracks.
    """
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        result.warnings.append(f"{path}: could not read ({exc})")
        return
    start, end = existing.find(_RECORDS_BEGIN), existing.find(_RECORDS_END)
    body = existing
    if start != -1 and end != -1:
        body = existing[:start] + existing[end + len(_RECORDS_END):]
    kept = [line for line in body.splitlines() if line.strip() not in _LEGACY_RECORD_LINES]
    text = "\n".join(kept).rstrip()
    config = load_config(root)
    block = records_gitignore_block(config.record_scope, config.record_store)
    desired = (text + "\n\n" if text else "") + block + "\n"
    if desired == existing:
        result.skipped.append(str(path))
        return
    try:
        path.write_text(desired, encoding="utf-8", newline="\n")
    except OSError as exc:
        result.warnings.append(f"{path}: could not write ({exc})")
        return
    result.created.append(str(path))


def _ensure_gitattributes(root: Path, result: InitResult) -> None:
    """Mark session records as generated, so MR diffs collapse them."""
    _append_gitignore_lines(
        root,
        (_GITATTRIBUTES_LINE,),
        "# Bewit session records: collapsed in GitHub/GitLab merge-request diffs.",
        result,
        filename=".gitattributes",
    )


def _write_if_absent(path: Path, content: str, result: InitResult) -> None:
    rel = str(path)
    if path.exists():
        result.skipped.append(rel)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    result.created.append(rel)


def _merge_hooks_json(path: Path, result: InitResult, shim: str) -> None:
    """Add Bewit entries to hooks.json, preserving everything else."""
    data: dict = {"version": 1, "hooks": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            result.warnings.append(
                f"{path}: existing file is not valid JSON; left untouched. "
                "Add the Bewit hooks manually (see README)."
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
            "Run `bewit init` again after `git init`."
        )
        return
    prefix = _SHIM_PREFIX[shim]
    content_desired = _GIT_HOOK.format(
        launcher=prefix.split()[0], prefix=prefix
    )
    hook_path = git_dir / "hooks" / "prepare-commit-msg"
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        if "bewit" not in content:
            result.warnings.append(
                f"{hook_path} already exists and is not Bewit's. Append this "
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


_PRE_PUSH_HOOK = """\
#!/bin/sh
# Bewit: push session records (refs/bewit/records) along with your
# branch. Fails open by design: it never blocks or alters your push.
[ -n "$BEWIT_IN_PRE_PUSH" ] && exit 0
command -v {launcher} >/dev/null 2>&1 || exit 0
BEWIT_IN_PRE_PUSH=1 {prefix}sync --remote "$1" --quiet >/dev/null 2>&1 || true
exit 0
"""


def _install_pre_push_hook(root: Path, result: InitResult, shim: str) -> None:
    """``record_store: ref``: share the records ref on every ``git push``."""
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return
    prefix = _SHIM_PREFIX[shim]
    desired = _PRE_PUSH_HOOK.format(launcher=prefix.split()[0], prefix=prefix)
    hook_path = git_dir / "hooks" / "pre-push"
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        if "bewit" not in content:
            result.warnings.append(
                f"{hook_path} already exists and is not Bewit's. Session records "
                "are not pushed automatically; run `bewit sync` after pushing, or "
                "add `bewit sync --remote \"$1\" --quiet || true` to that hook."
            )
            return
        if content == desired:
            result.skipped.append(str(hook_path))
            return
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(desired, encoding="utf-8", newline="\n")
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    result.created.append(str(hook_path))


def _set_config_value(path: Path, key: str, value: str, result: InitResult) -> None:
    """Set a top-level ``key: value`` in config.yaml, keeping comments."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        result.warnings.append(f"{path}: could not read ({exc})")
        return
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            if line.split(":", 1)[1].strip() == value:
                return
            lines[i] = f"{key}: {value}"
            replaced = True
            break
    if not replaced:
        lines += ["", f"{key}: {value}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    result.created.append(str(path))


def _merge_native_hooks(
    path: Path, result: InitResult, shim: str, *, kind: str
) -> None:
    """Merge Bewit entries into Claude ``settings.json`` or Codex ``hooks.json``."""
    if kind == "codex":
        data: dict = {
            "description": "Bewit lifecycle hooks (trust with /hooks).",
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
                "Add the Bewit hooks manually (see README)."
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


def _disabled_hook_settings(root: Path, home: Path | None) -> list[str]:
    """Claude Code settings files that turn every hook off.

    ``disableAllHooks`` in the project's local settings or the user's own
    settings silences Bewit without touching a governed file, and a
    session that never runs a hook leaves no record to review. Only the
    person running the agent can see this, so ``bewit rules`` and the
    viewer say so. Read-only; never raises.
    """
    candidates = [
        (".claude/settings.json", root / ".claude" / "settings.json"),
        (".claude/settings.local.json", root / ".claude" / "settings.local.json"),
    ]
    try:
        user_dir = (home if home is not None else Path.home()) / ".claude"
        candidates.append(("~/.claude/settings.json", user_dir / "settings.json"))
    except RuntimeError:  # no resolvable home directory
        pass
    found: list[str] = []
    for label, path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("disableAllHooks") is True:
            found.append(
                f"{label}: disableAllHooks is true, so Claude Code runs no "
                "Bewit hook (nothing is gated or recorded)"
            )
    return found


def wiring_problems(root: Path, home: Path | None = None) -> list[str]:
    """Differences between the installed Claude/Codex wiring and this version.

    Wiring is written once by ``bewit init``; upgrading Bewit does not
    touch it. Stale wiring degrades silently (an outdated PostToolUse
    matcher leaves every shell command window open until the turn ends,
    blurring attribution), so ``bewit rules`` and the viewer report it.
    Also reports Claude Code settings that disable all hooks (``home``
    defaults to the user's home directory). Read-only; never raises.
    """
    problems: list[str] = _disabled_hook_settings(root, home)
    for rel in (".claude/settings.json", ".codex/hooks.json"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            problems.append(f"{rel}: not valid JSON")
            continue
        if not isinstance(hooks, dict):
            problems.append(f"{rel}: 'hooks' is not an object")
            continue
        for event, matcher, _extras in _NATIVE_HOOK_SPECS:
            entries = hooks.get(event)
            groups = [
                g for g in (entries if isinstance(entries, list) else [])
                if isinstance(g, dict)
                and isinstance(g.get("hooks"), list)
                and any(
                    isinstance(h, dict) and _OURS_RE.search(str(h.get("command", "")))
                    for h in g["hooks"]
                )
            ]
            if not groups:
                problems.append(f"{rel}: no Bewit hook for {event}")
            elif matcher and groups[0].get("matcher") != matcher:
                problems.append(
                    f"{rel}: {event} matcher is outdated "
                    f"('{groups[0].get('matcher')}', expected '{matcher}')"
                )
    return problems


def _ensure_agents_md(root: Path, result: InitResult) -> None:
    """Write AGENTS.md, or append the Bewit protocol if it is missing."""
    path = root / "AGENTS.md"
    if not path.exists():
        _write_if_absent(path, _AGENTS_MD, result)
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.warnings.append(f"{path}: could not read ({exc})")
        return
    if "Bewit" in text:
        result.skipped.append(str(path))
        return
    path.write_text(text.rstrip() + "\n\n" + _AGENT_PROTOCOL, encoding="utf-8", newline="\n")
    result.created.append(str(path))


def init_repo(
    root: Path,
    shim: str = "none",
    runtimes: tuple[str, ...] | None = None,
    record_store: str | None = None,
) -> InitResult:
    """Install Bewit into ``root``; returns what was created/skipped.

    ``shim="uvx"`` writes hook commands as ``uvx bewit ...`` so teammates
    need no Bewit installation at all — uv auto-installs it on first hook
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
    base = root / BEWIT_DIRNAME
    _write_if_absent(base / "config.yaml", _CONFIG_TEMPLATE, result)
    if record_store is not None:
        _set_config_value(base / "config.yaml", "record_store", record_store, result)
    _ensure_runtime_gitignore(root, result)
    _ensure_records_gitignore(root, result)
    _ensure_gitattributes(root, result)
    _write_if_absent(base / "rules" / "00-starter-rules.yaml", _STARTER_RULES, result)
    _write_if_absent(
        base / "rules" / "90-bewit-self-protection.yaml",
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
        _write_if_absent(root / ".cursor" / "rules" / "bewit.mdc", _AGENT_RULE, result)
    if "claude" in selected:
        _merge_native_hooks(
            root / ".claude" / "settings.json", result, shim, kind="claude"
        )
        _write_if_absent(
            root / ".claude" / "rules" / "bewit.md", _AGENT_PROTOCOL, result
        )
    if "codex" in selected:
        _merge_native_hooks(
            root / ".codex" / "hooks.json", result, shim, kind="codex"
        )
        _ensure_agents_md(root, result)
    _install_git_hook(root, result, shim)
    if load_config(root).record_store == "ref":
        _install_pre_push_hook(root, result, shim)
    # Several steps can touch one file (.gitignore gets two blocks): report
    # each file once, as created when any step wrote it.
    result.created = list(dict.fromkeys(result.created))
    result.skipped = [p for p in dict.fromkeys(result.skipped) if p not in result.created]
    return result
