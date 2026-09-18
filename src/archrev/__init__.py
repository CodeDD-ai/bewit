"""ArchRev: session provenance and architecture-rule enforcement for AI coding agents.

ArchRev records every agent session (prompt, plan, rule verdicts, file edits,
commits) as plain files inside the repository, enforces architect-defined
rules at edit time via agent hooks (Cursor, Claude Code, Codex), and
renders the full chain as a live timeline that can be reviewed at any
point in time.
"""

__version__ = "0.4.0"
