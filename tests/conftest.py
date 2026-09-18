"""Shared fixtures: a real temporary git repository with ArchRev installed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repository with one initial commit and .archrev scaffolding."""
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.email", "test@archrev.local")
    git(tmp_path, "config", "user.name", "ArchRev Tests")
    git(tmp_path, "config", "commit.gpgsign", "false")

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    (tmp_path / "db" / "migrations" / "0001_init.sql").write_text(
        "CREATE TABLE t (id INT);\n", encoding="utf-8"
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--quiet", "-m", "chore: initial commit")

    rules_dir = tmp_path / ".archrev" / "rules"
    rules_dir.mkdir(parents=True)
    rules_dir.joinpath("rules.yaml").write_text(
        """
- id: protect-migrations
  kind: path
  match: ["db/migrations/**"]
  action: block
  message: "Migrations need approval."
- id: flag-infra
  kind: path
  match: ["Dockerfile*", "k8s/**"]
  action: flag
  message: "Infrastructure change."
- id: api-rate-limit
  kind: prompt
  policy: "New API endpoints must declare rate limiting."
""",
        encoding="utf-8",
    )
    return tmp_path
