# Changelog

## Unreleased

### Review

- **Review-first reports.** One definition of "needs review" (gate
  bypasses, failed `block` checks, a failed plan check, unplanned files, a
  broken record) drives the agent's end-of-turn message, the viewer's
  review box and sidebar count, `archrev show`, and `report.md`. Each
  finding says why it matters, the likely cause, and what to do.
- **Viewer rebuilt.** Review box with View diff and Acknowledge; summary
  of what the rules did; a timeline per prompt, newest first, including
  every command the agent ran; one plain-language status per file; a
  Rules tab that shows what each rule did. Deep links
  (`#project=…&session=…&tab=…`).
- **One viewer per project, or a hub.** `archrev serve` reuses a viewer
  already serving the project, moves to a free port when another project
  holds it, and opens the browser (`--no-open`). `archrev serve --all DIR`
  serves several repositories with a project switcher.
- **Approvals are recorded.** `gate_resolved` marks a paused action that
  ran, so the review shows *you approved* / *did not run* / *waiting*.

### Enforcement

- **Agents cannot clear their own findings.** Acknowledgments carry an
  `actor`; one made while an agent command runs is the agent's and clears
  only plan drift. New shipped rule `archrev-no-agent-ack` refuses
  `archrev ack` from the agent. The end-of-turn message tells the agent to
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
- `archrev verify` separates harmless duplicate hook deliveries (pass)
  from forks and modifications (fail), with a reason.
- `archrev rules` and the viewer warn when hook wiring is older than the
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
  reopens it), `archrev ack` commands keep a label marker without full
  capture, and sessions without checkpoints give *unverified* acks.
- A plan that names no files no longer auto-passes scoped policies; a
  backticked new top-level file (`CHANGELOG.md`) is declared.
- Codex pauses (enforced as refusals) are recorded as refused; an old
  declined pause is not turned into "approved" by a later turn.
- `cp` destinations are not reads; `curl` uploads are. Malformed hook
  wiring no longer crashes `archrev rules` or the viewer.

### Viewer security

- Requests with a non-loopback `Host` header are refused (DNS rebinding).
- Acknowledge and unseal require an `X-ArchRev` header (CSRF).
- HTML escaping covers attribute values (a crafted file name could
  previously inject markup).

### Record format (additive)

`edit.fingerprint`; `gate_resolved`; `ack.actor` / `actor_basis` / `via`;
`plan_check` verdict `n/a` and `scope`; manifest `review`, `commands`,
`gate_events[].outcome`, `chain.duplicates` / `forks` / `reason`. Older
records read unchanged.

### Docs

New: `docs/reviewing.md`, `docs/security-model.md`, `docs/demo.md`,
`.test/` rule matrix with `simulate.py`. README rewritten.

## 0.5.0

Claude Code and Codex hook adapters, read rules and logging, event log
checkpoints for parallel sessions. See git history for earlier releases.
