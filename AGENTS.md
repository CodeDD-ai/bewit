# Agent instructions

# Bewit workflow (mandatory in this repository)

This repository records agent sessions with Bewit. Follow this protocol:

1. **Register your plan before implementing.** After planning and before the
   first file edit, run one of:
   - `bewit plan register <path-to-plan-file>`
   - `bewit plan register --text "<short plan: goal + files you will touch>"`
   Mention every file you expect to change by its full repository path;
   unplanned files are reported as drift in the session review. Fix any
   path the command reports as not found. For each later step in the same
   session, register again: the new files are added to the earlier plan
   (`--replace` starts over).

2. **Check the plan against the rules.** Run `bewit rules` to see the
   active policies, evaluate each `prompt` rule against your plan honestly,
   then record the verdicts:
   `bewit check plan --attest <rule-id>=pass --attest <other-id>=fail ...`
   Verdicts carry over for policies whose part of the plan did not change;
   attest the ones the check lists as not attested. If any verdict is
   `fail`, stop and resolve it with the user before implementing.

3. **Respect the edit gate.** If an edit is paused or denied by Bewit, do
   not work around it (e.g. by writing the file via shell commands) - the
   final diff scan reports bypasses. Ask the user or adjust the plan.

4. Never modify files under `.bewit/sessions/` - they are the audit record.
