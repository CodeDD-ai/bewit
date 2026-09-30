"""Bewit command-line interface.

Two audiences share this CLI:

- **Agent runtimes** invoke ``bewit hook`` (Cursor, Claude Code, Codex)
  and git invokes ``bewit git-trailer`` — the always-on capture path.
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

from bewit import __version__
from bewit.config import find_root, load_config
from bewit.storage import Session, SessionStore

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _require_root() -> Path:
    root = find_root()
    if root is None:
        raise click.ClickException(
            "No repository found (looked for .bewit or .git upward from "
            "the current directory). Run `bewit init` at your repo root."
        )
    return root


def _hydrate(root: Path) -> None:
    """Fill in sessions from refs/bewit/records before reading (no network)."""
    from bewit.refstore import hydrate_quietly

    hydrate_quietly(root)


def _resolve_session(root: Path, ref: str) -> Session:
    _hydrate(root)
    session = SessionStore(root).resolve(ref)
    if session is None:
        raise click.ClickException(
            f"Unknown session '{ref}'. List sessions with `bewit sessions`."
        )
    return session


def _resolve_writer(root: Path, ref: str, verb: str) -> Session:
    """The session a writing command (``plan register``, ``check plan``,
    ``ack``) acts on: ``latest`` means the calling session, never a guess
    among several live ones."""
    _hydrate(root)
    session, how = SessionStore(root).resolve_caller(ref, verb)
    if session is not None:
        return session
    if ref != "latest":
        raise click.ClickException(
            f"Unknown session '{ref}'. List sessions with `bewit sessions`."
        )
    if "active" in how or "at once" in how:
        raise click.ClickException(
            f"Cannot tell which session runs this command ({how}). Pass "
            "--session <id> (see `bewit sessions`), or set BEWIT_SESSION."
        )
    raise click.ClickException("No session recorded yet in this repository.")


@click.group()
@click.version_option(version=__version__, prog_name="bewit")
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
@click.option(
    "--shim",
    type=click.Choice(["none", "uvx"]),
    default="none",
    show_default=True,
    help="Command shim for hooks. 'uvx' writes hooks as `uvx bewit ...`, "
    "which auto-installs Bewit on first use: commit the wiring once and "
    "every teammate is captured with zero setup (only uv required).",
)
@click.option(
    "--runtime",
    "runtimes",
    multiple=True,
    type=click.Choice(["cursor", "claude", "codex"]),
    help="Agent runtime to wire (repeatable). Default: cursor, claude, and "
    "codex. Use --runtime cursor to install only Cursor wiring.",
)
@click.option(
    "--record-store",
    type=click.Choice(["ref", "tree"]),
    default=None,
    help="Where committed session records live: 'ref' (refs/bewit/records, "
    "never in code commits; the default for new repositories) or 'tree' "
    "(.bewit/sessions/, committed with the code). Sets record_store in "
    "config.yaml; omit to keep the current setting.",
)
def init(
    target: Path | None, shim: str, runtimes: tuple[str, ...], record_store: str | None
) -> None:
    """Install Bewit into a repository (idempotent, merge-safe)."""
    from bewit.scaffold import init_repo

    root = (target or Path.cwd()).resolve()
    selected = runtimes or ("cursor", "claude", "codex")
    result = init_repo(root, shim=shim, runtimes=selected, record_store=record_store)
    click.echo(f"Bewit in {root}")
    for item in result.created:
        click.secho(f"  written  {_rel(root, item)}", fg="green")
    for item in result.skipped:
        click.echo(f"  kept     {_rel(root, item)}")
    for warning in result.warnings:
        click.secho(f"  warning  {warning}", fg="yellow")
    click.echo(f"\nWired: {', '.join(selected)}.")
    if load_config(root).record_store == "ref":
        click.echo(
            "Records: published to refs/bewit/records, never in code commits. "
            "`git push` shares them (pre-push hook); `bewit sync` fetches colleagues'."
        )
    if "codex" in selected:
        click.secho(
            "  Codex runs project hooks only after you trust them: run /hooks in Codex.",
            fg="yellow",
        )
    click.echo(
        "\nNext:\n"
        "  1. Review .bewit/rules/ (00-starter-rules.yaml has disabled examples).\n"
        "  2. Commit .bewit/ and the hook wiring: they are the policy and the record.\n"
        "  3. Run `bewit serve`, then start an agent session in this folder."
    )


def _rel(root: Path, item: str | Path) -> str:
    """``item`` relative to ``root`` with forward slashes, when inside it."""
    try:
        return Path(item).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return str(item)


@main.command()
@click.argument(
    "event",
    required=False,
    default="auto",
    type=click.Choice(
        [
            "auto", "prompt", "edit", "gate", "shell", "read", "mcp",
            "exec_end", "finalize",
        ]
    ),
)
def hook(event: str) -> None:
    """Hook adapter (reads the event payload from stdin).

    Cursor wires ``bewit hook <event>``. Claude Code and Codex wire a
    bare ``bewit hook``; the payload's ``hook_event_name`` selects the
    handler. Not intended for manual use. Never fails: on internal errors
    it logs to .bewit/hook-errors.log and answers permissively.
    """
    from bewit.hooks import run_hook

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
        from bewit.trailer import add_trailers

        root = find_root()
        if root is not None:
            add_trailers(msg_file, root)
            # record_store: ref - the records this commit links to are
            # published with it (a separate ref and a temporary index, so
            # the commit in progress is untouched).
            from bewit.refstore import publish_quietly

            publish_quietly(root, load_config(root))
    except Exception:  # noqa: BLE001 — must never break a commit
        pass


# ---------------------------------------------------------------------------
# In-session commands (run by the agent, per .cursor/rules/bewit.mdc)
# ---------------------------------------------------------------------------


@main.group()
def plan() -> None:
    """Register the implementation plan for the current session."""


@plan.command(name="register")
@click.argument(
    "plan_file", required=False, type=click.Path(exists=True, path_type=Path)
)
@click.option("--text", "plan_text", default=None, help="Inline plan text.")
@click.option(
    "--session", "session_ref", default="latest", show_default=True,
    help="Session id or prefix. 'latest' binds to the calling agent's session "
    "and refuses to guess when several sessions are live.",
)
@click.option(
    "--replace",
    is_flag=True,
    help="Start the declaration over instead of adding to the session's "
    "earlier plan (the default merges, so earlier steps stay declared).",
)
@click.option("--amend", is_flag=True, hidden=True)  # merging is now the default
def plan_register(
    plan_file: Path | None,
    plan_text: str | None,
    session_ref: str,
    replace: bool,
    amend: bool,
) -> None:
    """Snapshot the plan (file or --text) into the session record.

    Registering again in the same session adds the new plan's files to the
    earlier declaration; pass --replace to start over.
    """
    from bewit.planning import register_plan, unresolved_paths

    if (plan_file is None) == (plan_text is None):
        raise click.ClickException(
            "Provide the plan as a file or with --text (exactly one), e.g. "
            "bewit plan register --text \"Edit `src/app.py` to ...\""
        )
    if replace and amend:
        raise click.ClickException("--replace and --amend contradict each other.")
    root = _require_root()
    session = _resolve_writer(root, session_ref, "plan register")
    text = plan_text if plan_text is not None else plan_file.read_text(encoding="utf-8")
    origin = str(plan_file) if plan_file else "inline"
    previous = session.last_event("plan_registered") if not replace else None
    prior = {
        p.lower() for p in (previous or {}).get("declared_files") or []
        if isinstance(p, str)
    }
    declared = register_plan(session, text, root, origin=origin, amend=not replace)
    click.echo(f"Plan registered for session {session.id}.")
    added = [p for p in declared if p.lower() not in prior]
    if prior:
        click.echo(
            f"Declared files ({len(declared)}: {len(added)} new, "
            f"{len(declared) - len(added)} from earlier plans this session):"
        )
    else:
        click.echo(f"Declared files ({len(declared)}):")
    for path in declared:
        marker = "+" if prior and path.lower() not in prior else "-"
        click.echo(f"  {marker} {path}")
    if not declared:
        click.secho(
            "  (none detected - mention concrete file paths in the plan so "
            "drift can be measured)",
            fg="yellow",
        )
    missing = unresolved_paths(added, root)
    if missing:
        click.secho(
            "\nNot in the repository (a new file, or a mistyped path?):", fg="yellow"
        )
        for path, suggestion in missing:
            hint = f"   did you mean {suggestion}?" if suggestion else "   (new file)"
            click.secho(f"  ? {path}{hint}", fg="yellow")
        if any(s for _, s in missing):
            click.echo(
                "Register again with the corrected paths; the plan merges, so "
                "only the corrections are needed."
            )


@main.group()
def check() -> None:
    """Validate plans and diffs against the rules."""


def _parse_attestations(pairs: tuple[str, ...]) -> dict[str, str]:
    attests: dict[str, str] = {}
    for pair in pairs:
        rule_id, sep, verdict = pair.partition("=")
        verdict = {"na": "n/a"}.get(verdict.strip().lower(), verdict.strip().lower())
        from bewit.planning import VERDICTS

        if not sep or verdict not in VERDICTS:
            raise click.ClickException(
                f"Invalid attestation '{pair}' (expected <rule-id>=pass|fail|n/a)."
            )
        attests[rule_id.strip()] = verdict
    return attests


@check.command(name="plan")
@click.option(
    "--attest",
    "attest_pairs",
    multiple=True,
    help="Prompt-rule verdict as <rule-id>=pass|fail|n/a (repeatable). "
    "n/a states the policy does not apply to this plan.",
)
@click.option("--note", default=None, help="Free-text note stored with the check.")
@click.option("--session", "session_ref", default="latest", show_default=True)
def check_plan_cmd(
    attest_pairs: tuple[str, ...], note: str | None, session_ref: str
) -> None:
    """Check the registered plan against path and prompt rules.

    Verdicts from the previous check carry over for policies whose part of
    the plan is unchanged; only new or changed ones need --attest. A check
    without --attest that would not pass is shown but not recorded.

    Exits non-zero when any prompt rule is failed or unattested, so the
    outcome is unmissable in the agent's shell output.
    """
    from bewit.planning import check_plan
    from bewit.rules import load_rules

    root = _require_root()
    session = _resolve_writer(root, session_ref, "check plan")
    config = load_config(root)
    ruleset = load_rules(root)
    report = check_plan(
        config, ruleset, session, _parse_attestations(attest_pairs), note
    )

    declared = report["declared_files"]
    click.echo(f"Plan: {len(declared)} declared file(s)" if report["plan_registered"]
               else "Plan: none registered")
    if report["path_findings"]:
        click.echo("\nWhat the gate will do with the declared files:")
        effect = {"deny": "refused", "block": "pauses for approval", "flag": "flagged"}
        for f in report["path_findings"]:
            color = {"deny": "red", "block": "yellow"}.get(f["action"], "cyan")
            click.secho(
                f"  {effect.get(f['action'], f['action']):<20} {f['path']}  [{f['rule_id']}]",
                fg=color,
            )
    if report["prompt_rules"]:
        click.echo("\nPolicies:")
    for pr in report["prompt_rules"]:
        color = {"pass": "green", "fail": "red", "n/a": "cyan"}.get(pr["verdict"], "yellow")
        scope = f" ({pr['scope']})" if pr.get("scope") else ""
        if pr.get("carried"):
            scope += " (kept from the previous check: its part of the plan is unchanged)"
        click.secho(f"  {pr['verdict']:<10} {pr['rule_id']}{scope}", fg=color)
        click.echo(f"             {pr['policy']}")
    for unknown in report["unknown_attestations"]:
        click.secho(f"  unknown rule id in --attest: {unknown}", fg="yellow")

    click.echo("")
    if report["ok"]:
        click.secho("Plan check passed (recorded).", fg="green")
        return
    reasons = []
    if not report["plan_registered"]:
        reasons.append("no plan is registered: run `bewit plan register --text \"...\"` "
                       "naming the files you will change")
    unattested = [p["rule_id"] for p in report["prompt_rules"] if p["verdict"] == "unattested"]
    failed = [p["rule_id"] for p in report["prompt_rules"] if p["verdict"] == "fail"]
    if unattested:
        reasons.append("not attested: " + ", ".join(unattested) + ". Add "
                       + " ".join(f"--attest {r}=pass|fail|n/a" for r in unattested))
    if failed:
        reasons.append("attested fail: " + ", ".join(failed)
                       + ". Resolve these with the user before implementing")
    if not report.get("recorded", True):
        click.secho(
            "Plan check did NOT pass (not recorded: no --attest was given, so "
            "the previous check stands):",
            fg="red",
        )
        for reason in reasons:
            click.secho(f"  - {reason}", fg="red")
        sys.exit(1)
    click.secho("Plan check did NOT pass (recorded):", fg="red")
    for reason in reasons:
        click.secho(f"  - {reason}", fg="red")
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
    from bewit.gate import rule_hits_for_paths
    from bewit.gitutil import Git
    from bewit.quality import run_checks
    from bewit.rules import load_rules

    root = _require_root()
    git = Git(root)
    if not git.is_repo():
        raise click.ClickException("Not a git repository.")
    changed = [e.path for e in git.numstat(base)] + git.untracked_files()
    ruleset = load_rules(root)

    blocking = False
    hits = rule_hits_for_paths(ruleset, changed)
    for hit in hits:
        color = "red" if hit.action in ("block", "deny") else "yellow"
        blocking = blocking or hit.action in ("block", "deny")
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

    blocked_paths = sum(1 for h in hits if h.action in ("block", "deny"))
    if blocking or quality_failed:
        parts = []
        if blocked_paths:
            parts.append(f"{blocked_paths} protected path(s) changed")
        if quality_failed:
            parts.append("a required quality check failed")
        click.secho(
            f"\nFAILED: {' and '.join(parts)} ({len(changed)} changed file(s) scanned).",
            fg="red",
        )
    else:
        note = f", {len(hits)} flagged for review" if hits else ""
        click.secho(
            f"\nOK: {len(changed)} changed file(s), no blocking findings{note}.",
            fg="green",
        )
    sys.exit(1 if (blocking or quality_failed) else 0)


# ---------------------------------------------------------------------------
# Review commands
# ---------------------------------------------------------------------------


@main.group(invoke_without_command=True)
@click.pass_context
def rules(ctx: click.Context) -> None:
    """List active rules and any loading problems."""
    if ctx.invoked_subcommand is None:
        _print_rules()


def _print_rules() -> None:
    from bewit.rules import load_rules

    root = _require_root()
    ruleset = load_rules(root)
    config = load_config(root)
    on_off = {True: "on", False: "off"}
    click.echo(
        f"Config ({_rel(root, root / '.bewit' / 'config.yaml')}): "
        f"enforcement {config.enforcement} | strict plan check {on_off[config.strict_plan_check]} | "
        f"protected scan {on_off[config.protected_scan]} | end-of-turn review "
        f"{on_off[config.final_check]} | prompts {config.prompt_capture} | "
        f"record scope {config.record_scope}"
    )
    sections = (
        ("File edits", ruleset.path_rules),
        ("File reads", ruleset.read_rules),
        ("Shell commands", ruleset.shell_rules),
        ("MCP tools", ruleset.mcp_rules),
        ("Agent tools", ruleset.tool_rules),
        ("Quality checks (end of each turn, and CI)", ruleset.check_rules),
    )
    effect = {"deny": "refuse", "block": "ask", "flag": "flag"}
    color = {"deny": "red", "block": "yellow", "flag": "cyan"}
    for title, group in sections:
        if not group:
            continue
        click.secho(f"\n{title} ({len(group)})", bold=True)
        for rule in group:
            click.secho(f"  {effect.get(rule.action, rule.action):<7}", fg=color.get(rule.action), nl=False)
            click.echo(f" {rule.id}")
            targets = rule.match or rule.patterns
            click.echo(f"          {'  '.join(targets)}")
            if rule.applies_to:
                click.echo(f"          only in: {', '.join(rule.applies_to)}")
            if rule.command:
                click.echo(f"          runs: {rule.command}")
            if rule.message:
                click.secho(f"          {rule.message}", dim=True)
    click.secho(f"\nPlan policies the agent attests ({len(ruleset.prompt_rules)})", bold=True)
    for rule in ruleset.prompt_rules:
        scope = f"  (only for plans touching {', '.join(rule.applies_to)})" if rule.applies_to else ""
        click.echo(f"  {rule.id}{scope}")
        click.secho(f"          {rule.policy}", dim=True)
    if not ruleset.rules:
        click.echo("\nNo rules yet. See .bewit/rules/00-starter-rules.yaml for examples.")
    if ruleset.errors:
        click.secho(f"\nProblems ({len(ruleset.errors)}):", fg="yellow")
        for error in ruleset.errors:
            click.secho(f"  {error}", fg="yellow")
    from bewit.scaffold import wiring_problems

    stale = wiring_problems(root)
    if stale:
        click.secho(
            "\nHook wiring problems (run `bewit init` to upgrade stale wiring; "
            "remove disableAllHooks yourself):",
            fg="yellow",
        )
        for problem in stale:
            click.secho(f"  {problem}", fg="yellow")


def explain_path(root: Path, relpath: str, kind: str) -> str:
    """Human answer to 'why did this path (not) gate?'."""
    from bewit import globmatch
    from bewit.rules import load_rules

    ruleset = load_rules(root)
    matched = (
        ruleset.match_read(relpath) if kind == "read" else ruleset.match_path(relpath)
    )
    lines = [f"{relpath}  ({kind})"]
    exempt = [g for g in load_config(root).exempt if globmatch.matches_any((g,), relpath)]
    if exempt:
        # The gate skips exempt paths before any rule is evaluated.
        lines.append(f"  exempt (config `exempt: {exempt[0]}`): never "
                     f"{'refused or paused' if kind == 'read' else 'paused, denied, or flagged'} by the gate.")
        if matched:
            lines.append("  Without the exemption these rules would apply: "
                         + ", ".join(f"{r.id} [{r.action}]" for r in matched) + ".")
        if kind == "read":
            lines.append("  Shell rules match command text and do not consult `exempt`.")
        return "\n".join(lines)
    if not matched:
        lines.append("  no rule matches.")
        if kind == "edit":
            lines.append(
                "  An edit of this path is not paused, denied, or flagged. "
                "It can still show up as out-of-plan drift if a plan was "
                "registered and did not declare it."
            )
        else:
            lines.append("  A read of this path is not gated.")
        return "\n".join(lines)
    lines.append(f"  {len(matched)} matching rule(s):")
    for rule in matched:
        effect = {
            "deny": "refused outright",
            "block": "paused for your approval (denied on Codex, which cannot ask)",
            "flag": "allowed, recorded, and highlighted",
        }.get(rule.action, rule.action)
        lines.append(f"  [{rule.action:5}] {rule.id}: {effect}")
        if rule.message:
            lines.append(f"          {rule.message}")
    if kind == "edit":
        lines.append(
            "  Checkpoints: live edit gate; plan-check preview; "
            "end-of-session protected scan; `bewit check diff` in CI."
        )
    else:
        lines.append(
            "  Checkpoint: live read gate only (beforeReadFile / PreToolUse Read). "
            "Reads are not part of the diff scan or CI path check."
        )
    return "\n".join(lines)


@rules.command(name="explain")
@click.argument("path")
@click.option(
    "--kind",
    type=click.Choice(["all", "edit", "read"]),
    default="all",
    show_default=True,
    help="Which rule family to evaluate (default: both edits and reads).",
)
def rules_explain(path: str, kind: str) -> None:
    """Show which rules match PATH and what each checkpoint would do.

    Answers "why was this edit not prompted?" and "can the agent read
    this?" - often the honest answer is that no rule covers the path.
    """
    from bewit.gate import relativize

    root = _require_root()
    rel = relativize(path, root) or path.replace("\\", "/")
    kinds = ("edit", "read") if kind == "all" else (kind,)
    click.echo("\n\n".join(explain_path(root, rel, k) for k in kinds))


@main.command()
@click.option("--limit", default=20, show_default=True)
def sessions(limit: int) -> None:
    """List recent sessions, most recent first."""
    from bewit.report.render import clean_title, fmt_when, session_state, short_id
    from bewit.report.server import _session_summary

    root = _require_root()
    _hydrate(root)
    store = SessionStore(root)
    agent, other = [], []
    for session in store.list_sessions():
        (agent if session.id not in ("human", "unknown") else other).append(session)
    if not agent and not other:
        click.echo("No sessions recorded yet. Start an agent session in this repository.")
        return
    click.secho(f"{'ID':<9} {'STARTED':<13} {'STATUS':<10} {'RUNTIME':<8} {'EDITS':>5}  "
                f"{'REVIEW':<12} TITLE", bold=True)
    for session in agent[:limit]:
        summary = _session_summary(root, session)
        prompts = [p for p in session.prompts()
                   if not str(p.get("text") or "").startswith("Bewit final review")]
        title = clean_title(prompts[0].get("text") if prompts else "", 60)
        state = session_state(summary.get("last_activity"))
        review = f"{summary['to_review']} to review" if summary.get("to_review") else "-"
        click.echo(f"{short_id(session.id):<9} {fmt_when(summary.get('started_at')):<13} ", nl=False)
        click.secho(f"{state:<10}", fg="green" if state == "recording" else None, nl=False)
        click.echo(f" {str(summary.get('runtime') or '-'):<8} {summary.get('edits', 0):>5}  ", nl=False)
        click.secho(f"{review:<12}", fg="red" if summary.get("to_review") else None, nl=False)
        click.echo(f" {title}")
    hidden = len(agent) - limit
    notes = []
    if hidden > 0:
        notes.append(f"{hidden} older (--limit {len(agent)})")
    if other:
        notes.append("other records: " + ", ".join(s.id for s in other))
    if notes:
        click.secho("\n" + " | ".join(notes), dim=True)
    click.secho("Details: bewit show <ID>  |  viewer: bewit serve", dim=True)


@main.command()
@click.argument("session_ref", default="latest")
@click.option("--md", "as_markdown", is_flag=True, help="Render as markdown.")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), default=None,
    help="Write to a file instead of stdout.",
)
@click.option(
    "--unseal",
    is_flag=True,
    help="Decrypt sealed prompts with the local seal key. Asks for a "
    "passphrase when the key is protected. Plaintext is printed, not stored.",
)
@click.option(
    "--all", "show_all", is_flag=True,
    help="Also list every command, file read, and change not made by this session.",
)
def show(
    session_ref: str, as_markdown: bool, output: Path | None, unseal: bool, show_all: bool
) -> None:
    """Show one session, review first (default: the latest).

    SESSION_REF is an id or a unique prefix (the 8 characters `bewit
    sessions` prints).
    """
    from bewit.drift import load_view
    from bewit.report.render import render_markdown, render_text
    from bewit.rules import load_rules

    root = _require_root()
    session = _resolve_session(root, session_ref)
    view = load_view(root, load_config(root), load_rules(root), session)
    if unseal:
        _unseal_view_prompts(session, view)
    text = render_markdown(view) if as_markdown else render_text(view, full=show_all)
    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(f"Written to {output}")
    else:
        click.echo(text)


def _unseal_view_prompts(session: Session, view: dict) -> None:
    """Replace sealed prompt excerpts in ``view`` with plaintext. Not persisted."""
    from bewit.seal import KEY_PATH, SealUnavailable, open_sealed

    events = [e for e in session.events() if e.get("type") == "prompt"]
    sealed = [e for e in events if e.get("capture") == "sealed" and e.get("sealed")]
    if not sealed:
        click.echo("No sealed prompts in this session.")
        return
    passphrase = None
    try:
        blob = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        blob = {}
    if "private_key_sealed" in blob:
        passphrase = click.prompt("Seal passphrase", hide_input=True)
    prompts = view.get("prompts") or []
    for event, prompt in zip(events, prompts):
        sealed_blob = event.get("sealed")
        if event.get("capture") != "sealed" or not isinstance(sealed_blob, dict):
            continue
        try:
            prompt["text"] = open_sealed(sealed_blob, passphrase)
        except SealUnavailable as exc:
            raise click.ClickException(str(exc)) from exc


@main.command()
@click.argument("target")
def trace(target: str) -> None:
    """Reverse lookup: which sessions produced a file or a commit.

    TARGET is a repo-relative path or a commit SHA (short or full).
    """
    from bewit.gate import relativize
    from bewit.gitutil import TRAILER_KEY, Git

    root = _require_root()
    _hydrate(root)
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
        click.echo(f"No session found for '{target}'. "
                   "(Only changes made by agent tools or linked commits are traced.)")
        return
    from bewit.report.render import clean_title, fmt_when, short_id
    from bewit.report.server import _session_summary

    what = "commit" if _SHA_RE.match(target) else "file"
    click.echo(f"{len(matches)} session(s) produced {what} {target}:")
    for session, via in matches:
        summary = _session_summary(root, session)
        prompts = [p for p in session.prompts()
                   if not str(p.get("text") or "").startswith("Bewit final review")]
        title = clean_title(prompts[0].get("text") if prompts else "", 80)
        review = f"  {summary['to_review']} to review" if summary.get("to_review") else ""
        click.echo(f"\n  {short_id(session.id)}  {fmt_when(summary.get('started_at'))}  "
                   f"{summary.get('runtime') or 'agent'}  (via {via})", nl=False)
        click.secho(review, fg="red")
        click.echo(f"    \"{title}\"")
        click.secho(f"    bewit show {short_id(session.id)}", dim=True)


@main.group()
def ci() -> None:
    """Generate CI enforcement templates."""


@ci.command(name="gitlab")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), default=None,
    help="Output file (default: .gitlab/bewit-ci.yml).",
)
def ci_gitlab(output: Path | None) -> None:
    """Write a GitLab CI template: rule + quality gate, chain verification,
    and an optional MR comment with the session report.

    CI enforcement needs zero developer adoption - the jobs run against
    every merge request regardless of what is installed locally.
    """
    from bewit.ci import write_gitlab_template

    root = _require_root()
    target = write_gitlab_template(root, output)
    click.echo(f"Written {target}")
    try:
        include = target.relative_to(root).as_posix()
    except ValueError:
        include = str(target)
    click.echo("\nAdd to your .gitlab-ci.yml:")
    click.echo(f"  include:\n    - local: {include}")
    click.echo(
        "\nOptional: set BEWIT_GITLAB_TOKEN (project access token, api "
        "scope) to post session reports as MR comments."
    )


@main.command()
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def index(paths: tuple[Path, ...], as_json: bool) -> None:
    """Aggregate oversight metrics across repositories (read-only).

    PATHS are repository roots or parent directories of checkouts
    (default: current directory). Pull-based by design: the session
    records already travel with git, so this reads what is checked out -
    no streaming, no telemetry, no server.
    """
    from dataclasses import asdict

    from bewit.metrics import collect

    targets = list(paths) or [Path.cwd()]
    per_repo, total = collect(targets)
    if not per_repo:
        raise click.ClickException(
            "No Bewit repositories found (looked for .bewit in the "
            "given paths and their immediate children)."
        )
    if as_json:
        payload = {
            "repos": [
                {**asdict(m), "drift_rate": m.drift_rate} for m in per_repo
            ],
            "total": {**asdict(total), "drift_rate": total.drift_rate},
        }
        click.echo(json.dumps(payload, indent=2))
        return

    rows = per_repo + ([total] if len(per_repo) > 1 else [])
    header = (
        f"{'repository':<40} {'sess':>5} {'plan':>5} {'chk':>4} {'drift':>6} "
        f"{'ask':>4} {'deny':>4} {'flag':>4} {'bypass':>6} {'qfail':>5} "
        f"{'ack':>4} {'human':>5} {'chain!':>6}"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for m in rows:
        name = Path(m.repo).name if m.repo != "TOTAL" else "TOTAL"
        drift = f"{m.drift_rate:.0%}" if m.drift_rate is not None else "-"
        line = (
            f"{name:<40} {m.sessions:>5} {m.with_plan:>5} "
            f"{m.with_passing_check:>4} {drift:>6} {m.gate_asks:>4} "
            f"{m.gate_denies:>4} {m.gate_flags:>4} {m.bypasses:>6} "
            f"{m.quality_failures:>5} {m.acks:>4} {m.human_change_events:>5} "
            f"{m.chain_breaks:>6}"
        )
        if m.chain_breaks or m.bypasses:
            click.secho(line, fg="red")
        elif m.drift_rate and m.drift_rate > 0.3:
            click.secho(line, fg="yellow")
        else:
            click.echo(line)
    click.echo(
        "\nplan/chk = sessions with a registered plan / a passing check; "
        "drift = out-of-plan share of touched files;\nbypass = protected "
        "changes outside the gate; qfail = failed quality checks; "
        "chain! = sessions with broken event chains."
    )


def _expand_ack_targets(
    root: Path, session: Session, targets: tuple[str, ...], earlier_plans: bool
) -> list[str]:
    """Concrete finding targets for ``ack``: patterns and ``--earlier-plans``
    expand against the session's open findings *now*, so an ack never
    covers a finding that did not exist when the reviewer made it."""
    from bewit import globmatch
    from bewit.drift import compute_view
    from bewit.rules import load_rules

    plain = [t.replace("\\", "/") for t in targets]
    patterns = [t for t in plain if any(c in t for c in "*?[")]
    out: dict[str, None] = {t: None for t in plain if t not in patterns}
    if not patterns and not earlier_plans:
        return list(out)
    view = compute_view(root, load_config(root), load_rules(root), session)
    open_items = view["review"]["open"]
    for pattern in patterns:
        hits = [
            i["target"] for i in open_items
            if globmatch.matches(pattern, i["target"])
        ]
        if not hits:
            click.secho(f"  no open finding matches '{pattern}'", fg="yellow")
        out.update(dict.fromkeys(hits))
    if earlier_plans:
        revisions = (view.get("plan") or {}).get("revisions") or []
        earlier = {
            p.lower() for rev in revisions[:-1] for p in rev.get("declared_files") or []
        }
        hits = [
            i["target"] for i in open_items
            if i["kind"] == "drift" and i["target"].lower() in earlier
        ]
        if not hits:
            click.secho(
                "  no open drift is declared in an earlier plan of this session",
                fg="yellow",
            )
        out.update(dict.fromkeys(hits))
    return list(out)


@main.command()
@click.argument("targets", nargs=-1)
@click.option(
    "--note",
    required=True,
    help="Why this finding is legitimate (stored in the audit log).",
)
@click.option(
    "--earlier-plans",
    is_flag=True,
    help="Also acknowledge every open drift finding whose file an earlier "
    "plan revision of this session declared.",
)
@click.option("--session", "session_ref", default="latest", show_default=True)
def ack(
    targets: tuple[str, ...], note: str, earlier_plans: bool, session_ref: str
) -> None:
    """Acknowledge final-review findings as legitimate.

    Each TARGET is the path a finding names (for gate bypasses and
    out-of-plan drift), a quality-check rule id, or the literal
    ``plan-check`` (for a failed check). Glob patterns (``'tests/**'``)
    select the open findings they match. Each acknowledgment is an
    audited, hash-chained event: the finding stops being re-raised at
    session end, but who acknowledged what, when, and why stays in the
    permanent record.

    Acknowledging is the reviewer's decision. When an agent command is
    running, the ack is recorded as agent-made and clears only plan drift.
    """
    from bewit.review import detect_ack_actor

    if not targets and not earlier_plans:
        raise click.ClickException("Name at least one TARGET, or pass --earlier-plans.")
    root = _require_root()
    session = _resolve_writer(root, session_ref, "ack")
    resolved = _expand_ack_targets(root, session, targets, earlier_plans)
    if not resolved:
        raise click.ClickException("Nothing to acknowledge.")
    actor, basis = detect_ack_actor(root, session)
    for normalized in resolved:
        session.append_event(
            "ack", target=normalized, note=note, actor=actor, actor_basis=basis, via="cli"
        )
    if actor in ("agent", "unknown"):
        label = "an AGENT" if actor == "agent" else "an UNVERIFIED"
        for normalized in resolved:
            click.secho(
                f"Recorded as {label} acknowledgment of '{normalized}' ({basis}).",
                fg="yellow",
            )
        click.echo(
            "It clears plan drift only; bypasses and failed checks still need "
            "the reviewer."
        )
        return
    for normalized in resolved:
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
    _hydrate(root)
    store = SessionStore(root)
    if verify_all:
        targets = store.list_sessions()
    else:
        targets = [_resolve_session(root, session_ref or "latest")]
    if not targets:
        click.echo("No sessions recorded yet.")
        return
    broken = 0
    local_only = 0
    for session in targets:
        if not session.event_log_present():
            local_only += 1
            manifest = session.manifest() or {}
            head = manifest.get("chain_head")
            head_note = f" committed chain head {str(head)[:12]}" if head else ""
            click.secho(
                f"  local   {session.id}: event log is not in this checkout"
                f"{head_note} - verify on the author's machine",
                fg="yellow",
            )
            continue
        result = session.verify_chain()
        if result["ok"]:
            notes = []
            if result["legacy"]:
                notes.append(f"{result['legacy']} legacy pre-chain event(s)")
            if result.get("duplicates"):
                notes.append(
                    f"{result['duplicates']} duplicate hook delivery(ies), content unchanged"
                )
            note = f" ({'; '.join(notes)})" if notes else ""
            click.secho(
                f"  ok      {session.id}: {result['checked']} event(s) verified{note}",
                fg="green",
            )
        else:
            broken += 1
            why = (
                "two events chain to the same predecessor - usually concurrent "
                "hook writes, but an inserted event looks the same"
                if result.get("reason") == "fork"
                else "an event was modified or removed after it was written"
            )
            click.secho(
                f"  BROKEN  {session.id}: chain breaks at event #{result['break_at']} - {why}",
                fg="red",
            )
    verified = len(targets) - broken - local_only
    summary = f"\n{verified} verified"
    if local_only:
        summary += f", {local_only} not verifiable here (event log on the author's machine)"
    summary += f", {broken} broken."
    click.secho(summary, fg="red" if broken else "green")
    if broken:
        sys.exit(1)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option(
    "--port",
    default=4177,
    show_default=True,
    help="Preferred port. If another project's viewer holds it, the next "
    "free port is used; a viewer for this project is reused.",
)
@click.option(
    "--all",
    "hub_dirs",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Serve every Bewit repository in DIR (the directory itself or its "
    "immediate children) with a project switcher. Repeatable.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    show_default=True,
    help="Open the viewer in the default browser.",
)
def serve(host: str, port: int, hub_dirs: tuple[Path, ...], open_browser: bool) -> None:
    """Serve the live session timeline (view-only; capture is independent).

    Without --all, the viewer shows only the project of the current
    directory. Capture never depends on the viewer.
    """
    from bewit.report.server import bind_viewer, run_server

    hub = bool(hub_dirs)
    if hub:
        from bewit.metrics import discover_repos

        roots = discover_repos(list(hub_dirs))
        if not roots:
            raise click.ClickException(
                "No Bewit repositories found (looked for .bewit in the "
                "given directories and their immediate children)."
            )
    else:
        roots = [_require_root()]

    try:
        binding = bind_viewer(roots, host, port, hub=hub)
    except OSError as exc:
        raise click.ClickException(f"Cannot start the viewer: {exc}") from exc

    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{binding.port}/"
    what = (
        f"{len(roots)} projects: " + ", ".join(r.name for r in roots)
        if hub
        else f"{roots[0].name}  ({roots[0]})"
    )
    if binding.displaced_by is not None:
        holder = (
            "the viewer for " + ", ".join(Path(p).name for p in binding.displaced_by)
            if binding.displaced_by
            else "another program"
        )
        click.secho(
            f"Port {port} is used by {holder}; this viewer uses {binding.port}.",
            fg="yellow",
        )
    if binding.reused:
        click.echo(f"A viewer for {what} is already running at {url}")
        if open_browser:
            _open_browser(url)
        return

    click.echo(f"Bewit viewer for {what}")
    click.echo(f"  {url}")
    click.echo("Press Ctrl+C to stop. Capture continues either way.")
    if open_browser:
        _open_browser(url)
    try:
        run_server(binding.server)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


def _open_browser(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — a missing browser must not stop the viewer
        pass


@main.command()
@click.option("--remote", default=None,
              help="Git remote to sync with. Default: the remote `git push` uses "
                   "(the branch's upstream), else origin, else the only remote.")
@click.option("--no-push", is_flag=True, help="Fetch and merge only; do not push.")
@click.option("--quiet", is_flag=True, help="Print nothing on success (used by the pre-push hook).")
def sync(remote: str | None, no_push: bool, quiet: bool) -> None:
    """Share session records via refs/bewit/records.

    Fetches the remote's records, merges them with the sessions recorded
    here (union), publishes, and pushes the ref. Code branches are never
    touched. With record_store: ref the pre-push hook runs this for you.
    """
    from bewit.refstore import RefStoreError
    from bewit.refstore import sync as run_sync

    root = _require_root()
    try:
        result = run_sync(root, load_config(root), remote=remote, push=not no_push)
    except RefStoreError as exc:
        raise click.ClickException(str(exc)) from exc
    if quiet:
        return
    click.echo(f"Records ref: refs/bewit/records ({result['remote']})")
    click.echo(f"  fetched    {'yes' if result['fetched'] else 'nothing yet'}")
    click.echo(f"  published  {'new commit ' + result['published'][:10] if result['published'] else 'no changes'}")
    click.echo(f"  hydrated   {result['hydrated']} file(s) from colleagues")
    if not no_push:
        click.secho(f"  pushed     {'yes' if result['pushed'] else 'no'}",
                    fg="green" if result["pushed"] else "yellow")
    for note in result["notes"]:
        click.secho(f"  note: {note}", dim=True)


@main.command()
@click.option("--keep-days", default=30, show_default=True, type=int)
def prune(keep_days: int) -> None:
    """Delete local event logs older than KEEP_DAYS.

    Only finalized sessions are touched. Manifests, plans, and reports stay,
    so the committed audit tier is unaffected. A live session is never pruned.
    """
    root = _require_root()
    removed = SessionStore(root).prune_event_logs(keep_days)
    if not removed:
        click.echo(f"Nothing older than {keep_days} days to prune.")
        return
    for session_id in removed:
        click.echo(f"  pruned  {session_id}")
    click.echo(f"Removed {len(removed)} local event log(s).")


@main.group()
def seal() -> None:
    """Encrypt prompts to committed public keys."""


@seal.command(name="keygen")
@click.option("--name", default=None, help="Recipient name (default: OS user).")
@click.option(
    "--protect",
    is_flag=True,
    help="Prompt for a passphrase that wraps the private key.",
)
def seal_keygen(name: str | None, protect: bool) -> None:
    """Create a seal key and add its public half to this repository.

    The private key is written to ~/.bewit/seal.key and is never committed.
    The public key is appended to .bewit/recipients.yaml.
    """
    import getpass

    from bewit.seal import SealUnavailable, add_recipient, generate_keypair

    root = _require_root()
    passphrase = None
    if protect:
        passphrase = click.prompt(
            "Passphrase", hide_input=True, confirmation_prompt=True
        )
    try:
        public = generate_keypair(passphrase)
    except SealUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    recipient = name or getpass.getuser()
    add_recipient(root, recipient, public)
    click.secho(f"Public key for '{recipient}' written to .bewit/recipients.yaml", fg="green")
    click.echo("Private key: ~/.bewit/seal.key (do not commit it).")
    click.echo("Set prompt_capture: sealed in .bewit/config.yaml to start sealing.")


@main.command()
@click.argument("session_ref", default="latest")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), default=None,
    help="Output file (default: bewit-session-<id>.html).",
)
def export(session_ref: str, output: Path | None) -> None:
    """Export one session as a self-contained HTML report."""
    from bewit.report.server import export_html

    root = _require_root()
    session = _resolve_session(root, session_ref)
    html = export_html(root, session.id)
    if html is None:
        raise click.ClickException(f"Could not build a view for '{session_ref}'.")
    target = output or Path(f"bewit-session-{session.id[:12]}.html")
    target.write_text(html, encoding="utf-8")
    click.echo(f"Exported to {target}")


if __name__ == "__main__":
    main()
