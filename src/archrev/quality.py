"""Quality-gate checks: run ``kind: check`` rule commands against changed files.

A check rule delegates the actual semantics to an external tool — semgrep,
eslint, ruff, pytest, or a custom script (including an LLM judge wrapper).
ArchRev is the harness: it decides *which* rules apply to *which* changed
files, runs the command, and records the verdict as evidence.

Contract with the command:

- ``{files}`` in the command is replaced with the matched changed files
  (space-separated, individually quoted, repo-relative). Commands without
  the placeholder run as-is (e.g. a project-wide lint).
- Exit code 0 means the check passed; anything else is a failure. Captured
  output (stdout+stderr, capped) is stored as the evidence trail.
- Commands run with the repository root as working directory, through the
  shell (check rules are repo configuration and the rules directory is
  gate-protected — treat them with the same care as CI config).

Failure semantics follow the rule's action: ``block`` failures become
final-review findings and fail ``archrev check diff`` (CI); ``flag``
failures are recorded and highlighted only. A command that cannot run at
all (missing tool, timeout) is an *error*, not a failure — ArchRev never
breaks the session over its own tooling (fail-open).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from archrev import globmatch
from archrev.rules import Rule, RuleSet

#: Maximum characters of command output stored per check event.
_OUTPUT_CAP = 4000


def _matched_files(rule: Rule, files: list[str]) -> list[str]:
    return [
        f
        for f in files
        if rule.scope_matches(f) and globmatch.matches_any(rule.match, f)
    ]


def _build_command(rule: Rule, matched: list[str]) -> str:
    if "{files}" not in rule.command:
        return rule.command
    quoted = " ".join(f'"{f}"' for f in matched)
    return rule.command.replace("{files}", quoted)


def run_check(root: Path, rule: Rule, matched: list[str]) -> dict:
    """Run one check rule; returns its result record (never raises)."""
    command = _build_command(rule, matched)
    result: dict = {
        "rule_id": rule.id,
        "action": rule.action,
        "message": rule.message,
        "files": matched,
        "command": command,
        "ok": None,  # None = could not run (error), True/False = verdict
        "exit_code": None,
        "output": "",
        "error": None,
    }
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=rule.timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        result["ok"] = proc.returncode == 0
        result["exit_code"] = proc.returncode
        result["output"] = output[-_OUTPUT_CAP:]
    except subprocess.TimeoutExpired:
        result["error"] = f"timed out after {rule.timeout}s"
    except OSError as exc:
        result["error"] = f"could not run: {exc}"
    return result


def run_checks(root: Path, ruleset: RuleSet, files: list[str]) -> list[dict]:
    """Run every enabled check rule that matches any of ``files``.

    Rules whose globs match none of the changed files are skipped entirely
    — a session that never touched ``api/**`` pays nothing for API checks.
    """
    results = []
    for rule in ruleset.check_rules:
        matched = _matched_files(rule, files)
        if not matched:
            continue
        results.append(run_check(root, rule, matched))
    return results
