# How ArchRev works

Day-to-day commands are in the [README](../README.md). Rule examples and
the enforcement checkpoints are in [rules.md](rules.md). This page is the
rest: runtimes, review, attribution, rollout, storage, and limits.

## Agent runtimes

Rules, logs, plan checks, quality gates, git trailers, CI, and
`archrev index` are the same on every runtime. The adapter is what
differs: which events are wired, how the payload is read, and how
deny / ask / follow-up is returned.

| Need | Cursor | Claude Code | Codex |
| --- | --- | --- | --- |
| Prompt | `beforeSubmitPrompt` | `UserPromptSubmit` | `UserPromptSubmit` |
| Gate (path / tool / shell / read / MCP) | separate events | one `PreToolUse`, split by tool | one `PreToolUse` |
| Edit capture | `afterFileEdit` | `PostToolUse` (`Edit` / `Write`) | `PostToolUse` (`apply_patch`) |
| Command finished | `afterShellExecution`, `afterMCPExecution` | `PostToolUse` (`Bash`, `mcp__*`) | `PostToolUse` (shell tools) |
| Final review nag | `stop` → `followup_message` | `Stop` → `decision: block` | `Stop` → `decision: block` |
| Durable finalize | the same `stop` | `SessionEnd` | `SessionEnd` |
| `block` rule | pause (`ask`) | pause (`ask`) | **deny** |
| Human tab edits | `afterTabFileEdit` | git trailer only | git trailer only |

`archrev hook` with no event name reads `hook_event_name` from stdin, so
Claude and Codex share one command. Cursor keeps explicit
`archrev hook prompt|gate|...` so the stdout shape stays stable.

`Stop` fires every turn. ArchRev debounces it and uses it only to nag
about findings after material work. `SessionEnd` always writes the
manifest and runs quality checks, including protected-path changes that
never produced an edit event.

Aider, Cline, and Copilot Chat get the protocol file and CI, not in-editor
gates.

## Human changes

Agent edits are captured by hooks. Human edits are captured at the
checkpoints where they are visible:

- **Tab completions** (Cursor) arrive on `afterTabFileEdit` and are tagged
  `origin: tab`.
- **Hand edits** do not pass through hooks. At commit time, staged files
  that no recent session touched are appended to `.archrev/sessions/human/`.
  Every committed change is either linked to an agent session or marked
  human. This starts only after an agent session exists, so a purely
  manual repository stays quiet.

## Parallel sessions

Several agent windows and your own edits share one working tree, so "the
diff since the session started" mixes everyone's work. ArchRev attributes
a change to the session that could have made it:

- **Tool edits** belong to the session whose hook recorded them. Another
  session sees them as other sessions' edits.
- **Changes with no edit event** (shell writes, MCP side effects) belong
  to a session only while one of its own commands was running. Each
  session records working-tree checkpoints when it starts, around each
  shell or MCP command, and at each prompt and stop. The review names the
  command, for example `db/x.sql (during shell: python manage.py makemigrations)`.
- **Everything else** is background: hand edits, another window, or work
  that pre-dates the session. It is listed, and it is not raised as this
  session's gate bypass.

If the after-command hooks are missing (re-run `archrev init`), a command
window stays open until the next checkpoint. Attribution then asks for
more review, not less. Sessions recorded before checkpoints existed keep
since-start attribution (`"attribution": "since_start"` in the manifest).

A hand edit saved *while* one of this session's commands is running cannot
be separated from that command's writes. It is attributed to the session;
resolve it with `archrev ack` if the review raises it.

## Tamper-evident log

Every event stores a hash of its content plus the previous event's hash.
Editing, reordering, or deleting an event breaks the chain.

```powershell
archrev verify           # latest session
archrev verify --all     # every session; exit 1 on any break
```

The timeline shows *event chain: intact / BROKEN* per session. This is
tamper-evidence, not tamper-proofing. Someone who can rewrite the
repository can rewrite the chain. They cannot quietly change a history
that an exported report or a reviewed merge request already cites.

Appends are serialized per session, including parallel tool calls, so two
hooks cannot fork the chain by reading the same previous hash.

## Reviewing

**While the agent works:**

```powershell
archrev serve        # http://127.0.0.1:4177
```

The page refreshes on its own: prompt, plan, verdicts, edits with line
counts, pauses and flags, drift ("planned 6, touched 9, 3 out-of-plan"),
and linked commits. A file row expands to its diff. The timeline can be
filtered and searched. Stopping the server does not affect capture.

**A shareable file:**

```powershell
archrev export <session>    # one HTML file, diffs embedded (size-capped)
```

Every session directory also contains `report.md`, regenerated at
finalize, readable in any git UI.

## Team rollout

One person wires a repository. Everyone else pulls.

### Zero-install hooks

```powershell
archrev init --shim uvx
```

Hook commands are written as `uvx archrev ...`.
[uv](https://docs.astral.sh/uv/) fetches ArchRev on first use, so
teammates do not install it. uv itself is the prerequisite. Commit
`.cursor/`, `.claude/`, `.codex/`, `.archrev/`, and the rules. Codex
still needs `/hooks` trust on each machine. Re-running `init` with a
different `--shim` updates the existing entries in place.

### CI

```powershell
archrev ci gitlab        # writes .gitlab/archrev-ci.yml
```

The template has two merge-request jobs:

- **`archrev:rules`** (required). `archrev check diff` against the target
  branch — path rules and `check` rules — plus `archrev verify --all`.
  Block-level findings fail the pipeline.
- **`archrev:mr-report`** (`allow_failure`). Resolves the session from
  the head commit's `ArchRev-Session:` trailer and renders the markdown
  report. With `ARCHREV_GITLAB_TOKEN` (project access token, `api` scope,
  masked) the report is posted as a comment; otherwise it is a job
  artifact.

Both jobs install `ARCHREV_PIP_SPEC`, which defaults to the release that
generated the template (`archrev==<version>`). Point it at a private
index or a pinned git URL if you need to. Do not replace it with a bare
`archrev`: an unpinned name runs whatever the index serves, with the
pipeline's variables in reach.

A laptop with hooks disabled still cannot merge a change that violates a
block-level rule.

### Prompt privacy

| Mode | What is stored |
| --- | --- |
| `full` (default) | the whole prompt |
| `excerpt` | the first 200 characters and the total length |
| `none` | length and a SHA-256 only |
| `sealed` | ciphertext. `archrev seal keygen` writes a private key to `~/.archrev/seal.key` (optional passphrase) and the public key to `.archrev/recipients.yaml`. Decrypt with `archrev show --unseal` or the passphrase field in the local viewer. Plaintext is never written back. |

`sealed` is only as private as the recipient set. A public key in
`.archrev/recipients.yaml` can read every sealed prompt. There is no
per-prompt access control.

### What gets committed

| Mode | Committed | Local only |
| --- | --- | --- |
| `audit` (default for new `archrev init`) | `manifest.json`, `plan.md`, `report.md`, with the event-log chain head in the manifest | `events.jsonl` (gitignored). `archrev verify` on a checkout without the log says so, instead of treating the chain as empty. |
| `full` | the event log too | — |

`archrev prune --keep-days 30` deletes event logs of finalized sessions
older than that. It does not touch manifests.

### Cross-repo index

```powershell
archrev index E:\checkouts
archrev index --json
```

Session records already travel with git, so org-level visibility is a
read of whatever is checked out. No telemetry agent, no server. Per
repository and in total: sessions, plan discipline, drift rate, gate
pressure, bypasses, quality-check failures, acknowledgments, human-change
events, and broken chains.

## Storage format

```
.archrev/
  config.yaml
  rules/*.yaml
  sessions/<conversation-id>/
    meta.json        id, started_at, head_sha, branch, optional runtime
    events.jsonl     append-only event stream
    plan.md          latest plan snapshot
    manifest.json    files, line counts, drift, verdicts, reads, commits
    report.md        rendered report
```

`events.jsonl` is the source of truth. The manifest and the report are
derived and can be regenerated. Records from before the hash chain still
read; `archrev verify` counts them as legacy and does not fail them.

A `read` event is written for every read ArchRev is asked about, allowed
or not. The manifest stores a summary, one row per path: how many times,
the strictest decision, and which hooks reported it (`preToolUse`,
`beforeReadFile`). The raw events stay in the log.

Worktree checkpoints are additive. Logs without them stay valid and use
since-start attribution:

```json
{"type": "worktree", "phase": "exec_start", "label": "shell: pytest",
 "changed": {"db/x.sql": "3f2a9c01d4e5b6a7", "gone.py": "-"}}
```

`phase` is `start`, `exec_start`, `exec_end`, `turn_start`, or `turn_end`.
`changed` is a content fingerprint relative to the previous checkpoint
(`-` means deleted). The `start` event is the full baseline of changes
that already existed. Command text is stored in `label` only when
`prompt_capture` is `full`; otherwise the label is `shell`. The manifest
adds `background_changes`, an `attribution` mode (`checkpoints` or
`since_start`), and a `during` list on unexplained changes and protected
findings.

Runtime files that are not part of the record — `hook-errors.log`,
`fingerprint-cache.json`, and `locks/` — are gitignored by `archrev init`.

## Limitations

- Live capture needs lifecycle hooks. Cursor, Claude Code, and Codex are
  wired. Other agents get the protocol file and CI.
- Codex enforces `block` as `deny`. Codex project hooks stay inactive
  until trusted with `/hooks`.
- Prompt-rule verdicts are self-attestations.
- On Cursor, a `block` still depends on approval settings being visible.
  `deny` does not.
- Tab-completion capture is Cursor-only. Elsewhere, human edits land in
  the commit-time `human` ledger, and between commits they show up as
  background changes.
- A hand edit saved during one of this session's commands is attributed
  to the session. Timestamps have one-second resolution, so another
  session's edit in that same second counts as theirs.
- `plan register` and `check plan` bind to the most recently active
  session. The shell hook just before the command marks that session
  active. Pass `--session` when several are live.
- `record_scope: audit` means a colleague's `archrev verify` can show the
  committed chain head and cannot recompute the chain. Verify the log
  where it was written.
- If hooks are disabled or the CLI is not on `PATH`, capture stops. That
  is fail-open. `archrev sessions` shows the gap.
- A `read` rule sees `Read` and `Grep` when they name a path. It does not
  see a workspace-wide search or a shell command that prints a file.
  Shell rules cover the command text if you write them.

## Roadmap

**Across an organization**

- Rule packs shared across repositories, not copied per repo.
- Drift and gate metrics over time, on top of `archrev index`.
- A documented containment story: ArchRev policy plus network-isolated
  containers.

**Daily use**

- An MCP server so agents query rules and history without a shell.
- A smaller, faster hook process.
- Structured plans (frontmatter file lists) instead of path extraction.
- Session labels (`archrev annotate`).
- An IDE panel for the timeline.

Shipped already: hash-chained logs (`archrev verify`), `archrev index`,
GitLab CI (`archrev ci gitlab`), `archrev init --shim uvx`, prompt
privacy, and Claude Code / Codex adapters.
