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
