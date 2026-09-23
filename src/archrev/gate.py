"""The edit gate: decides whether an agent file edit proceeds, pauses, or stops.

Invoked from the ``preToolUse`` hook before Cursor executes a file-editing
tool. Decision policy, in order:

1. Paths exempt by configuration (session bookkeeping and plan files, plus
   any ``exempt`` globs) are always allowed — the gate must never block
   its own bookkeeping.
2. Strict mode: if ``strict_plan_check`` is on and this session has not
   recorded a plan check yet, the edit is denied with instructions to the
   agent to register and check a plan first ("no validated plan, no code").
3. Path rules: a matching ``block`` rule returns ``ask`` so the *user*
   explicitly approves in Cursor's dialog; a matching ``flag`` rule allows
   the edit but records it prominently. ``monitor`` enforcement downgrades
   blocks to flags; ``off`` disables rule evaluation entirely.

Every non-trivial decision is appended to the session event log, so gate
activity is always visible in the timeline even when nothing was stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from archrev import globmatch
from archrev.config import Config
from archrev.rules import Rule, RuleSet
from archrev.storage import Session


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    action: str
    path: str
    message: str


@dataclass(frozen=True)
class GateDecision:
    """Outcome of gating one tool call."""

    permission: str  # "allow" | "ask" | "deny"
    user_message: str = ""
    agent_message: str = ""
    hits: tuple[RuleHit, ...] = field(default_factory=tuple)

    def to_hook_output(self) -> dict:
        """Serialize to the JSON shape Cursor's preToolUse hook expects."""
        out: dict = {"permission": self.permission}
        if self.user_message:
            out["user_message"] = self.user_message
        if self.agent_message:
            out["agent_message"] = self.agent_message
        return out


_STRICT_AGENT_MESSAGE = (
    "ArchRev strict mode: no validated plan is recorded for this session, so "
    "file edits are not allowed yet. Before editing, run:\n"
    "  1. archrev plan register <plan-file>   (or: archrev plan register --text \"...\")\n"
    "  2. archrev check plan                  (attest prompt rules with --attest <id>=pass|fail)\n"
    "Then retry the edit. Do not bypass this by writing files via shell commands."
)

_STRICT_FAILED_CHECK_MESSAGE = (
    "ArchRev strict mode: the latest plan check for this session did NOT "
    "pass (failed or unattested prompt rules), so file edits remain "
    "blocked. Resolve every failed policy with the user, then re-run "
    "`archrev check plan` with updated attestations. A failing check can "
    "not be 'attested through' - the user must agree to the resolution."
)


def relativize(path: str, root: Path) -> str:
    """Best-effort repo-relative normalized path for matching and storage."""
    # A file URI (file:///e%3A/repo/.env) must not slip past path rules.
    if path[:7].lower() == "file://":
        path = unquote(path[7:])
    norm = globmatch.normalize(path)
    root_norm = globmatch.normalize(str(root))
    if root_norm and norm.lower().startswith(root_norm.lower() + "/"):
        return norm[len(root_norm) + 1 :]
    if norm.lower() == root_norm.lower():
        return ""
    return norm


def _apply_mode(action: str, config: Config) -> str:
    """Downgrade stopping actions to ``flag`` in monitor mode."""
    if config.enforcement == "monitor" and action in ("block", "deny"):
        return "flag"
    return action


def decide_from_hits(
    hits: list[RuleHit], config: Config, what: str
) -> GateDecision:
    """Turn rule hits into a decision: deny > block(ask) > flag(allow).

    ``what`` names the gated action ("edit", "shell command", ...) in the
    user- and agent-facing messages.
    """
    if not hits:
        return GateDecision(permission="allow")

    def _lines(subset: list[RuleHit]) -> str:
        return "\n".join(
            f"[{h.rule_id}] {h.path}: {h.message or 'matched rule'}"
            for h in subset
        )

    denying = [h for h in hits if h.action == "deny"]
    if denying:
        return GateDecision(
            permission="deny",
            user_message=(
                f"ArchRev denied a {what} (hard policy).\n" + _lines(denying)
            ),
            agent_message=(
                f"ArchRev denied this {what} outright (rule(s): "
                + ", ".join(sorted({h.rule_id for h in denying}))
                + "). This is a hard policy - do not retry variations or "
                "work around it; adjust the approach or ask the user."
            ),
            hits=tuple(hits),
        )

    blocking = [h for h in hits if h.action == "block"]
    if blocking:
        return GateDecision(
            permission="ask",
            user_message=(
                f"ArchRev: this {what} needs your approval.\n"
                + _lines(blocking)
            ),
            agent_message=(
                f"ArchRev paused this {what} for explicit user approval "
                "(rule(s): "
                + ", ".join(sorted({h.rule_id for h in blocking}))
                + "). If the user declines, adjust the plan instead of "
                "working around the gate."
            ),
            hits=tuple(hits),
        )

    flagged = ", ".join(sorted({h.rule_id for h in hits}))
    return GateDecision(
        permission="allow",
        agent_message=(
            f"ArchRev flagged this {what} (rule(s): {flagged}). It is "
            "allowed but will be highlighted in the session review."
        ),
        hits=tuple(hits),
    )


def evaluate_edit(
    config: Config,
    ruleset: RuleSet,
    session: Session,
    paths: list[str],
    root: Path,
) -> GateDecision:
    """Gate one edit affecting ``paths`` (raw, possibly absolute)."""
    rel_paths = [p for p in (relativize(p, root) for p in paths) if p]
    actionable = [
        p for p in rel_paths if not globmatch.matches_any(config.exempt, p)
    ]
    if not actionable:
        return GateDecision(permission="allow")

    # 'off' disables the gate entirely, including strict mode - it must be
    # the master switch the documentation promises.
    if config.enforcement == "off":
        return GateDecision(permission="allow")

    # Strict mode precedes rule matching: without a *passing* plan check
    # the session has no validated intent to measure any edit against. A
    # recorded-but-failed check must not unlock the gate, otherwise the
    # agent could attest 'fail' and proceed anyway.
    if config.strict_plan_check:
        last_check = session.last_event("plan_check")
        if last_check is None or not last_check.get("ok"):
            failed = last_check is not None
            return GateDecision(
                permission="deny",
                user_message=(
                    "ArchRev blocked an edit: strict mode requires a "
                    + ("passing" if failed else "registered, checked")
                    + " plan before any file changes in this session."
                ),
                agent_message=(
                    _STRICT_FAILED_CHECK_MESSAGE if failed else _STRICT_AGENT_MESSAGE
                ),
            )

    hits: list[RuleHit] = []
    for rel in actionable:
        for rule in ruleset.match_path(rel):
            hits.append(
                RuleHit(
                    rule_id=rule.id,
                    action=_apply_mode(rule.action, config),
                    path=rel,
                    message=rule.message,
                )
            )
    return decide_from_hits(hits, config, "edit")


def evaluate_shell(
    config: Config, ruleset: RuleSet, command: str
) -> GateDecision:
    """Gate one shell command against ``shell`` rules.

    Strict plan mode deliberately does not apply here: the agent must be
    able to run ``archrev plan register`` / ``check plan`` via shell to
    satisfy strict mode in the first place.
    """
    if config.enforcement == "off" or not command.strip():
        return GateDecision(permission="allow")
    excerpt = command.strip()[:200]
    hits = [
        RuleHit(
            rule_id=r.id,
            action=_apply_mode(r.action, config),
            path=excerpt,
            message=r.message,
        )
        for r in ruleset.match_text("shell", command)
    ]
    return decide_from_hits(hits, config, "shell command")


def evaluate_read(
    config: Config, ruleset: RuleSet, paths: list[str], root: Path
) -> GateDecision:
    """Gate file reads against ``read`` rules (e.g. secrets)."""
    if config.enforcement == "off":
        return GateDecision(permission="allow")
    hits: list[RuleHit] = []
    for rel in (relativize(p, root) for p in paths):
        if not rel or globmatch.matches_any(config.exempt, rel):
            continue
        for rule in ruleset.match_read(rel):
            hits.append(
                RuleHit(
                    rule_id=rule.id,
                    action=_apply_mode(rule.action, config),
                    path=rel,
                    message=rule.message,
                )
            )
    return decide_from_hits(hits, config, "file read")


def evaluate_tool(
    config: Config, ruleset: RuleSet, kind: str, identifier: str
) -> GateDecision:
    """Gate a tool or MCP invocation by name (``kind``: 'tool' | 'mcp')."""
    if config.enforcement == "off" or not identifier:
        return GateDecision(permission="allow")
    hits = [
        RuleHit(
            rule_id=r.id,
            action=_apply_mode(r.action, config),
            path=identifier,
            message=r.message,
        )
        for r in ruleset.match_text(kind, identifier)
    ]
    what = "MCP tool call" if kind == "mcp" else "tool call"
    return decide_from_hits(hits, config, what)


def rule_hits_for_paths(ruleset: RuleSet, rel_paths: list[str]) -> list[RuleHit]:
    """Pure rule matching used by plan checks and the finalize diff scan."""
    hits: list[RuleHit] = []
    for rel in rel_paths:
        for rule in ruleset.match_path(rel):
            hits.append(
                RuleHit(
                    rule_id=rule.id,
                    action=rule.action,
                    path=rel,
                    message=rule.message,
                )
            )
    return hits
