"""Render a session view (see :mod:`archrev.drift`) as text or markdown.

Both renderers read the same view dict, so the terminal output, the
``report.md`` written at finalization, and ``archrev show --md`` can never
disagree about what happened.
"""

from __future__ import annotations


def _loc(added: object, removed: object) -> str:
    a = f"+{added}" if isinstance(added, int) else "+?"
    r = f"-{removed}" if isinstance(removed, int) else "-?"
    return f"{a}/{r}"


def _file_flags(entry: dict) -> str:
    flags = []
    if entry.get("untracked"):
        flags.append("new")
    if not entry.get("in_plan", True):
        flags.append("out-of-plan")
    for rule in entry.get("rules", []):
        flags.append(f"{rule['action']}:{rule['rule_id']}")
    return ", ".join(flags)


def _drift_line(view: dict) -> str:
    plan = view.get("plan", {})
    if not plan.get("registered"):
        return "no plan registered - drift not measurable"
    declared = len(plan.get("declared_files", []))
    touched = len(view.get("files", []))
    out = len(view.get("drift", {}).get("out_of_plan", []))
    unrealized = len(view.get("drift", {}).get("unrealized", []))
    return (
        f"planned {declared} file(s), touched {touched}, "
        f"{out} out-of-plan, {unrealized} unrealized"
    )


def render_text(view: dict) -> str:
    """Plain-text report for the terminal (``archrev show``)."""
    lines: list[str] = []
    add = lines.append
    add(f"Session   {view['id']}")
    add(f"Started   {view.get('started_at') or 'unknown'}")
    add(f"State     {'finalized' if view.get('finalized') else 'live'}")
    if view.get("areas"):
        add(f"Areas     {', '.join(view['areas'])}")
    add("")

    prompts = view.get("prompts", [])
    add(f"Prompts ({len(prompts)})")
    for p in prompts:
        first_line = (p.get("text") or "").strip().splitlines()
        add(f"  [{p.get('ts')}] {first_line[0][:120] if first_line else '(empty)'}")
    add("")

    plan = view.get("plan", {})
    add(f"Plan      {'registered' if plan.get('registered') else 'NOT registered'}")
    for path in plan.get("declared_files", []):
        add(f"  - {path}")
    add("")

    checks = view.get("checks", [])
    add(f"Plan checks ({len(checks)})")
    for check in checks:
        add(f"  [{check.get('ts')}] ok={check.get('ok')}")
        for finding in check.get("path_findings", []):
            add(
                f"    gate preview: [{finding['action']}] {finding['path']}"
                f" ({finding['rule_id']})"
            )
        for pr in check.get("prompt_rules", []):
            add(f"    attest {pr['verdict']:>10}: {pr['rule_id']}")
    add("")

    gates = view.get("gate_events", [])
    add(f"Gate events ({len(gates)})")
    for g in gates:
        hits = ", ".join(
            f"{h['action']}:{h['rule_id']}" for h in g.get("hits", [])
        )
        add(f"  [{g.get('ts')}] {g.get('permission')} {', '.join(g.get('paths', []))}"
            + (f"  [{hits}]" if hits else ""))
    add("")

    files = view.get("files", [])
    add(f"Files touched ({len(files)})")
    for f in files:
        flags = _file_flags(f)
        add(f"  {_loc(f.get('added'), f.get('removed')):>9}  {f['path']}"
            + (f"  ({flags})" if flags else ""))
    other = view.get("other_changes", [])
    if other:
        add(f"Other working-tree changes, not from tracked edits ({len(other)})")
        for f in other:
            add(f"  {_loc(f.get('added'), f.get('removed')):>9}  {f['path']}")
    add("")

    add(f"Drift     {_drift_line(view)}")
    findings = view.get("protected_findings", [])
    if findings:
        add(f"Protected paths changed ({len(findings)})")
        for fnd in findings:
            via = "via gate" if fnd.get("via_gate") else "BYPASSED GATE (shell/manual)"
            add(f"  [{fnd['action']}] {fnd['path']} ({fnd['rule_id']}, {via})")
    add("")

    commits = view.get("commits", [])
    add(f"Commits ({len(commits)})")
    for c in commits:
        add(f"  {c['sha'][:10]}  {c['subject']}")
    return "\n".join(lines) + "\n"


def render_markdown(view: dict) -> str:
    """Markdown report (``report.md``, MR descriptions)."""
    lines: list[str] = []
    add = lines.append
    add(f"# ArchRev session `{view['id']}`")
    add("")
    state = "finalized" if view.get("finalized") else "live"
    add(f"- **Started:** {view.get('started_at') or 'unknown'}")
    add(f"- **State:** {state}")
    add(f"- **Areas:** {', '.join(view.get('areas', [])) or '-'}")
    add(f"- **Drift:** {_drift_line(view)}")
    add("")

    add("## Prompts")
    for p in view.get("prompts", []):
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
        add("**Not registered** - drift against the plan cannot be measured.")
    add("")

    add("## Rule verdicts")
    checks = view.get("checks", [])
    if not checks:
        add("No plan check recorded.")
    for check in checks:
        add(f"### Check at {check.get('ts')} - {'OK' if check.get('ok') else 'NOT OK'}")
        for finding in check.get("path_findings", []):
            add(
                f"- gate preview `{finding['path']}`: **{finding['action']}**"
                f" ({finding['rule_id']})"
            )
        for pr in check.get("prompt_rules", []):
            icon = {"pass": "PASS", "fail": "FAIL"}.get(pr["verdict"], "UNATTESTED")
            add(f"- {icon} `{pr['rule_id']}`: {pr['policy'][:160]}")
        add("")

    add("## Files")
    add("")
    add("| File | LOC | Notes |")
    add("| --- | --- | --- |")
    for f in view.get("files", []):
        add(
            f"| `{f['path']}` | {_loc(f.get('added'), f.get('removed'))} "
            f"| {_file_flags(f) or '-'} |"
        )
    for f in view.get("other_changes", []):
        add(
            f"| `{f['path']}` | {_loc(f.get('added'), f.get('removed'))} "
            f"| changed outside tracked edits |"
        )
    add("")

    findings = view.get("protected_findings", [])
    if findings:
        add("## Protected paths changed")
        for fnd in findings:
            via = "via gate" if fnd.get("via_gate") else "**bypassed gate** (shell/manual)"
            add(f"- **{fnd['action']}** `{fnd['path']}` ({fnd['rule_id']}, {via})")
        add("")

    add("## Commits")
    commits = view.get("commits", [])
    if not commits:
        add("No linked commits (yet).")
    for c in commits:
        add(f"- `{c['sha'][:10]}` {c['subject']}")
    add("")
    add(f"*Generated by ArchRev at {view.get('generated_at')}*")
    return "\n".join(lines) + "\n"
