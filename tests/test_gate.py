from pathlib import Path

from bewit.config import Config, load_config
from bewit.gate import evaluate_edit, evaluate_read, relativize
from bewit.rules import load_rules
from bewit.storage import SessionStore


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
        [".bewit/sessions/s1/plan.md", "notes.plan.md"],
        repo,
    )
    assert decision.permission == "allow"


def test_governance_files_are_not_exempt(repo: Path):
    """Regression: an agent must not be able to edit Bewit's own config,
    rules, or hooks wiring without gating (tamper protection)."""
    (repo / ".bewit" / "rules" / "90-self.yaml").write_text(
        "{id: self-protect, kind: path, action: block,\n"
        " match: ['.bewit/config.yaml', '.bewit/rules/**', "
        "'.cursor/hooks.json', '.cursor/rules/bewit.mdc']}",
        encoding="utf-8",
    )
    ruleset = load_rules(repo)
    for path in (
        ".bewit/config.yaml",
        ".bewit/rules/rules.yaml",
        ".cursor/hooks.json",
        ".cursor/rules/bewit.mdc",
    ):
        decision = evaluate_edit(Config(), ruleset, _session(repo), [path], repo)
        assert decision.permission == "ask", path


def test_strict_mode_denies_until_plan_checked(repo: Path):
    config = Config(strict_plan_check=True)
    session = _session(repo)
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "deny"
    assert "bewit plan register" in decision.agent_message

    session.append_event("plan_check", ok=True)
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "allow"


def test_strict_mode_failed_check_keeps_gate_closed(repo: Path):
    """Regression: attesting 'fail' must not unlock strict mode."""
    config = Config(strict_plan_check=True)
    session = _session(repo)
    session.append_event("plan_check", ok=False)
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "deny"
    assert "did NOT pass" in decision.agent_message

    # A later passing check (after resolving with the user) unlocks.
    session.append_event("plan_check", ok=True)
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "allow"


def test_strict_mode_check_goes_stale_on_re_registration(repo: Path):
    """Regression: a check of the --text plan kept unlocking the gate after
    the agent re-registered a different plan from a file."""
    config = Config(strict_plan_check=True)
    session = _session(repo)
    session.append_event("plan_registered", declared_files=["app/main.py"])
    session.append_event("plan_check", ok=True)
    session.append_event("plan_registered", declared_files=["app/other.py"])
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "deny"
    assert "registered again" in decision.agent_message

    session.append_event("plan_check", ok=True)
    decision = evaluate_edit(config, load_rules(repo), session, ["app/main.py"], repo)
    assert decision.permission == "allow"


def test_strict_mode_does_not_gate_paths_outside_the_repo(repo: Path, tmp_path_factory):
    """Reported from real use: plan mode could not write its own plan file
    under ~/.claude/plans before a repository plan existed."""
    config = Config(strict_plan_check=True)
    session = _session(repo)
    outside = str(tmp_path_factory.mktemp("home") / ".claude" / "plans" / "p.md")
    decision = evaluate_edit(config, load_rules(repo), session, [outside], repo)
    assert decision.permission == "allow"
    # One repo path in the same edit still needs the plan.
    decision = evaluate_edit(
        config, load_rules(repo), session, [outside, "app/main.py"], repo
    )
    assert decision.permission == "deny"


def test_enforcement_off_is_the_master_switch(repo: Path):
    """Regression: 'off' must disable strict mode too, as documented."""
    config = Config(enforcement="off", strict_plan_check=True)
    decision = evaluate_edit(
        config, load_rules(repo), _session(repo), ["app/main.py"], repo
    )
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


def test_read_rules_skip_exempt_paths(repo: Path):
    (repo / ".bewit" / "rules" / "reads.yaml").write_text(
        '- id: read-all\n  kind: read\n  match: ["**"]\n  action: deny\n',
        encoding="utf-8",
    )
    rules = load_rules(repo)
    denied = evaluate_read(Config(), rules, [".env"], repo)
    assert denied.permission == "deny"
    allowed = evaluate_read(
        Config(), rules, [".bewit/sessions/s/events.jsonl"], repo
    )
    assert allowed.permission == "allow"
    assert not allowed.hits


def test_relativize_decodes_file_uris(repo: Path):
    uri = "file:///" + str(repo / ".env").replace("\\", "/").replace(":", "%3A", 1)
    assert relativize(uri, repo) == ".env"
    assert relativize("FILE://" + str(repo / "app" / "main.py"), repo) == "app/main.py"


def test_relativize_keeps_posix_paths_outside_the_repo_absolute(repo: Path):
    """normalize() strips the leading "/"; an outside path must not become
    repo-relative (Linux/macOS: ~/.claude/plans counted as a repo file)."""
    from bewit.gate import is_outside_repo

    outside = relativize("/home/u/.claude/plans/p.md", repo)
    assert outside == "/home/u/.claude/plans/p.md" and is_outside_repo(outside)
    # A file URI's drive form stays a Windows path, not a POSIX one.
    assert relativize("file:///C:/elsewhere/x.py", repo) == "C:/elsewhere/x.py"


def test_relativize_through_a_symlinked_directory(tmp_path: Path):
    """macOS: /var/folders/... is a symlink to /private/var/folders/..."""
    import os

    import pytest

    real = tmp_path / "real-repo"
    (real / "app").mkdir(parents=True)
    link = tmp_path / "link-repo"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    root = real.resolve()  # find_root resolves the root
    assert relativize(str(link / "app" / "main.py"), root) == "app/main.py"
    assert relativize(str(link / ".bewit" / "rules" / "x.yaml"), root) == ".bewit/rules/x.yaml"


def test_relativize_with_a_windows_short_name(tmp_path: Path):
    """Windows CI: TEMP is C:/Users/RUNNER~1/..., the resolved root is long."""
    import ctypes
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("8.3 short names are a Windows feature")
    long_dir = tmp_path / "a-long-directory-name"
    (long_dir / "app").mkdir(parents=True)
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), buf, 1024):
        pytest.skip("GetShortPathNameW failed")
    short = buf.value
    if short.lower() == str(long_dir).lower():
        pytest.skip("8.3 short names are disabled on this volume")
    root = long_dir.resolve()
    assert relativize(short + "\\app\\main.py", root) == "app/main.py"


def test_config_exempt_extends_defaults(repo: Path):
    (repo / ".bewit" / "config.yaml").write_text(
        "exempt:\n  - docs/**\n", encoding="utf-8"
    )
    config = load_config(repo)
    assert ".bewit/sessions/**" in config.exempt and "docs/**" in config.exempt
    # Governance paths must never sneak back into the defaults.
    assert ".bewit/**" not in config.exempt
    assert ".cursor/**" not in config.exempt
