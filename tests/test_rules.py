from pathlib import Path

from archrev.rules import load_rules


def _write_rules(root: Path, name: str, content: str) -> None:
    rules_dir = root / ".archrev" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / name).write_text(content, encoding="utf-8")


def test_load_valid_rules(repo: Path):
    ruleset = load_rules(repo)
    assert not ruleset.errors
    assert {r.id for r in ruleset.path_rules} == {"protect-migrations", "flag-infra"}
    assert {r.id for r in ruleset.prompt_rules} == {"api-rate-limit"}


def test_match_path_returns_declaration_order(repo: Path):
    hits = load_rules(repo).match_path("db/migrations/0002_add.sql")
    assert [r.id for r in hits] == ["protect-migrations"]


def test_invalid_rules_collected_not_raised(tmp_path: Path):
    _write_rules(tmp_path, "bad.yaml", """
- kind: path
  match: ["x/**"]
- id: no-kind
- id: bad-action
  kind: path
  match: ["y/**"]
  action: explode
- id: prompt-no-policy
  kind: prompt
""")
    ruleset = load_rules(tmp_path)
    assert ruleset.rules == []
    assert len(ruleset.errors) == 4


def test_duplicate_ids_rejected(tmp_path: Path):
    _write_rules(tmp_path, "a.yaml", "{id: dup, kind: path, match: ['a/**']}")
    _write_rules(tmp_path, "b.yaml", "{id: dup, kind: path, match: ['b/**']}")
    ruleset = load_rules(tmp_path)
    assert len(ruleset.rules) == 1
    assert any("duplicate" in e for e in ruleset.errors)


def test_disabled_rules_do_not_match(tmp_path: Path):
    _write_rules(
        tmp_path, "off.yaml",
        "{id: off-rule, kind: path, match: ['**'], action: block, enabled: false}",
    )
    ruleset = load_rules(tmp_path)
    assert not ruleset.errors
    assert ruleset.match_path("anything.txt") == []


def test_applies_to_scopes_rule(tmp_path: Path):
    _write_rules(
        tmp_path, "scoped.yaml",
        "{id: scoped, kind: path, match: ['**/*.sql'], action: flag, applies_to: ['backend/**']}",
    )
    ruleset = load_rules(tmp_path)
    assert ruleset.match_path("backend/db/x.sql")
    assert not ruleset.match_path("frontend/db/x.sql")


def test_unparseable_yaml_is_an_error_not_a_crash(tmp_path: Path):
    _write_rules(tmp_path, "broken.yaml", "id: [unclosed")
    ruleset = load_rules(tmp_path)
    assert ruleset.rules == []
    assert len(ruleset.errors) == 1
