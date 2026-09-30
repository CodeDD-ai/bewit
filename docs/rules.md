# Rules

Rules live in `.bewit/rules/*.yaml`. A file may hold one rule, a list of
rules, or several YAML documents. Invalid rules are reported by
`bewit rules` and never crash a hook.

Write shell and tool regexes in single-quoted YAML scalars. Double quotes
treat `\s` and `\d` as escape sequences.

## Actions

Machine-enforced kinds (`path`, `read`, `shell`, `mcp`, `tool`) share three
actions:

| Action | Effect |
| --- | --- |
| `deny` | Hard stop. The agent is refused and told not to retry a variation. |
| `block` | Pause for your approval. Cursor and Claude Code ask; Codex denies, because returning "ask" there fails the hook and allows the tool. |
| `flag` | Allow, record, and highlight in the session review. |

`block` becomes Cursor's *ask* permission. If that category is set to
auto-accept, the pause is resolved without a dialog. The event is still
recorded. When the stop must not depend on editor settings, use `deny`.

`enforcement: monitor` downgrades `deny` and `block` to `flag`.
`enforcement: off` skips rule evaluation. Capture still runs.

These rules gate the agent's *attempts* and record every attempt. They are
not a sandbox: an allowed shell can still open the network, and a search
over a directory can still see a file a `read` rule would have denied.
What is and is not covered: [security-model.md](security-model.md).

## Path rules

Applied when an edit tool is about to change a file.

```yaml
- id: protect-typedb-schema
  kind: path
  match: ["type-db/**/*.tql"]   # ** spans directories; a pattern with no /
  action: block                  # matches at any depth, like .gitignore
  message: "TypeDB schema change requires explicit approval."

- id: flag-infrastructure
  kind: path
  match: ["k8s/**", "Dockerfile*", "docker-compose*.yml"]
  action: flag
  message: "Infrastructure change - highlighted for review."
  applies_to: ["deploy/**"]      # optional monorepo scope
```

Matching is case-insensitive. `applies_to` limits the rule to a subtree;
omit it to cover the whole repository.

The gate does not block `.bewit/sessions/**` or `*.plan.md`. Governance
files (config, rules, hook wiring, `AGENTS.md`) are deliberately not
exempt. `bewit init` installs three enabled rules in
`90-bewit-self-protection.yaml`:

| Rule | Kind | Effect |
| --- | --- | --- |
| `bewit-self-protection` | path, block | Edits to governance files pause for approval. |
| `bewit-no-record-deletion` | shell, block | Deleting or moving `.bewit/` or anything in it (`rm`, `del`, `Remove-Item`, `git rm`, `mv`, …) pauses for approval. Path rules only see edits; deletion is a shell act. |
| `bewit-no-agent-ack` | shell, deny | The agent cannot acknowledge findings, from the CLI or by calling the viewer with an HTTP client. Mentioning either in text (a plan, a commit message) is allowed. |

Existing repositories get them with the next `bewit init` only if the
file does not exist yet; otherwise copy the rules from a fresh init.

## Read, shell, MCP, and tool rules

```yaml
- id: no-secret-reads
  kind: read
  match: ["dev-secrets/**", ".env", ".env.*"]
  action: deny
  message: "Agents may not read secrets or environment files."

- id: no-push
  kind: shell
  match_command: ['\bgit\s+push\b']
  action: block
  message: "Pushing requires explicit approval."

- id: flag-installs
  kind: shell
  match_command: ['\b(pip|uv pip|npm|pnpm|yarn)\s+(install|add)\b']
  action: flag
  message: "Dependency installation - recorded for review."

- id: no-web-mcp
  kind: mcp
  match_tool: ['web|fetch|search']
  action: deny
  message: "Internet-facing MCP tools are not permitted here."

- id: no-subagents
  kind: tool
  match_tool: ['^Task$']
  action: deny
  message: "Subagents are not permitted in this repository."
```

`read` rules run on `beforeReadFile` and on `preToolUse` for `Read`,
`read_file`, and `Grep`. Cursor often delivers agent reads only through
`preToolUse`. Every read Bewit sees is logged, including allowed ones,
with the hook that reported it (`via`). A read of a known file with no
event means the runtime never called Bewit. Exempt paths are not denied.

`read` rules also apply to shell commands (Bash and PowerShell tools):

- reader programs' file arguments: `cat .env`, `head < .env`,
  `grep KEY config/.env.local`, `sha256sum .env`, `cp .env /tmp`,
  `Get-Content .env`, `Get-FileHash .env`;
- quoted paths in interpreter inline code: `python -c "open('.env')"`,
  `node -e`, `pwsh -Command`, and nested `bash -c 'cat .env'`;
- quoted paths anywhere in a PowerShell-tool command
  (`[IO.File]::ReadAllText('.env')`).

Commands that only *mention* a path (`ls`, `git add`, a commit message)
are not reads. Trade-off: a quoted secret path in inline code counts as a
read even in `print('.env')`. Out of reach: paths built at runtime
(`'.e'+'nv'`, `cat $(echo .e)nv`), programs that open files themselves
(`python app.py`), and `Grep` pointed at a directory that contains a
secret. Bewit never records command output. This is policy, not a
sandbox.

`shell` patterns are case-insensitive regexes over the command text, so a
determined agent can evade them (`bewit-""deny`). Treat shell rules as a
clear statement of intent plus an audit trail. `mcp` and `tool` patterns
match the tool identifier.

## Check rules

`check` rules delegate to a real analyzer — Semgrep, Ruff, pytest, or any
script — over the files the session changed.

```yaml
- id: endpoint-validation
  kind: check
  match: ["api/**/*.py"]
  command: "semgrep scan --config .bewit/checks/endpoints.yaml --error --quiet {files}"
  action: block          # block | flag. deny is rejected: checks are post-hoc
  message: "New/changed endpoints must validate input."
  timeout: 60
```

- `{files}` is replaced with the matched paths, quoted for the platform
  shell. Exit code 0 is pass. Output is stored with the event (capped).
- A rule whose globs match nothing is skipped.
- A missing tool or a timeout is an *error*, not a failure. The session
  is not failed because Bewit's own tooling is absent.
- `block` failures become final-review findings and fail
  `bewit check diff`. `flag` failures are recorded and highlighted only.

Three honesty tiers show up in the review: **verified** (check rules),
**gated** (path, read, shell, mcp, tool), and **attested** (prompt rules).

## Prompt rules

```yaml
- id: api-rate-limit
  kind: prompt
  policy: "Every new API endpoint must specify rate limiting and authorization."
```

The agent runs `bewit rules`, judges each policy, and records
`bewit check plan --attest api-rate-limit=pass`. Verdicts are `pass`,
`fail`, or `n/a` (the policy does not apply to this plan; it counts as
satisfied and stays visible). A prompt rule with `applies_to` is `n/a`
automatically when the plan declares no file in its scope. Verdicts are
attestations. A missing or failed attestation is visible in the review and,
in strict mode, keeps the edit gate closed. A check without a registered
plan never passes.

## Checkpoints

```
prompt ──> plan ──> check ──> edits ──> stop ──> commit
            │         │         │         │         │
            │   [2] check plan  │   [4] finalize    │
      [1] plan register   [3] live gates      [5] git trailer
```

1. **Plan registration** (`bewit plan register <file>` or `--text`).
   The plan is snapshotted and declared paths are extracted for drift.
   Registering again in the same session adds the new paths to the
   earlier declaration; `--replace` starts over. Paths that do not exist
   are listed with the closest repository file, so a truncated or
   mistyped path is caught before it turns into drift.
2. **Plan check** (`bewit check plan`). Declared files are matched
   against path rules (a preview of the gate) and every prompt rule needs
   a pass/fail/n/a attestation. A verdict from the previous check carries
   over when the declared files inside that rule's `applies_to` scope
   (the whole declaration, for an unscoped rule) are unchanged. The check
   exits non-zero when it does not pass, including when no plan is
   registered. A check run without `--attest` that does not pass is
   shown but not recorded, so it cannot replace a passing check.
3. **Live gates.** Edits, reads, shell commands, and MCP/tool calls are
   matched before they run. In strict mode the first edit of a repository
   file is denied until a *passing* plan check exists; files outside the
   repository (a runtime's own plan files) are not held back. Strict mode does not apply to shell
   commands, so the agent can run `bewit plan register` and
   `bewit check plan` to unlock itself.
4. **Finalize.** The stop hook writes `manifest.json` and `report.md`:
   line counts, drift (touched but not planned, planned but not touched),
   and a protected-path scan of the diff. The scan is session-scoped; see
   [Parallel sessions](guide.md#parallel-sessions). Material findings
   (gate bypasses, a failed plan check, out-of-plan drift, failed `block`
   checks) are sent back to the agent once, with the instruction to report
   them to you rather than acknowledge them. `final_check: false` turns
   that notification off. The same findings, with why each matters and
   what to do, lead `bewit show`, `report.md`, and the viewer's review
   box. Resolve a legitimate finding with an audited acknowledgment, from
   your own terminal or the viewer's **Acknowledge** button:

   ```powershell
   bewit ack .cursor/hooks.json --note "hooks rewired by bewit init, user-approved"
   bewit ack plan-check --note "policy waived for this hotfix"
   bewit ack check-python-syntax --note "generated file, excluded upstream"
   ```

   The finding stops being re-raised. The ack stays in the hash chain with
   its `actor`. **Acknowledging is the reviewer's decision.** An ack made
   while one of the agent's commands runs is recorded as `actor: agent`
   and clears only plan drift; gate bypasses, failed checks, a failed plan
   check, and a broken chain still need you. The shipped
   `bewit-no-agent-ack` shell rule refuses `bewit ack` from the agent
   outright; the attribution is the backstop when that regex is evaded.
   Acks recorded before attribution existed count as the reviewer's. The
   protected-path result in the manifest is unchanged.

   A paused action that then ran is recorded as approved (`gate_resolved`),
   so the review can tell "you approved" from "did not run".
5. **Commit linking.** The git hook matches staged files to recent
   sessions and appends `Bewit-Session:` trailers, including commits
   made hours later.

`bewit rules explain <path>` prints which rules match and which
checkpoint would fire, including an explicit "no rule matches".

## Configuration

`.bewit/config.yaml`. Invalid values fall back to defaults and are
reported by `bewit rules`.

```yaml
enforcement: on       # on | monitor | off
strict_plan_check: false
protected_scan: true
final_check: true     # tell the agent about open findings at session end
prompt_capture: full  # full | excerpt | none | sealed
record_scope: audit   # audit (share manifest.json only) | full (also the event log)
record_store: ref     # ref (refs/bewit/records, never in code commits) | tree (.bewit/sessions/)
# exempt:             # added to the built-in exemptions, never a replacement
#   - docs/**
```

Prompt text is the most sensitive thing Bewit stores. Choose
`prompt_capture` before a team rollout. `sealed` keeps ciphertext for the
public keys in `.bewit/recipients.yaml` (`bewit seal keygen`). If
sealing cannot run, capture falls back to `none`, never to plaintext.
Details: [Prompt privacy](guide.md#prompt-privacy).
