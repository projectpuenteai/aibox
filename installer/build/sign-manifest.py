"""
Sign an AIBox release manifest with the project ed25519 private key.

Reads the manifest, canonicalizes it (see manifest_canonical.py),
ed25519-signs the canonical bytes, and writes a detached signature
file next to it (manifest-<v>.json.sig — 64 raw bytes).

The private key input can be either:
  * a path to the 32-byte raw private seed file (binary), or
  * a base64-encoded 32-byte seed read from --key-base64-env (the
    GitHub Actions secret form).

Usage (local with key file):
  python sign-manifest.py \\
      --manifest aibox/installer/manifests/manifest-0.0.1.json \\
      --key-file aibox/installer/build/.secrets/release.ed25519.sk

Usage (CI with env var):
  python sign-manifest.py \\
      --manifest manifest-1.0.0.json \\
      --key-base64-env AIBOX_MANIFEST_SIGNING_KEY

  # --privkey-env is an alias for --key-base64-env (backward compat):
  python sign-manifest.py \\
      --manifest manifest-1.0.0.json \\
      --privkey-env AIBOX_MANIFEST_SIGNING_KEY
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

# Allow invocation from any CWD (e.g. the repo root in CI).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from manifest_canonical import canonical_bytes


def load_key(args: argparse.Namespace) -> Ed25519PrivateKey:
    if args.key_file:
        raw = Path(args.key_file).read_bytes()
    elif args.key_base64_env:
        b64 = os.environ.get(args.key_base64_env)
        if not b64:
            print(
                f"ERROR: environment variable {args.key_base64_env} "
                "is empty or unset.",
                file=sys.stderr,
            )
            sys.exit(2)
        raw = base64.b64decode(b64)
    else:
        print(
            "ERROR: must pass either --key-file or --key-base64-env.",
            file=sys.stderr,
        )
        sys.exit(2)

    if len(raw) != 32:
        print(
            f"ERROR: private key seed must be exactly 32 bytes, "
            f"got {len(raw)}.",
            file=sys.stderr,
        )
        sys.exit(2)

    return Ed25519PrivateKey.from_private_bytes(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to the manifest JSON to sign.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--key-file",
        type=Path,
        help="Path to the raw 32-byte ed25519 private seed.",
    )
    group.add_argument(
        "--key-base64-env",
        help="Environment variable name holding the base64-encoded "
        "32-byte private seed (typical CI input).",
    )
    # --privkey-env is a backward-compatible alias for --key-base64-env so
    # older workflow invocations continue to work without changes.
    group.add_argument(
        "--privkey-env",
        dest="key_base64_env",
        help="Alias for --key-base64-env (backward-compatible).",
    )
    parser.add_argument(
        "--sig-out",
        type=Path,
        default=None,
        help="Output path for the signature (defaults to manifest + '.sig').",
    )
    args = parser.parse_args()

    manifest_obj = json.loads(args.manifest.read_text(encoding="utf-8"))
    payload = canonical_bytes(manifest_obj)

    private_key = load_key(args)
    signature = private_key.sign(payload)
    assert len(signature) == 64, "ed25519 signature must be 64 bytes"

    sig_path = args.sig_out or args.manifest.with_suffix(
        args.manifest.suffix + ".sig"
    )
    sig_path.write_bytes(signature)

    print(f"Signed {args.manifest}")
    print(f"  canonical payload: {len(payload):,} bytes")
    print(f"  signature:         {sig_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
