"""Session view computation: files, line counts, drift, protected scan, commits.

``compute_view`` builds the complete picture of one session from its event
log plus git. It powers the live timeline, the terminal/markdown reports,
and — when called with ``finalize=True`` from the ``stop`` hook — writes
the durable ``manifest.json`` and ``report.md``.

Definitions:

- **touched**: files recorded by edit events (the agent's tracked edits).
- **other changes**: files changed in the working tree since session start
  that have *no* edit event — written by shell commands (``makemigrations``),
  hand edits, or other sessions. Shown separately, never attributed.
- **drift**: touched files not declared in the plan (``out_of_plan``) and
  declared files never touched (``unrealized``).
- **protected findings**: every changed path matching a path rule,
  regardless of how it was changed — the diff-level backstop for anything
  that bypassed the edit gate.
"""

from __future__ import annotations

from pathlib import Path

from archrev.config import Config
from archrev.gate import rule_hits_for_paths
from archrev.gitutil import Git
from archrev.rules import RuleSet
from archrev.storage import Session, utc_now_iso

#: Maximum prompt characters stored in views/manifests (full text stays in events).
_PROMPT_EXCERPT = 2000


def _areas(paths: list[str]) -> list[str]:
    """Top-level repository areas touched (monorepo orientation aid)."""
    areas = {p.split("/", 1)[0] for p in paths if p}
    return sorted(areas)


def compute_view(
    root: Path,
    config: Config,
    ruleset: RuleSet,
    session: Session,
    finalize: bool = False,
) -> dict:
    """Assemble the full session view; optionally persist it as the manifest."""
    meta = session.meta() or {}
    events = session.events()
    git = Git(root)

    prompts = [
        {"ts": e.get("ts"), "text": str(e.get("text", ""))[:_PROMPT_EXCERPT]}
        for e in events
        if e.get("type") == "prompt"
    ]

    plan_registered = False
    declared: list[str] = []
    for event in reversed(events):
        if event.get("type") == "plan_registered":
            plan_registered = True
            raw = event.get("declared_files")
            declared = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
            break

    checks = [e for e in events if e.get("type") == "plan_check"]
    gate_events = [e for e in events if e.get("type") == "gate"]
    edits = [
        {
            "ts": e.get("ts"),
            "path": e.get("path"),
            "tool": e.get("tool"),
            "origin": e.get("origin", "agent"),
        }
        for e in events
        if e.get("type") == "edit"
    ]
    final_checks = [e for e in events if e.get("type") == "final_check"]
    human_changes = [e for e in events if e.get("type") == "human_changes"]

    touched = session.touched_files()

    # Line statistics: git diff since the session-start HEAD covers both
    # committed and uncommitted changes; untracked files appear separately.
    base_sha = meta.get("head_sha")
    numstat = {e.path: e for e in git.numstat(base_sha)} if git.is_repo() else {}
    untracked = set(git.untracked_files()) if git.is_repo() else set()

    declared_set = {d.lower() for d in declared}
    files = []
    for path in touched:
        stat = numstat.get(path)
        added = stat.added if stat else None
        removed = stat.removed if stat else None
        if stat is None and path in untracked:
            # git diff never covers untracked files; count a brand-new
            # file's lines directly so the review shows real numbers.
            try:
                added = len(
                    (root / path)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
                removed = 0
            except OSError:
                pass
        files.append(
            {
                "path": path,
                "added": added,
                "removed": removed,
                "untracked": path in untracked,
                "in_plan": path.lower() in declared_set,
                "rules": [
                    {"rule_id": h.rule_id, "action": h.action}
                    for h in rule_hits_for_paths(ruleset, [path])
                ],
            }
        )

    touched_set = {t.lower() for t in touched}
    other_changes = [
        {"path": path, "added": stat.added, "removed": stat.removed}
        for path, stat in sorted(numstat.items())
        if path.lower() not in touched_set
    ]

    drift = {
        "out_of_plan": [f["path"] for f in files if not f["in_plan"]] if plan_registered else [],
        "unrealized": [d for d in declared if d.lower() not in touched_set],
    }

    protected_findings: list[dict] = []
    if config.protected_scan:
        all_changed = sorted(touched_set | {p.lower() for p in numstat})
        # Re-run matching on original-case paths for readable output.
        originals = {p.lower(): p for p in [*touched, *numstat]}
        for hit in rule_hits_for_paths(
            ruleset, [originals[p] for p in all_changed if p in originals]
        ):
            protected_findings.append(
                {
                    "path": hit.path,
                    "rule_id": hit.rule_id,
                    "action": hit.action,
                    "message": hit.message,
                    "via_gate": hit.path.lower() in touched_set,
                }
            )

    commits = git.commits_with_session(session.id) if git.is_repo() else []

    view = {
        "id": session.id,
        "started_at": meta.get("started_at"),
        "head_sha": base_sha,
        "finalized": finalize or session.manifest() is not None,
        "generated_at": utc_now_iso(),
        "prompts": prompts,
        "plan": {
            "registered": plan_registered,
            "declared_files": declared,
            "text": session.plan_text(),
        },
        "checks": checks,
        "final_checks": final_checks,
        "human_changes": human_changes,
        "gate_events": gate_events,
        "edits": edits,
        "files": files,
        "other_changes": other_changes,
        "drift": drift,
        "protected_findings": protected_findings,
        "commits": commits,
        "areas": _areas(touched),
        "event_count": len(events),
        # Tamper evidence: hash-chain verification over the event log.
        "chain": session.verify_chain(),
    }

    if finalize:
        # The manifest omits the full plan text (plan.md sits next to it)
        # to keep it a compact, diff-friendly summary.
        manifest = {k: v for k, v in view.items() if k != "plan"}
        manifest["plan"] = {
            "registered": plan_registered,
            "declared_files": declared,
        }
        manifest["finalized_at"] = utc_now_iso()
        session.write_manifest(manifest)
    return view
