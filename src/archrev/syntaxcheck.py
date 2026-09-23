"""Hermetic Python syntax check for ``kind: check`` rules.

``python -m py_compile`` writes ``.pyc`` files beside the sources, so a
syntax gate can fail on a file lock (observed on Windows) or dirty the
worktree. This module only parses. It writes nothing.

Usage (from a check rule)::

    python -m archrev.syntaxcheck {files}
"""

from __future__ import annotations

import ast
import sys


def main(argv: list[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths:
        return 0
    for path in paths:
        try:
            source = open(path, encoding="utf-8").read()
        except OSError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 1
        try:
            ast.parse(source, filename=path)
        except SyntaxError as exc:
            print(f"{path}:{exc.lineno}: {exc.msg}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
