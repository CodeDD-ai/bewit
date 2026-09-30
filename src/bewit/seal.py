"""Prompt sealing: encrypt prompt text to committed public keys.

The private key never enters the repository. It lives in
``~/.bewit/seal.key`` (optionally wrapped with a passphrase). Recipients'
public keys live in ``.bewit/recipients.yaml`` so a team can see *who*
can read prompts — including an optional org escrow key — without seeing
the prompts.

Envelope (age-style, not age-compatible):

- a random AES-256 content key encrypts the prompt (AESGCM);
- each recipient gets that key wrapped by X25519 + HKDF.

Decryption happens only with the operator's consent (CLI prompt or the
localhost viewer). Plaintext is never written back to the session log.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import yaml

from bewit.config import bewit_dir

KEY_PATH = Path.home() / ".bewit" / "seal.key"
_INFO = b"bewit-seal-v1"
_AAD = b"bewit-prompt"
_PBKDF2_ROUNDS = 200_000


class SealUnavailable(Exception):
    """Sealing cannot proceed; callers must not store plaintext instead."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _ub64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))


def _crypto():
    """Import cryptography lazily so Bewit runs without the extra."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as exc:
        raise SealUnavailable(
            "prompt_capture is 'sealed' but the cryptography package is "
            "not installed (pip install 'bewit[seal]')"
        ) from exc
    return {
        "hashes": hashes,
        "serialization": serialization,
        "X25519PrivateKey": X25519PrivateKey,
        "X25519PublicKey": X25519PublicKey,
        "AESGCM": AESGCM,
        "HKDF": HKDF,
        "PBKDF2HMAC": PBKDF2HMAC,
    }


def _wrap_key(crypto: dict, shared: bytes) -> bytes:
    return crypto["HKDF"](
        algorithm=crypto["hashes"].SHA256(),
        length=32,
        salt=None,
        info=_INFO,
    ).derive(shared)


def _pub_raw(crypto: dict, private) -> bytes:
    return private.public_key().public_bytes(
        encoding=crypto["serialization"].Encoding.Raw,
        format=crypto["serialization"].PublicFormat.Raw,
    )


def generate_keypair(passphrase: str | None = None) -> str:
    """Write ``~/.bewit/seal.key`` and return the public key (base64)."""
    crypto = _crypto()
    private = crypto["X25519PrivateKey"].generate()
    raw = private.private_bytes(
        encoding=crypto["serialization"].Encoding.Raw,
        format=crypto["serialization"].PrivateFormat.Raw,
        encryption_algorithm=crypto["serialization"].NoEncryption(),
    )
    public = _pub_raw(crypto, private)
    blob: dict = {"v": 1, "public_key": _b64(public)}
    if passphrase:
        salt = os.urandom(16)
        derived = crypto["PBKDF2HMAC"](
            algorithm=crypto["hashes"].SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ROUNDS,
        ).derive(passphrase.encode("utf-8"))
        nonce = os.urandom(12)
        blob["salt"] = _b64(salt)
        blob["nonce"] = _b64(nonce)
        blob["private_key_sealed"] = _b64(
            crypto["AESGCM"](derived).encrypt(nonce, raw, None)
        )
    else:
        blob["private_key"] = _b64(raw)
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    try:
        KEY_PATH.chmod(0o600)
    except OSError:
        pass
    return blob["public_key"]


def load_private_key(passphrase: str | None):
    """Load the local private key. Raises ``SealUnavailable`` on failure."""
    crypto = _crypto()
    try:
        blob = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SealUnavailable(
            f"No usable seal key at {KEY_PATH}. Run `bewit seal keygen`."
        ) from exc
    try:
        if blob.get("private_key"):
            raw = _ub64(blob["private_key"])
        else:
            if not passphrase:
                raise SealUnavailable("This seal key is passphrase-protected.")
            derived = crypto["PBKDF2HMAC"](
                algorithm=crypto["hashes"].SHA256(),
                length=32,
                salt=_ub64(blob["salt"]),
                iterations=_PBKDF2_ROUNDS,
            ).derive(passphrase.encode("utf-8"))
            raw = crypto["AESGCM"](derived).decrypt(
                _ub64(blob["nonce"]), _ub64(blob["private_key_sealed"]), None
            )
    except SealUnavailable:
        raise
    except Exception as exc:  # wrong passphrase or corrupt file
        raise SealUnavailable(
            "Could not open the seal key (wrong passphrase, or the key file "
            "is corrupt)."
        ) from exc
    return crypto["X25519PrivateKey"].from_private_bytes(raw)


def recipients_path(root: Path) -> Path:
    return bewit_dir(root) / "recipients.yaml"


def load_recipients(root: Path) -> list[dict]:
    path = recipients_path(root)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("recipients") or []
    return [item for item in items if isinstance(item, dict) and item.get("public_key")]


def add_recipient(root: Path, name: str, public_key: str) -> None:
    """Commit a public key into ``.bewit/recipients.yaml`` (idempotent)."""
    path = recipients_path(root)
    existing = load_recipients(root)
    if any(item.get("public_key") == public_key for item in existing):
        return
    existing.append({"name": name, "public_key": public_key})
    body = yaml.safe_dump(
        {"recipients": existing}, sort_keys=False, allow_unicode=True
    )
    header = (
        "# Public keys allowed to unseal prompts in this repository.\n"
        "# Private keys stay in ~/.bewit/seal.key and are never committed.\n"
        "# Add an org escrow key here only as an explicit governance decision.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body, encoding="utf-8", newline="\n")


def seal_text(root: Path, plaintext: str) -> dict:
    """Encrypt ``plaintext`` to every recipient. Never returns plaintext."""
    crypto = _crypto()
    recipients = load_recipients(root)
    if not recipients:
        raise SealUnavailable(
            "prompt_capture is 'sealed' but .bewit/recipients.yaml has no "
            "public keys. Run `bewit seal keygen`."
        )
    content_key = os.urandom(32)
    nonce = os.urandom(12)
    ciphertext = crypto["AESGCM"](content_key).encrypt(
        nonce, plaintext.encode("utf-8"), _AAD
    )
    wraps = []
    for item in recipients:
        try:
            pub_raw = _ub64(str(item["public_key"]))
            public = crypto["X25519PublicKey"].from_public_bytes(pub_raw)
        except Exception as exc:
            raise SealUnavailable(
                f"Recipient {item.get('name')!r} has an invalid public key."
            ) from exc
        ephemeral = crypto["X25519PrivateKey"].generate()
        shared = ephemeral.exchange(public)
        wrap_key = _wrap_key(crypto, shared)
        wrap_nonce = os.urandom(12)
        wraps.append(
            {
                "name": str(item.get("name") or ""),
                "recipient": str(item["public_key"]),
                "ephemeral_public": _b64(_pub_raw(crypto, ephemeral)),
                "wrap_nonce": _b64(wrap_nonce),
                "wrapped_key": _b64(
                    crypto["AESGCM"](wrap_key).encrypt(wrap_nonce, content_key, pub_raw)
                ),
            }
        )
    return {
        "v": 1,
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
        "recipients": wraps,
    }


def open_sealed(sealed: dict, passphrase: str | None) -> str:
    """Decrypt one sealed prompt with the local private key."""
    crypto = _crypto()
    private = load_private_key(passphrase or None)
    public = _b64(_pub_raw(crypto, private))
    entry = next(
        (item for item in sealed.get("recipients") or [] if item.get("recipient") == public),
        None,
    )
    if entry is None:
        raise SealUnavailable(
            "The local seal key is not a recipient of this prompt."
        )
    ephemeral = crypto["X25519PublicKey"].from_public_bytes(
        _ub64(entry["ephemeral_public"])
    )
    shared = private.exchange(ephemeral)
    wrap_key = _wrap_key(crypto, shared)
    try:
        content_key = crypto["AESGCM"](wrap_key).decrypt(
            _ub64(entry["wrap_nonce"]),
            _ub64(entry["wrapped_key"]),
            _ub64(entry["recipient"]),
        )
        plaintext = crypto["AESGCM"](content_key).decrypt(
            _ub64(sealed["nonce"]), _ub64(sealed["ciphertext"]), _AAD
        )
    except Exception as exc:
        raise SealUnavailable("Could not unseal this prompt.") from exc
    return plaintext.decode("utf-8")
