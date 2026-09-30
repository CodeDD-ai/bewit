<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/CodeDD-ai/bewit/master/docs/images/bewit-wordmark-reversed.svg">
    <img src="https://raw.githubusercontent.com/CodeDD-ai/bewit/master/docs/images/bewit-wordmark.svg" alt="Bewit" width="300">
  </picture>
</h1>

**See what your AI coding agent did, why, and whether it kept to your rules.**

Git records *what* changed. Bewit records *why* — the prompt, the plan,
every rule decision, and every command — and tells a reviewer exactly
which parts need a human look. It runs inside Cursor, Claude Code, and
Codex through their hooks, and stores everything as plain files in your
repository.

```
 you prompt ─► agent plans ─► Bewit checks the plan ─► agent works ─► Bewit reviews the turn
                                     │                        │                    │
                            policies attested        each edit, read,       bypasses, failed checks,
                            by the agent             command gated live     unplanned files → you
```

- **Stops what you forbid, pauses what you want to approve.** Rules on
  file edits, file reads, shell commands, MCP calls, and tools are checked
  *before* the agent acts.
- **Catches what slips past.** At the end of every turn Bewit compares
  the working tree with what the gate saw. A protected file changed
  through the shell, a file the plan never mentioned, a failed quality
  check: each becomes a finding with *why it matters* and *what to do*.
- **Leaves a record you can trust.** An append-only, hash-chained log per
  session, linked to commits with `Bewit-Session:` trailers.
- **Stays out of the way.** No daemon, no database, no server. Hooks
  append a line and exit. If Bewit breaks, it fails open and records
  that it did.

![The bewit serve viewer: one session with its review box, rule outcomes, changed files, and a tamper-evident record](https://raw.githubusercontent.com/CodeDD-ai/bewit/master/docs/images/bewit-serve.png)

The parts and the trust model on one page: [docs/how-it-works.md](docs/how-it-works.md).

Bewit is policy plus audit at the agent's tool layer, not a sandbox.
[docs/security-model.md](docs/security-model.md) spells out what is
enforced, what is only recorded, and what is known to slip through.

## Quick start

Python 3.11+, git, and [Cursor](https://cursor.com),
[Claude Code](https://code.claude.com), or
[Codex](https://developers.openai.com/codex).

```powershell
uv tool install bewit          # or: pipx install bewit
cd your-repo
bewit init                     # wires Cursor + Claude Code + Codex (idempotent)
bewit serve                    # opens the viewer for this project
```

Start an agent session in the repository and watch it appear under
**Recording now**. Commit `.bewit/`: it is the audit record.

`init` writes `.bewit/config.yaml`, starter rules (disabled examples plus
enabled self-protection), the hook wiring for each runtime, the agent
protocol (`AGENTS.md` and the Cursor/Claude equivalents), and a
`prepare-commit-msg` hook. Codex runs project hooks only after each
developer trusts them with `/hooks`. After upgrading Bewit, run
`bewit init` again: `bewit rules` warns when the wiring is out of date.

## A first rule

```yaml
# .bewit/rules/10-team.yaml
- id: protect-migrations
  kind: path
  match: ["**/migrations/**"]
  action: block                   # deny | block (ask you) | flag (allow, highlight)
  message: "Database migrations need explicit approval."

- id: no-secret-reads
  kind: read
  match: [".env", ".env.*", "**/*.pem"]
  action: deny
  message: "Agents may not read secrets."

- id: api-auth
  kind: prompt                    # the agent attests this in its plan check
  policy: "Every new API endpoint declares authentication and rate limiting."
```

`bewit rules` lists what is active; `bewit rules explain <path>` shows
why a path is or is not gated. All rule kinds: [docs/rules.md](docs/rules.md).

## Reviewing a session

`bewit serve` shows one project (`bewit serve --all ~/code` shows
several). A session page answers three questions, top to bottom:

1. **What needs me?** The review box: each open finding with why it
   matters, the likely cause, what to do, the diff, and an *Acknowledge*
   button. Empty means nothing needs you.
2. **What did the rules do?** Refused, paused (you approved / it did not
   run), flagged. Rules stopping the agent is the system working.
3. **What exactly happened?** A timeline per prompt, newest first: plan,
   plan check, every rule decision, every command, every file change
   with its diff, end-of-turn reviews, acknowledgments, commits.

The same review leads `bewit show` and `report.md`. Walkthrough:
[docs/reviewing.md](docs/reviewing.md).

## Commands

```powershell
bewit serve [--all DIR]        # viewer (reuses a running one for this project)
bewit sync                     # share session records (refs/bewit/records)
bewit sessions                 # recent sessions
bewit show [<id>] [--md]       # a session, review first
bewit trace <path|commit>      # which session produced a file or commit
bewit ack <target>... --note ".." # reviewer: accept findings (paths, globs, --earlier-plans)
bewit rules [explain <path>]   # active rules, wiring problems
bewit check diff               # changed files vs rules; exit 1 on block (CI)
bewit verify [--all]           # hash-chain check; exit 1 on a break
bewit export <id>              # one self-contained HTML report
bewit ci gitlab                # CI template
bewit index DIR                # metrics across repositories
```

The agent itself runs `bewit plan register` and `bewit check plan`
(the protocol file tells it how). It must not run `bewit ack`: a shipped
rule refuses that, and an acknowledgment made while an agent command runs
is recorded as the agent's and does not clear bypasses or failed checks.

## What is stored

```
.bewit/
  config.yaml, rules/*.yaml         your policy, committed (changes need your approval)
  sessions/<id>/                    written by the hooks on this machine
    manifest.json   summary: plan, review, decisions, files, chain head   shared
    events.jsonl    append-only, hash-chained log: the source of truth    shared in full scope
    meta.json       start time, git HEAD, branch, runtime                 shared in full scope
    plan.md, report.md   rendered locally for convenience                 never shared
  locks/                            runtime locks and CLI session claims  never shared
```

**Session records never touch your code history.** With
`record_store: ref` (the default for new repositories) the shared files are
published to their own git ref, `refs/bewit/records`, at the end of each
agent turn and at each commit. `git push` carries that ref along (a
pre-push hook), `bewit sync` fetches colleagues' records, and CI or a hub
collects everything by fetching one ref. Your branches, commits, and merge
requests contain only code, plus an `Bewit-Session:` trailer that points
to the session.

`record_scope: audit` (default) shares **one file per session**,
`manifest.json`; the event log stays on the machine that wrote it.
`record_scope: full` shares the log too. Prefer records in the tree?
`bewit init --record-store tree` commits them with the code instead
(collapsed in GitHub/GitLab diffs).

Bewit stores prompts (configurable: full, excerpt, none, or sealed),
plans, rule decisions, file paths, content fingerprints, and command text.
It never stores command or tool **output**. Formats:
[docs/guide.md#storage-format](docs/guide.md#storage-format).

## Documentation

| | |
| --- | --- |
| [docs/how-it-works.md](docs/how-it-works.md) | The parts, the flow, and what an agent can and cannot get around |
| [docs/reviewing.md](docs/reviewing.md) | Reading a session: the review box, timeline, files, rules, acknowledgments |
| [docs/rules.md](docs/rules.md) | Rule kinds, actions, checkpoints, configuration |
| [docs/security-model.md](docs/security-model.md) | Enforced vs recorded vs attested; known gaps |
| [docs/guide.md](docs/guide.md) | Runtimes, attribution, team rollout, CI, storage, limitations |
| [CHANGELOG.md](CHANGELOG.md) | What changed |

## Development

```powershell
uv venv && uv pip install -e ".[dev]"
.venv\Scripts\python -m pytest       # unit and integration tests
python tests/rule_matrix.py          # every rule kind replayed through the real hooks (~1 min)
```

Licensed under the [Apache License 2.0](LICENSE). Contributions welcome;
they are accepted under the same license.
