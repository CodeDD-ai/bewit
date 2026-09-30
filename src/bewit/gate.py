"""The edit gate: decides whether an agent file edit proceeds, pauses, or stops.

Invoked from the ``preToolUse`` hook before Cursor executes a file-editing
tool. Decision policy, in order:

1. Paths exempt by configuration (session bookkeeping and plan files, plus
   any ``exempt`` globs) are always allowed — the gate must never block
   its own bookkeeping.
2. Strict mode: if ``strict_plan_check`` is on and this session has not
   recorded a plan check yet, the edit is denied with instructions to the
   agent to register and check a plan first ("no validated plan, no code").
3. Path rules: a matching ``block`` rule returns ``ask`` so the *user*
   explicitly approves in Cursor's dialog; a matching ``flag`` rule allows
   the edit but records it prominently. ``monitor`` enforcement downgrades
   blocks to flags; ``off`` disables rule evaluation entirely.

Every non-trivial decision is appended to the session event log, so gate
activity is always visible in the timeline even when nothing was stopped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from bewit import globmatch
from bewit.config import Config
from bewit.rules import RuleSet
from bewit.storage import Session


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    action: str
    path: str
    message: str


@dataclass(frozen=True)
class GateDecision:
    """Outcome of gating one tool call."""

    permission: str  # "allow" | "ask" | "deny"
    user_message: str = ""
    agent_message: str = ""
    hits: tuple[RuleHit, ...] = field(default_factory=tuple)

    def to_hook_output(self) -> dict:
        """Serialize to the JSON shape Cursor's preToolUse hook expects."""
        out: dict = {"permission": self.permission}
        if self.user_message:
            out["user_message"] = self.user_message
        if self.agent_message:
            out["agent_message"] = self.agent_message
        return out


_STRICT_AGENT_MESSAGE = (
    "Bewit strict mode: no validated plan is recorded for this session, so "
    "file edits are not allowed yet. Before editing, run:\n"
    "  1. bewit plan register <plan-file>   (or: bewit plan register --text \"...\")\n"
    "  2. bewit check plan                  (attest prompt rules with --attest <id>=pass|fail)\n"
    "Then retry the edit. Do not bypass this by writing files via shell commands."
)

_STRICT_FAILED_CHECK_MESSAGE = (
    "Bewit strict mode: the latest plan check for this session did NOT "
    "pass (failed or unattested prompt rules), so file edits remain "
    "blocked. Resolve every failed policy with the user, then re-run "
    "`bewit check plan` with updated attestations. A failing check can "
    "not be 'attested through' - the user must agree to the resolution."
)

_STRICT_STALE_CHECK_MESSAGE = (
    "Bewit strict mode: the plan was registered again after the latest "
    "plan check, so that check no longer covers the current plan and file "
    "edits are blocked. Re-run `bewit check plan` (attest prompt rules "
    "with --attest <id>=pass|fail|n/a) against the current plan, then retry."
)


def is_outside_repo(path: str) -> bool:
    """True for paths that are not repo-relative (absolute or parent-escaping).

    Edit events store repo-relative paths for files under the root;
    anything absolute (``C:/...``, ``/home/...``) or escaping upward
    (``../``) lies outside the repository.
    """
    return bool(
        re.match(r"^([A-Za-z]:[/\\]|[/\\])", path) or path.startswith("..")
    )


def relativize(path: str, root: Path) -> str:
    """Best-effort repo-relative normalized path for matching and storage."""
    # A file URI (file:///e%3A/repo/.env) must not slip past path rules.
    if path[:7].lower() == "file://":
        path = unquote(path[7:])
    norm = globmatch.normalize(path)
    root_norm = globmatch.normalize(str(root))
    if root_norm and norm.lower().startswith(root_norm.lower() + "/"):
        return norm[len(root_norm) + 1 :]
    if norm.lower() == root_norm.lower():
        return ""
    return norm


def _apply_mode(action: str, config: Config) -> str:
    """Downgrade stopping actions to ``flag`` in monitor mode."""
    if config.enforcement == "monitor" and action in ("block", "deny"):
        return "flag"
    return action


def _article(noun: str) -> str:
    """'an edit', 'an MCP tool call', 'a shell command'."""
    return ("an " if noun[:1].lower() in "aeiou" or noun.startswith("MCP") else "a ") + noun


def decide_from_hits(
    hits: list[RuleHit], config: Config, what: str
) -> GateDecision:
    """Turn rule hits into a decision: deny > block(ask) > flag(allow).

    ``what`` names the gated action ("edit", "shell command", ...) in the
    user- and agent-facing messages.
    """
    if not hits:
        return GateDecision(permission="allow")
    a_what = _article(what)

    def _lines(subset: list[RuleHit]) -> str:
        return "\n".join(
            f"[{h.rule_id}] {h.path}: {h.message or 'matched rule'}"
            for h in subset
        )

    denying = [h for h in hits if h.action == "deny"]
    if denying:
        return GateDecision(
            permission="deny",
            user_message=(
                f"Bewit denied {a_what} (hard policy).\n" + _lines(denying)
            ),
            agent_message=(
                f"Bewit denied this {what} outright (rule(s): "
                + ", ".join(sorted({h.rule_id for h in denying}))
                + "). This is a hard policy - do not retry variations or "
                "work around it; adjust the approach or ask the user."
            ),
            hits=tuple(hits),
        )

    blocking = [h for h in hits if h.action == "block"]
    if blocking:
        return GateDecision(
            permission="ask",
            user_message=(
                f"Bewit: this {what} needs your approval.\n"
                + _lines(blocking)
            ),
            agent_message=(
                f"Bewit paused this {what} for explicit user approval "
                "(rule(s): "
                + ", ".join(sorted({h.rule_id for h in blocking}))
                + "). If the user declines, adjust the plan instead of "
                "working around the gate."
            ),
            hits=tuple(hits),
        )

    flagged = ", ".join(sorted({h.rule_id for h in hits}))
    return GateDecision(
        permission="allow",
        agent_message=(
            f"Bewit flagged this {what} (rule(s): {flagged}). It is "
            "allowed but will be highlighted in the session review."
        ),
        hits=tuple(hits),
    )


def evaluate_edit(
    config: Config,
    ruleset: RuleSet,
    session: Session,
    paths: list[str],
    root: Path,
) -> GateDecision:
    """Gate one edit affecting ``paths`` (raw, possibly absolute)."""
    rel_paths = [p for p in (relativize(p, root) for p in paths) if p]
    actionable = [
        p for p in rel_paths if not globmatch.matches_any(config.exempt, p)
    ]
    if not actionable:
        return GateDecision(permission="allow")

    # 'off' disables the gate entirely, including strict mode - it must be
    # the master switch the documentation promises.
    if config.enforcement == "off":
        return GateDecision(permission="allow")

    # Strict mode precedes rule matching: without a *passing* plan check
    # the session has no validated intent to measure any edit against. A
    # recorded-but-failed check must not unlock the gate, otherwise the
    # agent could attest 'fail' and proceed anyway. A check recorded before
    # the latest registration validated a plan that has since been replaced,
    # so it must not unlock the gate either. Paths outside the repository
    # (the runtime's own plan files, scratch space) can never be part of a
    # repository plan, so strict mode only applies to repo paths; path
    # rules below still see every path.
    if config.strict_plan_check and any(
        not is_outside_repo(p) for p in actionable
    ):
        last_check = None
        stale = False
        for event in reversed(session.events()):
            etype = event.get("type")
            if etype == "plan_check":
                last_check = event
                break
            if etype == "plan_registered":
                stale = True
        if last_check is None or stale or not last_check.get("ok"):
            if last_check is None:
                needed, agent_message = "registered, checked", _STRICT_AGENT_MESSAGE
            elif stale:
                needed, agent_message = "re-checked", _STRICT_STALE_CHECK_MESSAGE
            else:
                needed, agent_message = "passing", _STRICT_FAILED_CHECK_MESSAGE
            return GateDecision(
                permission="deny",
                user_message=(
                    f"Bewit blocked an edit: strict mode requires a {needed} "
                    "plan before any file changes in this session."
                ),
                agent_message=agent_message,
            )

    hits: list[RuleHit] = []
    for rel in actionable:
        for rule in ruleset.match_path(rel):
            hits.append(
                RuleHit(
                    rule_id=rule.id,
                    action=_apply_mode(rule.action, config),
                    path=rel,
                    message=rule.message,
                )
            )
    return decide_from_hits(hits, config, "edit")


#: Programs whose file arguments are read for their content. Commands that
#: only mention a path (``ls``, ``git add``, ``echo``, a commit message, a
#: script's source text) are deliberately absent: flagging every mention
#: made ordinary work impossible (observed while dogfooding).
_READER_PROGRAMS = frozenset({
    # display / transform
    "cat", "tac", "less", "more", "head", "tail", "bat", "nl", "strings",
    "xxd", "od", "hexdump", "base64", "sed", "awk", "cut", "sort", "uniq",
    "wc", "diff", "cmp", "source", ".", "grep", "egrep", "fgrep", "rg", "ag",
    # hashes: a digest still proves the agent read the content
    "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum", "md5sum",
    "b2sum", "cksum", "shasum", "md5", "openssl", "certutil",
    # copies and archives move the content somewhere else
    "cp", "scp", "rsync", "tar", "zip", "gzip", "7z", "copy", "xcopy", "robocopy",
    # Windows / PowerShell readers (cmdlets and common aliases)
    "type", "findstr", "get-content", "gc", "select-string", "sls",
    "get-filehash", "copy-item", "cpi", "import-csv", "import-clixml",
    "format-hex", "fhx",
})
#: Readers whose first positional argument is a pattern, not a file.
_PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "findstr", "select-string", "sls", "sed", "awk"})
#: Interpreters that run inline code given with one of these flags. Quoted
#: paths inside that code are checked (``python -c "open('.env')"``).
_INLINE_CODE_FLAGS = {
    "python": ("-c",), "python3": ("-c",), "py": ("-c",), "pypy3": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"), "deno": ("eval",), "bun": ("-e", "--eval"),
    "ruby": ("-e",), "perl": ("-e", "-E"), "php": ("-r",),
    "bash": ("-c",), "sh": ("-c",), "zsh": ("-c",), "dash": ("-c",),
    "pwsh": ("-c", "-command"), "powershell": ("-c", "-command"),
    "cmd": ("/c", "/k"),
}
_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"})
#: Quoted string literals without whitespace: candidate paths in code.
_QUOTED_PATH = re.compile(r"""(['"])([^'"\s]+)\1""")


def _segments(command: str) -> list[str]:
    """Split on ``|``, ``||``, ``&&``, ``;``, ``&``, and newlines outside quotes.

    Inline code (``python -c "import os; print(1)"``) must stay one
    segment, or the code is cut apart before it can be inspected.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in ";\n|" or (ch == "&" and not (buf and buf[-1] in "<>0123456789")):
            parts.append("".join(buf))
            buf = []
            if i + 1 < len(command) and command[i + 1] == ch and ch in "|&":
                i += 1
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _argv(segment: str, dialect: str = "posix") -> list[str]:
    """Split one segment into words.

    PowerShell and cmd keep backslashes (``Get-Content .\\.env``,
    ``E:\\repo\\.env``); POSIX shlex would eat them and no rule would match.
    """
    import shlex

    try:
        if dialect == "posix":
            return shlex.split(segment, posix=True)
        return [w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in "'\"" else w
                for w in shlex.split(segment, posix=False)]
    except ValueError:
        return segment.split()


#: Copy-like programs: the last positional argument is the destination
#: (written, not read). ``cp .env.example .env`` reads only the example.
_COPY_PROGRAMS = frozenset({"cp", "scp", "rsync", "copy", "xcopy", "copy-item", "cpi"})
#: Uploaders: ``curl -d @.env``, ``-F f=@.env``, ``-T .env`` send a file.
_UPLOAD_FLAGS = frozenset({"-t", "--upload-file"})


def _git_bash_path(token: str) -> str:
    """``/e/repo/x`` (Git Bash, MSYS) -> ``e:/repo/x`` on Windows."""
    import os

    if os.name == "nt" and re.match(r"^/[A-Za-z]/", token):
        return f"{token[1]}:/{token[3:]}"
    return token


def shell_read_paths(
    command: str, root: Path | None, dialect: str = "posix", _depth: int = 0
) -> list[str]:
    """Repo-relative files a shell command reads, as far as can be seen.

    A heuristic, not a parser. Contributions:

    - each pipeline segment whose program reads file contents (``cat .env``,
      ``grep KEY config/.env.local``, ``sha256sum .env``, ``cp .env /tmp``,
      ``Get-Content .env``) and input redirection (``< .env``);
    - inline code of interpreters (``python -c``, ``node -e``,
      ``pwsh -Command``, ``bash -c``): quoted paths in the code, and nested
      shell code is parsed again;
    - with ``dialect="powershell"`` (the PowerShell tool), quoted paths
      anywhere in the command, since the whole command is code
      (``[IO.File]::ReadAllText('.env')``).

    Out of reach: paths assembled at runtime (``'.e' + 'nv'``,
    ``cat $(echo .e)nv``) and programs that open files themselves
    (``python app.py``). This is policy, not a sandbox.
    """
    found: dict[str, None] = {}

    def add(token: str) -> None:
        if not token or token.startswith("-") or "*" in token or "$" in token or "://" in token:
            return
        token = _git_bash_path(token)
        rel = relativize(token, root) if root is not None else globmatch.normalize(token)
        if rel:
            found.setdefault(rel, None)

    def add_quoted(code: str) -> None:
        for _quote, literal in _QUOTED_PATH.findall(code):
            add(literal)

    # .NET calls read files without a cmdlet name to recognise
    # ([IO.File]::ReadAllText('.env')); cmdlets are handled per segment, so
    # Set-Content '.env' or Test-Path '.env' are not taken for reads.
    if dialect == "powershell" and ("::" in command or "new-object" in command.lower()):
        add_quoted(command)

    for segment in _segments(command):
        for redirected in re.findall(r"(?<![<0-9])<\s*([^\s|;&<>]+)", segment):
            add(redirected.strip("'\""))
        argv = _argv(
            re.sub(r"\d?[<>]{1,2}\s*[^\s|;&<>]+", " ", segment),
            "posix" if dialect == "posix" else "windows",
        )
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]  # VAR=value prefixes
        while len(argv) > 2 and argv[0].startswith("$") and argv[1] == "=":
            argv = argv[2:]  # PowerShell assignment: $x = Get-Content .env
        if argv and argv[0] in ("sudo", "command", "exec", "nohup", "time", "&"):
            argv = argv[1:]
        if not argv:
            continue
        # PowerShell wraps calls in parentheses: (Get-Content -Raw .env).Length
        head = argv[0].lstrip("(&").split(")", 1)[0]
        program = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
        program = program.removesuffix(".exe")
        raw = argv[1:]
        # (Get-Content .env).Length -> ".env": strip only a trailing call
        # paren, never inside inline code (require('fs').readFileSync(...)).
        rest = [a.split(").", 1)[0].rstrip(")") for a in raw]

        flags = _INLINE_CODE_FLAGS.get(program)
        if flags:
            for i, arg in enumerate(raw[:-1]):
                if arg.lower() in flags:
                    # A shell takes the rest of the line as its command
                    # (cmd /c type .env, pwsh -Command Get-Content .env);
                    # an interpreter takes one argument.
                    code = " ".join(raw[i + 1:]) if program in _SHELL_INTERPRETERS else raw[i + 1]
                    add_quoted(code)
                    if program in _SHELL_INTERPRETERS and _depth < 3:
                        nested = "powershell" if program in ("pwsh", "powershell") else "posix"
                        for rel in shell_read_paths(code, root, nested, _depth + 1):
                            found.setdefault(rel, None)
                    break
            continue

        if program in ("curl", "wget", "http", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"):
            for i, arg in enumerate(rest):
                if "@" in arg:  # -d @.env, -F file=@.env, --data-binary @.env
                    add(arg.split("@", 1)[1].split(";", 1)[0])
                if arg.lower() in _UPLOAD_FLAGS | {"-infile"} and i + 1 < len(rest):
                    add(rest[i + 1])
            continue

        if program not in _READER_PROGRAMS:
            continue
        # Named path parameters (PowerShell -Path/-LiteralPath, --file=)
        # name the file unambiguously; with them present, the remaining
        # positional words are patterns or values, not files.
        named = [rest[i + 1] for i, a in enumerate(rest[:-1])
                 if a.lower() in ("-path", "-literalpath", "-filepath", "-lp")]
        named += [a.split("=", 1)[1] for a in rest if a.lower().startswith(("--file=", "-path:"))]
        if named:
            for token in named:
                add(token)
            continue
        positional = [a for a in rest if a and not a.startswith("-")]
        if program in _PATTERN_FIRST and positional:
            positional = positional[1:]
        if program in _COPY_PROGRAMS and len(positional) > 1:
            positional = positional[:-1]  # the destination is written, not read
        for token in positional:
            add(token)
    return list(found)


def evaluate_shell(
    config: Config,
    ruleset: RuleSet,
    command: str,
    root: Path | None = None,
    dialect: str = "posix",
) -> GateDecision:
    """Gate one shell command against ``shell`` rules and ``read`` rules.

    ``read`` rules also apply to files the command reads with a known
    reader program (``cat .env``): reading a secret through the shell is
    still reading it. See :func:`shell_read_paths` for the limits.

    Strict plan mode deliberately does not apply here: the agent must be
    able to run ``bewit plan register`` / ``check plan`` via shell to
    satisfy strict mode in the first place.
    """
    if config.enforcement == "off" or not command.strip():
        return GateDecision(permission="allow")
    excerpt = command.strip()[:200]
    hits = [
        RuleHit(
            rule_id=r.id,
            action=_apply_mode(r.action, config),
            path=excerpt,
            message=r.message,
        )
        for r in ruleset.match_text("shell", command)
    ]
    if ruleset.read_rules:
        for rel in shell_read_paths(command, root, dialect):
            if globmatch.matches_any(config.exempt, rel):
                continue
            for rule in ruleset.match_read(rel):
                hits.append(
                    RuleHit(
                        rule_id=rule.id,
                        action=_apply_mode(rule.action, config),
                        path=rel,
                        message=rule.message,
                    )
                )
    return decide_from_hits(hits, config, "shell command")


def evaluate_read(
    config: Config, ruleset: RuleSet, paths: list[str], root: Path
) -> GateDecision:
    """Gate file reads against ``read`` rules (e.g. secrets)."""
    if config.enforcement == "off":
        return GateDecision(permission="allow")
    hits: list[RuleHit] = []
    for rel in (relativize(p, root) for p in paths):
        if not rel or globmatch.matches_any(config.exempt, rel):
            continue
        for rule in ruleset.match_read(rel):
            hits.append(
                RuleHit(
                    rule_id=rule.id,
                    action=_apply_mode(rule.action, config),
                    path=rel,
                    message=rule.message,
                )
            )
    return decide_from_hits(hits, config, "file read")


def evaluate_tool(
    config: Config, ruleset: RuleSet, kind: str, identifier: str
) -> GateDecision:
    """Gate a tool or MCP invocation by name (``kind``: 'tool' | 'mcp')."""
    if config.enforcement == "off" or not identifier:
        return GateDecision(permission="allow")
    hits = [
        RuleHit(
            rule_id=r.id,
            action=_apply_mode(r.action, config),
            path=identifier,
            message=r.message,
        )
        for r in ruleset.match_text(kind, identifier)
    ]
    what = "MCP tool call" if kind == "mcp" else "tool call"
    return decide_from_hits(hits, config, what)


def rule_hits_for_paths(ruleset: RuleSet, rel_paths: list[str]) -> list[RuleHit]:
    """Pure rule matching used by plan checks and the finalize diff scan."""
    hits: list[RuleHit] = []
    for rel in rel_paths:
        for rule in ruleset.match_path(rel):
            hits.append(
                RuleHit(
                    rule_id=rule.id,
                    action=rule.action,
                    path=rel,
                    message=rule.message,
                )
            )
    return hits
