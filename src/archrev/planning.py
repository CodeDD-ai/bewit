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
    kept when it exists in the repository, contains a path separator plus
    a file extension (a strong signal it is a concrete file path), or is a
    backticked name with an extension: a plan that writes `CHANGELOG.md`
    names a new top-level file on purpose (found while dogfooding: such
    files were dropped and later reported as unplanned).
    """
    candidates: list[tuple[str, bool]] = []
    for regex in (
        _BACKTICK_RE,
        _MDLINK_RE,
        _FENCE_CITATION_RE,
        _BARE_PATH_RE,
        _BARE_NAME_RE,
    ):
        explicit = regex is _BACKTICK_RE
        candidates.extend((raw, explicit) for raw in regex.findall(text))

    seen: dict[str, None] = {}
    for raw, explicit in candidates:
        token = _clean_candidate(raw)
        if not token:
            continue
        exists = (root / token).exists()
        has_extension = re.search(r"\.\w{1,10}$", token) is not None
        looks_like_file = "/" in token and has_extension
        if exists or looks_like_file or (explicit and has_extension):
            seen.setdefault(token, None)
    return list(seen)


#: Plan text stored per revision inside the event log (full text lives in
#: plan.md for the latest revision; earlier revisions survive here).
_PLAN_EVENT_TEXT_CAP = 20_000


def register_plan(
    session: Session,
    text: str,
    root: Path,
    origin: str | None = None,
    amend: bool = False,
) -> list[str]:
    """Snapshot ``text`` as the session plan; returns declared files.

    Each registration event carries its own text, so revision history
    stays reviewable. Drift uses the *latest* declared set. By default a
    new registration replaces that set (latest-plan-wins). ``amend=True``
    unions the new files with the previous declaration, which is what
    multi-batch sessions need — replacement silently un-declares earlier
    work and manufactures false drift.
    """
    declared = extract_declared_files(text, root)
    if amend:
        previous = session.last_event("plan_registered")
        prior = [
            p
            for p in (previous or {}).get("declared_files") or []
            if isinstance(p, str)
        ]
        merged: dict[str, str] = {p.lower(): p for p in prior}
        for path in declared:
            merged.setdefault(path.lower(), path)
        declared = list(merged.values())
    session.write_plan(text)
    session.append_event(
        "plan_registered",
        origin=origin or "inline",
        declared_files=declared,
        chars=len(text),
        text=text[:_PLAN_EVENT_TEXT_CAP],
        amend=amend,
    )
    return declared


#: Attestation verdicts. ``n/a`` states that a policy does not apply to
#: this plan; it counts as satisfied but stays visible in the review.
VERDICTS = ("pass", "fail", "n/a")
_SATISFIED = ("pass", "n/a")


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
      attestation (``pass`` / ``fail`` / ``n/a``) or ``unattested``. A rule
      whose ``applies_to`` scope contains none of the declared files is
      ``n/a`` automatically (``scope: "out of scope"``), unless attested.
    - ``ok``: True only when a plan is registered and every prompt rule is
      ``pass`` or ``n/a``. Without a plan there is no intent to check, so a
      check can never pass (and never unlock strict mode) before one.
    """
    declared: list[str] = []
    plan_registered = False
    for event in reversed(session.events()):
        if event.get("type") == "plan_registered":
            plan_registered = True
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
        entry = {"rule_id": rule.id, "policy": rule.policy}
        if rule.id in attestations:
            entry["verdict"] = attestations[rule.id]
        elif (
            rule.applies_to
            and declared  # a plan naming no files cannot show it is out of scope
            and not any(rule.scope_matches(p) for p in declared)
        ):
            entry["verdict"] = "n/a"
            entry["scope"] = "out of scope"
        else:
            entry["verdict"] = "unattested"
        prompt_results.append(entry)
    unknown_ids = sorted(set(attestations) - {r.id for r in ruleset.prompt_rules})

    ok = plan_registered and all(r["verdict"] in _SATISFIED for r in prompt_results)
    report = {
        "plan_registered": plan_registered,
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
