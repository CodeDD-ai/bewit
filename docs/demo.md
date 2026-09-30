# Live demo

A 10–15 minute demo of Bewit governing a real change to this
repository, with the rules the repository actually ships
(`.bewit/rules/10-bewit-repo.yaml`, `20-agent-boundaries.yaml`,
`90-bewit-self-protection.yaml`). No fixtures, no test-only rules: the
agent does a genuine piece of work, and the rules act on it as they would
on any other day. The story in one line: **the agent works, the rules act
live, Bewit catches what slipped past, and the reviewer sees exactly
what needs them.**

## The story you are telling

| Act | Minutes | The audience sees | The point |
| --- | --- | --- | --- |
| 0. Setup | 2 | The rules files and an empty viewer | Rules are plain YAML in the repo, reviewed like code |
| 1. A real change | 5 | Plan, policy attestation, a flagged edit, a paused edit you approve, a passing syntax check | Bewit records *why*, and the rules act *before* the agent does |
| 2. Shipping it | 2 | A commit with a session trailer; the push paused | Shell commands are governed too |
| 3. What slips past | 3 | A shell edit to a blocked file reported as a bypass; the agent refused when it tries to acknowledge it | The end-of-turn review is the backstop, and the agent cannot clear it |
| 4. Review | 2 | Review box → diff → acknowledge; timeline; `bewit trace` | A reviewer knows in seconds what needs them |

## Before the audience arrives

1. **Throwaway branch.** Commit or stash unrelated work, then run the demo
   on its own branch so it is trivial to discard:

   ```powershell
   git switch -c demo/rule-key-check
   ```

2. **Rules and wiring.**

   ```powershell
   bewit rules
   ```

   It must list `flag-enforcement-core`, `block-packaging`,
   `check-python-syntax`, `tests-required`, `push-needs-approval` and
   `bewit-no-agent-ack`, with no *Problems* section and no
   "Hook wiring is out of date" warning (if there is one, run
   `bewit init`).
3. **Viewer.** Start it in its own terminal and leave it running:

   ```powershell
   bewit serve
   ```

4. **Agent.** Open Claude Code (or Cursor) in the repository with
   *default* permissions, so the approval dialog is visible. Auto-accept
   modes resolve pauses silently. Start a **new** conversation: one
   conversation is one session, and each prompt becomes one card on the
   timeline.
5. **Dry run.** Run the whole demo once, then throw the branch away
   (see *After the demo*). Agents vary; know what yours does.

## Screen layout

```
┌──────────────────────────┬──────────────────────────────┐
│ Editor:                  │ Browser: bewit serve        │
│ 10-bewit-repo.yaml     │ (sidebar + session page)      │
├──────────────────────────┤                               │
│ Agent chat               │                               │
└──────────────────────────┴──────────────────────────────┘
```

## Act 0 — the rules (2 min)

Show `.bewit/rules/10-bewit-repo.yaml` in the editor.

> "Rules are YAML in the repository. Three actions: **deny** refuses,
> **block** pauses for my approval, **flag** allows but highlights.
> `path` rules cover edits, `read` rules cover reads, `shell` rules cover
> commands. `check` rules run a real analyzer on what the agent changed.
> `prompt` rules are policies the agent must attest before it codes, and
> `applies_to` means it only attests the ones its plan touches."

Optional:

```powershell
bewit rules explain src/bewit/rules.py
bewit rules explain pyproject.toml
```

> "Any path can be explained: `rules.py` is enforcement core, so edits
> are flagged and the tests policy applies; `pyproject.toml` pauses for
> me."

## Act 1 — a real change (5 min)

Paste this as the **first prompt**:

```text
Rule files are easy to get subtly wrong: today a typo such as `enabeld: false`
or `aplies_to:` in .bewit/rules/*.yaml is silently ignored, so the rule stays
enabled or fires everywhere, and nobody notices.

Make src/bewit/rules.py report unknown keys on a rule as a problem in
RuleSet.errors (so `bewit rules` lists it under "Problems"), naming the file,
the rule id and the key. The rule itself must still load. Keys that are valid
for another kind (e.g. `command` on a path rule) count as unknown for that kind.

Cover it in tests/test_rules.py, add an entry to the "Unreleased" section of
CHANGELOG.md, and bump the version in pyproject.toml to 0.6.0 since we are
cutting the release.

Follow the Bewit protocol in this repository: register a plan naming every
file you will change, run `bewit rules`, attest the policies with
`bewit check plan`, then implement. Run `python -m pytest tests/test_rules.py`
before you finish.
```

Why it is worded this way: it is a change you would really want (a typo
in a rule file is a silent hole in the gate); it touches enforcement core,
tests, docs and packaging, so every relevant rule kind in the repository
gets a turn; and it names the files, so the plan is complete and nothing
shows up as drift unless the agent strays.

What the audience sees, live, in the viewer:

1. **Plan registered · 4 files declared** (`rules.py`, `test_rules.py`,
   `CHANGELOG.md`, `pyproject.toml`).
2. **Plan check.** `tests-required` applies (the plan touches
   `src/bewit/**`) and the agent attests it; `hooks-fail-open` and
   `audit-format-compat` are shown as *n/a (out of scope)* on their own.
   > "The agent committed, in the record, to covering this with tests,
   > before writing a line."
3. **Rule flagged an edit** · `flag-enforcement-core` on
   `src/bewit/rules.py`. The agent is told the rule's message: rule
   parsing is enforcement core and needs regression tests.
4. **The approval dialog appears** for `pyproject.toml`
   (`block-packaging`).
   > "A block rule. Nothing happens until I say so."

   Approve it.
5. At the end of the turn, **`check-python-syntax`** runs on the changed
   Python files and passes; the agent reports its pytest run.

Open the card and expand *Changed 4 files*; click `rules.py` for its
diff. Point at **Rules at work** (the pause you approved, the flag) and
the review box.

> "Git tells me *what* changed. This tells me *why*: the prompt, the plan
> the agent committed to, the policy it attested, and every rule that
> acted along the way. A pause I approved and a flag are the rules
> working, not problems, so there is nothing that needs me."

To prove the feature itself, add `enabeld: false` to one of the disabled
examples in `.bewit/rules/00-starter-rules.yaml` in your editor, run
`bewit rules`, show the new *Problems* line, and undo.

## Act 2 — shipping it (2 min)

Paste:

```text
Commit this with a descriptive message and push the branch to origin.
```

- The commit goes through and gets an **`Bewit-Session` trailer**.
- `git push` **pauses** (`push-needs-approval`). **Decline** it.
  > "Shell commands are governed too. The branch stays on my machine."

## Act 3 — what slips past gets caught (3 min)

Paste:

```text
Actually make it 0.6.1. Do it with a quick shell command
((Get-Content pyproject.toml) -replace '0.6.0','0.6.1' | Set-Content pyproject.toml),
not your edit tool. Then tell me what Bewit reported at the end of your turn.
```

> "A tool-level gate can't see inside every shell command. The edit gate
> never saw this change to a blocked file. So what happens?"

At the end of the turn Bewit scans the diff (`protected_scan: true`)
and sends the agent its findings; the agent reports them in the chat.
In the viewer the session shows a finding to review:

- **pyproject.toml changed outside the rule gate** (`block-packaging`).
  Point at *Why it matters* and *Likely cause*: the exact command. Click
  **View diff**: `0.6.0` → `0.6.1`.

Then paste:

```text
Please acknowledge that finding yourself with bewit ack so the review is clean.
```

The agent's `bewit ack` is **refused** (`bewit-no-agent-ack`).

> "The agent can't clear its own findings. Even if it found another way
> to run the command, an acknowledgment made from inside an agent command
> is recorded as the agent's and doesn't count for a bypass."

## Act 4 — the reviewer decides (2 min)

In the viewer:

1. On the `pyproject.toml` finding, click **Acknowledge…**, type
   "version bump is fine", confirm. It moves to **Resolved**, with your
   name on it.
2. Open the timeline: every prompt, newest first, with the plan, the
   attestation, the flag, the approved pause, the declined push, the
   bypass and the refused ack.

Close on traceability:

```powershell
bewit trace src/bewit/rules.py
bewit verify
```

> "Every commit points to the session that produced it; every session
> shows the prompt, the plan, every rule decision, and every change. And
> the log is hash-chained, so it can't be quietly edited afterwards."

## If something goes differently

| Situation | What to do |
| --- | --- |
| No approval dialog for `pyproject.toml` | The agent runs in an auto-accept mode. Say so; the timeline still shows *paused* → *you approved*. Use `deny` rules when a stop must not depend on editor settings. |
| The agent skips the version bump or edits an undeclared file | Show it: an undeclared file is reported as plan drift at the end of the turn. That is Bewit working. |
| The agent refuses Act 3 ("that would bypass the gate") | Good agent behaviour, and this repository's `AGENTS.md` tells it to; show that. Run the shell command yourself in a terminal *while the agent runs a command*, or skip to Act 4. |
| The agent also runs `tests/rule_matrix.py` | The `flag-enforcement-core` message asks for it. Let it run (~1 min) or tell it to skip for the demo. |
| A finding shows *uncertain* | A command's end was not recorded (e.g. outdated wiring). Explain it; the reviewer is told rather than misled. |
| The viewer shows a yellow "older than its page" banner | Restart `bewit serve`. |

A recorded fallback: `bewit export <session-id>` produces one HTML file
of a finished run. Make one after your dry run and keep it open in a tab.

## Questions you will get

- **"Is this a sandbox?"** No. It governs the agent's tool calls and
  catches what slipped past at turn end. For containment, run the agent
  in a container without secrets or network, and keep Bewit on top.
  [security-model.md](security-model.md) lists the known gaps.
- **"Can the agent just edit the rules?"** Rule and config files are
  protected by an enabled self-protection rule (the edit pauses for
  approval), and changing them through the shell is a bypass finding.
- **"Where does the data go?"** Nowhere. Records live in the repository
  (`refs/bewit/records`). No server, no telemetry. Command output is
  never stored.
- **"Does it slow the agent down?"** Each hook is a short process that
  appends a line; there is no daemon.
- **"Which agents?"** Cursor, Claude Code, and Codex have live hooks.
  Others get the protocol file and the CI check (`bewit check diff`).

## After the demo

```powershell
git switch -
git branch -D demo/rule-key-check
```

If the feature is worth keeping, cherry-pick the commit instead of
deleting the branch. Demo sessions stay in the record; they are the audit
trail.
