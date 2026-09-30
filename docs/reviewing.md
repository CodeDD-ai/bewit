# Reviewing a session

A session is one agent conversation. This page explains how to read it in
the viewer (`bewit serve`), what each part means, and what to do about
it. The same content leads `bewit show` and each session's `report.md`.

## Opening the viewer

```powershell
bewit serve                 # the project of the current directory; opens the browser
bewit serve --all ~/code    # every Bewit repository in ~/code, with a project switcher
bewit serve --no-open       # print the URL only
```

The header names the project. A second `bewit serve` for the same project
reuses the running viewer. If another project holds the port, the new
viewer takes the next free port and says so. Links carry
`#project=…&session=…&tab=…`, so a URL opens the same view for a colleague
on the same machine.

If a yellow banner says the viewer is older than its page, restart
`bewit serve`. If it says the hook wiring is out of date, run
`bewit init` in the project.

## The sidebar

| Section | Contains |
| --- | --- |
| **Recording now** | Agent sessions with an event in the last 15 minutes. Runtime, branch, how long it has been recording, last event. |
| **Earlier** | Everything else, newest activity first. *Needs review only* filters to sessions with open findings. |
| **Other records** | `human` (commits no agent session explains) and `unknown` (hook events without a session id: diagnostics). |

A red **N to review** is the number of items in that session's review box.

## The session page

Top to bottom: what needs you, a summary, then the full trace.

### 1. The review box

Either **"Nothing needs your review"** or a list of findings. Each finding
has the same four parts:

- **Title**: what happened, to which file or check.
- **Why it matters**: the rule or expectation that was not met.
- **Likely cause**: the command that was running when the change
  happened, when Bewit can tell. *Uncertain* means that command never
  reported finishing, so the change may come from you, another tool, or
  another session.
- **What to do**, with **View diff** / **View check output** and
  **Acknowledge…**.

| Finding | Meaning | Typical response |
| --- | --- | --- |
| **Gate bypass** | A file covered by a `deny`/`block`/`flag` rule changed without the rule running: through a shell command, a hand edit, or a command after its approved edit. | Read the diff. Intended: acknowledge with a reason. Not intended: revert. |
| **Check failed** | A `block` quality check (`kind: check`) failed on files this session changed. | Read the output. Fix, or acknowledge if expected. |
| **Plan check** | The agent's own plan check did not pass: no plan, or a policy attested `fail` or left unattested. | Talk it through with the agent; have it re-check, or acknowledge a waiver. |
| **Not in plan** | The agent changed a file its plan never mentioned. | Confirm it belongs to the task. |
| **Record integrity** | The event log's hash chain does not verify. | Run `bewit verify`; compare with the committed manifest. |

Below the findings, **Resolved** lists acknowledged items (who and why),
and **Notes** lists advisory items: `flag`-level check failures, and
acknowledgments the agent attempted that did not count.

#### Acknowledging

Acknowledging accepts a finding as legitimate. It is recorded in the
hash-chained log with your note and never deleted.

- In the viewer: **Acknowledge…**, type why, confirm.
- Several at once: with more than three open findings, each gets a
  checkbox. Tick them, or use **Select all** or *all not in plan* (one
  link per kind), then **Acknowledge selected…**. The form sums up what
  you are accepting and warns when it includes a gate bypass or a failed
  check. One note applies to all. Each finding is still recorded as its
  own acknowledgment, so the log reads as if you had acknowledged them one
  by one. A broken record cannot be selected.
- In your terminal: `bewit ack <path | check-rule-id | plan-check> --note "why"`.

Only the reviewer acknowledges. An acknowledgment made while one of the
agent's commands is running is recorded as the **agent's**; it clears
only *Not in plan* findings. Bypasses, failed checks, a failed plan check,
and a broken record still need you. If you click Acknowledge while the
agent is mid-command, the viewer tells you and asks you to retry when the
agent is idle.

### 2. Summary cards

- **Rules at work**: how often rules stopped the agent (refused, paused)
  and flagged changes, with the outcome of each pause (*you approved*,
  *did not run*, *waiting for you*). Stops are the rules doing their job,
  not problems.
- **Changes**: files the agent changed, how many were not in the plan,
  and how many planned files were never touched.
- **Record**: whether the hash chain verifies.

Each card opens the tab with the details.

### 3. Timeline

One card per prompt, newest first. The newest is open; click any card to
expand it. Inside, the prompt text, then everything that followed, newest
first, each with its time:

| Event | Means |
| --- | --- |
| Plan registered · N files declared | The agent recorded its plan. Expand for the files; *Open plan* for the text. |
| Plan check passed / did not pass | The agent's attestation of each policy. |
| Rule refused / paused / flagged *an edit, a command, a file read, …* | A rule fired. The badge shows the outcome and the rule id. |
| Ran a command / Called an MCP tool | What the agent executed. |
| Started a command · *no completion recorded* | The command may still be running, may have been stopped by the agent runtime's own permission check before it ran, or its end hook is not wired. |
| Changed N files | Tool edits. Expand for the list; click a file for its diff. |
| Quality check passed / failed | A `check` rule's result, with output on failure. |
| End-of-turn review | What Bewit told the agent when its turn ended. |
| Acknowledged a finding | Who (reviewer or agent), and why. |
| Commit | A commit linked to the session by its trailer. |

Search filters prompts, files, rules, and commands.

### 4. Files, Rules, Plan

**Files**: every file this session changed, with lines added/removed and
one status:

| Status | Meaning |
| --- | --- |
| Changed outside the gate — review | A gate bypass (see the review box). |
| Not in the plan — review | Unplanned change. |
| Approved by you | A rule paused this edit and you approved it. |
| Flagged — worth a look | A `flag` rule covers the file; the change was allowed. |
| Acknowledged by reviewer / agent | A finding on this file was accepted. |
| OK | No rule or plan concern. |

Click a row for the diff against the commit the session started from.
Changes by other sessions, hand edits, and work that pre-dates the
session are listed separately under *Not from this session*; they are
never raised as this session's findings.

**Rules**: *What the rules did* (each rule that fired, its outcomes, and
every occurrence), *Plan policies* (the latest attestation per policy,
with earlier checks folded), *Quality checks* (latest result per check),
and *Other rules in this project* (active rules that did not fire, by
kind; disabled rules folded). Every rule shows one plain sentence of what
it does ("When the agent edits a file matching `pyproject.toml`, Bewit
pauses it until you approve") and its message.

**Rule details.** A rule name anywhere in the viewer (timeline, review
box, Files, Rules) is a button. It opens the rule's full definition:
what it does, its message or policy, the patterns and scope, the action,
the file that defines it, the rule as YAML, and every time it fired in
this session. The link (`…&rule=<id>`) can be shared. Rule events in the
timeline also quote the rule's message directly, so you do not have to
open anything to see why the agent was stopped.

The definition shown is the current one from `.bewit/rules/`. If the
rule was edited after it fired, the drawer says so and shows the message
it had then; if it was deleted, it is marked *not defined any more* and
shows what the record kept (events record each rule's message).

**Plan**: declared files (✓ changed, ○ not changed) and every plan
revision, newest first, with files added and dropped between revisions.

## From the terminal

```powershell
bewit show                     # latest session, review first
bewit show <id> --md -o r.md   # markdown, e.g. for a merge request
bewit trace src/api/views.py   # sessions that changed a file
bewit trace 1a2b3c4d           # the session behind a commit
bewit export <id>              # one HTML file with diffs, for sharing
```

## Terms

| Term | Meaning |
| --- | --- |
| **Gate** | The check Bewit runs before the agent's tool acts. |
| **deny / block / flag** | Refuse / pause for your approval / allow and highlight. |
| **Bypass** | A protected file changed without the gate seeing it. |
| **Drift** | Files changed but not planned (*not in plan*) or planned but not changed. |
| **Attestation** | The agent's own statement that its plan meets a policy. Evidence, not proof. |
| **Background change** | A change made while none of this session's commands ran. Not this session's. |
| **Acknowledgment** | A reviewer's audited acceptance of a finding. |
