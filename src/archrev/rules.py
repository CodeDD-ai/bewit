"""Rule loading and evaluation.

Rules live in ``.archrev/rules/*.yaml``. Each file may contain a single
rule mapping, a list of rule mappings, or multiple YAML documents. Two
kinds exist:

``path`` rules (machine-enforced)
    Glob patterns over repository paths with an ``action``:

    - ``block``: the edit gate stops the agent and asks the user for
      explicit approval before the edit proceeds.
    - ``flag``: the edit proceeds but is prominently recorded and surfaced
      in the session timeline and manifest.

``prompt`` rules (LLM-evaluated policy)
    Free-text policies (e.g. "new API endpoints must declare rate
    limiting") that the agent must attest to when running
    ``archrev check plan``. Verdicts are recorded, not verified — they are
    evidence, and the timeline makes missing or failed attestations loud.

Loading is total: invalid rules are collected as errors (shown by
``archrev rules``) and never crash a hook.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from archrev import globmatch
from archrev.config import archrev_dir

PATH_ACTIONS = ("block", "flag")


@dataclass(frozen=True)
class Rule:
    """One validated rule."""

    id: str
    kind: str  # "path" | "prompt"
    action: str = "flag"  # path rules only
    match: tuple[str, ...] = ()  # path rules only
    message: str = ""  # shown to user/agent when a path rule fires
    policy: str = ""  # prompt rules: the policy text to attest
    applies_to: tuple[str, ...] = ()  # optional monorepo scoping globs
    enabled: bool = True
    source: str = ""  # rules file this came from, for diagnostics

    def scope_matches(self, path: str) -> bool:
        """True when ``path`` is inside this rule's ``applies_to`` scope."""
        if not self.applies_to:
            return True
        return globmatch.matches_any(self.applies_to, path)


@dataclass
class RuleSet:
    """All rules of a repository plus any loading diagnostics."""

    rules: list[Rule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def path_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.kind == "path" and r.enabled]

    @property
    def prompt_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.kind == "prompt" and r.enabled]

    def match_path(self, relpath: str) -> list[Rule]:
        """Enabled path rules matching ``relpath``, in declaration order."""
        return [
            rule
            for rule in self.path_rules
            if rule.scope_matches(relpath)
            and globmatch.matches_any(rule.match, relpath)
        ]


def _string_tuple(value: object) -> tuple[str, ...] | None:
    """Coerce a str or list-of-str into a tuple; None when invalid."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return None


def _parse_rule(raw: object, source: str, errors: list[str]) -> Rule | None:
    """Validate one raw mapping into a Rule, recording precise errors."""
    if not isinstance(raw, dict):
        errors.append(f"{source}: rule entry is not a mapping")
        return None

    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        errors.append(f"{source}: rule is missing a string 'id'")
        return None
    rule_id = rule_id.strip()

    kind = raw.get("kind")
    if kind not in ("path", "prompt"):
        errors.append(f"{source}: rule '{rule_id}' has invalid kind {kind!r}")
        return None

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append(f"{source}: rule '{rule_id}' has non-boolean 'enabled'")
        enabled = True

    applies_to = _string_tuple(raw.get("applies_to", []))
    if applies_to is None:
        errors.append(f"{source}: rule '{rule_id}' has invalid 'applies_to'")
        applies_to = ()

    if kind == "path":
        match = _string_tuple(raw.get("match"))
        if not match:
            errors.append(f"{source}: path rule '{rule_id}' needs 'match' globs")
            return None
        action = raw.get("action", "flag")
        if action not in PATH_ACTIONS:
            errors.append(
                f"{source}: path rule '{rule_id}' has invalid action {action!r}"
                f" (expected one of {PATH_ACTIONS})"
            )
            return None
        message = raw.get("message", "")
        if not isinstance(message, str):
            message = ""
        return Rule(
            id=rule_id,
            kind="path",
            action=action,
            match=match,
            message=message.strip(),
            applies_to=applies_to,
            enabled=enabled,
            source=source,
        )

    policy = raw.get("policy", "")
    if not isinstance(policy, str) or not policy.strip():
        errors.append(f"{source}: prompt rule '{rule_id}' needs a 'policy' text")
        return None
    return Rule(
        id=rule_id,
        kind="prompt",
        policy=policy.strip(),
        applies_to=applies_to,
        enabled=enabled,
        source=source,
    )


def load_rules(root: Path) -> RuleSet:
    """Load every rule beneath ``.archrev/rules/``; never raises."""
    ruleset = RuleSet()
    rules_dir = archrev_dir(root) / "rules"
    if not rules_dir.is_dir():
        return ruleset

    seen_ids: dict[str, str] = {}
    for path in sorted(rules_dir.glob("*.y*ml")):
        source = path.name
        try:
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            ruleset.errors.append(f"{source}: cannot parse YAML ({exc})")
            continue
        for doc in documents:
            if doc is None:
                continue
            entries = doc if isinstance(doc, list) else [doc]
            for raw in entries:
                rule = _parse_rule(raw, source, ruleset.errors)
                if rule is None:
                    continue
                if rule.id in seen_ids:
                    ruleset.errors.append(
                        f"{source}: duplicate rule id '{rule.id}'"
                        f" (first defined in {seen_ids[rule.id]})"
                    )
                    continue
                seen_ids[rule.id] = source
                ruleset.rules.append(rule)
    return ruleset
