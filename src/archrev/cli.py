"""ArchRev command-line interface.

Two audiences share this CLI:

- **Agent runtimes** invoke ``archrev hook`` (Cursor, Claude Code, Codex)
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
@click.option(
    "--shim",
    type=click.Choice(["none", "uvx"]),
    default="none",
    show_default=True,
    help="Command shim for hooks. 'uvx' writes hooks as `uvx archrev ...`, "
    "which auto-installs ArchRev on first use: commit the wiring once and "
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
def init(target: Path | None, shim: str, runtimes: tuple[str, ...]) -> None:
    """Install ArchRev into a repository (idempotent, merge-safe)."""
    from archrev.scaffold import init_repo

    root = (target or Path.cwd()).resolve()
    selected = runtimes or ("cursor", "claude", "codex")
    result = init_repo(root, shim=shim, runtimes=selected)
    for item in result.created:
        click.echo(f"  created  {item}")
    for item in result.skipped:
        click.echo(f"  kept     {item}")
    for warning in result.warnings:
        click.secho(f"  warning  {warning}", fg="yellow")
    click.echo(
        "\nArchRev is installed. Runtimes wired: " + ", ".join(selected) + "."
    )
    if "cursor" in selected:
        click.echo("  Cursor reloads hooks automatically.")
    if "claude" in selected:
        click.echo("  Claude Code loads .claude/settings.json for this project.")
    if "codex" in selected:
        click.echo(
            "  Codex: run /hooks and trust the ArchRev commands before they fire."
        )
    click.echo("Start an agent session and run `archrev serve` to watch it live.")


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

    Cursor wires ``archrev hook <event>``. Claude Code and Codex wire a
    bare ``archrev hook``; the payload's ``hook_event_name`` selects the
    handler. Not intended for manual use. Never fails: on internal errors
    it logs to .archrev/hook-errors.log and answers permissively.
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
@click.option(
    "--amend",
    is_flag=True,
    help="Union newly declared files with the previous plan instead of "
    "replacing it. Use this when a session grows across batches.",
)
def plan_register(
    plan_file: Path | None,
    plan_text: str | None,
    session_ref: str,
    amend: bool,
) -> None:
    """Snapshot the plan (file or --text) into the session record."""
    from archrev.planning import register_plan

    if (plan_file is None) == (plan_text is None):
        raise click.ClickException("Provide either a plan file or --text, not both.")
    root = _require_root()
    session = _resolve_session(root, session_ref)
    text = plan_text if plan_text is not None else plan_file.read_text(encoding="utf-8")
    origin = str(plan_file) if plan_file else "inline"
    declared = register_plan(session, text, root, origin=origin, amend=amend)
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
        verdict = {"na": "n/a"}.get(verdict.strip().lower(), verdict.strip().lower())
        if not sep or verdict not in ("pass", "fail", "n/a"):
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
        color = {"pass": "green", "fail": "red", "n/a": "cyan"}.get(pr["verdict"], "yellow")
        scope = f"  ({pr['scope']})" if pr.get("scope") else ""
        click.secho(f"  attest {pr['verdict']:>10}  {pr['rule_id']}{scope}", fg=color)
        click.echo(f"    policy: {pr['policy']}")
    for unknown in report["unknown_attestations"]:
        click.secho(f"  unknown rule id in --attest: {unknown}", fg="yellow")

    if report["ok"]:
        click.secho("Plan check OK - recorded.", fg="green")
    else:
        click.secho(
            "Plan check NOT OK (no plan registered, or failed or unattested "
            "prompt rules) - recorded. "
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


@main.group(invoke_without_command=True)
@click.pass_context
def rules(ctx: click.Context) -> None:
    """List active rules and any loading problems."""
    if ctx.invoked_subcommand is None:
        _print_rules()


def _print_rules() -> None:
    from archrev.rules import load_rules

    root = _require_root()
    ruleset = load_rules(root)
    config = load_config(root)
    click.echo(
        f"enforcement={config.enforcement}  strict_plan_check={config.strict_plan_check}  "
        f"protected_scan={config.protected_scan}  final_check={config.final_check}  "
        f"prompt_capture={config.prompt_capture}  record_scope={config.record_scope}"
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
    from archrev.scaffold import wiring_problems

    stale = wiring_problems(root)
    if stale:
        click.secho("\nHook wiring is out of date (run `archrev init` to upgrade):", fg="yellow")
        for problem in stale:
            click.secho(f"  {problem}", fg="yellow")


def explain_path(root: Path, relpath: str, kind: str) -> str:
    """Human answer to 'why did this path (not) gate?'."""
    from archrev.rules import load_rules

    ruleset = load_rules(root)
    matched = (
        ruleset.match_read(relpath) if kind == "read" else ruleset.match_path(relpath)
    )
    lines = [f"{relpath}  ({kind})"]
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
            "end-of-session protected scan; `archrev check diff` in CI."
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
    type=click.Choice(["edit", "read"]),
    default="edit",
    show_default=True,
    help="Which rule family to evaluate.",
)
def rules_explain(path: str, kind: str) -> None:
    """Show which rules match PATH and what each checkpoint would do.

    Answers "why was this edit not prompted?" — often the honest answer
    is that no rule covers the path.
    """
    from archrev.gate import relativize

    root = _require_root()
    rel = relativize(path, root) or path.replace("\\", "/")
    click.echo(explain_path(root, rel, kind))


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
@click.option(
    "--unseal",
    is_flag=True,
    help="Decrypt sealed prompts with the local seal key. Asks for a "
    "passphrase when the key is protected. Plaintext is printed, not stored.",
)
def show(session_ref: str, as_markdown: bool, output: Path | None, unseal: bool) -> None:
    """Show the full chain of one session (default: the latest)."""
    from archrev.drift import compute_view
    from archrev.report.render import render_markdown, render_text
    from archrev.rules import load_rules

    root = _require_root()
    session = _resolve_session(root, session_ref)
    view = compute_view(root, load_config(root), load_rules(root), session)
    if unseal:
        _unseal_view_prompts(session, view)
    text = render_markdown(view) if as_markdown else render_text(view)
    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(f"Written to {output}")
    else:
        click.echo(text)


def _unseal_view_prompts(session: Session, view: dict) -> None:
    """Replace sealed prompt excerpts in ``view`` with plaintext. Not persisted."""
    from archrev.seal import KEY_PATH, SealUnavailable, open_sealed

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


@main.group()
def ci() -> None:
    """Generate CI enforcement templates."""


@ci.command(name="gitlab")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), default=None,
    help="Output file (default: .gitlab/archrev-ci.yml).",
)
def ci_gitlab(output: Path | None) -> None:
    """Write a GitLab CI template: rule + quality gate, chain verification,
    and an optional MR comment with the session report.

    CI enforcement needs zero developer adoption - the jobs run against
    every merge request regardless of what is installed locally.
    """
    from archrev.ci import write_gitlab_template

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
        "\nOptional: set ARCHREV_GITLAB_TOKEN (project access token, api "
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

    from archrev.metrics import collect

    targets = list(paths) or [Path.cwd()]
    per_repo, total = collect(targets)
    if not per_repo:
        raise click.ClickException(
            "No ArchRev repositories found (looked for .archrev in the "
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
    out-of-plan drift), a quality-check rule id, or the literal
    ``plan-check`` (for a failed check). The acknowledgment is an audited,
    hash-chained event: the finding stops being re-raised at session end,
    but who acknowledged what, when, and why stays in the permanent record.

    Acknowledging is the reviewer's decision. When an agent command is
    running, the ack is recorded as agent-made and clears only plan drift.
    """
    from archrev.review import detect_ack_actor

    root = _require_root()
    session = _resolve_session(root, session_ref)
    normalized = target.replace("\\", "/")
    actor, basis = detect_ack_actor(root, session)
    session.append_event(
        "ack", target=normalized, note=note, actor=actor, actor_basis=basis, via="cli"
    )
    if actor in ("agent", "unknown"):
        label = "an AGENT" if actor == "agent" else "an UNVERIFIED"
        click.secho(
            f"Recorded as {label} acknowledgment of '{normalized}' ({basis}).",
            fg="yellow",
        )
        click.echo(
            "It clears plan drift only; bypasses and failed checks still need "
            "the reviewer."
        )
        return
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
    local_only = 0
    for session in targets:
        if not session.event_log_present():
            local_only += 1
            manifest = session.manifest() or {}
            head = manifest.get("chain_head")
            head_note = f" committed chain head {str(head)[:12]}" if head else ""
            click.secho(
                f"  local   {session.id}: event log is not in this checkout"
                f"{head_note} — verify on the author's machine",
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
    help="Serve every ArchRev repository in DIR (the directory itself or its "
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
    from archrev.report.server import bind_viewer, run_server

    hub = bool(hub_dirs)
    if hub:
        from archrev.metrics import discover_repos

        roots = discover_repos(list(hub_dirs))
        if not roots:
            raise click.ClickException(
                "No ArchRev repositories found (looked for .archrev in the "
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

    click.echo(f"ArchRev viewer for {what}")
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

    The private key is written to ~/.archrev/seal.key and is never committed.
    The public key is appended to .archrev/recipients.yaml.
    """
    import getpass

    from archrev.seal import SealUnavailable, add_recipient, generate_keypair

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
    click.secho(f"Public key for '{recipient}' written to .archrev/recipients.yaml", fg="green")
    click.echo("Private key: ~/.archrev/seal.key (do not commit it).")
    click.echo("Set prompt_capture: sealed in .archrev/config.yaml to start sealing.")


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
