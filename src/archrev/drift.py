"""Session view computation: files, line counts, drift, protected scan, commits.

``compute_view`` builds the complete picture of one session from its event
log plus git. It powers the live timeline, the terminal/markdown reports,
and — when called with ``finalize=True`` from the ``stop`` hook — writes
the durable ``manifest.json`` and ``report.md``.

Definitions:

- **touched**: files recorded by edit events (the agent's tracked edits).
- **other changes**: files changed with *no* edit event that this session
  may have written — by its shell commands (``makemigrations``) or MCP
  tools. With working-tree checkpoints (:mod:`archrev.worktree`) these are
  exactly the changes made while one of this session's commands ran;
  sessions recorded without checkpoints fall back to "everything changed
  since the session started".
- **other sessions**: changes recorded as edits by a different session.
- **background changes**: changes outside this session's activity — hand
  edits, other windows' commands, work that pre-dates the session. Shown
  for context, excluded from this session's drift and bypass review.
- **drift**: touched files not declared in the plan (``out_of_plan``) and
  declared files never touched (``unrealized``).
- **protected findings**: every changed path matching a path rule,
  regardless of how it was changed — the diff-level backstop for anything
  that bypassed the edit gate.
"""

from __future__ import annotations

import re
from pathlib import Path

from archrev.config import Config
from archrev.gate import rule_hits_for_paths
from archrev.gitutil import Git
from archrev.rules import RuleSet
from archrev.storage import HUMAN_SESSION_ID, Session, SessionStore, utc_now_iso
from archrev.worktree import Replay, fold_live_tail, is_bookkeeping, replay

#: Maximum prompt characters stored in views/manifests (full text stays in events).
_PROMPT_EXCERPT = 2000


def _is_outside_repo(path: str) -> bool:
    """True for paths that are not repo-relative (absolute or parent-escaping).

    Edit events store repo-relative paths for files under the root;
    anything absolute (``C:/...``, ``/home/...``) or escaping upward
    (``../``) lies outside the repository.
    """
    return bool(
        re.match(r"^([A-Za-z]:[/\\]|[/\\])", path) or path.startswith("..")
    )


def _areas(paths: list[str]) -> list[str]:
    """Top-level repository areas touched (monorepo orientation aid)."""
    areas = {p.split("/", 1)[0] for p in paths if p}
    return sorted(areas)


def _edits_by_other_sessions(
    root: Path, session_id: str
) -> dict[str, dict[str, list[str]]]:
    """Map lowercased path -> {other session id: [edit timestamps]}."""
    found: dict[str, dict[str, list[str]]] = {}
    for other in SessionStore(root).list_sessions():
        if other.id in (session_id, HUMAN_SESSION_ID):
            continue
        for event in other.events():
            path = event.get("path")
            if event.get("type") != "edit" or not isinstance(path, str) or not path:
                continue
            if _is_outside_repo(path):
                continue
            stamps = found.setdefault(path.lower(), {}).setdefault(other.id, [])
            if isinstance(event.get("ts"), str):
                stamps.append(event["ts"])
    return found


def _count_lines(root: Path, path: str) -> int | None:
    try:
        return len(
            (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return None


def _classify_unedited(
    root: Path,
    scope: Replay,
    candidates: list[str],
    numstat: dict,
    foreign: dict[str, dict[str, list[str]]],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split changes without an edit event in this session into
    ``(other_changes, other_sessions, background_changes)``.

    With checkpoints, a change is this session's only if it happened inside
    one of its command windows and no other session recorded an edit of the
    file during that window. Without checkpoints (older records), every
    change since the session started is this session's unless another
    session ever edited the file — the previous behavior.
    """
    windows = {p.lower(): w for p, w in scope.windows.items()}
    own: list[dict] = []
    owned_elsewhere: list[dict] = []
    background: list[dict] = []
    for path in candidates:
        stat = numstat.get(path)
        entry: dict = (
            {"path": path, "added": stat.added, "removed": stat.removed}
            if stat
            else {"path": path, "added": None, "removed": None, "untracked": True}
        )
        owners = foreign.get(path.lower(), {})
        if not scope.enabled:
            if owners:
                owned_elsewhere.append({**entry, "sessions": list(owners)})
            else:
                own.append(entry)
            continue
        mine = [
            w
            for w in windows.get(path.lower(), [])
            if not any(w.contains(ts) for stamps in owners.values() for ts in stamps)
        ]
        if mine:
            if entry.get("untracked"):
                entry["added"], entry["removed"] = _count_lines(root, path), 0
            entry["during"] = sorted({w.label for w in mine})
            own.append(entry)
        elif owners:
            owned_elsewhere.append({**entry, "sessions": list(owners)})
        elif not is_bookkeeping(path):
            background.append(entry)
    return own, owned_elsewhere, background


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
        {
            "ts": e.get("ts"),
            "text": str(e.get("text", ""))[:_PROMPT_EXCERPT],
            "capture": e.get("capture") or "full",
            "chars": e.get("chars"),
            "sealed": e.get("capture") == "sealed",
        }
        for e in events
        if e.get("type") == "prompt"
    ]

    # Plan revisions: every registration is kept (re-registering amends the
    # plan), so how the plan evolved during development stays reviewable.
    plan_revisions = [
        {
            "ts": e.get("ts"),
            "origin": e.get("origin"),
            "declared_files": [
                p for p in (e.get("declared_files") or []) if isinstance(p, str)
            ],
            "text": e.get("text"),  # None for pre-v0.2 events
        }
        for e in events
        if e.get("type") == "plan_registered"
    ]
    plan_registered = bool(plan_revisions)
    declared = plan_revisions[-1]["declared_files"] if plan_revisions else []

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
    # Acknowledgments: audited resolutions of final-review findings
    # (`archrev ack`). The final review skips acknowledged targets; the
    # record of who acknowledged what, when, and why stays in the log.
    acks = [e for e in events if e.get("type") == "ack"]
    quality_checks = [e for e in events if e.get("type") == "quality_check"]

    all_touched = session.touched_files()
    # Paths outside the repository root (other repos, Cursor's own chat
    # asset saves under C:/Users/.../.cursor/...) can never be declared in
    # a plan and are not part of this codebase: they are recorded — an
    # agent writing outside the repo is signal — but segregated from the
    # files table and drift instead of producing false out-of-plan noise.
    outside_repo = [p for p in all_touched if _is_outside_repo(p)]
    touched = [p for p in all_touched if not _is_outside_repo(p)]

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
            lines = _count_lines(root, path)
            if lines is not None:
                added, removed = lines, 0
        files.append(
            {
                "path": path,
                "added": added,
                "removed": removed,
                "untracked": path in untracked,
                "in_plan": path.lower() in declared_set,
                "rules": [
                    {
                        "rule_id": h.rule_id,
                        "action": h.action,
                        "message": h.message,
                    }
                    for h in rule_hits_for_paths(ruleset, [path])
                ],
            }
        )

    touched_set = {t.lower() for t in touched}
    # Several sessions (and the human) share one working tree. Checkpoints
    # bound what this session could have written without an edit event to
    # the windows in which its own commands ran; everything else belongs
    # to someone else and must not be raised in this session's review.
    scope = replay(events)
    if scope.enabled and git.is_repo():
        fold_live_tail(root, scope)
    foreign = _edits_by_other_sessions(root, session.id)
    changed = set(numstat) | (untracked if scope.enabled else set())
    other_changes, other_sessions, background_changes = _classify_unedited(
        root,
        scope,
        sorted(p for p in changed if p.lower() not in touched_set),
        numstat,
        foreign,
    )

    drift = {
        "out_of_plan": [f["path"] for f in files if not f["in_plan"]] if plan_registered else [],
        "unrealized": [d for d in declared if d.lower() not in touched_set],
    }

    protected_findings: list[dict] = []
    if config.protected_scan:
        # The diff-level backstop covers this session's tracked edits and
        # its unexplained changes, never other sessions' or background work.
        during = {c["path"].lower(): c.get("during") for c in other_changes}
        scanned = {p.lower(): p for p in [*touched, *(c["path"] for c in other_changes)]}
        for hit in rule_hits_for_paths(ruleset, [scanned[p] for p in sorted(scanned)]):
            finding = {
                "path": hit.path,
                "rule_id": hit.rule_id,
                "action": hit.action,
                "message": hit.message,
                "via_gate": hit.path.lower() in touched_set,
            }
            if not finding["via_gate"] and during.get(hit.path.lower()):
                finding["during"] = during[hit.path.lower()]
            protected_findings.append(finding)

    commits = git.commits_with_session(session.id) if git.is_repo() else []

    view = {
        "id": session.id,
        "started_at": meta.get("started_at"),
        "head_sha": base_sha,
        "finalized": finalize or session.manifest() is not None,
        "generated_at": utc_now_iso(),
        "prompts": prompts,
        "branch": meta.get("branch") or (git.branch() if git.is_repo() else None),
        "runtime": meta.get("runtime") or None,
        "plan": {
            "registered": plan_registered,
            "declared_files": declared,
            # Progress: which declared files have actually been touched.
            "progress": [
                {"path": d, "touched": d.lower() in touched_set}
                for d in declared
            ],
            "revisions": plan_revisions,
            "text": session.plan_text(),
        },
        "checks": checks,
        "final_checks": final_checks,
        "human_changes": human_changes,
        "acks": acks,
        "quality_checks": quality_checks,
        "gate_events": gate_events,
        "edits": edits,
        "files": files,
        "outside_repo": outside_repo,
        "other_sessions": other_sessions,
        "other_changes": other_changes,
        "background_changes": background_changes,
        # "checkpoints": attribution bounded to this session's command
        # windows; "since_start": older records without checkpoints.
        "attribution": "checkpoints" if scope.enabled else "since_start",
        "drift": drift,
        "protected_findings": protected_findings,
        "commits": commits,
        "areas": _areas(touched),
        "event_count": len(events),
        "chain_head": events[-1].get("hash") if events else None,
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
