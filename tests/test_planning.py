from pathlib import Path

from archrev.config import Config
from archrev.planning import check_plan, extract_declared_files, register_plan
from archrev.rules import load_rules
from archrev.storage import SessionStore

PLAN = """
# Plan: add rate limiting

Touch `app/main.py` and [the settings](app/settings.py).

```12:20:db/migrations/0002_add.sql
ALTER TABLE t ADD COLUMN x INT;
```

Also update app/api/views.py for the endpoint. Not a path: 3.14, v1.2.3.
See https://example.com/app/fake.py for docs.
"""


def test_extract_declared_files(repo: Path):
    declared = extract_declared_files(PLAN, repo)
    assert "app/main.py" in declared
    assert "app/settings.py" in declared
    assert "db/migrations/0002_add.sql" in declared  # fence citation
    assert "app/api/views.py" in declared  # bare path
    assert not any("example.com" in d for d in declared)
    assert not any(d in ("3.14", "v1.2.3") for d in declared)


def test_dotfile_paths_keep_their_leading_dot(repo: Path):
    """Regression: lstrip('./') ate the dot of '.archrev/...' paths."""
    text = "Touch `.archrev/rules/20-x.yaml` and `./app/main.py` and `.env.example`."
    declared = extract_declared_files(text, repo)
    assert ".archrev/rules/20-x.yaml" in declared
    assert "app/main.py" in declared
    assert not any(d.startswith("archrev/") for d in declared)


def test_bare_toplevel_filenames_extracted_when_they_exist(repo: Path):
    """Regression: 'Update README.md and pyproject.toml' in prose was not
    extracted (no path separator), producing false out-of-plan drift."""
    (repo / "README.md").write_text("# x\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    text = "Update README.md and bump the version in pyproject.toml."
    declared = extract_declared_files(text, repo)
    assert "README.md" in declared
    assert "pyproject.toml" in declared
    # Bare names that do NOT exist in the repo stay excluded (precision).
    declared = extract_declared_files("See CHANGES.md for details.", repo)
    assert "CHANGES.md" not in declared


def test_register_and_check_plan(repo: Path):
    session = SessionStore(repo).session("s1")
    session.ensure_meta(None)
    declared = register_plan(session, PLAN, repo, origin="test")
    assert declared
    assert session.plan_text() is not None
    assert session.has_event("plan_registered")


    report = check_plan(
        Config(), load_rules(repo), session, {"api-rate-limit": "pass"}
    )
    assert report["ok"] is True
    assert session.has_event("plan_check")
    # The migration file in the plan must appear as a gate preview.
    assert any(
        f["rule_id"] == "protect-migrations" and f["action"] == "block"
        for f in report["path_findings"]
    )


def test_plan_revisions_keep_their_text(repo: Path):
    """Re-registering amends the plan; every revision's text must survive
    in the event log so plan evolution stays reviewable."""
    session = SessionStore(repo).session("s-rev")
    session.ensure_meta(None)
    register_plan(session, "v1: touch `app/main.py`", repo)
    register_plan(session, "v2: touch `app/main.py` and `app/api/views.py`", repo)
    events = [e for e in session.events() if e["type"] == "plan_registered"]
    assert len(events) == 2
    assert events[0]["text"].startswith("v1:")
    assert events[1]["text"].startswith("v2:")
    # plan.md holds the latest text.
    assert session.plan_text().startswith("v2:")


def test_check_plan_fails_on_unattested_rules(repo: Path):
    session = SessionStore(repo).session("s2")
    session.ensure_meta(None)
    register_plan(session, "touch `app/main.py`", repo)
    report = check_plan(Config(), load_rules(repo), session, {})
    assert report["ok"] is False
    assert report["prompt_rules"][0]["verdict"] == "unattested"


def test_check_plan_reports_unknown_attestations(repo: Path):
    session = SessionStore(repo).session("s3")
    session.ensure_meta(None)
    register_plan(session, "plan", repo)
    report = check_plan(
        Config(), load_rules(repo), session,
        {"api-rate-limit": "pass", "typo-rule": "pass"},
    )
    assert report["unknown_attestations"] == ["typo-rule"]
