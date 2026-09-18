"""ArchRev command-line interface.

Two audiences share this CLI:

- **Cursor** invokes ``archrev hook <event>`` (wired by ``archrev init``)
  and git invokes ``archrev git-trailer`` — the always-on capture path.
- **Humans and agents** use everything else: ``plan register`` and
  ``check plan`` during a session, ``sessions`` / ``show`` / ``trace`` /
  ``serve`` / ``export`` to review at any point in time.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click

from archrev import __version__
from archrev.config import find_root, load_config
from archrev.storage import Session, SessionStore

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _require_root() -> Path:
    root = find_root()
    if root is None:
        raise click.ClickException(
            "No repository found (looked for .archrev or .git upward from "
            "the current directory). Run `archrev init` at your repo root."
        )
    return root


def _resolve_session(root: Path, ref: str) -> Session:
    session = SessionStore(root).resolve(ref)
    if session is None:
        raise click.ClickException(
            f"Unknown session '{ref}'. List sessions with `archrev sessions`."
        )
    return session


@click.group()
@click.version_option(version=__version__, prog_name="archrev")
def main() -> None:
    """Session provenance and architecture-rule enforcement for AI agents."""


# ---------------------------------------------------------------------------
# Setup and capture
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--path",
    "target",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repository root to install into (default: current directory).",
)
def init(target: Path | None) -> None:
    """Install ArchRev into a repository (idempotent, merge-safe)."""
    from archrev.scaffold import init_repo

    root = (target or Path.cwd()).resolve()
    result = init_repo(root)
    for item in result.created:
        click.echo(f"  created  {item}")
    for item in result.skipped:
        click.echo(f"  kept     {item}")
    for warning in result.warnings:
        click.secho(f"  warning  {warning}", fg="yellow")
    click.echo(
        "\nArchRev is installed. Cursor reloads hooks automatically; "
        "start an agent session and run `archrev serve` to watch it live."
    )


@main.command()
@click.argument(
    "event",
    type=click.Choice(
        ["prompt", "edit", "gate", "shell", "read", "mcp", "finalize"]
    ),
)
def hook(event: str) -> None:
    """Cursor hook adapter (reads the event payload from stdin).

    Not intended for manual use. Never fails: on internal errors it logs to
    .archrev/hook-errors.log and answers permissively.
    """
    from archrev.hooks import run_hook

    # Read raw bytes and decode as UTF-8 with BOM stripping: on Windows,
    # text-mode stdin decodes Cursor's UTF-8 payload with the locale codec,
    # which mangles the BOM and breaks JSON parsing (observed in the wild).
    raw = sys.stdin.buffer.read()
    response = run_hook(event, raw.decode("utf-8-sig", errors="replace"))
    click.echo(json.dumps(response))


@main.command(name="git-trailer", hidden=True)
@click.argument("msg_file", type=click.Path(path_type=Path))
def git_trailer(msg_file: Path) -> None:
    """prepare-commit-msg helper: append session trailers (never fails)."""
    try:
        from archrev.trailer import add_trailers

        root = find_root()
        if root is not None:
            add_trailers(msg_file, root)
    except Exception:  # noqa: BLE001 — must never break a commit
        pass


# ---------------------------------------------------------------------------
# In-session commands (run by the agent, per .cursor/rules/archrev.mdc)
# ---------------------------------------------------------------------------


@main.group()
def plan() -> None:
    """Register the implementation plan for the current session."""


@plan.command(name="register")
@click.argument(
    "plan_file", required=False, type=click.Path(exists=True, path_type=Path)
)
@click.option("--text", "plan_text", default=None, help="Inline plan text.")
@click.option("--session", "session_ref", default="latest", show_default=True)
def plan_register(
    plan_file: Path | None, plan_text: str | None, session_ref: str
) -> None:
    """Snapshot the plan (file or --text) into the session record."""
    from archrev.planning import register_plan

    if (plan_file is None) == (plan_text is None):
        raise click.ClickException("Provide either a plan file or --text, not both.")
    root = _require_root()
    session = _resolve_session(root, session_ref)
    text = plan_text if plan_text is not None else plan_file.read_text(encoding="utf-8")
    origin = str(plan_file) if plan_file else "inline"
    declared = register_plan(session, text, root, origin=origin)
    click.echo(f"Plan registered for session {session.id}.")
    click.echo(f"Declared files ({len(declared)}):")
    for path in declared:
        click.echo(f"  - {path}")
    if not declared:
        click.secho(
            "  (none detected - mention concrete file paths in the plan so "
            "drift can be measured)",
            fg="yellow",
        )


@main.group()
def check() -> None:
    """Validate plans and diffs against the rules."""


def _parse_attestations(pairs: tuple[str, ...]) -> dict[str, str]:
    attests: dict[str, str] = {}
    for pair in pairs:
        rule_id, sep, verdict = pair.partition("=")
        if not sep or verdict not in ("pass", "fail"):
            raise click.ClickException(
                f"Invalid attestation '{pair}' (expected <rule-id>=pass|fail)."
            )
        attests[rule_id.strip()] = verdict
    return attests


@check.command(name="plan")
@click.option(
    "--attest",
    "attest_pairs",
    multiple=True,
    help="Prompt-rule verdict as <rule-id>=pass|fail (repeatable).",
)
@click.option("--note", default=None, help="Free-text note stored with the check.")
@click.option("--session", "session_ref", default="latest", show_default=True)
def check_plan_cmd(
    attest_pairs: tuple[str, ...], note: str | None, session_ref: str
) -> None:
    """Check the registered plan against path and prompt rules.

    Exits non-zero when any prompt rule is failed or unattested, so the
    outcome is unmissable in the agent's shell output.
    """
    from archrev.planning import check_plan
    from archrev.rules import load_rules

    root = _require_root()
    session = _resolve_session(root, session_ref)
    config = load_config(root)
    ruleset = load_rules(root)
    report = check_plan(
        config, ruleset, session, _parse_attestations(attest_pairs), note
    )

    if not report["plan_registered"]:
        click.secho("No plan registered - run `archrev plan register` first.", fg="yellow")
    click.echo(f"Declared files: {len(report['declared_files'])}")
    if report["path_findings"]:
        click.echo("Gate preview (these edits will pause or be flagged):")
        for f in report["path_findings"]:
            color = "red" if f["action"] == "block" else "yellow"
            click.secho(
                f"  [{f['action']}] {f['path']}  ({f['rule_id']}) {f['message']}",
                fg=color,
            )
    for pr in report["prompt_rules"]:
        color = {"pass": "green", "fail": "red"}.get(pr["verdict"], "yellow")
        click.secho(f"  attest {pr['verdict']:>10}  {pr['rule_id']}", fg=color)
        click.echo(f"    policy: {pr['policy']}")
    for unknown in report["unknown_attestations"]:
        click.secho(f"  unknown rule id in --attest: {unknown}", fg="yellow")

    if report["ok"]:
        click.secho("Plan check OK - recorded.", fg="green")
    else:
        click.secho(
            "Plan check NOT OK (failed or unattested prompt rules) - recorded. "
            "Resolve with the user before implementing.",
            fg="red",
        )
        sys.exit(1)


@check.command(name="diff")
@click.option("--base", default=None, help="Git base ref (default: HEAD).")
@click.option(
    "--no-quality", is_flag=True, help="Skip check-rule commands (path scan only)."
)
def check_diff_cmd(base: str | None, no_quality: bool) -> None:
    """Scan current git changes against path and quality rules (CI usage).

    Exits 1 when any changed file matches a block path rule or any
    block-level quality check fails.
    """
    from archrev.gate import rule_hits_for_paths
    from archrev.gitutil import Git
    from archrev.quality import run_checks
    from archrev.rules import load_rules

    root = _require_root()
    git = Git(root)
    if not git.is_repo():
        raise click.ClickException("Not a git repository.")
    changed = [e.path for e in git.numstat(base)] + git.untracked_files()
    ruleset = load_rules(root)

    blocking = False
    hits = rule_hits_for_paths(ruleset, changed)
    for hit in hits:
        color = "red" if hit.action == "block" else "yellow"
        blocking = blocking or hit.action == "block"
        click.secho(
            f"  [{hit.action}] {hit.path}  ({hit.rule_id}) {hit.message}", fg=color
        )

    quality_failed = False
    if not no_quality and ruleset.check_rules:
        for result in run_checks(root, ruleset, changed):
            rule_id = result["rule_id"]
            if result["ok"] is True:
                click.secho(
                    f"  [check] {rule_id}: passed "
                    f"({len(result['files'])} file(s))",
                    fg="green",
                )
            elif result["ok"] is False:
                is_block = result["action"] == "block"
                quality_failed = quality_failed or is_block
                click.secho(
                    f"  [check] {rule_id}: FAILED ({result['action']}) - "
                    f"{result['message'] or 'see output below'}",
                    fg="red" if is_block else "yellow",
                )
                if result["output"].strip():
                    for line in result["output"].strip().splitlines()[-20:]:
                        click.echo(f"          {line}")
            else:
                click.secho(
                    f"  [check] {rule_id}: could not run - {result['error']}",
                    fg="yellow",
                )

    if not hits and not quality_failed:
        click.secho(
            f"OK: {len(changed)} changed file(s), no blocking findings.",
            fg="green",
        )
    sys.exit(1 if (blocking or quality_failed) else 0)


# ---------------------------------------------------------------------------
# Review commands
# ---------------------------------------------------------------------------


@main.command()
def rules() -> None:
    """List active rules and any loading problems."""
    from archrev.rules import load_rules

    root = _require_root()
    ruleset = load_rules(root)
    config = load_config(root)
    click.echo(
        f"enforcement={config.enforcement}  strict_plan_check={config.strict_plan_check}  "
        f"protected_scan={config.protected_scan}  final_check={config.final_check}"
    )
    sections = (
        ("Path rules (edits)", ruleset.path_rules),
        ("Read rules", ruleset.read_rules),
        ("Shell rules", ruleset.shell_rules),
        ("MCP rules", ruleset.mcp_rules),
        ("Tool rules", ruleset.tool_rules),
        ("Check rules (quality gates)", ruleset.check_rules),
    )
    for title, group in sections:
        if not group and title != "Path rules (edits)":
            continue
        click.echo(f"\n{title} ({len(group)}):")
        for rule in group:
            scope = f"  applies_to={list(rule.applies_to)}" if rule.applies_to else ""
            targets = list(rule.match) if rule.match else list(rule.patterns)
            click.echo(f"  [{rule.action:5}] {rule.id}: {targets}{scope}")
            if rule.command:
                click.echo(f"          run: {rule.command}")
            if rule.message:
                click.echo(f"          {rule.message}")
    click.echo(f"\nPrompt rules ({len(ruleset.prompt_rules)}):")
    for rule in ruleset.prompt_rules:
        click.echo(f"  {rule.id}: {rule.policy}")
    if ruleset.errors:
        click.secho(f"\nProblems ({len(ruleset.errors)}):", fg="yellow")
        for error in ruleset.errors:
            click.secho(f"  {error}", fg="yellow")


@main.command()
@click.option("--limit", default=20, show_default=True)
def sessions(limit: int) -> None:
    """List recent sessions, most recent first."""
    root = _require_root()
    all_sessions = SessionStore(root).list_sessions()[:limit]
    if not all_sessions:
        click.echo("No sessions recorded yet.")
        return
    for session in all_sessions:
        meta = session.meta() or {}
        prompts = session.prompts()
        excerpt = (
            str(prompts[0].get("text", "")).strip().splitlines()[0][:80]
            if prompts and str(prompts[0].get("text", "")).strip()
            else "(no prompt)"
        )
        state = "final" if session.manifest() is not None else "live "
        click.echo(
            f"{session.id:<40} {state} {meta.get('started_at', '?'):<21} {excerpt}"
        )


@main.command()
@click.argument("session_ref", default="latest")
@click.option("--md", "as_markdown", is_flag=True, help="Render as markdown.")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), default=None,
    help="Write to a file instead of stdout.",
)
def show(session_ref: str, as_markdown: bool, output: Path | None) -> None:
    """Show the full chain of one session (default: the latest)."""
    from archrev.drift import compute_view
    from archrev.report.render import render_markdown, render_text
    from archrev.rules import load_rules

    root = _require_root()
    session = _resolve_session(root, session_ref)
    view = compute_view(root, load_config(root), load_rules(root), session)
    text = render_markdown(view) if as_markdown else render_text(view)
    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(f"Written to {output}")
    else:
        click.echo(text)


@main.command()
@click.argument("target")
def trace(target: str) -> None:
    """Reverse lookup: which sessions produced a file or a commit.

    TARGET is a repo-relative path or a commit SHA (short or full).
    """
    from archrev.gate import relativize
    from archrev.gitutil import TRAILER_KEY, Git

    root = _require_root()
    store = SessionStore(root)
    matches: list[tuple[Session, str]] = []

    if _SHA_RE.match(target):
        message = Git(root).commit_message(target)
        if message:
            trailer_re = re.compile(rf"^{TRAILER_KEY}:\s*(\S+)", re.MULTILINE)
            for sid in trailer_re.findall(message):
                session = store.resolve(sid)
                if session:
                    matches.append((session, f"commit trailer {TRAILER_KEY}"))
        # Manifests may know commits even without trailers.
        for session in store.list_sessions():
            manifest = session.manifest() or {}
            for commit in manifest.get("commits", []):
                if commit.get("sha", "").startswith(target.lower()):
                    if all(s.id != session.id for s, _ in matches):
                        matches.append((session, "manifest commit list"))
    else:
        rel = relativize(target, root).lower()
        for session in store.list_sessions():
            if any(t.lower() == rel for t in session.touched_files()):
                matches.append((session, "edit event"))

    if not matches:
        click.echo(f"No session found for '{target}'.")
        return
    for session, via in matches:
        meta = session.meta() or {}
        prompts = session.prompts()
        excerpt = (
            str(prompts[0].get("text", "")).strip().splitlines()[0][:100]
            if prompts and str(prompts[0].get("text", "")).strip()
            else "(no prompt)"
        )
        click.echo(f"{session.id}  ({via})")
        click.echo(f"  started: {meta.get('started_at', '?')}")
        click.echo(f"  prompt:  {excerpt}")
        click.echo(f"  review:  archrev show {session.id}")


@main.command()
@click.argument("target")
@click.option(
    "--note",
    required=True,
    help="Why this finding is legitimate (stored in the audit log).",
)
@click.option("--session", "session_ref", default="latest", show_default=True)
def ack(target: str, note: str, session_ref: str) -> None:
    """Acknowledge a final-review finding as legitimate.

    TARGET is the path the finding names (for gate bypasses and
    out-of-plan drift) or the literal ``plan-check`` (for a failed check).
    The acknowledgment is an audited, hash-chained event: the finding stops
    being re-raised at session end, but who acknowledged what, when, and
    why stays in the permanent record.
    """
    root = _require_root()
    session = _resolve_session(root, session_ref)
    normalized = target.replace("\\", "/")
    session.append_event("ack", target=normalized, note=note)
    click.secho(
        f"Acknowledged '{normalized}' for session {session.id}.", fg="green"
    )
    click.echo("Recorded in the audit log; the final review will skip it.")


@main.command()
@click.argument("session_ref", default=None, required=False)
@click.option("--all", "verify_all", is_flag=True, help="Verify every session.")
def verify(session_ref: str | None, verify_all: bool) -> None:
    """Verify the tamper-evident hash chain of session event logs.

    Every event carries a hash over its content plus the previous event's
    hash. Any edit, reorder, or deletion inside the log breaks the chain.
    Exits 1 when any chain is broken.
    """
    root = _require_root()
    store = SessionStore(root)
    if verify_all:
        targets = store.list_sessions()
    else:
        targets = [_resolve_session(root, session_ref or "latest")]
    if not targets:
        click.echo("No sessions recorded yet.")
        return
    broken = 0
    for session in targets:
        result = session.verify_chain()
        if result["ok"]:
            note = f" ({result['legacy']} legacy pre-chain event(s))" if result["legacy"] else ""
            click.secho(
                f"  ok      {session.id}: {result['checked']} event(s) verified{note}",
                fg="green",
            )
        else:
            broken += 1
            click.secho(
                f"  BROKEN  {session.id}: chain breaks at event #{result['break_at']} "
                "- the log was modified after the fact",
                fg="red",
            )
    if broken:
        sys.exit(1)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=4177, show_default=True)
def serve(host: str, port: int) -> None:
    """Serve the live session timeline (view-only; capture is independent)."""
    from archrev.report.server import serve as run_server

    root = _require_root()
    click.echo(f"ArchRev viewer: http://{host}:{port}/  (root: {root})")
    click.echo("Press Ctrl+C to stop. Capture continues either way.")
    try:
        run_server(root, host, port)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@main.command()
@click.argument("session_ref", default="latest")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), default=None,
    help="Output file (default: archrev-session-<id>.html).",
)
def export(session_ref: str, output: Path | None) -> None:
    """Export one session as a self-contained HTML report."""
    from archrev.report.server import export_html

    root = _require_root()
    session = _resolve_session(root, session_ref)
    html = export_html(root, session.id)
    if html is None:
        raise click.ClickException(f"Could not build a view for '{session_ref}'.")
    target = output or Path(f"archrev-session-{session.id[:12]}.html")
    target.write_text(html, encoding="utf-8")
    click.echo(f"Exported to {target}")


if __name__ == "__main__":
    main()
