"""Repository discovery and ArchRev configuration.

The configuration lives in ``.archrev/config.yaml`` at the repository root.
Loading is deliberately tolerant: a missing or partially invalid file never
raises — hooks must not break the editor because of a config typo. Invalid
values fall back to safe defaults and are reported by ``archrev rules``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

#: Name of the ArchRev data directory at the repository root.
ARCHREV_DIRNAME = ".archrev"

#: Paths never gated: session bookkeeping and plan documents only.
#: Deliberately narrow - ArchRev's *governance* files (config, rules,
#: hooks.json, archrev.mdc) are NOT exempt, otherwise an agent could
#: silently disable enforcement by editing them. `archrev init` ships an
#: enabled self-protection rule that gates exactly those files.
DEFAULT_EXEMPT: tuple[str, ...] = (
    ".archrev/sessions/**",
    "**/*.plan.md",
)

#: Valid values for :attr:`Config.enforcement`.
ENFORCEMENT_MODES = ("on", "monitor", "off")


@dataclasses.dataclass(frozen=True)
class Config:
    """Effective ArchRev configuration for one repository.

    Attributes:
        enforcement: ``on`` enforces rule actions as written, ``monitor``
            downgrades ``block`` rules to ``flag`` (record but never stop),
            ``off`` disables the edit gate entirely (capture still runs).
        strict_plan_check: When true, the first gated edit of a session is
            denied until a plan check has been recorded for that session
            ("no validated plan, no code").
        protected_scan: When true, session finalization scans the full git
            diff against path rules, catching files changed outside the edit
            gate (e.g. written by shell commands).
        final_check: When true, session finalization sends the agent a
            follow-up message when material findings exist (gate bypasses,
            failed plan checks, out-of-plan drift), so every implementation
            ends with an explicit rule review instead of a silent manifest.
        exempt: Glob patterns for paths the gate never blocks.
    """

    enforcement: str = "on"
    strict_plan_check: bool = False
    protected_scan: bool = True
    final_check: bool = True
    exempt: tuple[str, ...] = DEFAULT_EXEMPT


def find_root(start: Path | None = None) -> Path | None:
    """Locate the repository root, preferring an ``.archrev`` directory.

    Walks upward from ``start`` (default: cwd). A directory containing
    ``.archrev`` wins over one containing only ``.git`` so that ArchRev can
    be rooted at a monorepo top level even when nested git checkouts exist.
    """
    cur = (start or Path.cwd()).resolve()
    candidates = (cur, *cur.parents)
    for candidate in candidates:
        if (candidate / ARCHREV_DIRNAME).is_dir():
            return candidate
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return None


def archrev_dir(root: Path) -> Path:
    """Return the ``.archrev`` directory for ``root`` (not created)."""
    return root / ARCHREV_DIRNAME


def load_config(root: Path) -> Config:
    """Load ``.archrev/config.yaml`` beneath ``root``, tolerating any error."""
    path = archrev_dir(root) / "config.yaml"
    defaults = Config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return defaults
    if not isinstance(raw, dict):
        return defaults

    enforcement = raw.get("enforcement", defaults.enforcement)
    if enforcement not in ENFORCEMENT_MODES:
        enforcement = defaults.enforcement

    strict = raw.get("strict_plan_check", defaults.strict_plan_check)
    if not isinstance(strict, bool):
        strict = defaults.strict_plan_check

    scan = raw.get("protected_scan", defaults.protected_scan)
    if not isinstance(scan, bool):
        scan = defaults.protected_scan

    final_check = raw.get("final_check", defaults.final_check)
    if not isinstance(final_check, bool):
        final_check = defaults.final_check

    exempt_raw = raw.get("exempt", None)
    if isinstance(exempt_raw, list) and all(isinstance(x, str) for x in exempt_raw):
        # User-provided exemptions extend (not replace) the built-in ones so
        # ArchRev's own data directory can never be accidentally gated.
        exempt = tuple(dict.fromkeys((*DEFAULT_EXEMPT, *exempt_raw)))
    else:
        exempt = defaults.exempt

    return Config(
        enforcement=enforcement,
        strict_plan_check=strict,
        protected_scan=scan,
        final_check=final_check,
        exempt=exempt,
    )
