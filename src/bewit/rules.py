"""Rule loading and evaluation.

Rules live in ``.bewit/rules/*.yaml``. Each file may contain a single
rule mapping, a list of rule mappings, or multiple YAML documents.

Machine-enforced kinds (evaluated by hooks before the action happens):

- ``path``  — globs over repository paths, gating agent *file edits*.
- ``read``  — globs over repository paths, gating agent *file reads*
  (e.g. deny reading ``dev-secrets/**``).
- ``shell`` — regex patterns (``match_command``) over shell commands the
  agent wants to run (e.g. deny ``git push``, flag ``pip install``).
- ``mcp``   — regex patterns (``match_tool``) over MCP tool identifiers.
- ``tool``  — regex patterns (``match_tool``) over agent tool names
  (e.g. deny ``Task`` to forbid subagents).

Actions for all machine-enforced kinds:

- ``deny``  — hard stop; the agent is refused outright with the message.
- ``block`` — pause; the *user* is asked to approve in Cursor's dialog.
- ``flag``  — allow, but record prominently in the session review.

``prompt`` rules are LLM-evaluated policies the agent must attest to when
running ``bewit check plan``; verdicts are recorded evidence, not proof.

Enforcement honesty: these rules gate the agent's *attempts* at the tool
layer and record everything. They are policy plus audit, not a sandbox —
an allowed process can still do whatever the OS permits. Use containers /
network isolation underneath when containment is required.

Loading is total: invalid rules are collected as errors (shown by
``bewit rules``) and never crash a hook.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from bewit import globmatch
from bewit.config import bewit_dir, read_text_any

ACTIONS = ("deny", "block", "flag")
GLOB_KINDS = ("path", "read")
PATTERN_KINDS = ("shell", "mcp", "tool")
#: check rules run a quality-gate command (semgrep, eslint, pytest, an LLM
#: judge script, ...) against the session's changed files. Post-hoc by
#: nature, so deny is meaningless: block failures become final-review
#: findings (and fail `bewit check diff` in CI); flag failures are
#: recorded and highlighted only.
CHECK_ACTIONS = ("block", "flag")
ALL_KINDS = (*GLOB_KINDS, *PATTERN_KINDS, "check", "prompt")

#: Default seconds a check command may run before it is abandoned
#: (recorded as an error, never blocking - fail-open like every hook path).
DEFAULT_CHECK_TIMEOUT = 60

#: YAML key that carries the regex patterns, per pattern kind.
_PATTERN_KEY = {"shell": "match_command", "mcp": "match_tool", "tool": "match_tool"}


@dataclass(frozen=True)
class Rule:
    """One validated rule."""

    id: str
    kind: str  # see ALL_KINDS
    action: str = "flag"  # machine-enforced kinds only
    match: tuple[str, ...] = ()  # glob kinds (path, read)
    patterns: tuple[str, ...] = ()  # pattern kinds (shell, mcp, tool)
    message: str = ""  # shown to user/agent when the rule fires
    policy: str = ""  # prompt rules: the policy text to attest
    command: str = ""  # check rules: quality-gate command ({files} placeholder)
    timeout: int = DEFAULT_CHECK_TIMEOUT  # check rules: max runtime seconds
    applies_to: tuple[str, ...] = ()  # optional monorepo scoping globs
    enabled: bool = True
    source: str = ""  # rules file this came from, for diagnostics

    def scope_matches(self, path: str) -> bool:
        """True when ``path`` is inside this rule's ``applies_to`` scope."""
        if not self.applies_to:
            return True
        return globmatch.matches_any(self.applies_to, path)

    def pattern_matches(self, text: str) -> bool:
        """True when any regex pattern matches ``text`` (case-insensitive)."""
        for pattern in self.patterns:
            compiled = _compile_pattern(pattern)
            if compiled is not None and compiled.search(text):
                return True
        return False


@dataclass
class RuleSet:
    """All rules of a repository plus any loading diagnostics."""

    rules: list[Rule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def _enabled(self, kind: str) -> list[Rule]:
        return [r for r in self.rules if r.kind == kind and r.enabled]

    @property
    def path_rules(self) -> list[Rule]:
        return self._enabled("path")

    @property
    def read_rules(self) -> list[Rule]:
        return self._enabled("read")

    @property
    def shell_rules(self) -> list[Rule]:
        return self._enabled("shell")

    @property
    def mcp_rules(self) -> list[Rule]:
        return self._enabled("mcp")

    @property
    def tool_rules(self) -> list[Rule]:
        return self._enabled("tool")

    @property
    def check_rules(self) -> list[Rule]:
        return self._enabled("check")

    @property
    def prompt_rules(self) -> list[Rule]:
        return self._enabled("prompt")

    def match_path(self, relpath: str) -> list[Rule]:
        """Enabled path rules matching ``relpath``, in declaration order."""
        return [
            rule
            for rule in self.path_rules
            if rule.scope_matches(relpath)
            and globmatch.matches_any(rule.match, relpath)
        ]

    def match_read(self, relpath: str) -> list[Rule]:
        """Enabled read rules matching ``relpath``."""
        return [
            rule
            for rule in self.read_rules
            if rule.scope_matches(relpath)
            and globmatch.matches_any(rule.match, relpath)
        ]

    def match_text(self, kind: str, text: str) -> list[Rule]:
        """Enabled pattern rules of ``kind`` matching ``text``."""
        return [r for r in self._enabled(kind) if r.pattern_matches(text)]


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
    if kind not in ALL_KINDS:
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

    if kind == "prompt":
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

    # Machine-enforced kinds share action/message validation.
    action = raw.get("action", "flag")
    if action not in ACTIONS:
        errors.append(
            f"{source}: {kind} rule '{rule_id}' has invalid action {action!r}"
            f" (expected one of {ACTIONS})"
        )
        return None
    message = raw.get("message", "")
    if not isinstance(message, str):
        message = ""

    if kind in GLOB_KINDS:
        match = _string_tuple(raw.get("match"))
        if not match:
            errors.append(f"{source}: {kind} rule '{rule_id}' needs 'match' globs")
            return None
        return Rule(
            id=rule_id,
            kind=kind,
            action=action,
            match=match,
            message=message.strip(),
            applies_to=applies_to,
            enabled=enabled,
            source=source,
        )

    if kind == "check":
        if action not in CHECK_ACTIONS:
            errors.append(
                f"{source}: check rule '{rule_id}' has action {action!r}; "
                f"check rules run post-hoc and support only {CHECK_ACTIONS}"
            )
            return None
        match = _string_tuple(raw.get("match"))
        if not match:
            errors.append(f"{source}: check rule '{rule_id}' needs 'match' globs")
            return None
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            errors.append(f"{source}: check rule '{rule_id}' needs a 'command'")
            return None
        timeout = raw.get("timeout", DEFAULT_CHECK_TIMEOUT)
        if not isinstance(timeout, int) or timeout <= 0:
            errors.append(f"{source}: check rule '{rule_id}' has invalid 'timeout'")
            timeout = DEFAULT_CHECK_TIMEOUT
        return Rule(
            id=rule_id,
            kind="check",
            action=action,
            match=match,
            command=command.strip(),
            timeout=timeout,
            message=message.strip(),
            applies_to=applies_to,
            enabled=enabled,
            source=source,
        )

    # Pattern kinds: shell / mcp / tool with validated regexes.
    key = _PATTERN_KEY[kind]
    patterns = _string_tuple(raw.get(key))
    if not patterns:
        errors.append(
            f"{source}: {kind} rule '{rule_id}' needs '{key}' regex patterns"
        )
        return None
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(
                f"{source}: {kind} rule '{rule_id}' has invalid regex "
                f"{pattern!r} ({exc})"
            )
            return None
    return Rule(
        id=rule_id,
        kind=kind,
        action=action,
        patterns=patterns,
        message=message.strip(),
        applies_to=applies_to,
        enabled=enabled,
        source=source,
    )


@lru_cache(maxsize=512)
def _compile_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile a rule regex once. ``None`` when the pattern is invalid."""
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


#: Parsed rule sets, keyed by the rules directory. Invalidated when any
#: rules file's name, size, or mtime changes. Hooks reload rules on every
#: event; within one process (and across a viewer's polls) that is wasted.
_RULES_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], RuleSet]] = {}


def load_rules(root: Path) -> RuleSet:
    """Load every rule beneath ``.bewit/rules/``; never raises."""
    rules_dir = bewit_dir(root) / "rules"
    if not rules_dir.is_dir():
        return RuleSet()
    signature: list[tuple[str, int, int]] = []
    for path in sorted(rules_dir.glob("*.y*ml")):
        try:
            st = path.stat()
        except OSError:
            continue
        signature.append((path.name, st.st_mtime_ns, st.st_size))
    sig = tuple(signature)
    key = str(rules_dir.resolve())
    cached = _RULES_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    ruleset = _load_rules_from(rules_dir)
    _RULES_CACHE[key] = (sig, ruleset)
    return ruleset


def _load_rules_from(rules_dir: Path) -> RuleSet:
    """Parse ``rules_dir`` with no cache."""
    ruleset = RuleSet()

    seen_ids: dict[str, str] = {}
    for path in sorted(rules_dir.glob("*.y*ml")):
        source = path.name
        try:
            documents = list(yaml.safe_load_all(read_text_any(path)))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            # ValueError: undecodable bytes. Recorded, never raised: one bad
            # file must not take every other rule down with it.
            ruleset.errors.append(f"{source}: cannot read rules ({exc})")
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
