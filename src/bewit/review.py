"""What a reviewer must look at in one session, why, and what to do.

One definition of "needs review", shared by every surface: the end-of-turn
follow-up the agent receives, the viewer's review box and sidebar count,
and ``report.md``. Each item says what happened, why it matters, and how
to resolve it, so a reviewer never has to decode a metric.

Acknowledgments (``bewit ack``) resolve items, with one limit: an
acknowledgment made by the agent itself resolves only plan drift. Gate
bypasses, failed quality checks, a failed plan check, and a broken event
chain need the reviewer; an agent cannot clear its own findings. Acks
recorded before actor attribution existed count as the reviewer's.
"""

from __future__ import annotations

import re

#: Item kinds an agent-made acknowledgment may resolve.
AGENT_RESOLVABLE = frozenset({"drift"})

#: Plain-language effect of each rule action, for "why it matters" text.
ACTION_WORDS = {
    "deny": "refuses these changes outright",
    "block": "requires your approval for changes",
    "flag": "asks that changes be highlighted for review",
}


#: An open command window older than this (no event since) is treated as
#: abandoned — a crashed agent must not turn every later human ack into an
#: agent ack.
_STALE_WINDOW_SECONDS = 30 * 60


def detect_ack_actor(root, session) -> tuple[str, str]:
    """Who is acknowledging right now: ``(actor, basis)``.

    Hooks bracket each agent shell/MCP command with ``exec_start`` /
    ``exec_end`` checkpoints. An acknowledgment made while one of this
    session's commands is open ran inside that command, so the agent made
    it. So did one made while another session runs a command that names
    ``bewit ack`` (the agent passing ``--session``). Otherwise nothing
    agent-driven is running and a person made it. The signal is structural,
    not an environment variable a user's terminal might also carry.
    """
    import time
    from datetime import datetime, timezone

    from bewit.storage import SessionStore
    from bewit.worktree import replay

    def recent(events: list[dict]) -> bool:
        try:
            last = datetime.strptime(
                str(events[-1].get("ts")), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return time.time() - last.timestamp() <= _STALE_WINDOW_SECONDS

    def open_labels(candidate) -> list[str]:
        events = candidate.events()
        if not events or not recent(events):
            return []
        return replay(events).open_labels

    events = session.events()
    mine = open_labels(session)
    if mine:
        return "agent", f"an agent command of this session was running ({mine[-1]})"
    for other in SessionStore(root).list_sessions():
        if other.id == session.id:
            continue
        for label in open_labels(other):
            if re.search(r"bewit(\.exe)?\W*\s+ack\b|/api/ack\b|\[bewit ack\]", label, re.IGNORECASE):
                return "agent", f"agent session {other.id} was running `{label}`"
    if events and recent(events) and not replay(events).enabled:
        # Active, but recorded without command checkpoints (an older
        # session, or git unavailable at its start): nothing shows whether
        # an agent command is running right now.
        return "unknown", "this session has no command checkpoints, so Bewit cannot tell"
    return "human", "no agent command was running"


def ack_actor(ack: dict) -> str:
    """``human``, ``agent``, or ``unknown``.

    Acks recorded before attribution existed count as human. ``unknown``
    is treated like an agent's: it clears plan drift only.
    """
    actor = ack.get("actor")
    return actor if actor in ("agent", "unknown") else "human"


def _resolving_ack(
    acks: list[dict], target: str, kind: str, evidence_ts: str | None
) -> dict | None:
    """The acknowledgment that resolves this finding, if any.

    An ack answers the finding as it stood when it was made: evidence
    newer than the ack (a new failing check run, another change to the
    file) reopens it. Timestamps are second-resolution; an ack in the same
    second as the evidence counts as made after seeing it.
    """
    wanted = target.lower()
    for ack in reversed(acks):
        if str(ack.get("target", "")).lower() != wanted:
            continue
        if evidence_ts and str(ack.get("ts") or "") < evidence_ts:
            continue
        if ack_actor(ack) == "human" or kind in AGENT_RESOLVABLE:
            return ack
    return None


def _latest_quality(view: dict) -> list[dict]:
    latest: dict[str, dict] = {}
    for q in view.get("quality_checks", []) or []:
        latest[str(q.get("rule_id"))] = q
    return list(latest.values())


def _check_is_stale(check: dict, view: dict) -> bool:
    """True when a plan registration came after ``check``.

    Checks record ``plan_revision`` (registrations seen when they ran);
    timestamps have second resolution, so they only decide for older
    records without it, and then only when strictly later.
    """
    revisions = (view.get("plan") or {}).get("revisions") or []
    if not revisions:
        return False
    seen = check.get("plan_revision")
    if isinstance(seen, int):
        return seen < len(revisions)
    last_ts, check_ts = revisions[-1].get("ts"), check.get("ts")
    return bool(last_ts and check_ts and last_ts > check_ts)


def _candidates(view: dict) -> list[dict]:
    """Every potential finding, before acknowledgments are applied."""
    items: list[dict] = []

    for f in view.get("protected_findings", []) or []:
        if f.get("via_gate"):
            continue
        path, rule_id, action = f.get("path", ""), f.get("rule_id", ""), f.get("action", "")
        likely = ", ".join(f.get("during") or [])
        if likely and f.get("uncertain"):
            likely += (
                " (uncertain: this command never reported finishing, so the "
                "change may come from you, another tool, or another session)"
            )
        how = (
            "was changed again by a command after its approved edit"
            if f.get("after_gate")
            else "was changed by a command or by hand, so the rule never ran"
        )
        items.append({
            "id": f"bypass:{path.lower()}",
            "kind": "bypass",
            "target": path,
            "severity": "high" if action in ("deny", "block") else "medium",
            "title": f"{path} changed outside the rule gate",
            "why": f"Rule {rule_id} {ACTION_WORDS.get(action, 'covers this file')}. "
                   f"This file {how}.",
            "likely_cause": likely,
            "action": "Open the diff. If the change is intended, acknowledge it; "
                      "otherwise revert it.",
            "rule_id": rule_id,
            "paths": [path],
            "evidence_ts": f.get("changed_at"),
        })

    for q in _latest_quality(view):
        if q.get("ok") is not False:
            continue
        rule_id = str(q.get("rule_id"))
        block = q.get("action") == "block"
        items.append({
            "id": f"quality:{rule_id.lower()}",
            "kind": "quality",
            "target": rule_id,
            "severity": "high" if block else "note",
            "title": f"Quality check {rule_id} failed",
            "why": (q.get("message") or "A required check failed")
                   + (" This check is required (block)." if block
                      else " This check is advisory (flag)."),
            "likely_cause": "",
            "action": "Read the check output and fix the files, or acknowledge "
                      "if the failure is expected.",
            "rule_id": rule_id,
            "paths": list(q.get("files") or []),
            "output": str(q.get("output") or "")[-2000:],
            "evidence_ts": q.get("ts"),
        })

    checks = view.get("checks", []) or []
    if checks and not checks[-1].get("ok"):
        last = checks[-1]
        failing = [
            f"{r.get('rule_id')}={r.get('verdict')}"
            for r in last.get("prompt_rules", []) or []
            if r.get("verdict") not in ("pass", "n/a")
        ]
        reason = (
            "no plan was registered" if last.get("plan_registered") is False
            else "policies not satisfied: " + ", ".join(failing)
        )
        items.append({
            "id": "plan-check",
            "kind": "plan-check",
            "target": "plan-check",
            "severity": "high",
            "title": "The latest plan check did not pass",
            "why": f"The agent's own check says {reason}.",
            "likely_cause": "",
            "action": "Resolve the failed policies with the agent, then have it "
                      "re-run `bewit check plan`, or acknowledge.",
            "rule_id": "",
            "paths": [],
            "evidence_ts": last.get("ts"),
        })
    elif checks and _check_is_stale(checks[-1], view):
        revisions = (view.get("plan") or {}).get("revisions") or []
        items.append({
            "id": "plan-check-stale",
            "kind": "plan-check",
            "target": "plan-check",
            "severity": "high",
            "title": "The plan changed after the latest plan check",
            "why": "The plan was registered again after the agent's last check, "
                   "so the current plan's policies were never attested.",
            "likely_cause": "The agent re-registered the plan (for example from a "
                            "file after an inline --text plan) without re-checking.",
            "action": "Have the agent re-run `bewit check plan` against the "
                      "current plan, or acknowledge.",
            "rule_id": "",
            "paths": [],
            "evidence_ts": revisions[-1].get("ts") if revisions else checks[-1].get("ts"),
        })

    last_edit_ts: dict[str, str] = {}
    for edit in view.get("edits", []) or []:
        if isinstance(edit.get("path"), str) and edit.get("ts"):
            last_edit_ts[edit["path"].lower()] = edit["ts"]
    if (view.get("plan") or {}).get("registered"):
        for path in (view.get("drift") or {}).get("out_of_plan", []) or []:
            items.append({
                "id": f"drift:{path.lower()}",
                "kind": "drift",
                "target": path,
                "severity": "medium",
                "title": f"{path} was changed but not in the plan",
                "why": "The agent changed a file it did not announce. Unplanned "
                       "changes are where scope creep and surprises hide.",
                "likely_cause": "",
                "action": "Check that the change belongs to this task; "
                          "acknowledge or revert it.",
                "rule_id": "",
                "paths": [path],
                "evidence_ts": last_edit_ts.get(path.lower()),
            })

    chain = view.get("chain") or {}
    if chain and not chain.get("ok", True):
        fork = chain.get("reason") == "fork"
        items.append({
            "id": "chain",
            "kind": "chain",
            "target": "chain",
            "severity": "high",
            "title": f"Event log integrity check failed at event #{chain.get('break_at')}",
            "why": ("Two events chain to the same predecessor. Usually two hooks "
                    "wrote at once, but an inserted event looks the same."
                    if fork else
                    "An event was modified or removed after it was written, so "
                    "this record cannot be trusted as-is."),
            "likely_cause": "",
            "action": "Run `bewit verify` and compare with the committed "
                      "manifest's chain head.",
            "rule_id": "",
            "paths": [],
        })
    return items


def review_items(view: dict) -> dict:
    """Split findings into ``open`` / ``resolved`` and collect ``notes``.

    Returns ``{"open": [...], "resolved": [...], "notes": [...]}``. Items
    are dicts with ``id, kind, target, severity, title, why, likely_cause,
    action, rule_id, paths``; resolved ones add ``ack``. Advisory items
    (``severity == "note"``) go to ``notes``, never ``open``.
    """
    acks = list(view.get("acks", []) or [])
    open_items: list[dict] = []
    resolved: list[dict] = []
    notes: list[dict] = []
    for item in _candidates(view):
        ack = _resolving_ack(acks, item["target"], item["kind"], item.get("evidence_ts"))
        if ack is not None:
            resolved.append({**item, "ack": ack})
        elif item["severity"] == "note":
            notes.append(item)
        else:
            open_items.append(item)

    # Agent acks that did not count are themselves worth a look.
    counted = {id(r["ack"]) for r in resolved}
    for ack in acks:
        if ack_actor(ack) != "human" and id(ack) not in counted:
            unverified = ack_actor(ack) == "unknown"
            notes.append({
                "id": f"agent-ack:{str(ack.get('target', '')).lower()}",
                "kind": "agent-ack",
                "target": str(ack.get("target", "")),
                "severity": "note",
                "title": (f"An acknowledgment of {ack.get('target')} could not be verified"
                          if unverified else
                          f"The agent tried to acknowledge {ack.get('target')}"),
                "why": ("It was made in a session without command checkpoints, so "
                        "Bewit could not tell it came from a person; it counts "
                        "for plan drift only."
                        if unverified else
                        "Only the reviewer can resolve this kind of finding; the "
                        "agent's acknowledgment was recorded and ignored."),
                "likely_cause": "",
                "action": "",
                "rule_id": "",
                "paths": [],
                "ack": ack,
            })
    rank = {"high": 0, "medium": 1, "note": 2}
    open_items.sort(key=lambda i: rank.get(i["severity"], 3))
    return {"open": open_items, "resolved": resolved, "notes": notes}


def finding_key(item: dict) -> str:
    """Identity of a finding across turns, for "new since last review".

    A file or quality rule stays the same finding while it stays open; a
    plan-check finding is keyed by its check, so a newly recorded failing
    check is new even though the previous one failed too.
    """
    if item["kind"] == "plan-check":
        return f"{item['id']}@{item.get('evidence_ts') or ''}"
    return item["id"]


def raisable_items(view: dict) -> list[dict]:
    """Open items the agent is told about (the event chain is omitted: the
    agent cannot resolve it, the reviewer sees it in the viewer)."""
    return [i for i in review_items(view)["open"] if i["kind"] != "chain"]


def final_findings(view: dict, only: set[str] | None = None) -> list[str]:
    """One line per finding kind for the agent's end-of-turn follow-up.

    ``only`` restricts the lines to findings with these :func:`finding_key`
    values (the new ones). The wording stays stable (the fingerprint of the
    full list is kept for older records).
    """
    items = [
        i for i in raisable_items(view) if only is None or finding_key(i) in only
    ]
    lines: list[str] = []
    bypass = [i for i in items if i["kind"] == "bypass"]
    if bypass:
        listed = ", ".join(
            i["target"] + (f" (during {i['likely_cause']})" if i["likely_cause"] else "")
            for i in bypass[:5]
        )
        lines.append(
            f"{len(bypass)} protected path(s) changed OUTSIDE the edit gate "
            f"(shell/manual): {listed}"
        )
    if any(i["kind"] == "plan-check" for i in items):
        lines.append(
            "the latest plan check did NOT pass (no plan registered, or "
            "failed or unattested prompt rules)"
        )
    drift = [i["target"] for i in items if i["kind"] == "drift"]
    if drift:
        lines.append(
            f"{len(drift)} file(s) touched but not declared in the plan: "
            + ", ".join(drift[:5])
        )
    for i in items:
        if i["kind"] == "quality":
            lines.append(f"quality check '{i['target']}' FAILED: {i['why'].split(' This check')[0]}")
    return lines
