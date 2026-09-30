# How Bewit works

In one sentence: **policy-as-code plus an audit trail for AI coding
agents.** Rules live in the repository. Every agent action passes a gate
that can allow it, pause it for your approval, or refuse it. Everything
is recorded in a hash-chained log. At the end of each turn, the real diff
is checked against the rules and the agent's declared plan. A human
signs off on anything unusual, and CI enforces the same rules.

```
 prompt ─► plan registered ─► plan checked ─► tools run through the gate ─► turn review ─► human ack ─► CI
              (declared files)   (policies attested)   (allow / pause / refuse)    (bypass, drift, checks)
```

## The parts

### 1. Hook adapter: `bewit hook`

This is one CLI entry point, registered in each agent runtime's hook
config (`.claude/settings.json`, `.codex/hooks.json`,
`.cursor/hooks.json`). The runtime calls it at each step:

- **Prompt submitted:** records the prompt.
- **Before a tool runs** (edit, write, read, shell, MCP): decides whether
  to allow, pause, or refuse.
- **After a tool runs:** records what actually changed.
- **Agent stops or session ends:** runs the review.

Each runtime's hook format is translated into one common event model,
which is why the same rules work across vendors.

### 2. Rules: `.bewit/rules/*.yaml`

Rules are declarative files, versioned with the code. Each rule matches a
target and takes one of three actions: `deny` refuses, `block` pauses
for a human, and `flag` allows the action but highlights it in the
review.

| Kind | Matches |
| --- | --- |
| `path` | files being written, e.g. `config/**` |
| `read` | files being read, e.g. `.env`, including by common shell readers |
| `shell`, `mcp`, `tool` | a regex over the command text or the tool name |
| `check` | a quality gate (linter, parser) run on changed files at turn end and in CI |
| `prompt` | a policy the agent attests in its plan check, e.g. "new functions get tests" |

Details: [rules.md](rules.md).

### 3. Plan registration and check

Before editing, the agent declares its intent and the files it expects
to touch (`bewit plan register`). It then attests each relevant
`prompt` policy (`bewit check plan`). The check also shows which
declared files the gate will pause or refuse. A file changed later that
the plan never declared is reported as **drift**. With
`strict_plan_check: true`, the first gated edit is refused until a plan
has passed its check.

### 4. Session recorder

Every event is appended to `events.jsonl`, and each entry carries the
hash of the one before it. Editing or removing an entry breaks the
chain, and `bewit verify` reports it. The recorder also snapshots the
working tree around each shell command, so it knows which files a
command changed. `record_scope` decides what is shared:

- `audit` shares only `manifest.json`, which carries the chain head.
- `full` also shares the event log.

Records are published to `refs/bewit/records`, not to your branches.

### 5. End-of-turn review

When the agent tries to stop, Bewit compares the actual working-tree
changes with the rules and the plan. It looks for:

- **Bypasses:** a protected file changed without passing the gate, for
  example by `echo >> file` in a shell.
- **Drift:** changed files the plan did not declare.
- **Failed checks:** a `check` rule's analyzer failed.

Findings are sent back to the agent, which cannot finish silently.

### 6. Human acknowledgment

A reviewer acknowledges each finding with a reason, in the viewer
(`bewit serve`) or with `bewit ack`. Agents are refused `bewit ack`.
An acknowledgment made while an agent command is running is recorded as
the agent's and does not clear bypasses or failed checks. See
[reviewing.md](reviewing.md).

### 7. CI: `bewit check diff`, `bewit verify`

The same path and check rules run in the pipeline. This catches changes
made outside any agent session and records that were tampered with.

### 8. Config and self-protection

`.bewit/config.yaml` sets:

- the enforcement mode: `on`, `monitor` (record everything, never stop),
  or `off`;
- how prompts are stored (`full`, `excerpt`, `none`, or `sealed` to
  reviewers' keys);
- whether a validated plan is required before the first edit.

The config, the rules, the hook wiring, and the agent protocol files are
protected by the `bewit-self-protection` rule. An agent cannot switch
off its own guardrails without your approval, and changing them through
the shell is reported as a bypass.

## Can the agent skip a hook?

The model cannot. The runtime (Claude Code, Cursor, Codex) runs the
hooks, not the model, and a refused tool call never happens. What an
agent *can* do is go around what the gate sees:

| Route | Stopped live? | Caught afterwards? |
| --- | --- | --- |
| Write a protected file through the shell | no | yes: bypass finding |
| Read a secret through the shell | common readers (`cat`, `Get-Content`, `python -c "open('.env')"`, …) are refused by `read` rules | the command is recorded; obfuscated forms slip through |
| Edit hook wiring or rules | yes: pauses for approval | yes: bypass if done through the shell |
| `disableAllHooks` in local or user Claude settings | no | `bewit rules` and the viewer warn on that machine; the session itself has no record |
| Never register a plan | yes with `strict_plan_check: true` | yes: every file is drift |
| Delete or edit the audit log | obvious commands pause | edits break the chain; the shared manifest keeps the chain head |
| Acknowledge its own findings | refused | recorded as agent-made; bypasses stay open |

If Bewit itself crashes, the hook fails **open** (it answers `allow`
and logs the error to the timeline). That is deliberate: a bug in
Bewit must never block the editor or break a commit.

## The limit to state up front

Bewit gates what an agent attempts through its tools. It is policy
plus audit, **not a sandbox**. A command that escapes the gate is caught
afterwards by the diff scan, not prevented. For secrets and systems that
really matter, add containment: run the agent in a container or under a
separate user account, keep secrets out of the workspace (inject them at
runtime), and restrict network access. Every known gap is listed in
[security-model.md](security-model.md#known-gaps).
