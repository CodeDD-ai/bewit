"""Plan registration and rule checking.

Cursor stores plan files outside the workspace (``~/.cursor/plans``), so
passive capture cannot see them. The agent therefore *registers* its plan
explicitly (``bewit plan register``); the plan text is snapshotted into
the session directory and the file paths it declares are extracted for
later drift detection.

``bewit check plan`` evaluates the declared files against path rules
(early warning about what will gate) and records the agent's attestations
for every prompt rule. The check event doubles as the strict-mode token
that unlocks the edit gate.
"""

from __future__ import annotations

import re
from pathlib import Path

from bewit.config import Config
from bewit.gate import rule_hits_for_paths
from bewit.rules import RuleSet
from bewit.storage import Session

# Path candidates inside a plan: backticked tokens, markdown link targets,
# code-fence citation headers (```12:34:path/to/file), and bare path-like
# tokens containing a separator plus a file extension.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_MDLINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)\)")
_FENCE_CITATION_RE = re.compile(r"^\s*```\s*\d+:\d+:(.+?)\s*$", re.MULTILINE)
# The negative lookbehind rejects starts right after '/', '.', or word chars
# so fragments of URLs (https://example.com/x.py) never match; an optional
# leading './' is still allowed because the match may begin at the dot.
# A '(' is rejected only right after a word or ']' (a call such as
# open(x/y.py), or a markdown link target, which _MDLINK_RE covers): a
# plan saying "the viewer (src/report/x.py)" must still declare the file.
_BARE_PATH_RE = re.compile(
    r"(?<![\w`/.{])(?<![\w\]]\()((?:\./)?(?:[\w.-]+[/\\])+[\w.-]+\.\w{1,10})"
)
# Bare top-level filenames (README.md, pyproject.toml) carry no separator,
# so _BARE_PATH_RE never sees them. They are collected as candidates and
# kept ONLY when they exist in the repository (the existence check below),
# preserving precision. Found via dogfooding: a plan declaring README.md
# in prose was reported as out-of-plan drift by the final review.
_BARE_NAME_RE = re.compile(r"(?<![\w`/.{])(?<![\w\]]\()([\w-][\w.-]*\.\w{1,10})")
# Brace lists (src/{cli,gate}.py, tests/test_{a,b}.py) name several files.
_BRACE_RE = re.compile(r"(?<![\w`/.])((?:[\w.-]+/)*[\w.-]*\{[\w.,/-]+\}[\w./-]*)")
_BRACE_PART_RE = re.compile(r"\{([^{}]*)\}")
_FILE_SEGMENT_RE = re.compile(r"^[\w-][\w.-]*\.\w{1,10}$")
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")


def _clean_candidate(raw: str) -> str | None:
    """Normalize one candidate token to a repo-relative-looking path."""
    token = raw.strip().strip("'\"").rstrip(".,;:!?")
    if not token or "://" in token or " " in token:
        return None
    token = _LINE_SUFFIX_RE.sub("", token)  # drop trailing :line(:col)
    token = token.replace("\\", "/")
    # Strip only './' prefixes; str.lstrip("./") would eat the leading dot
    # of dotfile paths like '.bewit/rules/x.yaml' (real dogfooding bug).
    while token.startswith("./"):
        token = token[2:]
    # Windows absolute paths inside plans are kept as-is; relativization
    # happens against the repo root below.
    return token or None


def _expand_braces(token: str) -> list[str]:
    """``src/{cli,gate}.py`` -> ``src/cli.py``, ``src/gate.py`` (nested too)."""
    match = _BRACE_PART_RE.search(token)
    if not match:
        return [token]
    head, tail = token[: match.start()], token[match.end() :]
    out: list[str] = []
    for part in match.group(1).split(","):
        out.extend(_expand_braces(head + part.strip() + tail))
    return out


def _split_joined(token: str, root: Path) -> list[str]:
    """Split a slash-joined file list into its files.

    ``tests/test_a.py/test_b.py`` is not a path (a file cannot have
    children); it is the plan's shorthand for two files in ``tests/``.
    Found in real use: such a list was declared as one bogus path, so all
    of its files later showed up as undeclared drift.
    """
    if (root / token).exists():
        return [token]
    segments = token.split("/")
    first = next(
        (i for i, s in enumerate(segments[:-1]) if _FILE_SEGMENT_RE.match(s)),
        None,
    )
    if first is None or (root.joinpath(*segments[: first + 1])).is_dir():
        return [token]
    files = segments[first:]
    if not all(_FILE_SEGMENT_RE.match(s) for s in files):
        return [token]
    directory = "/".join(segments[:first])
    return [f"{directory}/{f}" if directory else f for f in files]


def extract_declared_files(text: str, root: Path) -> list[str]:
    """Extract the repository files a plan declares it will touch.

    Heuristic by design: precision is preferred over recall, since false
    positives would produce noisy "unrealized plan" drift. A candidate is
    kept when it exists in the repository, contains a path separator plus
    a file extension (a strong signal it is a concrete file path), or is a
    backticked name with an extension: a plan that writes `CHANGELOG.md`
    names a new top-level file on purpose (found while dogfooding: such
    files were dropped and later reported as unplanned).
    """
    candidates: list[tuple[str, bool]] = []
    for regex in (
        _BACKTICK_RE,
        _MDLINK_RE,
        _FENCE_CITATION_RE,
        _BARE_PATH_RE,
        _BARE_NAME_RE,
        _BRACE_RE,
    ):
        explicit = regex in (_BACKTICK_RE, _BRACE_RE)
        candidates.extend((raw, explicit) for raw in regex.findall(text))

    seen: dict[str, None] = {}
    for raw, explicit in candidates:
        cleaned = _clean_candidate(raw)
        if not cleaned:
            continue
        for expanded in _expand_braces(cleaned):
            for token in _split_joined(expanded, root):
                if "{" in token or "}" in token:
                    continue
                exists = (root / token).exists()
                has_extension = re.search(r"\.\w{1,10}$", token) is not None
                looks_like_file = "/" in token and has_extension
                if exists or looks_like_file or (explicit and has_extension):
                    seen.setdefault(token, None)
    return list(seen)


def _repo_files(root: Path) -> list[str]:
    """Tracked plus untracked (not ignored) files, for path suggestions."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def unresolved_paths(declared: list[str], root: Path) -> list[tuple[str, str | None]]:
    """Declared paths that do not exist, each with its likeliest intended file.

    A missing path is either a file the plan will create or a mistake
    (a truncated or mistyped path). The suggestion is the one repository
    file with the same name, else the closest path by similarity; None
    when nothing is close, which usually means a genuinely new file.
    """
    import difflib

    missing = [p for p in declared if not (root / p).exists()]
    if not missing:
        return []
    files = _repo_files(root)
    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(f.rsplit("/", 1)[-1].lower(), []).append(f)
    out: list[tuple[str, str | None]] = []
    for path in missing:
        # Same file name, and one path is a suffix of the other: the plan
        # dropped (or added) leading directories, e.g. tools/visuals.py
        # for src/tools/visuals.py.
        low = path.lower()
        same_name = [
            f for f in by_name.get(low.rsplit("/", 1)[-1], [])
            if f.lower().endswith("/" + low) or low.endswith("/" + f.lower())
        ]
        if len(same_name) == 1:
            out.append((path, same_name[0]))
            continue
        close = difflib.get_close_matches(path, files, n=1, cutoff=0.8)
        out.append((path, close[0] if close else None))
    return out


#: Plan text stored per revision inside the event log (full text lives in
#: plan.md for the latest revision; earlier revisions survive here).
_PLAN_EVENT_TEXT_CAP = 20_000


def register_plan(
    session: Session,
    text: str,
    root: Path,
    origin: str | None = None,
    amend: bool = True,
) -> list[str]:
    """Snapshot ``text`` as the session plan; returns declared files.

    Each registration event carries its own text, so revision history
    stays reviewable. Drift uses the *latest* declared set. By default a
    registration unions its files with the previous declaration: sessions
    grow in steps, and replacing silently un-declared earlier steps' work,
    which the review then reported as drift on every later step (the
    single largest source of noise agents reported). ``amend=False``
    (``--replace``) starts the declaration over.
    """
    declared = extract_declared_files(text, root)
    if amend:
        previous = session.last_event("plan_registered")
        prior = [
            p
            for p in (previous or {}).get("declared_files") or []
            if isinstance(p, str)
        ]
        merged: dict[str, str] = {p.lower(): p for p in prior}
        for path in declared:
            merged.setdefault(path.lower(), path)
        declared = list(merged.values())
    session.write_plan(text)
    session.append_event(
        "plan_registered",
        origin=origin or "inline",
        declared_files=declared,
        chars=len(text),
        text=text[:_PLAN_EVENT_TEXT_CAP],
        amend=amend,
    )
    return declared


#: Attestation verdicts. ``n/a`` states that a policy does not apply to
#: this plan; it counts as satisfied but stays visible in the review.
VERDICTS = ("pass", "fail", "n/a")
_SATISFIED = ("pass", "n/a")


def check_plan(
    config: Config,
    ruleset: RuleSet,
    session: Session,
    attestations: dict[str, str],
    note: str | None = None,
) -> dict:
    """Evaluate the registered plan against all rules; record the verdicts.

    Returns the check report:

    - ``path_findings``: declared files that match path rules — a preview
      of what the edit gate will block or flag during implementation.
    - ``prompt_rules``: one entry per enabled prompt rule with the agent's
      attestation (``pass`` / ``fail`` / ``n/a``) or ``unattested``. A rule
      whose ``applies_to`` scope contains none of the declared files is
      ``n/a`` automatically (``scope: "out of scope"``), unless attested.
    - ``ok``: True only when a plan is registered and every prompt rule is
      ``pass`` or ``n/a``. Without a plan there is no intent to check, so a
      check can never pass (and never unlock strict mode) before one.
    - ``plan_revision``: how many registrations preceded this check (1 =
      the first plan). A later registration makes the check stale; the
      review compares this against the plan's revision count, because
      second-resolution timestamps cannot order events within one second.
    """
    registrations = [e for e in session.events() if e.get("type") == "plan_registered"]
    plan_registered = bool(registrations)
    declared: list[str] = []
    if registrations:
        raw = registrations[-1].get("declared_files")
        declared = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []

    path_findings = [
        {
            "path": hit.path,
            "rule_id": hit.rule_id,
            "action": hit.action,
            "message": hit.message,
        }
        for hit in rule_hits_for_paths(ruleset, declared)
    ]

    # Verdicts from the previous check carry over to rules not attested now,
    # as long as the part of the plan each rule covers is unchanged: a
    # scoped rule compares the declared files inside its scope, an unscoped
    # rule the whole declaration. A merged plan that adds files outside a
    # rule's scope therefore keeps that rule's verdict; anything new in
    # scope must be attested again.
    previous = session.last_event("plan_check") or {}
    prev_declared = [
        p for p in previous.get("declared_files") or [] if isinstance(p, str)
    ]
    prev_verdicts = {
        str(r.get("rule_id")): r.get("verdict")
        for r in previous.get("prompt_rules") or []
        if isinstance(r, dict)
        and r.get("verdict") in VERDICTS
        and r.get("scope") != "out of scope"
    }

    def _covered(rule, paths: list[str]) -> set[str]:
        return {
            p.lower() for p in paths if not rule.applies_to or rule.scope_matches(p)
        }

    prompt_results = []
    for rule in ruleset.prompt_rules:
        entry = {"rule_id": rule.id, "policy": rule.policy}
        if rule.id in attestations:
            entry["verdict"] = attestations[rule.id]
        elif (
            rule.id in prev_verdicts
            and declared
            and _covered(rule, declared) == _covered(rule, prev_declared)
        ):
            entry["verdict"] = prev_verdicts[rule.id]
            entry["carried"] = True
        elif (
            rule.applies_to
            and declared  # a plan naming no files cannot show it is out of scope
            and not any(rule.scope_matches(p) for p in declared)
        ):
            entry["verdict"] = "n/a"
            entry["scope"] = "out of scope"
        else:
            entry["verdict"] = "unattested"
        prompt_results.append(entry)
    unknown_ids = sorted(set(attestations) - {r.id for r in ruleset.prompt_rules})

    ok = plan_registered and all(r["verdict"] in _SATISFIED for r in prompt_results)
    report = {
        "plan_registered": plan_registered,
        "plan_revision": len(registrations),
        "declared_files": declared,
        "path_findings": path_findings,
        "prompt_rules": prompt_results,
        "unknown_attestations": unknown_ids,
        "note": note or "",
        "ok": ok,
        "strict_mode": config.strict_plan_check,
    }
    # A check that states nothing (no --attest) and cannot pass on carried
    # verdicts is not recorded: it would only overwrite the last real
    # check with a failure and close the gate (reported from real use: a
    # bare `bewit check plan` replaced a passing check).
    if not attestations and not ok:
        return {**report, "recorded": False}
    session.append_event("plan_check", **report)
    return {**report, "recorded": True}
