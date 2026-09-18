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
archrev init
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
3. **Edit gate** (automatic, `preToolUse` hook): every agent file edit is
   matched against path rules — `block` pauses for your approval, `flag`
   records. With `strict_plan_check: true`, the *first* edit of a session is
   denied until a plan check exists: **no validated plan, no code.**
4. **Finalization** (automatic, `stop` hook): writes `manifest.json` and
   `report.md` with per-file line counts (`git diff --numstat` since session
   start), **drift** (files touched but not planned, files planned but never
   touched), and a **protected-path scan** of the full diff — which catches
   protected files changed *around* the gate, e.g. by shell commands like
   `manage.py makemigrations`.
5. **Commit linking** (automatic, git hook): staged files are matched against
   recent sessions and `ArchRev-Session: <id>` trailers are appended, even
   when you commit hours after the session ended.

Enforcement is configurable in `.archrev/config.yaml`:

```yaml
enforcement: on      # on | monitor (record, never stop) | off
strict_plan_check: false
protected_scan: true
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

## How to review — at any point in time

**Live, while the agent works** (the second-monitor view):

```powershell
archrev serve        # http://127.0.0.1:4177
```

A local, auto-refreshing timeline per session: prompt, plan, rule verdicts,
every edit with line counts, gate pauses and flags highlighted inline, drift
chips ("planned 6, touched 9, 3 out-of-plan"), and linked commits. It is a
pure viewer — stopping it never affects capture.

**From the terminal:**

```powershell
archrev sessions                 # recent sessions
archrev show                     # full chain of the latest session
archrev show <id> --md -o r.md   # markdown report (MR descriptions)
archrev trace src/api/views.py   # which sessions touched this file?
archrev trace 1a2b3c4d           # which session produced this commit?
archrev rules                    # active rules + config + loading problems
archrev check diff               # scan current git changes against rules (CI-ready)
```

**As a shareable artifact:**

```powershell
archrev export <session>         # one self-contained HTML file
```

Every session directory also contains a durable `report.md`, regenerated at
finalization — readable in any git UI, forever.

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
can be regenerated. Because it all lives in git, provenance survives ArchRev
itself: even without the tool, the record is plain text in your history.

## Honest limitations (v0.1)

- Cursor-only capture (the hook adapters are thin; other agent runtimes with
  hook systems are a planned extension).
- Prompt-rule verdicts are agent self-attestations — recorded and surfaced,
  not independently verified.
- Human hand-edits between agent turns appear in the finalize diff
  (`other changes`), not in the per-edit stream.
- `plan register` / `check plan` bind to the most recently active session;
  with several agents in one repo simultaneously, pass `--session` explicitly.
- If hooks are disabled or the CLI leaves PATH, capture stops silently by
  design (fail-open); `archrev sessions` shows the gap.

## Development

```powershell
uv venv && uv pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

MIT licensed. Contributions welcome.
