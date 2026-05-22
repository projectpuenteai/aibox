"""
Convenience wrapper around generate-keypair.py for local dev and CI dry-runs.

Generates an ed25519 keypair under build/.secrets/dev.ed25519.{sk,pk} and
prints the base64-encoded public key so you can paste it into
first-run/Resources/release-pubkey.ed25519 for local testing.

This keypair is ONLY for local testing — it must never be used to sign a
production release. The production key is generated once via generate-keypair.py
and stored in GitHub Actions secrets (AIBOX_MANIFEST_PRIVKEY).

Usage
-----
    python build/setup_dev_keypair.py [--force]

    --force   Overwrite an existing dev keypair. Without this flag the script
              exits with an error if .secrets/dev.ed25519.sk already exists.

Output
------
Writes:
    build/.secrets/dev.ed25519.sk   (raw 32-byte private key seed)
    build/.secrets/dev.ed25519.pk   (raw 32-byte public key)

Prints the base64-encoded public key to stdout so you can:
    1. Paste it into first-run/Resources/release-pubkey.ed25519
    2. Export it as AIBOX_MANIFEST_PUBKEY_B64 for local verify-manifest.py runs
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SECRETS_DIR = _SCRIPT_DIR / ".secrets"
_SK_PATH = _SECRETS_DIR / "dev.ed25519.sk"
_PK_PATH = _SECRETS_DIR / "dev.ed25519.pk"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a dev ed25519 keypair for local AIBox installer testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing dev keypair files.",
    )
    args = parser.parse_args()

    # Guard against accidental overwrite.
    if _SK_PATH.exists() and not args.force:
        print(
            f"ERROR: {_SK_PATH} already exists.\n"
            "Use --force to overwrite. The existing keypair will be discarded.",
            file=sys.stderr,
        )
        return 1

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        print(
            "ERROR: 'cryptography' package not installed.\n"
            "Run:  pip install cryptography",
            file=sys.stderr,
        )
        return 1

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    raw_private = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    assert len(raw_private) == 32
    assert len(raw_public) == 32

    _SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    _SK_PATH.write_bytes(raw_private)
    _PK_PATH.write_bytes(raw_public)

    # Restrict private key permissions on POSIX (no-op on Windows).
    if os.name == "posix":
        os.chmod(_SK_PATH, 0o600)

    privkey_b64 = base64.b64encode(raw_private).decode("ascii")
    pubkey_b64 = base64.b64encode(raw_public).decode("ascii")

    pubkey_resource = (
        _SCRIPT_DIR.parent / "first-run" / "Resources" / "release-pubkey.ed25519"
    )

    print("Dev ed25519 keypair generated (LOCAL TESTING ONLY — not for production).")
    print()
    print(f"  Private key : {_SK_PATH}")
    print(f"  Public key  : {_PK_PATH}")
    print()
    print("Base64 public key (paste into first-run/Resources/release-pubkey.ed25519):")
    print()
    print(f"  {pubkey_b64}")
    print()
    print("To install it automatically:")
    print(f"  Set-Content -NoNewline '{pubkey_resource}' '{pubkey_b64}'")
    print()
    print("Environment variable for local verify-manifest.py runs:")
    print(f"  $env:AIBOX_MANIFEST_PUBKEY_B64 = '{pubkey_b64}'")
    print()
    print("Environment variable for local sign-manifest.py runs:")
    print(f"  $env:AIBOX_MANIFEST_PRIVKEY    = '{privkey_b64}'")
    print()
    print("WARNING: build/.secrets/ is in .gitignore. Never commit these files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
