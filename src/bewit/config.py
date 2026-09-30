"""Repository discovery and Bewit configuration.

The configuration lives in ``.bewit/config.yaml`` at the repository root.
Loading is deliberately tolerant: a missing or partially invalid file never
raises — hooks must not break the editor because of a config typo. Invalid
values fall back to safe defaults and are reported by ``bewit rules``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

#: Name of the Bewit data directory at the repository root.
BEWIT_DIRNAME = ".bewit"

#: Paths never gated: session bookkeeping and plan documents only.
#: Deliberately narrow - Bewit's *governance* files (config, rules,
#: hooks.json, bewit.mdc) are NOT exempt, otherwise an agent could
#: silently disable enforcement by editing them. `bewit init` ships an
#: enabled self-protection rule that gates exactly those files.
DEFAULT_EXEMPT: tuple[str, ...] = (
    ".bewit/sessions/**",
    "**/*.plan.md",
)

#: Valid values for :attr:`Config.enforcement`.
ENFORCEMENT_MODES = ("on", "monitor", "off")


@dataclasses.dataclass(frozen=True)
class Config:
    """Effective Bewit configuration for one repository.

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
        prompt_capture: How much prompt text enters the record:
            ``full`` (default), ``excerpt`` (first 200 chars + length),
            ``none`` (length + content hash only), or ``sealed``
            (ciphertext to the public keys in ``.bewit/recipients.yaml``).
        record_scope: ``audit`` (the init default for new repos) commits
            one file per session, ``manifest.json``, with the plan text,
            the review, and the event-log chain head. ``full`` also
            commits ``events.jsonl`` and ``meta.json``. ``plan.md`` and
            ``report.md`` are never committed (see
            ``scaffold.COMMITTED_SESSION_FILES``).
        record_store: where the committed files live. ``tree``: in the
            working tree, committed with the code. ``ref`` (the init
            default for new repos): on ``refs/bewit/records``, published
            by Bewit and shared with ``bewit sync``, never in a code
            commit (see :mod:`bewit.refstore`).
        exempt: Glob patterns for paths the gate never blocks.
    """

    enforcement: str = "on"
    strict_plan_check: bool = False
    protected_scan: bool = True
    final_check: bool = True
    prompt_capture: str = "full"
    record_scope: str = "full"
    record_store: str = "tree"
    exempt: tuple[str, ...] = DEFAULT_EXEMPT


def find_root(start: Path | None = None) -> Path | None:
    """Locate the repository root, preferring an ``.bewit`` directory.

    Walks upward from ``start`` (default: cwd). A directory containing
    ``.bewit`` wins over one containing only ``.git`` so that Bewit can
    be rooted at a monorepo top level even when nested git checkouts exist.
    """
    cur = (start or Path.cwd()).resolve()
    candidates = (cur, *cur.parents)
    for candidate in candidates:
        if (candidate / BEWIT_DIRNAME).is_dir():
            return candidate
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return None


def bewit_dir(root: Path) -> Path:
    """Return the ``.bewit`` directory for ``root`` (not created)."""
    return root / BEWIT_DIRNAME


def read_text_any(path: Path) -> str:
    """Read a config/rules file written by any common editor or shell.

    UTF-8 (with or without BOM) and UTF-16 with BOM (Windows PowerShell 5.1
    ``>`` / ``Out-File``) are decoded. Anything else raises ``ValueError``,
    which callers record as a problem: an undecodable file must never
    crash a hook, because a crashing hook fails open for *every* rule.
    """
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"not UTF-8 or UTF-16 text ({exc.reason})") from exc


def load_config(root: Path) -> Config:
    """Load ``.bewit/config.yaml`` beneath ``root``, tolerating any error."""
    path = bewit_dir(root) / "config.yaml"
    defaults = Config()
    try:
        raw = yaml.safe_load(read_text_any(path))
    except (OSError, ValueError, yaml.YAMLError):
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

    prompt_capture = str(raw.get("prompt_capture", defaults.prompt_capture)).lower()
    if prompt_capture not in ("full", "excerpt", "none", "sealed"):
        prompt_capture = defaults.prompt_capture

    record_scope = str(raw.get("record_scope", defaults.record_scope)).lower()
    if record_scope not in ("full", "audit"):
        record_scope = defaults.record_scope

    record_store = str(raw.get("record_store", defaults.record_store)).lower()
    if record_store not in ("tree", "ref"):
        record_store = defaults.record_store

    exempt_raw = raw.get("exempt", None)
    if isinstance(exempt_raw, list) and all(isinstance(x, str) for x in exempt_raw):
        # User-provided exemptions extend (not replace) the built-in ones so
        # Bewit's own data directory can never be accidentally gated.
        exempt = tuple(dict.fromkeys((*DEFAULT_EXEMPT, *exempt_raw)))
    else:
        exempt = defaults.exempt

    return Config(
        enforcement=enforcement,
        strict_plan_check=strict,
        protected_scan=scan,
        final_check=final_check,
        prompt_capture=prompt_capture,
        record_scope=record_scope,
        record_store=record_store,
        exempt=exempt,
    )
