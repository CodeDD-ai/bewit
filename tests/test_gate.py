from pathlib import Path

from archrev.config import Config, load_config
from archrev.gate import evaluate_edit, relativize
from archrev.rules import load_rules
from archrev.storage import SessionStore


def _session(repo: Path, sid: str = "s1"):
    session = SessionStore(repo).session(sid)
    session.ensure_meta(None)
    return session


def test_block_rule_asks_for_approval(repo: Path):
    decision = evaluate_edit(
        Config(), load_rules(repo), _session(repo),
        [str(repo / "db" / "migrations" / "0002_x.sql")], repo,
    )
    assert decision.permission == "ask"
    assert "protect-migrations" in decision.agent_message
    assert "Migrations need approval." in decision.user_message


def test_flag_rule_allows_with_note(repo: Path):
    decision = evaluate_edit(
        Config(), load_rules(repo), _session(repo), ["k8s/deploy.yaml"], repo
    )
    assert decision.permission == "allow"
    assert decision.hits and decision.hits[0].action == "flag"
    assert "flag-infra" in decision.agent_message


def test_unmatched_path_allows_silently(repo: Path):
    decision = evaluate_edit(
        Config(), load_rules(repo), _session(repo), ["app/main.py"], repo
    )
    assert decision.permission == "allow"
    assert not decision.hits
    assert not decision.agent_message


def test_exempt_paths_never_gated(repo: Path):
    config = Config(strict_plan_check=True)  # would deny anything else
    decision = evaluate_edit(
        config, load_rules(repo), _session(repo),
        [".archrev/sessions/s1/plan.md", ".cursor/rules/x.mdc", "notes.plan.md"],
        repo,
    )
    assert decision.permission == "allow"


def test_strict_mode_denies_until_plan_checked(repo: Path):
    config = Config(strict_plan_check=True)
    session = _session(repo)
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "deny"
    assert "archrev plan register" in decision.agent_message

    session.append_event("plan_check", ok=True)
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "allow"


def test_monitor_mode_downgrades_block_to_flag(repo: Path):
    config = Config(enforcement="monitor")
    decision = evaluate_edit(
        config, load_rules(repo), _session(repo),
        ["db/migrations/0003_y.sql"], repo,
    )
    assert decision.permission == "allow"
    assert decision.hits and decision.hits[0].action == "flag"


def test_enforcement_off_disables_rules(repo: Path):
    config = Config(enforcement="off")
    decision = evaluate_edit(
        config, load_rules(repo), _session(repo),
        ["db/migrations/0003_y.sql"], repo,
    )
    assert decision.permission == "allow"
    assert not decision.hits


def test_relativize_handles_absolute_and_relative(repo: Path):
    assert relativize(str(repo / "app" / "main.py"), repo) == "app/main.py"
    assert relativize("app\\main.py", repo) == "app/main.py"


def test_config_exempt_extends_defaults(repo: Path):
    (repo / ".archrev" / "config.yaml").write_text(
        "exempt:\n  - docs/**\n", encoding="utf-8"
    )
    config = load_config(repo)
    assert ".archrev/**" in config.exempt and "docs/**" in config.exempt
