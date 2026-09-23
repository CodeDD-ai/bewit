"""Sealed prompts: ciphertext at rest, plaintext only with the local key."""

import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from archrev.hooks import run_hook
from archrev.seal import (
    SealUnavailable,
    add_recipient,
    generate_keypair,
    open_sealed,
    seal_text,
)
from archrev.storage import SessionStore


@pytest.fixture()
def seal_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("archrev.seal.KEY_PATH", tmp_path / "seal.key")
    # handle_prompt imports seal_text from the module; KEY_PATH is read
    # inside generate/load via the module global, so patching the attribute
    # the functions close over is the module global itself.
    return tmp_path / "seal.key"


def test_seal_roundtrip_hides_plaintext(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("archrev.seal.KEY_PATH", tmp_path / "seal.key")
    public = generate_keypair(None)
    add_recipient(tmp_path, "dev", public)
    sealed = seal_text(tmp_path, "the deploy key is hunter2")
    blob = json.dumps(sealed)
    assert "hunter2" not in blob
    assert open_sealed(sealed, None) == "the deploy key is hunter2"


def test_passphrase_wraps_the_private_key(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("archrev.seal.KEY_PATH", tmp_path / "seal.key")
    public = generate_keypair("correct horse")
    add_recipient(tmp_path, "dev", public)
    sealed = seal_text(tmp_path, "secret")
    with pytest.raises(SealUnavailable):
        open_sealed(sealed, None)
    with pytest.raises(SealUnavailable):
        open_sealed(sealed, "wrong")
    assert open_sealed(sealed, "correct horse") == "secret"


def test_hook_seals_and_never_falls_back_to_plaintext(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("archrev.seal.KEY_PATH", repo / "seal.key")
    public = generate_keypair(None)
    add_recipient(repo, "dev", public)
    (repo / ".archrev" / "config.yaml").write_text(
        "prompt_capture: sealed\n", encoding="utf-8"
    )
    run_hook(
        "prompt",
        json.dumps({"conversation_id": "sealed-1", "prompt": "hunter2 please"}),
    )
    event = SessionStore(repo).session("sealed-1").events()[0]
    assert event["capture"] == "sealed"
    assert event["text"] == ""
    assert "hunter2" not in json.dumps(event)
    assert open_sealed(event["sealed"], None) == "hunter2 please"


def test_hook_without_recipients_stores_nothing(repo: Path, monkeypatch):
    monkeypatch.chdir(repo)
    (repo / ".archrev" / "config.yaml").write_text(
        "prompt_capture: sealed\n", encoding="utf-8"
    )
    run_hook(
        "prompt",
        json.dumps({"conversation_id": "sealed-2", "prompt": "hunter2"}),
    )
    event = SessionStore(repo).session("sealed-2").events()[0]
    assert event["capture"] == "none"
    assert event["text"] == ""
    assert "hunter2" not in json.dumps(event)
    assert "recipients" in event["seal_warning"]
