"""Cross-repo metrics (``archrev index``): pull-based, read-only aggregation.

The session records already travel with git, so organizational visibility
is a *pull* over checked-out repositories — no streaming from developer
machines, no telemetry, no server. Point ``archrev index`` at one or more
repository roots (or a parent directory of checkouts) and it aggregates
the metrics that matter for oversight:

- session counts and plan discipline (plan registered, check passed)
- drift rate: out-of-plan files vs files touched
- gate pressure: pauses (ask), denials, flags
- protected-path changes that bypassed the edit gate
- quality-check failures and acknowledgment counts
- human-attributed changes (commits without agent sessions)

Everything is computed from the event logs; finalized and live sessions
both count. The command never writes anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

from archrev.config import ARCHREV_DIRNAME
from archrev.storage import HUMAN_SESSION_ID, SessionStore


@dataclass
class RepoMetrics:
    """Aggregated numbers for one repository."""

    repo: str = ""
    sessions: int = 0
    finalized: int = 0
    with_plan: int = 0
    with_passing_check: int = 0
    edits: int = 0
    files_touched: int = 0
    out_of_plan: int = 0
    gate_asks: int = 0
    gate_denies: int = 0
    gate_flags: int = 0
    bypasses: int = 0
    quality_failures: int = 0
    acks: int = 0
    human_change_events: int = 0
    chain_breaks: int = 0

    def add(self, other: "RepoMetrics") -> None:
        for f in fields(self):
            if f.type == "int" or f.type is int:
                setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    @property
    def drift_rate(self) -> float | None:
        """Share of touched files that were never declared in a plan."""
        if not self.files_touched:
            return None
        return self.out_of_plan / self.files_touched


def discover_repos(paths: list[Path]) -> list[Path]:
    """Resolve arguments into repository roots that contain ``.archrev``.

    A path that is itself an ArchRev repo counts directly; otherwise its
    immediate children are scanned (the "directory full of checkouts"
    case). Deliberately shallow — no recursive filesystem crawls.
    """
    roots: list[Path] = []
    for path in paths:
        path = path.resolve()
        if (path / ARCHREV_DIRNAME).is_dir():
            roots.append(path)
            continue
        if path.is_dir():
            roots.extend(
                child
                for child in sorted(path.iterdir())
                if child.is_dir() and (child / ARCHREV_DIRNAME).is_dir()
            )
    # De-duplicate while preserving order.
    seen: dict[Path, None] = {}
    for root in roots:
        seen.setdefault(root, None)
    return list(seen)


def collect_repo(root: Path) -> RepoMetrics:
    """Compute metrics for one repository from its session event logs."""
    m = RepoMetrics(repo=str(root))
    store = SessionStore(root)
    for session in store.list_sessions():
        events = session.events()
        if session.id == HUMAN_SESSION_ID:
            m.human_change_events += sum(
                1 for e in events if e.get("type") == "human_changes"
            )
            continue
        m.sessions += 1
        if session.manifest() is not None:
            m.finalized += 1

        has_plan = False
        declared: set[str] = set()
        passing_check = False
        touched: dict[str, None] = {}
        latest_quality: dict[str, bool | None] = {}
        for e in events:
            etype = e.get("type")
            if etype == "plan_registered":
                has_plan = True
                declared = {
                    str(p).lower()
                    for p in (e.get("declared_files") or [])
                    if isinstance(p, str)
                }
            elif etype == "plan_check" and e.get("ok"):
                passing_check = True
            elif etype == "edit":
                m.edits += 1
                path = str(e.get("path") or "")
                if path:
                    touched.setdefault(path.lower(), None)
            elif etype == "gate":
                permission = e.get("permission")
                if permission == "ask":
                    m.gate_asks += 1
                elif permission == "deny":
                    m.gate_denies += 1
                elif e.get("hits"):
                    m.gate_flags += 1
            elif etype == "quality_check":
                latest_quality[str(e.get("rule_id"))] = e.get("ok")
            elif etype == "ack":
                m.acks += 1

        m.with_plan += 1 if has_plan else 0
        m.with_passing_check += 1 if passing_check else 0
        m.files_touched += len(touched)
        if has_plan:
            m.out_of_plan += sum(1 for t in touched if t not in declared)
        m.quality_failures += sum(
            1 for ok in latest_quality.values() if ok is False
        )

        manifest = session.manifest() or {}
        m.bypasses += sum(
            1
            for f in manifest.get("protected_findings", [])
            if not f.get("via_gate")
        )
        if not session.verify_chain()["ok"]:
            m.chain_breaks += 1
    return m


def collect(paths: list[Path]) -> tuple[list[RepoMetrics], RepoMetrics]:
    """Metrics per repository plus the aggregated total."""
    per_repo = [collect_repo(root) for root in discover_repos(paths)]
    total = RepoMetrics(repo="TOTAL")
    for m in per_repo:
        total.add(m)
    return per_repo, total
