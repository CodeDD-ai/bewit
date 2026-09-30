# How Bewit works

Day-to-day commands are in the [README](../README.md), rules and
checkpoints in [rules.md](rules.md), reading a session in
[reviewing.md](reviewing.md), and what is and is not enforced in
[security-model.md](security-model.md). This page covers the rest:
runtimes, attribution, rollout, storage, and limits.

## Agent runtimes

Rules, logs, plan checks, quality gates, git trailers, CI, and
`bewit index` are the same on every runtime. The adapter is what
differs: which events are wired, how the payload is read, and how
deny / ask / follow-up is returned.

| Need | Cursor | Claude Code | Codex |
| --- | --- | --- | --- |
| Prompt | `beforeSubmitPrompt` | `UserPromptSubmit` | `UserPromptSubmit` |
| Gate (path / tool / shell / read / MCP) | separate events | one `PreToolUse`, split by tool | one `PreToolUse` |
| Edit capture | `afterFileEdit` | `PostToolUse` (`Edit` / `Write`) | `PostToolUse` (`apply_patch`) |
| Shell tools | `beforeShellExecution` | `Bash`, `PowerShell` | `shell`, `exec_command` |
| Command finished | `afterShellExecution`, `afterMCPExecution` | `PostToolUse` (`Bash`, `PowerShell`, `mcp__*`) | `PostToolUse` (shell tools) |
| Final review nag | `stop` → `followup_message` | `Stop` → `decision: block` | `Stop` → `decision: block` |
| Durable finalize | the same `stop` | `SessionEnd` | `SessionEnd` |
| `block` rule | pause (`ask`) | pause (`ask`) | **deny** |
| Human tab edits | `afterTabFileEdit` | git trailer only | git trailer only |

`bewit hook` with no event name reads `hook_event_name` from stdin, so
Claude and Codex share one command. Cursor keeps explicit
`bewit hook prompt|gate|...` so the stdout shape stays stable.

`Stop` fires every turn. Bewit debounces it and uses it only to nag
about findings after material work, and names only findings the agent has
not been told about yet; findings already reported are counted, not
repeated (`raised_keys` on the `final_check` event). A finding that is
resolved and later reopens is new again. `SessionEnd` always writes the
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
  that no recent session touched are appended to `.bewit/sessions/human/`.
  Every committed change is either linked to an agent session or marked
  human. This starts only after an agent session exists, so a purely
  manual repository stays quiet.

## Parallel sessions

Several agent windows and your own edits share one working tree, so "the
diff since the session started" mixes everyone's work. Bewit attributes
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

If the after-command hooks are missing (re-run `bewit init`; `bewit
rules` and the viewer warn), a command window stays open until the turn
ends. Attribution then asks for more review, not less, and names the most
recently started command. When a later command reports finishing while an
older one never did, the older one's end was most likely lost; changes
attributed only to it are marked *uncertain* in the review. An end event
pairs with the most recent start of the same command. Sessions recorded
before checkpoints existed keep since-start attribution
(`"attribution": "since_start"` in the manifest).

A shell change to a file *after* its gated edit is also a bypass: each
edit event records the content fingerprint the tool left, and a later
change with different content during one of the session's commands is
raised as "changed again by a command after its approved edit".

Drift is measured on the net change: a file this session edited and then
reverted (no diff since the session's start commit) is listed under
`drift.no_net_change` in the view, not raised as out of plan.

Commands that write to a session (`plan register`, `check plan`, `ack`)
bind `--session latest` to the *calling* session: first the id the
runtime exports to its commands (`BEWIT_SESSION`, or Claude Code's
`CLAUDE_CODE_SESSION_ID`), then a short-lived claim the shell hook leaves
for the `bewit` command it is about to let run
(`.bewit/locks/cli-claims/`, untracked), then the only session active in
the last 15 minutes. When several are active and none of these binds, the
command refuses and asks for `--session` instead of guessing.

A hand edit saved *while* one of this session's commands is running cannot
be separated from that command's writes. It is attributed to the session;
resolve it with `bewit ack` if the review raises it.

## Tamper-evident log

Every event stores a hash of its content plus the previous event's hash.
Editing, reordering, or deleting an event breaks the chain.

```powershell
bewit verify           # latest session
bewit verify --all     # every session; exit 1 on any break
```

The timeline shows *event chain: intact / BROKEN* per session. This is
tamper-evidence, not tamper-proofing. Someone who can rewrite the
repository can rewrite the chain. They cannot quietly change a history
that an exported report or a reviewed merge request already cites.

Appends are serialized per session, including parallel tool calls, so two
hooks cannot fork the chain by reading the same previous hash.

`verify` names what it found. A **duplicate** (an event byte-identical to
its predecessor: the runtime delivered one hook twice) changes nothing and
passes, with a note. A **fork** (two valid events chaining to the same
predecessor) fails: usually two hooks wrote at once when the lock was
unavailable, but an inserted event looks the same. A **modified** break
(content that no longer matches its hash, or a missing predecessor) means
the log was edited after the fact.

## Reviewing

`bewit serve` (live), `bewit show` / `report.md` (text), and
`bewit export` (one HTML file with diffs) all lead with the same review.
How to read a session: [reviewing.md](reviewing.md). The viewer is
view-only apart from acknowledgments and unsealing; stopping it never
affects capture.

## Team rollout

One person wires a repository. Everyone else pulls.

### Zero-install hooks

```powershell
bewit init --shim uvx
```

Hook commands are written as `uvx bewit ...`.
[uv](https://docs.astral.sh/uv/) fetches Bewit on first use, so
teammates do not install it. uv itself is the prerequisite. Commit
`.cursor/`, `.claude/`, `.codex/`, `.bewit/`, and the rules. Codex
still needs `/hooks` trust on each machine. Re-running `init` with a
different `--shim` updates the existing entries in place.

### CI

```powershell
bewit ci gitlab        # writes .gitlab/bewit-ci.yml
```

The template has two merge-request jobs:

- **`bewit:rules`** (required). `bewit check diff` against the target
  branch — path rules and `check` rules — plus `bewit verify --all`.
  Block-level findings fail the pipeline.
- **`bewit:mr-report`** (`allow_failure`). Resolves the session from
  the head commit's `Bewit-Session:` trailer and renders the markdown
  report. With `BEWIT_GITLAB_TOKEN` (project access token, `api` scope,
  masked) the report is posted as a comment; otherwise it is a job
  artifact.

Both jobs install `BEWIT_PIP_SPEC`, which defaults to the release that
generated the template (`bewit==<version>`). Point it at a private
index or a pinned git URL if you need to. Do not replace it with a bare
`bewit`: an unpinned name runs whatever the index serves, with the
pipeline's variables in reach.

A laptop with hooks disabled still cannot merge a change that violates a
block-level rule.

### Prompt privacy

| Mode | What is stored |
| --- | --- |
| `full` (default) | the whole prompt |
| `excerpt` | the first 200 characters and the total length |
| `none` | length and a SHA-256 only |
| `sealed` | ciphertext. `bewit seal keygen` writes a private key to `~/.bewit/seal.key` (optional passphrase) and the public key to `.bewit/recipients.yaml`. Decrypt with `bewit show --unseal` or the passphrase field in the local viewer. Plaintext is never written back. |

`sealed` is only as private as the recipient set. A public key in
`.bewit/recipients.yaml` can read every sealed prompt. There is no
per-prompt access control.

### Where records live: the records ref

With `record_store: ref` (the default for new repositories) session
records never enter your branches:

1. Hooks write each session to `.bewit/sessions/<id>/` on the machine
   running the agent (gitignored as a whole).
2. At the end of each agent turn and at each commit, Bewit publishes the
   shared files (see the table below) to `refs/bewit/records`: its own
   commit history of plain files (`sessions/<id>/manifest.json`, ...),
   written with git plumbing. Your working tree and index are never
   touched; record commits are authored by "Bewit".
3. `git push` runs the pre-push hook, which calls `bewit sync`: fetch
   the remote's records into `refs/bewit-remote/<remote>/records`, merge
   them with yours (union; a session is only ever written by the machine
   that recorded it), publish, and push `refs/bewit/records`. It never
   blocks or changes your push.
4. `bewit sync` on a colleague's clone fetches the records and
   materializes them into `.bewit/sessions/` (marked `.from-ref`), so
   `serve`, `show`, `trace`, and `index` read them like local sessions.
   Sessions recorded on this machine are never overwritten.

`bewit sync` picks the remote the way `git push` does: the branch's
`pushRemote`, then `remote.pushDefault`, then the branch's upstream remote,
then `origin`, then the only remote. With several remotes and none of
those set, it asks for `--remote <name>`. The pre-push hook always syncs
with the remote being pushed to.

CI and hubs need one extra line:

```bash
git fetch origin "+refs/bewit/records:refs/bewit/records"
```

(`bewit ci gitlab` includes it.) Some hosts hide non-branch refs in
their UI; the ref is still fetched and pushed normally.

**Moving an existing repository to the ref store:**

```powershell
bewit init --record-store ref                   # config, .gitignore block, pre-push hook
bewit sync                                      # publish existing records and push them
git rm -r --cached --quiet .bewit/sessions      # stop tracking them in the tree
git commit -m "Move Bewit session records to refs/bewit/records"
```

The last two steps are yours to run: removing tracked files is a history
decision. Earlier commits keep their session files; new ones will not.

### What gets shared

| Mode | Shared per session | Local only |
| --- | --- | --- |
| `audit` (default for new `bewit init`) | `manifest.json`: summary, latest plan text, review, decisions, and the event-log chain head | `events.jsonl`, `meta.json`, `plan.md`, `report.md` |
| `full` | `events.jsonl`, `meta.json`, `manifest.json` | `plan.md`, `report.md` |

`plan.md` and `report.md` are derived: `bewit show` (and `--md`) renders
the report from the log, or from the manifest when the log is not in the
checkout, as for a colleague or CI in audit scope. The viewer marks such
sessions *from the committed summary*. `bewit verify` on a checkout
without the log says so, instead of treating the chain as empty.

"Shared" means published to the records ref (`record_store: ref`) or
committed with the code (`record_store: tree`). `bewit init` writes a
managed block in `.gitignore` for the current scope and store; re-run it
after changing either. `.gitignore` never untracks files that are already
committed. To drop derived files a tree-store repository committed before,
run once:

```powershell
git rm -r --cached --ignore-unmatch ".bewit/sessions/*/plan.md" ".bewit/sessions/*/report.md"
```

`init` also adds `.bewit/sessions/** linguist-generated=true
gitlab-generated=true` to `.gitattributes`, so GitHub and GitLab collapse
session records in merge-request diffs (they stay one click away).

`bewit prune --keep-days 30` deletes event logs of finalized sessions
older than that. It does not touch manifests.

### Cross-repo index

```powershell
bewit index E:\checkouts
bewit index --json
```

Session records already travel with git, so org-level visibility is a
read of whatever is checked out. No telemetry agent, no server. Per
repository and in total: sessions, plan discipline, drift rate, gate
pressure, bypasses, quality-check failures, acknowledgments, human-change
events, and broken chains.

## Storage format

```
.bewit/
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
read; `bewit verify` counts them as legacy and does not fail them.

A `read` event is written for every read Bewit is asked about, allowed
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

Other additive fields and events (older records without them stay valid):

| Where | Field / event | Meaning |
| --- | --- | --- |
| `edit` | `fingerprint` | Content fingerprint the tool left; detects a later shell change. |
| `gate.hits[]` | `message` | The rule's message when it fired, so the record explains the decision after the rule is edited or deleted. |
| `gate_resolved` | `ref`, `kind`, `target`, `outcome: approved` | A paused (`ask`) action then ran. `ref` is the gate event's hash. |
| `ack` | `actor` (`human` / `agent`), `actor_basis`, `via` (`cli` / `viewer`) | Who acknowledged, and why Bewit thinks so. Missing `actor` counts as `human`. |
| `plan_check.prompt_rules[]` | `verdict: n/a`, `scope: out of scope` | Policy not applicable; `applies_to` auto-n/a. |
| `plan_check.prompt_rules[]` | `carried: true` | Verdict kept from the previous check; the rule's part of the plan was unchanged. |
| `plan_registered` | `amend` | `true` (the default since this release): files merged with the earlier declaration; `false`: `--replace`. Older records with `false` were replacements too. |
| `final_check` | `raised_keys` | Findings already reported to the agent and still open; the next turn names only others. |
| manifest | `drift.no_net_change` | Touched, undeclared files with no diff since the session start; not raised. |
| manifest | `review` (`open` / `resolved` / `notes`) | The review as of that finalize. |
| manifest | `commands[]` (`ts`, `label`, `ended`) | Commands run, and whether each reported finishing. |
| manifest | `gate_events[].outcome` | `refused` / `approved` / `declined` / `waiting` / `flagged` / `unknown`. |
| manifest | `chain` (`duplicates`, `forks`, `reason`) | Hash-chain verification detail. |

Runtime files that are not part of the record — `hook-errors.log`,
`fingerprint-cache.json`, and `locks/` — are gitignored by `bewit init`.

## Limitations

- Live capture needs lifecycle hooks. Cursor, Claude Code, and Codex are
  wired. Other agents get the protocol file and CI.
- Codex enforces `block` as `deny`. Codex project hooks stay inactive
  until trusted with `/hooks`.
- Prompt-rule verdicts are self-attestations. The viewer and report label
  them so: a pass says the agent considered the policy, not that it holds.
- On Cursor, a `block` still depends on approval settings being visible.
  `deny` does not.
- Tab-completion capture is Cursor-only. Elsewhere, human edits land in
  the commit-time `human` ledger, and between commits they show up as
  background changes.
- A hand edit saved during one of this session's commands is attributed
  to the session. Timestamps have one-second resolution, so another
  session's edit in that same second counts as theirs.
- On runtimes that export no session id (Cursor, Codex), `plan register`,
  `check plan`, and `ack` bind through the shell hook's claim. If two
  agents run the same command within two minutes, neither binds and both
  must pass `--session` (see [Parallel sessions](#parallel-sessions)).
- `record_scope: audit` means a colleague's `bewit verify` can show the
  committed chain head and cannot recompute the chain. Verify the log
  where it was written.
- If hooks are disabled or the CLI is not on `PATH`, capture stops. That
  is fail-open. `bewit sessions` shows the gap.
- `read` rules cover file tools that name a path and shell commands that
  read a file visibly (reader programs, inline code). A directory-wide
  search, a path built at runtime, or a program that opens files itself
  is out of reach. Full list: [security-model.md](security-model.md#known-gaps).
- An agent runtime's own permission check runs after Bewit's pre-tool
  hook, so a command it stops still shows as *started, no completion
  recorded*.

## Roadmap

**Across an organization**

- Rule packs shared across repositories, not copied per repo.
- Drift and gate metrics over time, on top of `bewit index`.
- A documented containment story: Bewit policy plus network-isolated
  containers.

**Daily use**

- An MCP server so agents query rules and history without a shell.
- A smaller, faster hook process.
- Structured plans (frontmatter file lists) instead of path extraction.
- Session labels (`bewit annotate`).
- An IDE panel for the timeline.

Shipped already: hash-chained logs (`bewit verify`), `bewit index`,
GitLab CI (`bewit ci gitlab`), `bewit init --shim uvx`, prompt
privacy, and Claude Code / Codex adapters.
