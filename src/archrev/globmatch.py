"""Globstar path matching for rule patterns.

Python's :mod:`fnmatch` does not understand ``**``, and
``PurePath.full_match`` only exists on 3.13+, so ArchRev ships a small,
well-tested translator with git-style semantics:

- ``**`` matches any number of path segments (including zero when followed
  by ``/``), e.g. ``django/**/migrations/**`` or ``type-db/**/*.tql``.
- ``*`` matches within a single segment; ``?`` matches one character.
- A pattern without ``/`` matches at any depth (like ``.gitignore``), so
  ``.env*`` matches both ``.env`` and ``django/.env.local``.
- Matching is case-insensitive: repository paths on Windows are
  case-insensitive and rules must behave identically across platforms.
"""

from __future__ import annotations

import re
from functools import lru_cache

_SEGMENT_SPECIALS = re.compile(r"([.^$+{}\[\]|()\\])")


def normalize(path: str) -> str:
    """Normalize a path for matching: forward slashes, no leading ``./``."""
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def _segment_to_regex(segment: str) -> str:
    """Translate one non-``**`` glob segment to a regex fragment."""
    out = _SEGMENT_SPECIALS.sub(r"\\\1", segment)
    out = out.replace("*", "[^/]*").replace("?", "[^/]")
    return out


@lru_cache(maxsize=1024)
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern into a case-insensitive regex."""
    pat = normalize(pattern)
    if not pat:
        # An empty pattern matches nothing rather than everything: rules
        # should never silently become universal because of a typo.
        return re.compile(r"(?!x)x")
    # Basename-style patterns match at any depth, mirroring .gitignore.
    if "/" not in pat:
        pat = "**/" + pat

    parts = pat.split("/")
    pieces: list[str] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            # Trailing '**' swallows the rest of the path; interior '**/'
            # matches zero or more whole segments.
            pieces.append(".*" if last else "(?:[^/]+/)*")
        else:
            pieces.append(_segment_to_regex(part) + ("" if last else "/"))
    return re.compile("^" + "".join(pieces) + "$", re.IGNORECASE)


def matches(pattern: str, path: str) -> bool:
    """Return True if ``path`` (any separator style) matches ``pattern``."""
    return bool(compile_pattern(pattern).match(normalize(path)))


def matches_any(patterns: tuple[str, ...] | list[str], path: str) -> bool:
    """Return True if ``path`` matches at least one of ``patterns``."""
    norm = normalize(path)
    return any(compile_pattern(p).match(norm) for p in patterns)
