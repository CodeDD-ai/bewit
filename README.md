# ArchRev

**Session provenance and architecture-rule enforcement for AI coding agents.**

Git records *what* changed. ArchRev records *why* — the prompt, the plan,
and whether the agent followed your architecture rules — as plain files in
the repository.

- **No daemon.** The agent runtime calls ArchRev from hooks. Each call
  appends to a log and exits.
- **No database.** JSON, JSONL, and markdown under `.archrev/`, reviewed
  in merge requests like any other change.
- **Fails open.** A broken ArchRev never blocks the editor or a commit.
  Degradation is recorded, not silent.

Rules are policy plus an audit trail, not a sandbox. An allowed process can
still do what the operating system allows. For hard containment, run the
agent in a network-isolated environment.

## Setup

Python 3.11+, git, and one of [Cursor](https://cursor.com),
[Claude Code](https://code.claude.com), or
[Codex](https://developers.openai.com/codex).

```powershell
uv tool install archrev          # or: pipx install archrev
# from a checkout:  uv tool install --editable path/to/ArchRev

cd your-repo
archrev init                     # Cursor + Claude Code + Codex
# archrev init --runtime cursor
# archrev init --shim uvx        # teammates need uv, not a local install
```

`archrev init` is idempotent. It creates `.archrev/config.yaml`, starter
rules, `sessions/`, runtime hook files, an agent protocol file (`AGENTS.md`
and the Cursor/Claude equivalents), and a `prepare-commit-msg` hook that
adds `ArchRev-Session:` trailers. Commit `.archrev/` — that directory is
the audit record.

Codex ignores project hooks until each developer trusts them with `/hooks`.
Cursor and Claude Code load project hooks without that step.

## Daily commands

```powershell
archrev sessions                 # recent sessions
archrev show                     # latest session
archrev show <id> --md -o r.md   # markdown for an MR description
archrev trace src/api/views.py   # sessions that touched this file
archrev trace 1a2b3c4d           # session behind a commit
archrev rules                    # active rules and config
archrev rules explain <path>     # which rules match, and where they fire
archrev check diff               # current changes vs rules (CI-ready; exit 1 on block)
archrev serve                    # live timeline at http://127.0.0.1:4177
archrev export <session>         # one self-contained HTML file
archrev verify --all             # tamper-evidence check; exit 1 on a break
archrev ack <path|plan-check> --note "why"
```

The agent protocol (installed into the repo) is: register a plan, attest
prompt rules with `archrev check plan`, and do not route around a paused
or denied edit. With `strict_plan_check: true`, the first edit is denied
until that check passes.

## Rules, briefly

Rules are YAML files in `.archrev/rules/`. Machine-enforced kinds (`path`,
`read`, `shell`, `mcp`, `tool`) use `deny` (hard stop), `block` (pause for
approval; on Codex this is a deny, because Codex cannot ask), or `flag`
(allow and highlight). `check` rules run a real analyzer at session end
and in `archrev check diff`. `prompt` rules are attested by the agent;
they are evidence, not proof.

Examples, the five checkpoints, and config knobs:
**[docs/rules.md](docs/rules.md)**.

## Storage

```
.archrev/
  config.yaml
  rules/*.yaml
  sessions/<id>/
    meta.json       id, start time, git HEAD, runtime
    events.jsonl    append-only hash-chained log (the audit truth)
    plan.md         latest registered plan
    manifest.json   derived summary: files, drift, verdicts, reads, commits
    report.md       derived markdown report
```

Events carry `prev` and `hash`. Older logs without those fields still
read. `record_scope: audit` (the default for a new `archrev init`)
gitignores `events.jsonl` and commits the manifest, plan, and report.
Field-level detail: **[docs/guide.md](docs/guide.md#storage-format)**.

## Further reading

- **[docs/rules.md](docs/rules.md)** — rule kinds, examples, enforcement
- **[docs/guide.md](docs/guide.md)** — runtimes, review, parallel sessions,
  team rollout, limitations, roadmap

## Development

```powershell
uv venv && uv pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

MIT licensed. Contributions welcome.
