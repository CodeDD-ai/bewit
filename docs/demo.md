# Live demo

A 15-minute demo of what ArchRev does, using the test rules in
`.archrev/rules/30-test-matrix.yaml` and the fixtures in `.test/`. The
story in one line: **the agent works, the rules act live, ArchRev catches
what slipped past, and the reviewer sees exactly what needs them.**

The prompts are in [.test/demo/prompt.md](../.test/demo/prompt.md), ready
to paste.

## The story you are telling

| Act | Minutes | The audience sees | The point |
| --- | --- | --- | --- |
| 0. Setup | 2 | The rules file and an empty viewer | Rules are plain YAML in the repo |
| 1. Normal work | 3 | Plan, plan check, edits, a clean review | ArchRev records *why*: prompt → plan → changes |
| 2. Rules at work | 4 | A pause you approve, two refusals, a flag | Rules act *before* the agent does |
| 3. What slips past | 4 | 4 findings after a shell write, unplanned files, a failing check; the agent refused when it tries to acknowledge them | The end-of-turn review is the backstop, and the agent cannot clear it |
| 4. Review | 2 | Review box → diff → acknowledge; timeline; commit trace | A reviewer knows in seconds what needs them, with the full trace behind it |

## Before the audience arrives

1. **Working tree.** Commit or stash unrelated changes so the demo diff
   is only the demo.
2. **Fixtures.** Run the reset (it only touches demo files under `.test/`):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .test/demo/reset.ps1
   ```

3. **Rules and wiring.** Check that the test rules are active and the wiring
   is current:

   ```powershell
   archrev rules          # must list test-path-*, no-secret-reads, archrev-no-agent-ack, test-check-syntax
   ```

   No "Hook wiring is out of date" warning. If there is one, run
   `archrev init`.
4. **Viewer.** Start it in its own terminal and leave it running:

   ```powershell
   archrev serve
   ```

5. **Agent.** Open Claude Code (or Cursor) in the repository with
   *default* permissions, so the approval dialog is visible. Auto-accept
   modes resolve pauses silently. Start a **new** conversation: one
   conversation is one session.
6. **Dry run.** Run the whole demo once, then reset again. Agents vary;
   know what yours does.

## Screen layout

```
┌──────────────────────┬──────────────────────────────┐
│ Editor:              │ Browser: archrev serve        │
│ 30-test-matrix.yaml  │ (sidebar + session page)      │
├──────────────────────┤                               │
│ Agent chat           │                               │
└──────────────────────┴──────────────────────────────┘
```

Keep the viewer visible the whole time: the session appears under
**Recording now** within seconds of the first prompt and updates on its
own.

## Act 0 — the rules (2 min)

Show `.archrev/rules/30-test-matrix.yaml` in the editor.

> "Rules are YAML in the repository, reviewed like code. There are three
> actions: **deny** refuses, **block** pauses for my approval, **flag**
> allows but highlights. Rules can cover file edits, file reads, shell
> commands, MCP calls, and tools. `check` rules run a real analyzer on
> what the agent changed. `prompt` rules are policies the agent must
> attest before it codes."

Optional, in a terminal:

```powershell
archrev rules explain .test/protected/settings.ini
```

> "Any path can be explained: which rules cover it, and where they fire."

## Act 1 — normal work (3 min)

Paste **Act 1**. While the agent works, point at the viewer:

- the session appears under **Recording now**;
- open it: the timeline card shows *Plan registered · 2 files declared*,
  *Plan check passed*, *Changed 2 files*;
- expand *Changed 2 files* and click a file for its diff.

> "Git will tell me *what* changed. This tells me *why*: the prompt, the
> plan the agent committed to, and that its changes match the plan. The
> review box says *Nothing needs your review*, so I'm done."

## Act 2 — the rules at work (4 min)

Paste **Act 2**.

1. The agent tries to edit `settings.ini`. **The approval dialog appears.**
   > "A block rule. Nothing happens until I say so."

   Approve it.
2. The write to `.test/forbidden/` is **refused**.
3. Reading `.test/secrets/.env` is **refused**. The agent cannot tell you
   what is in it.
4. The flagged file is edited and **flagged**.

In the viewer, open the new card:

- *Rule paused an edit · **you approved*** · `test-path-block`;
- *Rule refused an edit / a file read · **refused***;
- *Rule flagged an edit · **flagged***.

Point at the **Rules at work** card: *Stopped the agent 3 times*.

> "Being stopped is not a problem to review: it's the rules working. The
> review box is still empty. I only get pulled in when something needs
> me."

Open the **Rules** tab: each rule, what it did, every occurrence.

## Act 3 — what slips past gets caught (4 min)

Paste **Act 3**.

> "Now the agent writes a protected file with a shell command instead of
> its edit tool, creates files it never planned, and writes broken code.
> A tool-level gate can't see inside every shell command. So what
> happens?"

When the turn ends, ArchRev sends the agent its findings and the agent
reports them in the chat. In the viewer, the session shows
**4 to review**. Walk the review box:

- **settings.ini changed outside the rule gate**. Point at *Why it
  matters* and *Likely cause*: the exact `echo … >> settings.ini`
  command. Click **View diff**: `debug=true`.
- **notes.txt / util.py not in the plan**.
- **Quality check failed**. **View check output**: the syntax error.

Then paste **Act 3b**. The agent's `archrev ack` is **refused**.

> "The agent can't clear its own findings. Even if it found another way
> to run the command, an acknowledgment made from inside an agent command
> is recorded as the agent's and doesn't count for this kind of finding."

## Act 4 — the reviewer decides (2 min)

In the viewer:

1. On the settings.ini finding, click **Acknowledge…**, type
   "debug flag is fine for the demo", confirm. It moves to **Resolved**,
   with your name on it: *acknowledged by the reviewer*.
2. Delete `.test/check/util.py` in the editor, then acknowledge its two
   findings with the note "reverted".
   > "Reverting doesn't erase history. The record keeps that the agent
   > wrote it; the reviewer closes the finding and says why."
3. Open the timeline: every step of every prompt, newest first, is there,
   including the refusals.

Close on traceability:

```powershell
git add -A; git commit -m "demo"     # the commit gets an ArchRev-Session trailer
archrev trace .test/protected/settings.ini
archrev verify
```

> "Every commit points to the session that produced it; every session
> shows the prompt, the plan, every rule decision, and every change. And
> the log is hash-chained, so it can't be quietly edited afterwards."

## If something goes differently

| Situation | What to do |
| --- | --- |
| No approval dialog in Act 2 | The agent runs in an auto-accept mode. Say so; the timeline still shows *paused* → *you approved*. Use `deny` rules when a stop must not depend on editor settings. |
| The agent refuses to do Act 3 ("that would bypass the gate") | Good agent behaviour; show that. Do the shell write yourself in a terminal while the agent runs a command, or skip to the recorded example (below). |
| A finding shows *uncertain* | A command's end was not recorded (e.g. outdated wiring). Explain it; the reviewer is told rather than misled. |
| The viewer shows a yellow "older than its page" banner | Restart `archrev serve`. |
| Anything else | Run `python .test/simulate.py`: 29 scripted cases through the real hooks, with PASS/GAP per case. |

A recorded fallback: `archrev export <session-id>` produces one HTML file
of a finished run. Make one after your dry run and keep it open in a tab.

## Questions you will get

- **"Is this a sandbox?"** No. It governs the agent's tool calls and
  catches what slipped past at turn end. For containment, run the agent
  in a container without secrets or network, and keep ArchRev on top.
  [security-model.md](security-model.md) lists the known gaps.
- **"Can the agent just edit the rules?"** Rule and config files are
  protected by an enabled self-protection rule (the edit pauses for
  approval), and changing them through the shell is a bypass finding.
- **"Where does the data go?"** Nowhere. Files in `.archrev/`, committed
  with the repository. No server, no telemetry. Command output is never
  stored.
- **"Does it slow the agent down?"** Each hook is a short process that
  appends a line; there is no daemon.
- **"Which agents?"** Cursor, Claude Code, and Codex have live hooks.
  Others get the protocol file and the CI check (`archrev check diff`).

## After the demo

```powershell
powershell -ExecutionPolicy Bypass -File .test/demo/reset.ps1
```

Demo sessions stay in `.archrev/sessions/`. They are the audit trail;
delete a session directory only if you are sure you don't need it.
