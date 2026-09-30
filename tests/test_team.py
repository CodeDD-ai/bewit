"""Team-adoption features: uvx shim, CI template, prompt privacy, metrics."""

import json
from pathlib import Path

import yaml

from bewit import __version__
from bewit.ci import write_gitlab_template
from bewit.config import load_config
from bewit.hooks import run_hook
from bewit.metrics import collect, discover_repos
from bewit.rules import load_rules
from bewit.scaffold import init_repo
from bewit.storage import SessionStore


# -- uvx shim ---------------------------------------------------------------


def test_init_uvx_shim_writes_shimmed_commands(repo: Path):
    init_repo(repo, shim="uvx")
    hooks = json.loads((repo / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        e["command"] for entries in hooks["hooks"].values() for e in entries
    ]
    assert commands and all(c.startswith("uvx bewit ") for c in commands)
    git_hook = (repo / ".git" / "hooks" / "prepare-commit-msg").read_text(
        encoding="utf-8"
    )
    assert 'uvx bewit git-trailer "$1"' in git_hook


def test_reinit_upgrades_between_shim_modes(repo: Path):
    """Switching modes must upgrade entries in place, never duplicate."""
    init_repo(repo, shim="none")
    init_repo(repo, shim="uvx")
    hooks = json.loads((repo / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    for event, entries in hooks["hooks"].items():
        ours = [e for e in entries if "bewit" in e.get("command", "")]
        assert len(ours) == 1, f"duplicated entry for {event}"
        assert ours[0]["command"].startswith("uvx bewit ")


def test_init_starter_rules_load_without_errors(repo: Path):
    """Generated rule files must parse; regex backslashes must survive YAML."""
    init_repo(repo)
    ruleset = load_rules(repo)
    assert ruleset.errors == []
    patterns = {r.id: r.patterns for r in ruleset.rules if r.kind == "shell"}
    assert patterns["example-no-push"] == ("\\bgit\\s+push\\b",)


# -- CI template -------------------------------------------------------------


def test_gitlab_template_written_with_jobs(repo: Path):
    target = write_gitlab_template(repo)
    assert target == repo / ".gitlab" / "bewit-ci.yml"
    content = target.read_text(encoding="utf-8")
    assert "bewit:rules:" in content
    assert "bewit check diff --base" in content
    assert "bewit verify --all" in content
    assert "bewit:mr-report:" in content
    assert "Bewit-Session:" in content  # trailer-based session lookup
    assert "local: .gitlab/bewit-ci.yml" in content  # include instructions


def test_gitlab_template_pins_install_source(repo: Path):
    """An unpinned `pip install bewit` would run whatever the index serves."""
    content = write_gitlab_template(repo).read_text(encoding="utf-8")
    jobs = yaml.safe_load(content)
    assert jobs[".bewit:base"]["variables"]["BEWIT_PIP_SPEC"] == (
        f"bewit=={__version__}"
    )
    for name in ("bewit:rules", "bewit:mr-report"):
        assert jobs[name]["extends"] == ".bewit:base"
        assert 'pip install --quiet "$BEWIT_PIP_SPEC"' in jobs[name]["before_script"]
    assert "pip install --quiet bewit\n" not in content


# -- prompt privacy ----------------------------------------------------------


def test_prompt_capture_modes(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    secret = "deploy key is hunter2, use it for the migration " + "x" * 300

    (repo / ".bewit" / "config.yaml").write_text(
        "prompt_capture: excerpt\n", encoding="utf-8"
    )
    assert load_config(repo).prompt_capture == "excerpt"
    run_hook("prompt", json.dumps({"conversation_id": "p-ex", "prompt": secret}))
    event = SessionStore(repo).session("p-ex").events()[0]
    assert event["capture"] == "excerpt"
    assert len(event["text"]) == 200
    assert event["chars"] == len(secret)

    (repo / ".bewit" / "config.yaml").write_text(
        "prompt_capture: none\n", encoding="utf-8"
    )
    run_hook("prompt", json.dumps({"conversation_id": "p-no", "prompt": secret}))
    event = SessionStore(repo).session("p-no").events()[0]
    assert event["text"] == ""
    assert "hunter2" not in json.dumps(event)
    assert event["chars"] == len(secret)
    assert len(event["content_hash"]) == 16


def test_invalid_prompt_capture_falls_back_to_full(repo: Path):
    (repo / ".bewit" / "config.yaml").write_text(
        "prompt_capture: nonsense\n", encoding="utf-8"
    )
    assert load_config(repo).prompt_capture == "full"


# -- cross-repo metrics ------------------------------------------------------


def _seed_session(repo: Path, sid: str) -> None:
    session = SessionStore(repo).session(sid)
    session.ensure_meta(None)
    session.append_event("prompt", text="hi")
    session.append_event(
        "plan_registered", declared_files=["app/main.py"], origin="t", chars=10
    )
    session.append_event("plan_check", ok=True)
    session.append_event("edit", path="app/main.py")
    session.append_event("edit", path="app/extra.py")  # out of plan
    session.append_event("gate", permission="ask", paths=["db/x.sql"], hits=[])


def test_index_metrics_across_repos(tmp_path: Path):
    for name in ("repo-a", "repo-b"):
        root = tmp_path / name
        (root / ".bewit").mkdir(parents=True)
        _seed_session(root, f"s-{name}")

    assert [r.name for r in discover_repos([tmp_path])] == ["repo-a", "repo-b"]
    per_repo, total = collect([tmp_path])
    assert len(per_repo) == 2
    assert total.sessions == 2
    assert total.with_plan == 2
    assert total.with_passing_check == 2
    assert total.edits == 4
    assert total.files_touched == 4
    assert total.out_of_plan == 2
    assert total.gate_asks == 2
    assert per_repo[0].drift_rate == 0.5
    assert total.chain_breaks == 0
