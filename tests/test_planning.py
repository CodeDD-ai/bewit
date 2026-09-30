from pathlib import Path

from bewit.config import Config
from bewit.planning import check_plan, extract_declared_files, register_plan
from bewit.rules import load_rules
from bewit.storage import SessionStore

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
    """Regression: lstrip('./') ate the dot of '.bewit/...' paths."""
    text = "Touch `.bewit/rules/20-x.yaml` and `./app/main.py` and `.env.example`."
    declared = extract_declared_files(text, repo)
    assert ".bewit/rules/20-x.yaml" in declared
    assert "app/main.py" in declared
    assert not any(d.startswith("bewit/") for d in declared)


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


def test_check_records_the_plan_revision_it_evaluated(repo: Path):
    session = SessionStore(repo).session("s-revcheck")
    session.ensure_meta(None)
    register_plan(session, "Touch `app/main.py`.", repo)
    first = check_plan(Config(), load_rules(repo), session, {"api-rate-limit": "pass"})
    register_plan(session, "Touch `app/api/views.py`.", repo)
    second = check_plan(Config(), load_rules(repo), session, {"api-rate-limit": "pass"})
    assert (first["plan_revision"], second["plan_revision"]) == (1, 2)
    # Registrations merge by default within a session.
    assert second["declared_files"] == ["app/main.py", "app/api/views.py"]


def test_amend_unions_declared_files(repo: Path):
    """--amend must not drop files declared by earlier revisions."""
    session = SessionStore(repo).session("s-amend")
    session.ensure_meta(None)
    first = register_plan(session, "Touch `app/main.py`.", repo)
    second = register_plan(
        session, "Also touch `app/api/views.py`.", repo, amend=True
    )
    assert first == ["app/main.py"]
    assert second == ["app/main.py", "app/api/views.py"]
    latest = session.last_event("plan_registered")
    assert latest["amend"] is True
    assert latest["declared_files"] == ["app/main.py", "app/api/views.py"]


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


# Agent feedback: parsing, merge default, carried verdicts, empty checks --------


def test_slash_joined_file_list_splits_into_files(repo: Path):
    declared = extract_declared_files(
        "Tests in tests/test_a.py/test_b.py/test_c.py.", repo
    )
    assert declared == ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]


def test_brace_lists_expand(repo: Path):
    declared = extract_declared_files(
        "Edit src/pkg/{cli,gate}.py and `tests/test_{a,b}.py`.", repo
    )
    assert set(declared) == {
        "src/pkg/cli.py", "src/pkg/gate.py", "tests/test_a.py", "tests/test_b.py",
    }


def test_parenthesized_paths_are_declared_but_calls_are_not(repo: Path):
    declared = extract_declared_files(
        "The viewer (src/report/view.py) changes; open(tmp/scratch.py) does not.",
        repo,
    )
    assert declared == ["src/report/view.py"]


def test_unresolved_paths_suggest_the_intended_file(repo: Path):
    from bewit.planning import unresolved_paths

    missing = dict(unresolved_paths(
        ["app/main.py", "main.py", "app/mian.py", "app/brand_new_module.py"], repo
    ))
    assert "app/main.py" not in missing  # exists
    assert missing["main.py"] == "app/main.py"  # leading directory dropped
    assert missing["app/mian.py"] == "app/main.py"  # typo
    assert missing["app/brand_new_module.py"] is None  # a genuinely new file


def test_registrations_merge_by_default_and_replace_resets(repo: Path):
    session = SessionStore(repo).session("s-merge")
    session.ensure_meta(None)
    register_plan(session, "Step 1: `app/main.py`.", repo)
    assert register_plan(session, "Step 2: `app/api/views.py`.", repo) == [
        "app/main.py", "app/api/views.py",
    ]
    assert register_plan(session, "Over: `app/other.py`.", repo, amend=False) == [
        "app/other.py",
    ]


def _scoped_rule(repo: Path) -> None:
    (repo / ".bewit" / "rules" / "scoped.yaml").write_text(
        "- id: api-docs\n  kind: prompt\n  policy: 'API changes are documented.'\n"
        "  applies_to: ['app/api/**']\n",
        encoding="utf-8",
    )


def test_verdicts_carry_over_when_their_part_of_the_plan_is_unchanged(repo: Path):
    _scoped_rule(repo)
    rules = load_rules(repo)
    session = SessionStore(repo).session("s-carry")
    session.ensure_meta(None)
    register_plan(session, "`app/api/views.py`", repo)
    first = check_plan(Config(), rules, session, {"api-rate-limit": "pass", "api-docs": "pass"})
    assert first["ok"] and first["recorded"]

    # A file outside api-docs' scope: its verdict carries; the unscoped
    # rule saw the declaration change and must be attested again.
    register_plan(session, "`app/main.py`", repo)
    second = check_plan(Config(), rules, session, {"api-rate-limit": "pass"})
    by_id = {r["rule_id"]: r for r in second["prompt_rules"]}
    assert by_id["api-docs"]["verdict"] == "pass" and by_id["api-docs"]["carried"]
    assert "carried" not in by_id["api-rate-limit"]
    assert second["ok"]

    # A new file inside api-docs' scope: nothing carries.
    register_plan(session, "`app/api/urls.py`", repo)
    third = check_plan(Config(), rules, session, {"api-rate-limit": "pass"})
    assert {r["rule_id"]: r["verdict"] for r in third["prompt_rules"]}["api-docs"] == "unattested"


def test_bare_check_reuses_verdicts_for_an_unchanged_plan(repo: Path):
    session = SessionStore(repo).session("s-bare")
    session.ensure_meta(None)
    register_plan(session, "`app/main.py`", repo)
    check_plan(Config(), load_rules(repo), session, {"api-rate-limit": "pass"})
    again = check_plan(Config(), load_rules(repo), session, {})
    assert again["ok"] and again["recorded"]
    assert again["prompt_rules"][0]["carried"] is True


def test_bare_failing_check_is_not_recorded(repo: Path):
    """Reported from real use: a bare `bewit check plan` recorded a fail
    over a passing check and closed the edit gate."""
    session = SessionStore(repo).session("s-empty")
    session.ensure_meta(None)
    register_plan(session, "`app/main.py`", repo)
    check_plan(Config(), load_rules(repo), session, {"api-rate-limit": "pass"})
    register_plan(session, "`app/other.py`", repo)  # unscoped rule must re-attest
    report = check_plan(Config(), load_rules(repo), session, {})
    assert report["ok"] is False and report["recorded"] is False
    checks = [e for e in session.events() if e["type"] == "plan_check"]
    assert len(checks) == 1 and checks[0]["ok"] is True
