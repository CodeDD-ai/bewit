"""Viewer server: project scoping, hub routing, port negotiation, Host check."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from bewit.gitutil import Git
from bewit.report.server import (
    _session_summary,
    _user_prompt_excerpt,
    bind_viewer,
    build_state,
    identify_viewer,
    make_server,
    project_ids,
)
from bewit.storage import SessionStore


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".bewit" / "sessions").mkdir(parents=True)
    return root


@pytest.fixture()
def running():
    """Start viewers on ephemeral ports; stop them after the test."""
    servers = []

    def start(roots: list[Path], hub: bool = False):
        httpd = make_server(roots, "127.0.0.1", 0, hub=hub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd, httpd.server_address[1]

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _get(port: int, path: str, host: str | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host} if host else {}
    conn.request("GET", path, headers=headers)
    res = conn.getresponse()
    body = json.loads(res.read().decode("utf-8") or "{}")
    conn.close()
    return res.status, body


def test_state_names_the_project_and_classifies_sessions(repo: Path):
    store = SessionStore(repo)
    agent = store.session("abc-123")
    agent.ensure_meta(Git(repo).head_sha(), branch="main", runtime="claude")
    agent.append_event("prompt", text="do the thing")
    store.session("human").append_event("human_changes", files=["x.py"])

    state = build_state(repo)
    assert state["project"] == repo.name
    assert state["app"] == "bewit-viewer"
    from bewit.report.server import API_LEVEL, _template

    assert state["api_level"] == API_LEVEL
    # The page checks the same number; they must move together.
    assert f"const API_LEVEL = {API_LEVEL};" in _template()
    by_id = {s["id"]: s for s in state["sessions"]}
    assert by_id["abc-123"]["category"] == "agent"
    assert by_id["abc-123"]["runtime"] == "claude"
    assert by_id["human"]["category"] == "human"


def test_last_activity_is_the_latest_event(repo: Path):
    session = SessionStore(repo).session("s1")
    session.ensure_meta(None)
    session.append_event("prompt", text="one")
    last = session.append_event("edit", path="app/main.py")
    assert _session_summary(repo, session)["last_activity"] == last["ts"]


def test_project_ids_are_unique(tmp_path: Path):
    a = _project(tmp_path / "x", "app")
    b = _project(tmp_path / "y", "app")
    assert list(project_ids([a, b])) == ["app", "app-2"]


def test_hub_routes_state_by_project(tmp_path: Path, running):
    a, b = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    _, port = running([a, b], hub=True)

    status, projects = _get(port, "/api/projects")
    assert status == 200 and projects["hub"] is True
    assert [p["id"] for p in projects["projects"]] == ["alpha", "beta"]

    assert _get(port, "/api/state?project=beta")[1]["project"] == "beta"
    assert _get(port, "/api/state")[1]["project"] == "alpha"  # default: first
    assert _get(port, "/api/state?project=nope")[0] == 404


def test_foreign_host_header_is_rejected(tmp_path: Path, running):
    _, port = running([_project(tmp_path, "alpha")])
    assert _get(port, "/api/state", host="evil.example:4177")[0] == 403
    assert _get(port, "/api/state", host=f"localhost:{port}")[0] == 200


def test_identify_viewer_reports_served_roots(tmp_path: Path, running):
    root = _project(tmp_path, "alpha")
    _, port = running([root])
    served = identify_viewer("127.0.0.1", port)
    assert served is not None and Path(served[0]) == root


def test_bind_reuses_a_viewer_for_the_same_project(tmp_path: Path, running):
    root = _project(tmp_path, "alpha")
    _, port = running([root])
    binding = bind_viewer([root], "127.0.0.1", port)
    assert binding.reused and binding.server is None and binding.port == port


def test_bind_moves_off_another_projects_port(tmp_path: Path, running):
    other, mine = _project(tmp_path, "other"), _project(tmp_path, "mine")
    _, port = running([other])
    binding = bind_viewer([mine], "127.0.0.1", port)
    try:
        assert not binding.reused
        assert binding.port != port
        assert binding.displaced_by is not None
        assert Path(binding.displaced_by[0]).name == "other"
    finally:
        if binding.server is not None:
            binding.server.server_close()


def _post(port: int, path: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=json.dumps(body),
                 headers={"Content-Type": "application/json", **(headers or {})})
    res = conn.getresponse()
    data = json.loads(res.read().decode("utf-8") or "{}")
    conn.close()
    return res.status, data


def test_viewer_ack_is_recorded_as_reviewer(repo: Path, running):
    session = SessionStore(repo).session("s-ack")
    session.ensure_meta(None)
    session.append_event("prompt", text="go")
    session.append_event("worktree", phase="start", changed={})  # checkpoint baseline
    _, port = running([repo])
    body = {"session": "s-ack", "target": "db/migrations/0001_init.sql", "note": "reviewed"}
    assert _post(port, "/api/ack", body)[0] == 403  # no X-Bewit header (CSRF)
    status, data = _post(port, "/api/ack", body, {"X-Bewit": "1"})
    assert status == 200 and data["actor"] == "human"
    ack = session.last_event("ack")
    assert (ack["via"], ack["actor"], ack["note"]) == ("viewer", "human", "reviewed")


def test_bulk_acknowledge_records_one_ack_per_finding(repo: Path, running):
    """The review box's bulk action posts each finding separately: the log
    reads exactly as if the reviewer had acknowledged them one by one."""
    from bewit.report.server import _template

    page = _template()
    assert "const BULK_MIN = 3;" in page and "data-pick-all" in page
    assert 'fetch("/api/ack"' in page.split("async function submitBulk", 1)[1].split("\nfunction ", 1)[0]

    session = SessionStore(repo).session("s-bulk")
    session.ensure_meta(None)
    session.append_event("prompt", text="go")
    session.append_event("worktree", phase="start", changed={})
    _, port = running([repo])
    targets = [f"app/f{i}.py" for i in range(5)]
    for target in targets:
        status, data = _post(port, "/api/ack", {"session": "s-bulk", "target": target, "note": "batch"},
                             {"X-Bewit": "1"})
        assert status == 200 and data["actor"] == "human"
    acks = [e for e in session.events() if e["type"] == "ack"]
    assert [a["target"] for a in acks] == targets
    assert {a["note"] for a in acks} == {"batch"}


def test_viewer_ack_during_an_agent_command_is_agent_made(repo: Path, running, monkeypatch):
    from bewit.hooks import run_hook

    monkeypatch.chdir(repo)
    run_hook("prompt", json.dumps({"conversation_id": "s-agent", "prompt": "go"}))
    run_hook("shell", json.dumps({"conversation_id": "s-agent", "command": "curl localhost"}))
    _, port = running([repo])
    status, data = _post(port, "/api/ack", {"session": "s-agent", "target": "x", "note": "n"},
                         {"X-Bewit": "1"})
    assert status == 200 and data["actor"] == "agent"


def test_session_view_lists_active_rules_and_review(repo: Path, running):
    session = SessionStore(repo).session("s-view")
    session.ensure_meta(None)
    session.append_event("prompt", text="go")
    _, port = running([repo])
    status, view = _get(port, "/api/session/s-view")
    assert status == 200
    assert {r["id"] for r in view["active_rules"]} >= {"protect-migrations", "api-rate-limit"}
    assert set(view["review"]) == {"open", "resolved", "notes"}


def test_session_view_carries_full_rule_definitions(repo: Path, running):
    """The rule drawer needs every rule, disabled ones included, with its source."""
    (repo / ".bewit" / "rules" / "zz-off.yaml").write_text(
        "- id: old-check\n  kind: check\n  match: ['**/*.py']\n  command: ruff {files}\n"
        "  action: flag\n  timeout: 30\n  enabled: false\n",
        encoding="utf-8",
    )
    SessionStore(repo).session("s-defs").ensure_meta(None)
    _, port = running([repo])
    rules = {r["id"]: r for r in _get(port, "/api/session/s-defs")[1]["active_rules"]}
    off = rules["old-check"]
    assert off["enabled"] is False
    assert (off["command"], off["timeout"], off["source"]) == (
        "ruff {files}", 30, ".bewit/rules/zz-off.yaml")
    assert rules["protect-migrations"]["enabled"] is True
    assert rules["protect-migrations"]["targets"]
    assert rules["protect-migrations"]["timeout"] is None


def test_page_resolves_rule_chips_to_a_drawer():
    """Every place a rule id appears renders it as a chip that opens the drawer."""
    from bewit.report.server import _template

    page = _template()
    assert "function ruleChip(" in page and "data-rule=" in page
    assert "function drawerHtml(" in page and 'params.set("rule"' in page


def test_diff_never_serves_files_under_read_rules(repo: Path, running):
    from bewit.report.server import export_html

    (repo / ".bewit" / "rules" / "secrets.yaml").write_text(
        "- id: no-env\n  kind: read\n  match: ['.env']\n  action: deny\n", encoding="utf-8"
    )
    (repo / ".env").write_text("TOKEN=supersecret\n", encoding="utf-8")  # untracked
    session = SessionStore(repo).session("s-secret")
    session.ensure_meta(Git(repo).head_sha())
    session.append_event("edit", path=".env")
    _, port = running([repo])
    status, body = _get(port, "/api/diff?session=s-secret&path=.env")
    assert status == 200 and "supersecret" not in body["diff"]
    assert "no-env" in body["diff"]
    assert "supersecret" not in export_html(repo, "s-secret")


def test_template_escapes_quotes_for_attributes():
    from bewit.report.server import _template

    html = _template()
    assert '.replace(/"/g, "&quot;")' in html


def test_prompt_excerpt_hides_pasted_content_tags():
    text = '<pasted_content id="41a2">\nGuardrail demo, part 2.\nMore.\n</pasted_content id="41a2">'
    assert _user_prompt_excerpt([{"text": text}]).startswith("Guardrail demo, part 2.")
    assert _user_prompt_excerpt([{"text": '<pasted_content id="x"></pasted_content id="x">'}]) == ""
