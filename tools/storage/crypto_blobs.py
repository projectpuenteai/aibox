"""Helpers for AIBox AES-GCM encrypted JSON blob files."""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from typing import Any

ENVELOPE_ALG = "AES-256-GCM"
ENVELOPE_VERSION = 1


def load_key(value: str) -> bytes:
    """Decode and validate a base64-encoded 32-byte AES key."""
    key = base64.b64decode((value or "").strip(), validate=True)
    if len(key) != 32:
        raise ValueError("key must decode to exactly 32 bytes")
    return key


def decrypt_blob(blob: bytes, key: bytes) -> bytes:
    """Decrypt one AIBox encrypted blob envelope."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    env: Any = json.loads(blob.decode("utf-8"))
    if not isinstance(env, dict):
        raise ValueError("encrypted blob envelope must be a JSON object")
    if env.get("v") != ENVELOPE_VERSION or env.get("alg") != ENVELOPE_ALG:
        raise ValueError("unsupported encrypted blob envelope")
    nonce = base64.b64decode(str(env.get("nonce", "")), validate=True)
    ciphertext = base64.b64decode(str(env.get("ciphertext", "")), validate=True)
    return AESGCM(key).decrypt(nonce, ciphertext, None)


def encrypt_blob(plain: bytes, key: bytes) -> bytes:
    """Encrypt bytes into the same envelope format used by app_storage.py."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plain, None)
    envelope = {
        "v": ENVELOPE_VERSION,
        "alg": ENVELOPE_ALG,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def looks_like_encrypted_blob(path: Path) -> bool:
    """Cheaply identify likely AIBox encrypted JSON blobs."""
    if not path.is_file() or path.suffix.lower() != ".json":
        return False
    try:
        with path.open("rb") as f:
            sample = f.read(256)
        env = json.loads(sample.decode("utf-8") + "}") if sample.rstrip().endswith(b",") else json.loads(sample.decode("utf-8"))
    except Exception:
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
    return isinstance(env, dict) and env.get("v") == ENVELOPE_VERSION and env.get("alg") == ENVELOPE_ALG


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Atomically replace a file and fsync the file plus containing directory when possible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{secrets.token_hex(8)}")
    try:
        with tmp_path.open("wb") as f:
            f.write(content)
            f.flush()
            try:
                import os

                os.fsync(f.fileno())
            except OSError:
                pass
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
