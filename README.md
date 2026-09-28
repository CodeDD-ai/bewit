# ArchRev

**See what your AI coding agent did, why, and whether it kept to your rules.**

Git records *what* changed. ArchRev records *why* — the prompt, the plan,
every rule decision, and every command — and tells a reviewer exactly
which parts need a human look. It runs inside Cursor, Claude Code, and
Codex through their hooks, and stores everything as plain files in your
repository.

```
 you prompt ─► agent plans ─► ArchRev checks the plan ─► agent works ─► ArchRev reviews the turn
                                     │                        │                    │
                            policies attested        each edit, read,       bypasses, failed checks,
                            by the agent             command gated live     unplanned files → you
```

- **Stops what you forbid, pauses what you want to approve.** Rules on
  file edits, file reads, shell commands, MCP calls, and tools are checked
  *before* the agent acts.
- **Catches what slips past.** At the end of every turn ArchRev compares
  the working tree with what the gate saw. A protected file changed
  through the shell, a file the plan never mentioned, a failed quality
  check: each becomes a finding with *why it matters* and *what to do*.
- **Leaves a record you can trust.** An append-only, hash-chained log per
  session, linked to commits with `ArchRev-Session:` trailers.
- **Stays out of the way.** No daemon, no database, no server. Hooks
  append a line and exit. If ArchRev breaks, it fails open and records
  that it did.

ArchRev is policy plus audit at the agent's tool layer, not a sandbox.
[docs/security-model.md](docs/security-model.md) spells out what is
enforced, what is only recorded, and what is known to slip through.

## Quick start

Python 3.11+, git, and [Cursor](https://cursor.com),
[Claude Code](https://code.claude.com), or
[Codex](https://developers.openai.com/codex).

```powershell
uv tool install archrev          # or: pipx install archrev
cd your-repo
archrev init                     # wires Cursor + Claude Code + Codex (idempotent)
archrev serve                    # opens the viewer for this project
```

Start an agent session in the repository and watch it appear under
**Recording now**. Commit `.archrev/`: it is the audit record.

`init` writes `.archrev/config.yaml`, starter rules (disabled examples plus
enabled self-protection), the hook wiring for each runtime, the agent
protocol (`AGENTS.md` and the Cursor/Claude equivalents), and a
`prepare-commit-msg` hook. Codex runs project hooks only after each
developer trusts them with `/hooks`. After upgrading ArchRev, run
`archrev init` again: `archrev rules` warns when the wiring is out of date.

## A first rule

```yaml
# .archrev/rules/10-team.yaml
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

`archrev rules` lists what is active; `archrev rules explain <path>` shows
why a path is or is not gated. All rule kinds: [docs/rules.md](docs/rules.md).

## Reviewing a session

`archrev serve` shows one project (`archrev serve --all ~/code` shows
several). A session page answers three questions, top to bottom:

1. **What needs me?** The review box: each open finding with why it
   matters, the likely cause, what to do, the diff, and an *Acknowledge*
   button. Empty means nothing needs you.
2. **What did the rules do?** Refused, paused (you approved / it did not
   run), flagged. Rules stopping the agent is the system working.
3. **What exactly happened?** A timeline per prompt, newest first: plan,
   plan check, every rule decision, every command, every file change
   with its diff, end-of-turn reviews, acknowledgments, commits.

The same review leads `archrev show` and `report.md`. Walkthrough:
[docs/reviewing.md](docs/reviewing.md).

## Commands

```powershell
archrev serve [--all DIR]        # viewer (reuses a running one for this project)
archrev sessions                 # recent sessions
archrev show [<id>] [--md]       # a session, review first
archrev trace <path|commit>      # which session produced a file or commit
archrev ack <target> --note ".." # reviewer: accept a finding (audited)
archrev rules [explain <path>]   # active rules, wiring problems
archrev check diff               # changed files vs rules; exit 1 on block (CI)
archrev verify [--all]           # hash-chain check; exit 1 on a break
archrev export <id>              # one self-contained HTML report
archrev ci gitlab                # CI template
archrev index DIR                # metrics across repositories
```

The agent itself runs `archrev plan register` and `archrev check plan`
(the protocol file tells it how). It must not run `archrev ack`: a shipped
rule refuses that, and an acknowledgment made while an agent command runs
is recorded as the agent's and does not clear bypasses or failed checks.

## What is stored

```
.archrev/
  config.yaml, rules/*.yaml         your policy (gated: changes need your approval)
  sessions/<id>/
    events.jsonl    append-only, hash-chained log: the source of truth
    meta.json       start time, git HEAD, branch, runtime
    plan.md         latest registered plan
    manifest.json   derived summary, including the review
    report.md       derived markdown report
```

ArchRev stores prompts (configurable: full, excerpt, none, or sealed),
plans, rule decisions, file paths, content fingerprints, and command text.
It never stores command or tool **output**. With `record_scope: audit`
(the default for new repositories) the event log stays local and the
manifest, plan, and report are committed. Formats:
[docs/guide.md#storage-format](docs/guide.md#storage-format).

## Documentation

| | |
| --- | --- |
| [docs/reviewing.md](docs/reviewing.md) | Reading a session: the review box, timeline, files, rules, acknowledgments |
| [docs/rules.md](docs/rules.md) | Rule kinds, actions, checkpoints, configuration |
| [docs/security-model.md](docs/security-model.md) | Enforced vs recorded vs attested; known gaps |
| [docs/guide.md](docs/guide.md) | Runtimes, attribution, team rollout, CI, storage, limitations |
| [docs/demo.md](docs/demo.md) | A 15-minute live demo using the `.test/` fixtures |
| [CHANGELOG.md](CHANGELOG.md) | What changed |

## Development

```powershell
uv venv && uv pip install -e ".[dev]"
.venv\Scripts\python -m pytest       # unit and integration tests
python .test/simulate.py             # rule matrix replayed through the real hooks
```

MIT licensed. Contributions welcome.
