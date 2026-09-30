# Security model

Bewit governs an AI agent at its **tool layer**: it sees each tool call
the agent runtime announces through hooks, before and after it runs. That
makes it good at stopping and recording what an agent *tries*, and it
makes the limits predictable: anything that does not pass through an
announced tool call, or that hides its intent inside one, is out of
reach. Bewit is policy plus audit, not a sandbox. For hard containment,
run the agent in an isolated environment (container, no network, no
secrets mounted) and use Bewit on top.

## Three tiers of assurance

| Tier | Rule kinds | Strength |
| --- | --- | --- |
| **Gated** | `path`, `read`, `shell`, `mcp`, `tool` | Checked before the tool runs; `deny` refuses, `block` asks you. Evadable only by routes listed below. |
| **Verified** | `check`, protected-path scan, hash chain | Computed from the working tree, a real analyzer, or the log itself, at turn end and in CI. Independent of what the agent says. |
| **Attested** | `prompt` | The agent's own statement about its plan. Evidence of intent, not proof. |

The end-of-turn review is the backstop for the gated tier: a protected
file that changed without passing the gate is raised as a **bypass**, no
matter how it changed.

## What is enforced where

| Agent action | Before it runs | After the turn |
| --- | --- | --- |
| Edit/Write/NotebookEdit/apply_patch | `path` rules (deny / ask / flag), strict-plan mode | edit recorded with a content fingerprint |
| Read, Grep with a file path | `read` rules | read recorded |
| Shell (Bash, PowerShell, exec_command) | `shell` rules on the command text; `read` rules on files it reads (see below) | command recorded; working-tree changes attributed to it |
| MCP tool | `mcp` rules | call recorded; changes attributed to it |
| Any tool | `tool` rules | — |
| Anything that changed a protected file without the gate | — | **bypass** finding |
| Changed files matching a `check` rule | — | analyzer runs; `block` failures are findings |
| Merge request | `bewit check diff`, `bewit verify` in CI | — |

`block` is an *ask* on Cursor and Claude Code. Codex cannot ask, so
`block` is enforced as `deny` there. On Cursor, an ask category set to
auto-accept resolves the pause without a dialog; use `deny` when the
stop must not depend on editor settings.

### Reads through the shell

`read` rules also apply to shell commands, heuristically:

- reader programs' file arguments: `cat`, `head`, `grep`, `sed`, `wc`,
  hash tools (`sha256sum`, `certutil`, `Get-FileHash`), copies (`cp`,
  `Copy-Item`, `robocopy`), `Get-Content`, input redirection `< .env`;
- quoted paths in interpreter inline code (`python -c`, `node -e`), and
  the command of nested shells (`bash -c 'cat .env'`, `cmd /c type .env`,
  `pwsh -Command Get-Content .env`);
- PowerShell: reader cmdlets with positional or named paths
  (`Get-Content .\.env`, `Select-String -Path .env`), assignments
  (`$x = Get-Content .env`), and quoted paths in .NET calls
  (`[IO.File]::ReadAllText('.env')`).

These are recognised at the top level of each command segment; the table
below lists forms that are not.

Also covered: uploads (`curl -d @.env`, `-F f=@.env`, `-T .env`),
PowerShell/cmd paths with backslashes (`Get-Content .\.env`), and Git
Bash paths (`/e/repo/.env`). Commands that only mention a path (`ls`,
`git add`, commit messages, `Set-Content`, `Test-Path`) are not reads, nor
is a copy's destination (`cp .env.example .env`). A quoted secret path in inline code counts as a read even in
`print('.env')`: the gate cannot tell printing from opening.

## Known gaps

These are verified by `tests/rule_matrix.py` (cases marked GAP) or follow
from the design. None is silent in the record unless stated.

| Gap | Example | What the record shows |
| --- | --- | --- |
| Directory search reads files inside it | `Grep` over the repo with a `.env` filter | a read of the directory, allowed |
| Paths assembled at runtime | `python -c "open('.e'+'nv')"`, `cat $(echo .e)nv` | the command |
| Readers hidden in subshells or nesting | `echo "$(cat .env)"`, backticks, `find . -exec cat {} \;`, `Write-Output (Get-Content .env)` | the command |
| Readers the heuristic does not know | `vim .env`, `git show HEAD:.env`, `sudo -u x cat .env` | the command |
| Programs that open files themselves | `python app.py` reading `.env` | the command |
| Regex shell rules can be rephrased | `echo bewit-test-""deny` | the command |
| Network access from an allowed command | `curl`, a script's HTTP call | the command (write `shell` rules for known tools) |
| Hooks disabled, CLI missing, or wiring stale | `disableAllHooks` in `~/.claude/settings.json` | nothing while off; `bewit rules` and the viewer warn about stale wiring and about `disableAllHooks` in the project, local, or user Claude settings (only on the machine that has it; see below) |
| Bewit itself crashes | a bug in a hook | the gate fails **open** by design (logged to `hook-errors.log` and the timeline), so a broken Bewit never blocks the editor |
| Hand edit saved while an agent command runs | — | attributed to that command (reviewer can acknowledge) |
| Plan attestations | an agent attests `pass` untruthfully | the attestation; the timeline shows what it actually did |

Close gaps with containment, not more regexes: keep secrets out of the
agent's workspace, run it without network where that matters, and let
Bewit record and review what it did.

Hooks turned off outside the repository leave no trace in it. Where that
matters, pin them with Claude Code's managed settings. An administrator
deploys these, and they take precedence over user and project settings.
Setting `disableAllHooks: false` there keeps the hooks on. Verify the
behaviour on your Claude Code version. `bewit init` protects
`.claude/settings.local.json` for the same reason.

## Acknowledgments and self-review

Findings can be accepted with `bewit ack` (terminal) or the viewer's
Acknowledge button. Because an agent could try to clear its own findings:

1. The shipped `bewit-no-agent-ack` shell rule refuses `bewit ack`
   from the agent, and an HTTP client (`curl`, `Invoke-RestMethod`,
   `requests`, `fetch`, …) calling the viewer's acknowledge endpoint.
   Text that only mentions the endpoint (a plan, a commit message) is not
   refused.
2. An acknowledgment made while one of the agent's commands is running is
   recorded with `actor: agent` and clears only *not in plan* findings.
   Bypasses, failed checks, a failed plan check, and a broken record stay
   open. This attribution is structural (an open command window in the
   log), not an environment variable a user's terminal might share.
3. The end-of-turn message tells the agent to report findings to the user
   and not acknowledge them.

4. Commands that run `bewit ack` keep a non-secret marker in their
   label (`shell [bewit ack]`) even without full command capture, so an
   agent acknowledging another session is still attributed.
5. A session active in the last 30 minutes but recorded without command
   checkpoints gives *unverified* acknowledgments, treated like the
   agent's.
6. An acknowledgment answers the finding as it stood: a failing check run,
   a plan check, or a change to the file *after* the acknowledgment
   reopens it.

Limits: a human acknowledging while the agent is mid-command is recorded
as the agent (conservative; retry when it is idle). Acknowledgments from
before attribution existed count as the reviewer's.

## Integrity of the record

- Every event carries the hash of its content and of the previous event.
  `bewit verify` reports a **modified** log (content changed, or an
  event removed), a **fork** (two events chaining to one predecessor:
  concurrent writes, or an insertion), and harmless **duplicate**
  deliveries separately.
- This is tamper-*evidence*. Someone who can rewrite the repository can
  rewrite a chain; they cannot quietly alter a history that a committed
  manifest (which carries the chain head), an exported report, or a
  reviewed merge request already cites.
- With `record_store: ref`, records live on `refs/bewit/records`. Bewit
  only ever fast-forwards or merges that ref and pushes without force, so
  history there is append-only in normal use. Anyone with push rights
  could force-push it; protect `refs/bewit/*` on the server (branch
  protection rules or a server-side hook) where that matters, as you
  would a release branch.
- Governance files (`.bewit/config.yaml`, `.bewit/rules/**`, hook
  wiring, `AGENTS.md`) are gated by the enabled `bewit-self-protection`
  rule, so an agent cannot silently disable capture or enforcement.
  Changing them through the shell is a bypass finding.
- Deleting or moving `.bewit/` or anything in it (`rm`, `rmdir`, `del`,
  `Remove-Item`, `git rm`, `mv`, `Move-Item`, `shutil.rmtree`) pauses for
  approval (`bewit-no-record-deletion`, shipped by `bewit init`). Like
  every shell rule it matches command text, so a command that only
  *mentions* such a deletion (a script's test string) pauses too, and a
  rephrased one can slip through; the end-of-turn scan still reports
  protected files that changed.

## What Bewit stores, and what it does not

Stored: prompts (per `prompt_capture`: full, excerpt, none, or sealed to
recipients' public keys), plan text, rule decisions, file paths, content
fingerprints (not content), command text (only with
`prompt_capture: full`, capped at 120 characters), the output of `check`
rule commands (capped; these run Bewit's configured analyzers, not the
agent's commands), and acknowledgment notes.

Never stored: tool or command **output**, file contents, or diffs (diffs
are computed from git on demand; `bewit export` embeds them in the HTML
file it writes). Under any `prompt_capture` other than `full`, rule
decisions on shell commands record a short hash instead of the command,
and diagnostic records of unrecognised hook payloads keep field names and
lengths, not text. A secret an agent manages to read ends up in the agent's
own transcript, not in `.bewit/`. A secret typed into a command line is
kept in the command label under `prompt_capture: full`.

## The viewer

`bewit serve` binds to `127.0.0.1` by default and serves only
`.bewit/` data and git diffs of the repository.

- Requests with a non-loopback `Host` header are refused, so a web page
  cannot read the viewer through DNS rebinding.
- The only writes (acknowledge, unseal) require an `X-Bewit` header,
  which a cross-site form cannot send.
- Diffs of files covered by a `deny`/`block` read rule are never served
  or exported (an untracked file's diff is its whole content). Without
  this, an agent could read a secret through the viewer with `curl`. A
  diff request names exactly one file: paths are taken literally and
  directories are refused, so `.`, a directory, or a glob cannot widen it.
- Values from session records passed to git (the session's starting
  commit) must be commit ids, so a crafted record cannot inject git
  options.
- There is no authentication. `--host 0.0.0.0` exposes prompts and diffs
  to the network; do not do that on a shared network.
- Unsealed prompts are decrypted for the requesting tab and never written
  back.

## Reporting a vulnerability

Open a private report with the maintainers rather than a public issue,
with the Bewit version, runtime, and a minimal reproduction (a
`tests/rule_matrix.py` case is ideal).
