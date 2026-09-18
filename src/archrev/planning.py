"""Plan registration and rule checking.

Cursor stores plan files outside the workspace (``~/.cursor/plans``), so
passive capture cannot see them. The agent therefore *registers* its plan
explicitly (``archrev plan register``); the plan text is snapshotted into
the session directory and the file paths it declares are extracted for
later drift detection.

``archrev check plan`` evaluates the declared files against path rules
(early warning about what will gate) and records the agent's attestations
for every prompt rule. The check event doubles as the strict-mode token
that unlocks the edit gate.
"""

from __future__ import annotations

import re
from pathlib import Path

from archrev.config import Config
from archrev.gate import rule_hits_for_paths
from archrev.rules import RuleSet
from archrev.storage import Session

# Path candidates inside a plan: backticked tokens, markdown link targets,
# code-fence citation headers (```12:34:path/to/file), and bare path-like
# tokens containing a separator plus a file extension.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_MDLINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)\)")
_FENCE_CITATION_RE = re.compile(r"^\s*```\s*\d+:\d+:(.+?)\s*$", re.MULTILINE)
# The negative lookbehind rejects starts right after '/', '.', or word chars
# so fragments of URLs (https://example.com/x.py) never match; an optional
# leading './' is still allowed because the match may begin at the dot.
_BARE_PATH_RE = re.compile(
    r"(?<![\w`(/.])((?:\./)?(?:[\w.-]+[/\\])+[\w.-]+\.\w{1,10})"
)
# Bare top-level filenames (README.md, pyproject.toml) carry no separator,
# so _BARE_PATH_RE never sees them. They are collected as candidates and
# kept ONLY when they exist in the repository (the existence check below),
# preserving precision. Found via dogfooding: a plan declaring README.md
# in prose was reported as out-of-plan drift by the final review.
_BARE_NAME_RE = re.compile(r"(?<![\w`(/.])([\w-][\w.-]*\.\w{1,10})")
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")


def _clean_candidate(raw: str) -> str | None:
    """Normalize one candidate token to a repo-relative-looking path."""
    token = raw.strip().strip("'\"").rstrip(".,;:!?")
    if not token or "://" in token or " " in token:
        return None
    token = _LINE_SUFFIX_RE.sub("", token)  # drop trailing :line(:col)
    token = token.replace("\\", "/")
    # Strip only './' prefixes; str.lstrip("./") would eat the leading dot
    # of dotfile paths like '.archrev/rules/x.yaml' (real dogfooding bug).
    while token.startswith("./"):
        token = token[2:]
    # Windows absolute paths inside plans are kept as-is; relativization
    # happens against the repo root below.
    return token or None


def extract_declared_files(text: str, root: Path) -> list[str]:
    """Extract the repository files a plan declares it will touch.

    Heuristic by design: precision is preferred over recall, since false
    positives would produce noisy "unrealized plan" drift. A candidate is
    kept when it exists in the repository, or contains a path separator
    plus a file extension (a strong signal it is a concrete file path).
    """
    candidates: list[str] = []
    for regex in (
        _BACKTICK_RE,
        _MDLINK_RE,
        _FENCE_CITATION_RE,
        _BARE_PATH_RE,
        _BARE_NAME_RE,
    ):
        candidates.extend(regex.findall(text))

    seen: dict[str, None] = {}
    for raw in candidates:
        token = _clean_candidate(raw)
        if not token:
            continue
        exists = (root / token).exists()
        looks_like_file = "/" in token and re.search(r"\.\w{1,10}$", token)
        if exists or looks_like_file:
            seen.setdefault(token, None)
    return list(seen)


#: Plan text stored per revision inside the event log (full text lives in
#: plan.md for the latest revision; earlier revisions survive here).
_PLAN_EVENT_TEXT_CAP = 20_000


def register_plan(
    session: Session, text: str, root: Path, origin: str | None = None
) -> list[str]:
    """Snapshot ``text`` as the session plan; returns declared files.

    Re-registering is how plans are *amended*; each registration event
    carries its own text, so the full revision history (how the plan
    evolved during development) is reviewable, not just the final state.
    """
    declared = extract_declared_files(text, root)
    session.write_plan(text)
    session.append_event(
        "plan_registered",
        origin=origin or "inline",
        declared_files=declared,
        chars=len(text),
        text=text[:_PLAN_EVENT_TEXT_CAP],
    )
    return declared


def check_plan(
    config: Config,
    ruleset: RuleSet,
    session: Session,
    attestations: dict[str, str],
    note: str | None = None,
) -> dict:
    """Evaluate the registered plan against all rules; record the verdicts.

    Returns the check report:

    - ``path_findings``: declared files that match path rules — a preview
      of what the edit gate will block or flag during implementation.
    - ``prompt_rules``: one entry per enabled prompt rule with the agent's
      attestation (``pass``/``fail``) or ``unattested``.
    - ``ok``: True only when every prompt rule is attested ``pass``.
    """
    declared: list[str] = []
    for event in reversed(session.events()):
        if event.get("type") == "plan_registered":
            raw = event.get("declared_files")
            declared = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
            break

    path_findings = [
        {
            "path": hit.path,
            "rule_id": hit.rule_id,
            "action": hit.action,
            "message": hit.message,
        }
        for hit in rule_hits_for_paths(ruleset, declared)
    ]

    prompt_results = []
    for rule in ruleset.prompt_rules:
        verdict = attestations.get(rule.id, "unattested")
        prompt_results.append(
            {"rule_id": rule.id, "policy": rule.policy, "verdict": verdict}
        )
    unknown_ids = sorted(set(attestations) - {r.id for r in ruleset.prompt_rules})

    ok = all(r["verdict"] == "pass" for r in prompt_results)
    report = {
        "plan_registered": session.plan_text() is not None,
        "declared_files": declared,
        "path_findings": path_findings,
        "prompt_rules": prompt_results,
        "unknown_attestations": unknown_ids,
        "note": note or "",
        "ok": ok,
        "strict_mode": config.strict_plan_check,
    }
    session.append_event("plan_check", **report)
    return report
