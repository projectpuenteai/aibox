"""
Generate the AIBox release ed25519 signing keypair.

Run this ONCE, ever, when bootstrapping the installer release pipeline.
The public key is committed to the repository and embedded in the WPF
First Run app's Resources/release-pubkey.ed25519 (32 raw bytes); the
private key is stored in GitHub Actions secrets as
AIBOX_MANIFEST_SIGNING_KEY (base64 of the 32-byte raw private seed).

After this script runs:
  1. Copy the printed AIBOX_MANIFEST_SIGNING_KEY value into the
     GitHub Actions repository secret.
  2. Commit the public key file (PUBLIC_OUT path) to the repo.
  3. Securely delete the local private-key file, or move it to a
     password manager / hardware token for backup.

If you run this twice, you ROTATE the signing key, which invalidates
every installer already shipped to users (they cannot verify new
manifests). Don't do that unless you intend to break compatibility.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-out",
        type=Path,
        required=True,
        help="Path for the 32-byte raw private key seed (binary).",
    )
    parser.add_argument(
        "--public-out",
        type=Path,
        required=True,
        help="Path for the 32-byte raw public key (binary). This is "
        "the file the WPF app embeds as Resources/release-pubkey.ed25519.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing key files. Without this flag, the "
        "script refuses to overwrite to prevent accidental rotation.",
    )
    args = parser.parse_args()

    for p in (args.private_out, args.public_out):
        if p.exists() and not args.force:
            print(
                f"ERROR: {p} already exists. Use --force to overwrite "
                "(but read the script docstring first — rotation "
                "breaks already-shipped installers).",
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

    assert len(raw_private) == 32, "ed25519 private seed must be 32 bytes"
    assert len(raw_public) == 32, "ed25519 public key must be 32 bytes"

    args.private_out.parent.mkdir(parents=True, exist_ok=True)
    args.public_out.parent.mkdir(parents=True, exist_ok=True)
    args.private_out.write_bytes(raw_private)
    args.public_out.write_bytes(raw_public)

    if os.name == "posix":
        os.chmod(args.private_out, 0o600)

    private_b64 = base64.b64encode(raw_private).decode("ascii")
    public_b64 = base64.b64encode(raw_public).decode("ascii")

    print("AIBox release ed25519 keypair generated.")
    print()
    print(f"Private key file: {args.private_out}")
    print(f"Public key file:  {args.public_out}")
    print()
    print("GitHub Actions secret (AIBOX_MANIFEST_SIGNING_KEY):")
    print(f"  {private_b64}")
    print()
    print("Public key (base64, for reference only — the WPF app reads")
    print("the raw bytes from the embedded resource, not this string):")
    print(f"  {public_b64}")
    print()
    print("NEXT STEPS:")
    print("  1. Set the GitHub Actions secret AIBOX_MANIFEST_SIGNING_KEY")
    print("     to the base64 value above.")
    print("  2. git add the public key file and commit it.")
    print("  3. Move or delete the private key file — do NOT commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
