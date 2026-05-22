"""
Verify an AIBox release manifest signature.

Parity reference for the C# verifier in the WPF First Run app. Reads
the manifest JSON, canonicalizes it (manifest_canonical.py), and
checks the ed25519 signature against the embedded public key.

Exit codes:
  0  signature valid
  1  signature invalid
  2  usage error / I/O error

Usage (pubkey from file):
  python verify-manifest.py \\
      --manifest aibox/installer/manifests/manifest-0.0.1.json \\
      --sig      aibox/installer/manifests/manifest-0.0.1.json.sig \\
      --pubkey   aibox/installer/first-run/Resources/release-pubkey.ed25519

Usage (pubkey from env var — CI / GitHub Actions):
  python verify-manifest.py \\
      --manifest manifest-1.0.0.json \\
      --sig      manifest-1.0.0.json.sig \\
      --pubkey-b64-env AIBOX_MANIFEST_VERIFY_KEY
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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from manifest_canonical import canonical_bytes


def _normalize_pubkey_bytes(raw: bytes, source: str) -> bytes:
    """Mirror the C# NormalizeKey behavior: accept raw 32 bytes OR base64-encoded text.

    The embedded WPF resource file ships base64 text (with or without a
    trailing newline), so local devs verifying with --pubkey <embedded file>
    need the same normalization.
    """
    if len(raw) == 32:
        return raw
    try:
        decoded = base64.b64decode(raw.strip(), validate=False)
    except Exception:
        decoded = b""
    if len(decoded) == 32:
        return decoded
    print(
        f"ERROR: public key from {source} is {len(raw)} bytes — "
        "expected either raw 32 bytes or a base64-encoded 32-byte key.",
        file=sys.stderr,
    )
    sys.exit(2)


def load_pubkey(args: argparse.Namespace) -> Ed25519PublicKey:
    """Load the ed25519 public key from either a file or a base64 env var."""
    if args.pubkey is not None:
        try:
            pub_raw = args.pubkey.read_bytes()
        except OSError as exc:
            print(f"ERROR: cannot read pubkey file: {exc}", file=sys.stderr)
            sys.exit(2)
        pub_raw = _normalize_pubkey_bytes(pub_raw, str(args.pubkey))
    else:
        # --pubkey-b64-env was supplied
        b64 = os.environ.get(args.pubkey_b64_env)
        if not b64:
            print(
                f"ERROR: environment variable {args.pubkey_b64_env!r} "
                "is empty or unset.",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            pub_raw = base64.b64decode(b64.strip())
        except Exception as exc:
            print(
                f"ERROR: could not base64-decode {args.pubkey_b64_env!r}: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        if len(pub_raw) != 32:
            print(
                f"ERROR: env-var pubkey must decode to exactly 32 bytes, "
                f"got {len(pub_raw)} (from env:{args.pubkey_b64_env}).",
                file=sys.stderr,
            )
            sys.exit(2)

    return Ed25519PublicKey.from_public_bytes(pub_raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to the manifest JSON to verify.",
    )
    # --sig and --signature are accepted (--signature matches the workflow flag name).
    parser.add_argument(
        "--sig",
        "--signature",
        dest="sig",
        type=Path,
        required=True,
        help="Path to the detached 64-byte ed25519 signature file.",
    )
    # Mutually exclusive pubkey source: file path OR env-var holding base64.
    key_group = parser.add_mutually_exclusive_group(required=True)
    key_group.add_argument(
        "--pubkey",
        type=Path,
        help="Path to the raw 32-byte ed25519 public key file.",
    )
    key_group.add_argument(
        "--pubkey-b64-env",
        metavar="NAME",
        help="Name of the environment variable holding the base64-encoded "
        "32-byte ed25519 public key (typical CI input).",
    )
    args = parser.parse_args()

    # Load and canonicalize the manifest.
    try:
        manifest_text = args.manifest.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read manifest: {exc}", file=sys.stderr)
        return 2
    manifest_obj = json.loads(manifest_text)
    payload = canonical_bytes(manifest_obj)

    # Load the public key.
    public_key = load_pubkey(args)

    # Load the signature.
    try:
        signature = args.sig.read_bytes()
    except OSError as exc:
        print(f"ERROR: cannot read signature file: {exc}", file=sys.stderr)
        return 2
    if len(signature) != 64:
        print(
            f"ERROR: signature must be exactly 64 bytes, "
            f"got {len(signature)}.",
            file=sys.stderr,
        )
        return 2

    try:
        public_key.verify(signature, payload)
    except InvalidSignature:
        print(f"INVALID signature for {args.manifest}", file=sys.stderr)
        return 1

    print(f"OK: {args.manifest} ({len(payload):,} canonical bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
