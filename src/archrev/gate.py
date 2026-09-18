"""The edit gate: decides whether an agent file edit proceeds, pauses, or stops.

Invoked from the ``preToolUse`` hook before Cursor executes a file-editing
tool. Decision policy, in order:

1. Paths exempt by configuration (ArchRev data, Cursor config, plan files)
   are always allowed — the gate must never block its own bookkeeping.
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
    norm = globmatch.normalize(path)
    root_norm = globmatch.normalize(str(root))
    if root_norm and norm.lower().startswith(root_norm.lower() + "/"):
        return norm[len(root_norm) + 1 :]
    if norm.lower() == root_norm.lower():
        return ""
    return norm


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
            action = rule.action
            if action == "block" and config.enforcement == "monitor":
                action = "flag"  # monitor mode records but never stops
            hits.append(
                RuleHit(
                    rule_id=rule.id, action=action, path=rel, message=rule.message
                )
            )

    if not hits:
        return GateDecision(permission="allow")

    blocking = [h for h in hits if h.action == "block"]
    if blocking:
        lines = [
            f"[{h.rule_id}] {h.path}: {h.message or 'protected path'}"
            for h in blocking
        ]
        return GateDecision(
            permission="ask",
            user_message=(
                "ArchRev: this edit touches protected paths and needs your "
                "approval.\n" + "\n".join(lines)
            ),
            agent_message=(
                "ArchRev paused this edit for explicit user approval "
                "(protected path rule"
                + ("s" if len(blocking) > 1 else "")
                + ": "
                + ", ".join(sorted({h.rule_id for h in blocking}))
                + "). If the user declines, adjust the plan instead of "
                "working around the gate."
            ),
            hits=tuple(hits),
        )

    # Flag-only: allow, and inform the agent so the flag lands in context.
    flagged_rules = ", ".join(sorted({h.rule_id for h in hits}))
    return GateDecision(
        permission="allow",
        agent_message=(
            f"ArchRev flagged this edit (rule(s): {flagged_rules}). It is "
            "allowed but will be highlighted in the session review."
        ),
        hits=tuple(hits),
    )


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
