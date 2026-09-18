# Agent instructions

# ArchRev workflow (mandatory in this repository)

This repository records agent sessions with ArchRev. Follow this protocol:

1. **Register your plan before implementing.** After planning and before the
   first file edit, run exactly one of:
   - `archrev plan register <path-to-plan-file>`
   - `archrev plan register --text "<short plan: goal + files you will touch>"`
   Mention every file you expect to change; unplanned files are reported as
   drift in the session review.

2. **Check the plan against the rules.** Run `archrev rules` to see the
   active policies, evaluate each `prompt` rule against your plan honestly,
   then record the verdicts:
   `archrev check plan --attest <rule-id>=pass --attest <other-id>=fail ...`
   If any verdict is `fail`, stop and resolve it with the user before
   implementing.

3. **Respect the edit gate.** If an edit is paused or denied by ArchRev, do
   not work around it (e.g. by writing the file via shell commands) - the
   final diff scan reports bypasses. Ask the user or adjust the plan.

4. Never modify files under `.archrev/sessions/` - they are the audit record.
