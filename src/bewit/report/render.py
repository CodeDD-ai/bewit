"""Render a session view (see :mod:`bewit.drift`) as text or markdown.

Both renderers read the same view dict, so the terminal output, the
``report.md`` written at finalization, and ``bewit show --md`` can never
disagree about what happened. The formatting helpers here (local times,
short ids, titles, recording/ended) are shared with the CLI listings.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

#: A session with an event this recent is "recording" (as in the viewer).
RECORDING_SECONDS = 15 * 60

_TAG_RE = re.compile(r"</?[A-Za-z_][\w-]*(\s[^<>]*)?>")
_REVIEW_NOTE = "Bewit final review found issues"


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------


def _parse(ts: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def fmt_when(ts: object) -> str:
    """ISO UTC -> local "28 Sep 11:38"; "?" when unknown."""
    dt = _parse(ts)
    return dt.astimezone().strftime("%d %b %H:%M").lstrip("0") if dt else "?"


def fmt_clock(ts: object) -> str:
    """ISO UTC -> local "11:38:05"."""
    dt = _parse(ts)
    return dt.astimezone().strftime("%H:%M:%S") if dt else "--:--:--"


def short_id(session_id: str) -> str:
    """UUID-style ids shortened to 8 characters (a unique prefix for ``show``)."""
    return session_id[:8] if len(session_id) > 12 and "-" in session_id else session_id


def clean_title(text: object, width: int = 72) -> str:
    """One readable line from a prompt: tags stripped, whitespace collapsed."""
    flat = " ".join(_TAG_RE.sub(" ", str(text or "")).split())
    if not flat:
        return "(no prompt)"
    return flat if len(flat) <= width else flat[: width - 3].rstrip() + "..."


def session_state(last_activity: object) -> str:
    """``recording`` when an event is recent, else ``ended``."""
    dt = _parse(last_activity)
    if dt and time.time() - dt.timestamp() < RECORDING_SECONDS:
        return "recording"
    return "ended"


def user_prompts(view: dict) -> list[dict]:
    """The user's prompts, without Bewit's own review notes or duplicates."""
    out: list[dict] = []
    for p in view.get("prompts", []) or []:
        text = str(p.get("text") or "")
        if text.startswith(_REVIEW_NOTE):
            continue
        if out and out[-1].get("ts") == p.get("ts") and out[-1].get("text") == p.get("text"):
            continue
        out.append(p)
    return out


def _loc(added: object, removed: object) -> str:
    a = f"+{added}" if isinstance(added, int) else "+?"
    r = f"-{removed}" if isinstance(removed, int) else "-?"
    return f"{a}/{r}"


def _file_flags(entry: dict) -> str:
    flags = []
    if entry.get("untracked"):
        flags.append("new")
    if not entry.get("in_plan", True):
        flags.append("not in plan")
    for rule in entry.get("rules", []):
        flags.append(f"{rule['action']}:{rule['rule_id']}")
    return ", ".join(flags)


def _during(entry: dict) -> str:
    """Which of the session's commands were running when a file changed."""
    return "; ".join(str(label) for label in entry.get("during") or [])


def _gate_target(gate: dict, width: int = 90) -> str:
    """What a rule decided on, on one line (commands can be multi-line)."""
    text = ", ".join(gate.get("paths") or []) or str(gate.get("target") or "")
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


_OUTCOME_WORDS = {
    "refused": "refused",
    "approved": "you approved",
    "declined": "did not run",
    "waiting": "waiting for you",
    "unknown": "outcome not recorded",
    "flagged": "flagged",
}
_SUBJECT = {"edit": "an edit", "shell": "a command", "read": "a file read",
            "mcp": "an MCP call", "tool": "a tool call"}


def _gate_line(gate: dict) -> str:
    permission = gate.get("permission")
    verb = "refused" if permission == "deny" else "paused" if permission == "ask" else "flagged"
    outcome = _OUTCOME_WORDS.get(str(gate.get("outcome")), "")
    rules = ", ".join(dict.fromkeys(h.get("rule_id", "") for h in gate.get("hits") or []))
    suffix = f" -> {outcome}" if permission == "ask" and outcome else ""
    return (f"Rule {verb} {_SUBJECT.get(str(gate.get('kind')), 'an action')}{suffix}"
            f"  [{rules}]  {_gate_target(gate)}")


def _drift_line(view: dict) -> str:
    plan = view.get("plan", {})
    if not plan.get("registered"):
        return "no plan registered, so planned and unplanned changes cannot be told apart"
    declared = len(plan.get("declared_files", []))
    touched = len(view.get("files", []))
    out = len(view.get("drift", {}).get("out_of_plan", []))
    unrealized = len(view.get("drift", {}).get("unrealized", []))
    return (
        f"{touched} file(s) changed, {declared} planned; "
        f"{out} not in the plan, {unrealized} planned but untouched"
    )


def _rules_line(view: dict) -> str:
    gates = view.get("gate_events", []) or []
    refused = sum(1 for g in gates if g.get("permission") == "deny")
    paused = [g for g in gates if g.get("permission") == "ask"]
    approved = sum(1 for g in paused if g.get("outcome") == "approved")
    flagged = sum(1 for g in gates if g.get("permission") not in ("deny", "ask"))
    if not gates:
        return "no rule fired"
    parts = []
    if refused or paused:
        parts.append(f"stopped the agent {refused + len(paused)} time(s): {refused} refused, "
                     f"{len(paused)} paused ({approved} approved)")
    if flagged:
        parts.append(f"{flagged} flagged")
    return "; ".join(parts) + ". Stops are the rules working; only the review above needs you."


def _review(view: dict) -> dict:
    review = view.get("review")
    if review is None:
        from bewit.review import review_items

        review = review_items(view)
    return review


def _ack_by(ack: dict | None) -> str:
    if not ack:
        return ""
    actor = ack.get("actor")
    if actor == "agent":
        return "the agent (clears plan drift only)"
    if actor == "unknown":
        return "an unverified actor (clears plan drift only)"
    return "the reviewer"


def _turns(view: dict) -> list[tuple[dict | None, list[tuple[str, str, str]]]]:
    """Events grouped under the prompt they followed, oldest turn first.

    Each event is ``(ts, kind, text)``; kinds: gate, command, edit, check,
    review, ack, plan.
    """
    events: list[tuple[str, str, str]] = []
    for g in view.get("gate_events", []) or []:
        events.append((str(g.get("ts") or ""), "gate", _gate_line(g)))
    for c in view.get("commands", []) or []:
        label = " ".join(str(c.get("label") or "command").split())
        label = label if len(label) <= 100 else label[:97] + "..."
        note = "" if c.get("ended", True) else "  (no completion recorded)"
        events.append((str(c.get("ts") or ""), "command", f"Ran {label}{note}"))
    for e in view.get("edits", []) or []:
        events.append((str(e.get("ts") or ""), "edit", str(e.get("path") or "")))
    for r in (view.get("plan") or {}).get("revisions", []) or []:
        events.append((str(r.get("ts") or ""), "plan",
                       f"Plan registered: {len(r.get('declared_files') or [])} file(s) declared"))
    for c in view.get("checks", []) or []:
        bad = [f"{p['rule_id']}={p['verdict']}" for p in c.get("prompt_rules", [])
               if p.get("verdict") not in ("pass", "n/a")]
        events.append((str(c.get("ts") or ""), "plan",
                       "Plan check passed" if c.get("ok") else
                       "Plan check did not pass" + (f": {', '.join(bad)}" if bad else ": no plan")))
    for q in view.get("quality_checks", []) or []:
        status = "passed" if q.get("ok") is True else "FAILED" if q.get("ok") is False else "could not run"
        events.append((str(q.get("ts") or ""), "check", f"Quality check {q.get('rule_id')} {status}"))
    for f in view.get("final_checks", []) or []:
        n = len(f.get("findings") or [])
        events.append((str(f.get("ts") or ""), "review",
                       f"End-of-turn review: {n} finding(s)" if n else "End-of-turn review: clean"))
    for a in view.get("acks", []) or []:
        events.append((str(a.get("ts") or ""), "ack",
                       f"Acknowledged {a.get('target')} by {_ack_by(a)}: \"{a.get('note', '')}\""))
    events.sort(key=lambda e: e[0])

    prompts = user_prompts(view)
    turns: list[tuple[dict | None, list[tuple[str, str, str]]]] = [(None, [])]
    index = 0
    for event in events:
        while index < len(prompts) and str(prompts[index].get("ts") or "") <= event[0]:
            turns.append((prompts[index], []))
            index += 1
        turns[-1][1].append(event)
    turns.extend((p, []) for p in prompts[index:])
    return [t for t in turns if t[0] is not None or t[1]]


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


def render_text(view: dict, full: bool = False) -> str:
    """Plain-text report for the terminal (``bewit show``).

    Reads top-down like the viewer: what needs you, what the rules did,
    then the trace per prompt (newest first) and the files. ``full`` adds
    every command, file read, and change by other sessions.
    """
    lines: list[str] = []
    add = lines.append
    prompts = user_prompts(view)
    title = clean_title(prompts[0].get("text") if prompts else "", 100)
    meta =[view.get("runtime") or "agent", view.get("branch") or "",
            f"started {fmt_when(view.get('started_at'))}"]
    if view.get("from_manifest"):
        meta.append("from the committed summary")
    add(f"Session {view['id']}")
    add("  " + " | ".join(m for m in meta if m))
    add(f"  \"{title}\"")
    add("")

    review = _review(view)
    open_items = review.get("open", [])
    if open_items:
        add(f"NEEDS YOUR REVIEW ({len(open_items)})")
        for item in open_items:
            add(f"  ! {item['title']}")
            add(f"      why   {item['why']}")
            if item.get("likely_cause"):
                add(f"      from  {item['likely_cause']}")
            add(f"      do    {item['action']}")
    else:
        add("REVIEW   nothing needs your review")
    for item in review.get("resolved", []):
        ack = item.get("ack") or {}
        add(f"  + resolved: {item['title']}, acknowledged by {_ack_by(ack)}"
            + (f" (\"{ack.get('note')}\")" if ack.get("note") else ""))
    add("")

    chain = view.get("chain") or {}
    add(f"RULES    {_rules_line(view)}")
    add(f"CHANGES  {_drift_line(view)}")
    if chain:
        status = ("intact" if chain.get("ok")
                  else f"BROKEN at event #{chain.get('break_at')} ({chain.get('reason') or 'modified'})")
        add(f"RECORD   hash chain {status}, {chain.get('checked', 0)} event(s)"
            + (f", {chain['duplicates']} duplicate delivery(ies)" if chain.get("duplicates") else ""))
    add("")

    turns = _turns(view)
    add(f"PROMPTS ({len(prompts)}, newest first)")
    for number, (prompt, events) in reversed(list(enumerate(turns))):
        head = (f"#{sum(1 for t in turns[:number + 1] if t[0] is not None)} "
                f"{fmt_when(prompt.get('ts'))}  \"{clean_title(prompt.get('text'))}\""
                if prompt else "before the first prompt")
        edits = {text for _, kind, text in events if kind == "edit"}
        commands = sum(1 for _, kind, _ in events if kind == "command")
        counts = ", ".join(x for x in (f"{len(edits)} file(s) changed" if edits else "",
                                       f"{commands} command(s)" if commands else "") if x)
        add(f"  {head}" + (f"  [{counts}]" if counts else ""))
        for ts, kind, text in reversed(events):
            if kind == "edit" or (kind == "command" and not full):
                continue
            add(f"      {fmt_clock(ts)}  {text}")
    add("")

    files = view.get("files", []) or []
    other = view.get("other_changes", []) or []
    add(f"FILES ({len(files) + len(other)} changed by this session)")
    for f in files:
        flags = _file_flags(f)
        add(f"  {_loc(f.get('added'), f.get('removed')):>10}  {f['path']}" + (f"  ({flags})" if flags else ""))
    for f in other:
        during = _during(f)
        add(f"  {_loc(f.get('added'), f.get('removed')):>10}  {f['path']}  (changed outside the edit tools"
            + (f", during {during}" if during else "") + ")")
    foreign = view.get("other_sessions", []) or []
    background = view.get("background_changes", []) or []
    outside = view.get("outside_repo", []) or []
    if full:
        for f in foreign:
            add(f"  {_loc(f.get('added'), f.get('removed')):>10}  {f['path']}  (other session: "
                + ", ".join(short_id(s) for s in f.get("sessions") or []) + ")")
        for f in background:
            add(f"  {_loc(f.get('added'), f.get('removed')):>10}  {f['path']}  (background, not this session)")
        for path in outside:
            add(f"  {'':>10}  {path}  (outside this repository)")
    elif foreign or background or outside:
        add(f"  + {len(foreign) + len(background)} change(s) not from this session"
            + (f", {len(outside)} outside the repository" if outside else "")
            + " (bewit show --all lists them)")
    add("")

    plan = view.get("plan", {}) or {}
    if plan.get("registered"):
        declared = plan.get("declared_files", []) or []
        touched = {str(p.get("path", "")).lower() for p in plan.get("progress", []) or [] if p.get("touched")}
        add(f"PLAN ({len(declared)} declared file(s); [x] changed, [ ] not changed)")
        for path in declared:
            add(f"  {'[x]' if path.lower() in touched else '[ ]'} {path}")
    else:
        add("PLAN     none registered")

    if full:
        reads = view.get("reads", []) or []
        add("")
        add(f"FILES READ ({len(reads)})")
        for r in reads:
            add(f"  {r.get('permission', 'allow'):>5}  {r['path'] or '(repository root)'}  x{r.get('count', 1)}")

    commits = view.get("commits", []) or []
    if commits:
        add("")
        add(f"COMMITS ({len(commits)})")
        for c in commits:
            add(f"  {c['sha'][:10]}  {c['subject']}")
    if not full:
        add("")
        add("More: bewit show <id> --all  |  bewit serve (diffs)  |  bewit show <id> --md")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Markdown (report.md, merge requests)
# ---------------------------------------------------------------------------


def render_markdown(view: dict) -> str:
    """Markdown report (``report.md``, MR descriptions)."""
    lines: list[str] = []
    add = lines.append
    prompts = user_prompts(view)
    add(f"# Bewit session `{short_id(view['id'])}`")
    add("")
    if prompts:
        add(f"> {clean_title(prompts[0].get('text'), 160)}")
        add("")
    meta = [view.get("runtime") or "agent", view.get("branch") and f"branch `{view['branch']}`",
            f"started {view.get('started_at') or 'unknown'} (UTC)", f"id `{view['id']}`"]
    add("- " + " · ".join(m for m in meta if m))
    add(f"- **Changes:** {_drift_line(view)}")
    add(f"- **Rules:** {_rules_line(view)}")
    add("")

    review = _review(view)
    open_items = review.get("open", [])
    add(f"## Needs your review ({len(open_items)})" if open_items else "## Review")
    add("")
    if not open_items:
        add("Nothing needs your review: no gate bypasses, failed checks, or unplanned changes left open.")
    for item in open_items:
        add(f"- **{item['title']}**  ")
        add(f"  *Why it matters:* {item['why']}  ")
        if item.get("likely_cause"):
            add(f"  *Likely cause:* `{item['likely_cause'].replace('`', chr(39))}`  ")
        add(f"  *What to do:* {item['action']}")
    for item in review.get("resolved", []):
        add(f"- ~~{item['title']}~~ — acknowledged by {_ack_by(item.get('ack'))}")
    add("")

    add("## Prompts")
    for p in prompts:
        text = (p.get("text") or "").strip()
        add(f"**{p.get('ts')}**")
        add("")
        add("> " + "\n> ".join(text.splitlines()[:15] or ["(empty)"]))
        add("")

    plan = view.get("plan", {})
    add("## Plan")
    if plan.get("registered"):
        add("Registered. Declared files:")
        add("")
        for path in plan.get("declared_files", []):
            add(f"- `{path}`")
    else:
        add("**Not registered** - planned and unplanned changes cannot be told apart.")
    add("")

    gates = view.get("gate_events", []) or []
    if gates:
        add("## Rule decisions")
        add("")
        for g in gates:
            add(f"- {g.get('ts')}: {_gate_line(g).replace('[', '`').replace(']', '`')}")
        add("")

    checks = view.get("checks", [])
    add("## Plan policies (the agent's attestations)")
    if not checks:
        add("No plan check recorded.")
    for check in checks[-1:]:
        add(f"Latest check {check.get('ts')}: **{'passed' if check.get('ok') else 'did not pass'}**"
            + (f" ({len(checks) - 1} earlier)" if len(checks) > 1 else ""))
        add("")
        add("_Self-reported: the agent judged its own plan. A pass shows the "
            "policy was considered, not that it holds; verify it in the diff._")
        add("")
        for pr in check.get("prompt_rules", []):
            verdict = {"pass": "PASS", "fail": "FAIL", "n/a": "N/A"}.get(pr["verdict"], "UNATTESTED")
            how = (" (out of scope)" if pr.get("scope") else
                   " (kept from an earlier check)" if pr.get("carried") else "")
            add(f"- {verdict}{how} `{pr['rule_id']}`: {pr['policy'][:160]}")
    add("")

    add("## Files")
    add("")
    add("| File | LOC | Notes |")
    add("| --- | --- | --- |")
    for f in view.get("files", []):
        add(f"| `{f['path']}` | {_loc(f.get('added'), f.get('removed'))} | {_file_flags(f) or '-'} |")
    for f in view.get("other_changes", []):
        # Shell commands routinely contain pipes and backticks, which would
        # split the table cell or break the code span.
        during = _during(f).replace("|", "\\|").replace("`", "'")
        note = f"changed outside the edit tools, during `{during}`" if during else "changed outside the edit tools"
        add(f"| `{f['path']}` | {_loc(f.get('added'), f.get('removed'))} | {note} |")
    add("")
    foreign = view.get("other_sessions", []) or []
    background = view.get("background_changes", []) or []
    outside = view.get("outside_repo", []) or []
    if foreign or background or outside:
        add(f"<details><summary>{len(foreign) + len(background) + len(outside)} change(s) "
            "not from this session (excluded from the review)</summary>")
        add("")
        for f in foreign:
            add(f"- `{f['path']}` — another session ({', '.join(short_id(s) for s in f.get('sessions') or [])})")
        for f in background:
            add(f"- `{f['path']}` — background change")
        for path in outside:
            add(f"- `{path}` — outside this repository")
        add("")
        add("</details>")
        add("")

    reads = view.get("reads", [])
    if reads:
        denied = sum(1 for r in reads if r.get("permission") != "allow")
        add("## Files read")
        add("")
        add(f"<details><summary>{len(reads)} file(s) read, {denied} stopped by a read rule</summary>")
        add("")
        add("| File | Reads | Decision |")
        add("| --- | --- | --- |")
        for r in reads:
            decision = r.get("permission", "allow")
            if decision != "allow":
                decision = f"**{decision}**"
            add(f"| `{r['path'] or '.'}` | {r.get('count', 1)} | {decision} |")
        add("")
        add("</details>")
        add("")

    quality = view.get("quality_checks", [])
    if quality:
        latest: dict[str, dict] = {}
        for q in quality:
            latest[str(q.get("rule_id"))] = q
        add("## Quality checks (latest run per check)")
        for q in latest.values():
            status = ("passed" if q.get("ok") is True else "**FAILED**" if q.get("ok") is False
                      else f"error: {q.get('error')}")
            add(f"- `{q.get('rule_id')}` {status} ({len(q.get('files', []))} file(s), {q.get('ts')})")
            if q.get("ok") is False and q.get("message"):
                add(f"  - {q['message']}")
        add("")

    acks = view.get("acks", [])
    if acks:
        add("## Acknowledged findings")
        for a in acks:
            add(f"- {a.get('ts')}: `{a.get('target')}` — {a.get('note', '')} *(by {_ack_by(a)})*")
        add("")

    human = view.get("human_changes", [])
    if human:
        add("## Human changes (no agent attribution)")
        for h in human:
            add(f"- {h.get('ts')}: " + ", ".join(f"`{p}`" for p in h.get("files", [])))
        add("")

    chain = view.get("chain")
    if chain:
        status = "intact" if chain.get("ok") else f"**BROKEN at event #{chain.get('break_at')}**"
        add(f"*Event log hash chain: {status}, {chain.get('checked')} event(s) verified.*")
        add("")

    add("## Commits")
    commits = view.get("commits", [])
    if not commits:
        add("No linked commits (yet).")
    for c in commits:
        add(f"- `{c['sha'][:10]}` {c['subject']}")
    add("")
    add(f"*Generated by Bewit at {view.get('generated_at')}*")
    return "\n".join(lines) + "\n"
