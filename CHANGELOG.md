# Changelog

## Unreleased

### Renamed: ArchRev is now Bewit

In falconry, the bewit is the strap that holds the bell on a falcon's leg:
the bird hunts on its own, and the bell tells the falconer where it is.

- Package and command: `bewit` (`pip install bewit`, `bewit init`).
- Per-repository folder `.bewit/`, records ref `refs/bewit/records`,
  commit trailer `Bewit-Session:`, environment variables `BEWIT_*`,
  viewer header `X-Bewit`, shipped rules `bewit-self-protection`,
  `bewit-no-record-deletion`, `bewit-no-agent-ack`, protocol files
  `.claude/rules/bewit.md` and `.cursor/rules/bewit.mdc`.
- **Not compatible with ArchRev records**: `.archrev/` folders,
  `refs/archrev/records`, and `ArchRev-Session:` trailers are not read.
  Remove the old setup and run `bewit init`.
- GitHub Actions: `ci.yml` (ruff, pytest on Linux/Windows/macOS with
  Python 3.11–3.13, the rule matrix, a packaging check) and `release.yml`
  (a `v*` tag runs CI, checks the tag against the package version, and
  publishes to PyPI with Trusted Publishing, no stored token).

### Less bookkeeping for agents (from agent feedback)

- **The CLI binds to the calling session.** `plan register`, `check plan`
  and `ack` resolve `--session latest` from the runtime's session id
  (`BEWIT_SESSION`, `CLAUDE_CODE_SESSION_ID`), then from a claim the
  shell hook leaves for the `bewit` command it lets run. With several
  live sessions and no binding they refuse instead of guessing. Before,
  a second agent's plan could land in the first agent's session.
- **Plans merge within a session.** Registering again adds files to the
  earlier declaration; `--replace` starts over (`--amend` is now the
  default and stays accepted). Earlier steps no longer turn into drift.
- **Plan paths.** Slash-joined lists (`tests/a.py/b.py`) and brace lists
  (`src/{a,b}.py`) declare each file; paths in parentheses are declared.
  Paths that do not exist are listed with the closest repository file.
- **Net-change drift.** A file edited and then reverted is listed under
  `drift.no_net_change`, not raised as out of plan.
- **New findings only.** The end-of-turn follow-up names findings not
  reported yet and counts the rest (`final_check.raised_keys`).
- **Plan checks.** Verdicts carry over for policies whose part of the plan
  is unchanged (`carried: true`). A check without `--attest` that does not
  pass is shown but not recorded, so it cannot replace a passing check.
- **Strict mode ignores files outside the repository**, such as a
  runtime's own plan files.
- **`bewit ack`** takes several targets and glob patterns, expanded
  against the open findings at that moment, and `--earlier-plans`.
- The report and viewer label policy attestations as self-reported.

### Hooks turned off

- `bewit rules` and the viewer now warn when `disableAllHooks` is true
  in `.claude/settings.json`, `.claude/settings.local.json`, or
  `~/.claude/settings.json`. Any of these silences every Bewit hook
  without touching a governed file.
- The shipped `bewit-self-protection` rule also covers
  `.claude/settings.local.json`. Existing repositories get it by adding
  the path to `.bewit/rules/90-bewit-self-protection.yaml`.
- New [docs/how-it-works.md](docs/how-it-works.md) explains the
  components and the trust model on one page.

### License

- **Apache License 2.0** (explicit patent grant), with a `NOTICE` file.

### Records on their own git ref

- New `record_store: ref` (default for new repositories): session records
  are published to `refs/bewit/records` with git plumbing at each turn
  end and commit, and never appear in code commits or merge-request
  diffs. New `bewit sync` fetches, merges (union), publishes, and pushes
  the ref; `bewit init` installs a pre-push hook that runs it, so
  `git push` shares records automatically. Readers (`serve`, `show`,
  `trace`, `index`, `verify`) materialize records from the ref. CI
  templates fetch it. `bewit init --record-store ref|tree` switches an
  existing repository; `tree` keeps the previous behaviour.
- `bewit sync` without `--remote` uses the remote `git push` would use
  (push remote, upstream, `origin`, or the only remote) instead of
  assuming `origin`, and names it in its output.
- `bewit rules explain` reports paths covered by `exempt` as exempt (it
  showed the rules the gate skips there), and lists the rules that would
  apply without the exemption.
- Fixed while building it: `check diff --base origin/<branch>` resolved
  the branch to HEAD after the last round's base validation; branch names
  now resolve to their commit.

### Rules explain themselves in the viewer

- Every rule name (timeline, review box, Files, Rules) opens a **rule
  drawer**: a plain-language sentence of what the rule does, its message
  or policy, patterns, scope, action, defining file, the rule as YAML,
  and everything it did in the session. Deep link: `&rule=<id>`.
- Timeline rule events quote the rule's message inline. The Rules tab
  describes every rule, lists all rules of the project by kind (disabled
  ones folded), and labels rules that were edited or deleted since they
  fired.
- Gate events record each rule's `message` (additive), so old records
  stay readable after a rule is removed.
- **Bulk acknowledge**: with more than three open findings, the review box
  offers checkboxes, *Select all*, and select-by-kind, with one note and
  one confirmation. Each finding is still recorded as its own
  acknowledgment.
- `bewit-no-agent-ack` no longer refuses commands that only *mention*
  the viewer's acknowledge endpoint (a plan registration, a commit
  message); it refuses HTTP clients calling it. Existing repositories:
  copy the rule from a fresh `bewit init`.
- Fixed: the viewer answered a refused POST before reading its body,
  which Windows could turn into a connection reset instead of the 403.

### Smaller committed record

- `record_scope: audit` commits **one file per session** (`manifest.json`,
  which now carries the plan text); `full` commits `events.jsonl`,
  `meta.json`, and `manifest.json`. `plan.md` and `report.md` are never
  committed; `bewit show`, `export`, and the viewer render from the
  manifest when the event log is not in the checkout.
- `bewit init` keeps a managed `.gitignore` block in line with the scope
  (replacing the old audit line) and marks session records
  `linguist-generated` / `gitlab-generated`, so merge-request diffs
  collapse them.

### Review

- **Review-first reports.** One definition of "needs review" (gate
  bypasses, failed `block` checks, a failed plan check, unplanned files, a
  broken record) drives the agent's end-of-turn message, the viewer's
  review box and sidebar count, `bewit show`, and `report.md`. Each
  finding says why it matters, the likely cause, and what to do.
- **Viewer rebuilt.** Review box with View diff and Acknowledge; summary
  of what the rules did; a timeline per prompt, newest first, including
  every command the agent ran; one plain-language status per file; a
  Rules tab that shows what each rule did. Deep links
  (`#project=…&session=…&tab=…`).
- **One viewer per project, or a hub.** `bewit serve` reuses a viewer
  already serving the project, moves to a free port when another project
  holds it, and opens the browser (`--no-open`). `bewit serve --all DIR`
  serves several repositories with a project switcher.
- **Approvals are recorded.** `gate_resolved` marks a paused action that
  ran, so the review shows *you approved* / *did not run* / *waiting*.

### Enforcement

- **Deleting the record pauses.** New shipped rule
  `bewit-no-record-deletion`: deleting or moving `.bewit/` or
  anything in it through the shell asks for approval (path rules only see
  edits).
- **Agents cannot clear their own findings.** Acknowledgments carry an
  `actor`; one made while an agent command runs is the agent's and clears
  only plan drift. New shipped rule `bewit-no-agent-ack` refuses
  `bewit ack` from the agent. The end-of-turn message tells the agent to
  report findings, not acknowledge them.
- **Strict mode** can no longer be unlocked by a plan check without a
  registered plan; such a check never passes.
- **`read` rules apply to shell commands**: reader, hash, and copy
  programs, input redirection, interpreter inline code, and PowerShell.
  The Claude Code **PowerShell** tool is now gated and tracked like Bash.
- **Shell change after an approved edit** is a bypass (edit events carry a
  content fingerprint).
- Prompt-rule verdict **`n/a`**; `applies_to` on prompt rules makes a
  rule `n/a` when the plan declares nothing in its scope.
- On a deny, Claude and Codex agents receive the agent-facing guidance.

### Attribution and integrity

- A bypass is attributed to the most recent open command, and marked
  *uncertain* when that command never reported finishing. Command ends
  pair with the most recent identical start.
- `bewit verify` separates harmless duplicate hook deliveries (pass)
  from forks and modifications (fail), with a reason.
- `bewit rules` and the viewer warn when hook wiring is older than the
  installed version.

### Fixed after an independent code review

- The viewer served full contents of untracked files through `/api/diff`,
  including files covered by read rules (an agent could `curl` a secret).
  Diffs and exports of deny/block read-rule paths are now withheld.
- PowerShell paths with backslashes were not matched by read rules.
- A tool edit after a shell change could hide the bypass; same-second
  timestamps could invent one. Changes are now judged individually in
  log order against the content tool edits produced.
- "Uncertain" attribution no longer sticks for a whole turn after two
  identical parallel commands.
- Acknowledgments are bound to the finding they answer (newer evidence
  reopens it), `bewit ack` commands keep a label marker without full
  capture, and sessions without checkpoints give *unverified* acks.
- A plan that names no files no longer auto-passes scoped policies; a
  backticked new top-level file (`CHANGELOG.md`) is declared.
- Codex pauses (enforced as refusals) are recorded as refused; an old
  declined pause is not turned into "approved" by a later turn.
- `cp` destinations are not reads; `curl` uploads are. Malformed hook
  wiring no longer crashes `bewit rules` or the viewer.

### Fixed after the final review

- The viewer's diff could still be widened to a read-protected file with
  a directory, `.`, or a glob path; paths are now literal, single files.
- A rules or config file that was not UTF-8 crashed every hook (so every
  rule failed open). UTF-16 with BOM (PowerShell 5.1 `>`) is now read;
  undecodable files become a reported problem.
- Codex patches sent through a shell tool now meet path rules.
- Check-rule file arguments are always quoted on Windows (a file named
  `a&whoami&.py` could run a command); unquotable names are skipped and
  reported.
- Git base values from session records must be commit ids.
- `prompt_capture` now also governs diagnostic payloads and the command
  text in shell rule decisions.
- Session ids `.`/`..` can no longer escape `.bewit/sessions/`.
- `bewit index` and the viewer sidebar read committed manifests in
  audit scope; `bewit check diff` fails on changed `deny` paths (it
  only failed on `block`); `bewit trace <file>` works from manifests.
- The shell-read heuristic covers `Select-String -Path`, `cmd /c`,
  unquoted `pwsh -Command`, and `$x = Get-Content`.

### Terminal output

- `bewit sessions` is a table (short id, local time, recording/ended,
  runtime, edits, items to review, clean title); `show` leads with the
  review, a three-line summary, and prompts newest first (`--all` for
  every command and unrelated change); `rules` prints plain patterns;
  `rules explain` covers edits and reads; `check plan` names what failed
  and the exact fix; `check diff`, `verify`, and `trace` end with a
  summary; `init` prints repository-relative paths and next steps.
  Terminal output is ASCII-only for Windows consoles.

### Viewer security

- Requests with a non-loopback `Host` header are refused (DNS rebinding).
- Acknowledge and unseal require an `X-Bewit` header (CSRF).
- HTML escaping covers attribute values (a crafted file name could
  previously inject markup).

### Record format (additive)

`edit.fingerprint`; `gate.hits[].message`; `gate_resolved`; `ack.actor` / `actor_basis` / `via`;
`plan_check` verdict `n/a` and `scope`; manifest `review`, `commands`,
`gate_events[].outcome`, `chain.duplicates` / `forks` / `reason`. Older
records read unchanged.

### Docs

New: `docs/reviewing.md`, `docs/security-model.md`, and
`tests/rule_matrix.py` (every rule kind replayed through the real hooks in
a fresh `bewit init` repository). README rewritten. The live demo lives
in its own repository (`bewit_demo`).

## 0.5.0

Claude Code and Codex hook adapters, read rules and logging, event log
checkpoints for parallel sessions. See git history for earlier releases.
