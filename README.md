# ArchRev

**Session provenance and architecture-rule enforcement for AI coding agents.**

Git records *what* changed. ArchRev records *why* — and makes sure the agent
followed your architecture rules while changing it.

When an AI agent implements a feature, the intent (prompt), the plan, and the
reasoning normally vanish the moment the chat window closes. ArchRev captures
each agent session as plain files inside your repository, validates plans
against architect-defined rules *before* implementation, gates edits to
protected paths *during* implementation, and links the resulting commits back
to the session — so weeks later you can still answer: *which prompt and plan
produced this line, was the plan checked against our rules, and did the
implementation drift from it?*

- **No daemon.** Cursor invokes ArchRev per event via hooks; each invocation
  appends to an event log and exits. Nothing runs in the background.
- **No database.** Everything is plain JSON/JSONL/markdown under `.archrev/`,
  versioned with your code and reviewable in merge requests.
- **Fails open.** A broken ArchRev never blocks your editor or your commits.
  Degradation is recorded and visible, never silent.

---

## Setup

Requirements: Python 3.11+, git, [Cursor](https://cursor.com) (hooks support).

```powershell
# 1. Install the CLI so it is on PATH (hooks and git invoke plain `archrev`)
uv tool install archrev        # or: pipx install archrev
# from a source checkout:      uv tool install --editable path/to/ArchRev

# 2. Install into your repository (idempotent, merge-safe)
cd your-repo
archrev init             # add --shim uvx for zero-install team wiring (below)
```

`archrev init` creates:

| File | Purpose |
| --- | --- |
| `.archrev/config.yaml` | enforcement mode, strict mode, exemptions |
| `.archrev/rules/00-starter-rules.yaml` | disabled example rules to copy from |
| `.archrev/sessions/` | one directory per agent session (the record) |
| `.cursor/hooks.json` | wires Cursor events to `archrev hook ...` (merged, never overwritten) |
| `.cursor/rules/archrev.mdc` | instructs the agent to register and check its plan |
| `.git/hooks/prepare-commit-msg` | adds `ArchRev-Session:` trailers to commits |

Cursor reloads hooks automatically; start an agent conversation and you will
see `.archrev/sessions/<conversation-id>/` appear. Commit the `.archrev`
directory — it is the audit record and is designed to be reviewed in MRs.

## How rules work

Rules are YAML files in `.archrev/rules/`. Two kinds:

### Path rules — machine-enforced at edit time

```yaml
- id: protect-typedb-schema
  kind: path
  match: ["type-db/**/*.tql"]        # globstar globs, case-insensitive;
  action: block                      # patterns without "/" match any depth
  message: "TypeDB schema change requires explicit approval."

- id: flag-infrastructure
  kind: path
  match: ["k8s/**", "Dockerfile*", "docker-compose*.yml"]
  action: flag
  message: "Infrastructure change - highlighted for review."
  applies_to: ["deploy/**"]          # optional monorepo scoping
```

- **`block`** — the edit pauses and Cursor asks *you* for approval, showing
  the rule's message. The agent is told not to work around the gate.
- **`flag`** — the edit proceeds, but is recorded, shown to the agent, and
  highlighted in the session review.

### Read, shell, MCP, and tool rules — gating beyond edits

The same engine gates what agents may **read**, which **shell commands**
they may run, and which **MCP servers / tools** they may use at all:

```yaml
- id: no-secret-reads
  kind: read                       # gates beforeReadFile
  match: ["dev-secrets/**", ".env", ".env.*"]
  action: deny
  message: "Agents may not read secrets or environment files."

- id: no-push
  kind: shell                      # gates beforeShellExecution
  match_command: ["\\bgit\\s+push\\b"]   # case-insensitive regexes
  action: block
  message: "Pushing requires explicit approval."

- id: flag-installs
  kind: shell
  match_command: ["\\b(pip|uv pip|npm|pnpm|yarn)\\s+(install|add)\\b"]
  action: flag
  message: "Dependency installation - recorded for review."

- id: no-web-mcp
  kind: mcp                        # gates beforeMCPExecution
  match_tool: ["web|fetch|search"]
  action: deny
  message: "Internet-facing MCP tools are not permitted here."

- id: no-subagents
  kind: tool                       # gates preToolUse by tool name
  match_tool: ["^Task$"]
  action: deny
  message: "Subagents are not permitted in this repository."
```

All machine-enforced kinds support three actions:

- **`deny`** — hard stop; the agent is refused outright with the message.
- **`block`** — pause; *you* approve or decline in Cursor's dialog.
- **`flag`** — allow, record, and highlight in the session review.

> **Approval caveat:** `block` translates to Cursor's *ask* permission. If
> your Cursor settings auto-approve that category of action (e.g. edits are
> set to auto-accept), the pause is resolved silently — the event is still
> recorded and highlighted in the review, but the agent is not visibly
> interrupted. When you need a stop that no editor setting can wave
> through, use **`deny`**.

**Enforcement honesty:** these rules gate the agent's *attempts* at the
tool layer and record every attempt, allowed or denied — they are policy
plus audit, not a sandbox. An allowed process can still do whatever the
OS permits (an approved script may open network connections). For hard
containment, run agent sessions inside network-isolated containers; the
container is the wall, ArchRev is the policy and the evidence.

### Check rules — real quality gates over changed code

Where path/shell/tool rules gate *names*, `check` rules gate *content* by
delegating to a real analyzer — Semgrep, ESLint, Ruff, pytest, or any
script (including an LLM-judge wrapper):

```yaml
- id: endpoint-validation
  kind: check
  match: ["api/**/*.py"]                 # which changed files trigger it
  command: "semgrep scan --config .archrev/checks/endpoints.yaml --error --quiet {files}"
  action: block                          # block | flag (post-hoc; no deny)
  message: "New/changed endpoints must validate input."
  timeout: 60
```

- `{files}` is replaced with the session's matched changed files; exit
  code 0 = pass. Output is captured as evidence in the event log.
- Checks run **at session end** (failures with `action: block` become
  final-review findings the agent must resolve or you `ack`) and in
  **`archrev check diff`** (exits 1 on block failures — CI-ready).
- Rules whose globs match no changed file are skipped, so sessions only
  pay for the checks they trigger.
- A check that cannot run (missing tool, timeout) is recorded as an
  *error*, never a failure — ArchRev stays fail-open.
- Three honesty tiers, all visible in the review: **verified** (check
  rules, machine verdicts), **gated** (path/shell/tool rules), and
  **attested** (prompt rules, agent self-reported).

### Prompt rules — policies the agent attests during planning

```yaml
- id: api-rate-limit
  kind: prompt
  policy: "Every new API endpoint must specify rate limiting and authorization."
```

Prompt rules are evaluated by the agent itself (guided by
`.cursor/rules/archrev.mdc`): it runs `archrev check plan
--attest api-rate-limit=pass ...` and the verdicts are recorded as evidence.
They are attestations, not proofs — the timeline makes missing or failed
attestations impossible to overlook.

## How rules are assessed and enforced

Four checkpoints, from planning to commit:

```
prompt ──> plan ──> check ──> edits ──> stop ──> commit
            │         │         │         │         │
            │   [2] check plan  │   [4] finalize    │
      [1] plan register   [3] edit gate       [5] git trailer
```

1. **Plan registration** (`archrev plan register <file|--text ...>`): the
   agent snapshots its plan into the session; file paths the plan declares
   are extracted for drift measurement.
2. **Plan check** (`archrev check plan`): declared files are matched against
   path rules (a preview of what will gate), and the agent records a
   pass/fail attestation for every prompt rule. Exits non-zero on failure.
3. **Live gates** (automatic): every agent file edit is matched against
   path rules (`preToolUse`), file reads against read rules
   (`beforeReadFile`), shell commands against shell rules
   (`beforeShellExecution`), and MCP/tool usage against mcp/tool rules —
   `deny` refuses, `block` pauses for your approval, `flag` records. With
   `strict_plan_check: true`, the *first* edit of a session is denied until
   a **passing** plan check exists: **no validated plan, no code.**
4. **Finalization + final review** (automatic, `stop` hook): writes
   `manifest.json` and `report.md` with per-file line counts (`git diff
   --numstat` since session start), **drift** (files touched but not
   planned, files planned but never touched), and a **protected-path scan**
   of the full diff — which catches protected files changed *around* the
   gate, e.g. by shell commands like `manage.py makemigrations`. When
   material findings exist (gate bypasses, a failed plan check, out-of-plan
   drift), the **agent receives a follow-up message** listing them, so every
   implementation ends with an explicit rule review instead of a silent
   manifest. The same findings are raised at most once (fingerprint guard +
   hook loop limit), and `final_check: false` turns the notification off.
   Legitimate findings are resolved with an **audited acknowledgment**:

   ```powershell
   archrev ack .cursor/hooks.json --note "hooks rewired by archrev init, user-approved"
   archrev ack plan-check --note "policy X intentionally waived for this hotfix"
   ```

   Acknowledged targets stop being re-raised, but the ack itself is a
   hash-chained event showing what was acknowledged, when, and why —
   resolved, never erased. (Honesty note: an agent *could* run `ack`
   itself; the event log and timeline make any self-acknowledgment
   plainly visible, and the protected-path scan result in the manifest is
   unaffected.)
5. **Commit linking** (automatic, git hook): staged files are matched against
   recent sessions and `ArchRev-Session: <id>` trailers are appended, even
   when you commit hours after the session ended.

Enforcement is configurable in `.archrev/config.yaml`:

```yaml
enforcement: on      # on | monitor (record, never stop) | off
strict_plan_check: false
protected_scan: true
final_check: true    # notify the agent of open findings at session end
prompt_capture: full # full | excerpt | none (see "Prompt privacy" below)
```

With strict mode on, only a **passing** plan check unlocks edits — a check
with failed or unattested policies keeps the gate closed until the agent
resolves them with you and re-checks.

The gate exempts only `.archrev/sessions/**` (ArchRev's own bookkeeping)
and `*.plan.md`. ArchRev's governance files — `.archrev/config.yaml`, the
rules directory, `.cursor/hooks.json`, `.cursor/rules/archrev.mdc` — are
deliberately *not* exempt: `archrev init` ships an enabled
`archrev-self-protection` rule that pauses any agent edit to them, so an
agent cannot silently switch enforcement off.

## Human changes are part of the record

Agent edits are captured live by hooks; human edits are captured at their
natural checkpoints, so the audit log stays complete:

- **Tab completions** (human-driven, editor-assisted) are captured via the
  `afterTabFileEdit` hook and tagged `origin: tab` in the session — the
  timeline shows them distinctly from agent edits.
- **Hand edits** never pass through hooks, so they are recorded at **commit
  time**: the git hook attributes every staged file to recent agent
  sessions; files no session touched are appended to a reserved `human`
  session ledger (`.archrev/sessions/human/`). Every committed change is
  therefore either linked to an agent session or explicitly marked human —
  nothing is silently unattributed. (Only active once agent sessions exist,
  so purely manual repositories generate no noise.)

## Tamper-evident audit log

Every event carries a hash over its content plus the previous event's hash.
Editing, reordering, or deleting any event breaks the chain:

```powershell
archrev verify           # latest session
archrev verify --all     # every session; exits 1 on any break
```

The timeline shows an *event chain: intact / BROKEN* chip per session. This
is tamper-*evidence*, not tamper-*proofing* — a determined attacker can
rewrite the whole chain, but cannot quietly alter history that exported
reports or reviewed MRs already reference.

## How to review — at any point in time

**Live, while the agent works** (the second-monitor view):

```powershell
archrev serve        # http://127.0.0.1:4177
```

A local, auto-refreshing timeline per session: prompt, plan, rule verdicts,
every edit with line counts, gate pauses and flags highlighted inline, drift
chips ("planned 6, touched 9, 3 out-of-plan"), and linked commits. **Click
any file row to expand its full diff** (new files included), **filter the
timeline by event type** (prompt / plan / check / gate / edit / human), and
**search** across events and files. It is a pure viewer — stopping it never
affects capture.

**From the terminal:**

```powershell
archrev sessions                 # recent sessions
archrev show                     # full chain of the latest session
archrev show <id> --md -o r.md   # markdown report (MR descriptions)
archrev trace src/api/views.py   # which sessions touched this file?
archrev trace 1a2b3c4d           # which session produced this commit?
archrev rules                    # active rules + config + loading problems
archrev check diff               # scan current git changes against rules (CI-ready)
archrev index <paths...>         # cross-repo oversight metrics (read-only)
```

**As a shareable artifact:**

```powershell
archrev export <session>         # one self-contained HTML file
```

Exports embed the per-file diffs (size-capped), so the expandable diff view
works offline too.

Every session directory also contains a durable `report.md`, regenerated at
finalization — readable in any git UI, forever.

## Team rollout — making adoption a no-brainer

The design goal: **one person wires a repo once; everyone else just pulls.**
Nothing to install per developer, nothing to start each morning, and the
enforcement backstop lives in CI where adoption is not optional.

### Zero-install wiring (`--shim uvx`)

```powershell
archrev init --shim uvx
```

Hook commands are written as `uvx archrev ...` instead of `archrev ...`.
[uv](https://docs.astral.sh/uv/)'s tool runner fetches and caches ArchRev
on first invocation, so teammates need **no ArchRev installation at all**
— uv itself is the only prerequisite. Commit `.cursor/hooks.json`,
`.archrev/`, and the rules once; from then on, onboarding a developer is
`git pull`. Re-running `init` with a different `--shim` upgrades the
existing wiring in place (never duplicates entries), so switching modes
later is safe.

### CI enforcement (`archrev ci gitlab`)

```powershell
archrev ci gitlab        # writes .gitlab/archrev-ci.yml
```

Generates a GitLab CI template with two merge-request jobs, then prints
the `include:` snippet for your `.gitlab-ci.yml`:

- **`archrev:rules`** (required): runs `archrev check diff` against the MR
  target branch — path rules *and* `check`-rule quality gates over the
  changed files — plus `archrev verify --all` so committed session logs
  arrive with intact tamper-evidence chains. Fails the pipeline on
  block-level findings.
- **`archrev:mr-report`** (best-effort, `allow_failure`): resolves the
  session behind the MR's head commit via its `ArchRev-Session:` trailer
  and renders the markdown session report. With `ARCHREV_GITLAB_TOKEN`
  set (project access token, `api` scope, masked variable) the report is
  posted as an MR comment; without it, it lands in the job artifacts.

This is the layer that needs zero developer adoption: even a laptop with
hooks disabled cannot merge changes that violate block-level rules, and
reviewers see the session provenance next to the diff.

### Prompt privacy (`prompt_capture`)

Prompts are the most sensitive artifact ArchRev stores — they may contain
secrets, credentials, or half-formed reasoning nobody intended to commit.
Before a team rollout, decide consciously what enters the (git-versioned)
record via `.archrev/config.yaml`:

| Mode | What is stored |
| --- | --- |
| `full` (default) | the whole prompt text — best provenance |
| `excerpt` | first 200 characters plus total length |
| `none` | no text; only length and a SHA-256 content hash |

Even `none` keeps sessions traceable: the hash proves *which* prompt
started a session if the text is later disclosed, without ArchRev ever
storing it.

### Cross-repo oversight (`archrev index`)

```powershell
archrev index E:\checkouts          # or several repo roots
archrev index --json                # machine-readable, for dashboards
```

Pull-based by design: session records already travel with git, so
org-level visibility is a *read* over whatever is checked out — no agents
streaming telemetry, no server to run, nothing developers can forget to
start. Per repository and in total, it reports: sessions and plan
discipline (registered plans, passing checks), **drift rate** (out-of-plan
share of touched files), gate pressure (pauses / denials / flags),
**gate bypasses**, quality-check failures, acknowledgments, human-change
events, and broken event chains. Rows with bypasses or chain breaks are
highlighted. A lead reviews a team's repos with one command; a platform
team feeds `--json` into whatever dashboard already exists.

## Storage format

```
.archrev/
  config.yaml
  rules/*.yaml
  sessions/<conversation-id>/
    meta.json        session start: id, timestamp, git HEAD at start
    events.jsonl     append-only event stream (the audit truth)
    plan.md          registered plan snapshot
    manifest.json    finalized summary: files, LOC, drift, verdicts, commits
    report.md        human-readable report
```

The JSONL event log is the source of truth; everything else is derived and
can be regenerated. Since v0.2 every event carries `prev`/`hash` fields
forming the tamper-evidence chain (`archrev verify`); records from earlier
versions remain readable and are reported as pre-chain legacy events. Because it all lives in git, provenance survives ArchRev
itself: even without the tool, the record is plain text in your history.

## Honest limitations (v0.3)

- Cursor-only capture (the hook adapters are thin; other agent runtimes with
  hook systems are a planned extension).
- Prompt-rule verdicts are agent self-attestations — recorded and surfaced,
  not independently verified.
- `block` rules depend on Cursor's approval settings being visible (see the
  approval caveat above); `deny` is the setting-independent hard stop.
- Human hand-edits are captured at commit time (the `human` ledger), not
  live; between commits they appear in the finalize diff as `other changes`.
- `plan register` / `check plan` bind to the most recently active session;
  with several agents in one repo simultaneously, pass `--session` explicitly.
- If hooks are disabled or the CLI leaves PATH, capture stops silently by
  design (fail-open); `archrev sessions` shows the gap.

## Roadmap — improvements by audience

Held in evidence from dogfooding sessions (2026-09-18); strikethrough as
they land.

**For a CTO (making this adoptable across an org):**

- Central, versioned rule packs shared across repos rather than per-repo YAML.
- ~~Tamper-evident event logs (hash-chained JSONL)~~ — shipped in v0.2
  (`archrev verify`).
- ~~A cross-repo index with the metrics that matter: drift rate,
  gate-override rate, failed-attestation trends per team~~ — shipped in
  v0.3 (`archrev index`); per-team trend lines over time still open.
- ~~CI enforcement: `archrev check diff` as a required GitLab job; MR
  descriptions auto-populated from session reports~~ — shipped in v0.3
  (`archrev ci gitlab`).
- A documented containment story (ArchRev policy + network-isolated
  containers) for the security review any rollout triggers.
  (Related, shipped in v0.3 though not on the original list: zero-install
  team wiring via `archrev init --shim uvx` and prompt-privacy controls
  via `prompt_capture`.)

**For an engineer (daily quality of life):**

- An MCP server so agents query rules and session history natively instead
  of shelling out.
- A faster hook runtime (small compiled shim or `python -S` trimming) to
  make gating cost invisible.
- Structured plans (frontmatter file lists) instead of regex extraction.
- Session labels (`archrev annotate`) so the sessions list reads like a
  changelog.
- An IDE panel so the timeline lives next to the code instead of a browser
  tab.

## Development

```powershell
uv venv && uv pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

MIT licensed. Contributions welcome.
