"""
Canonical encoding for AIBox release manifests.

The bytes we sign and the bytes we verify MUST be identical regardless
of the platform that built or read the manifest. We pick a strict
subset of JSON encoding so a hand-written C# verifier in the WPF app
can match it byte-for-byte:

  * UTF-8, no BOM
  * Sorted object keys at every depth
  * Separators (',', ':') with no extra spaces
  * No trailing whitespace
  * Unicode characters NOT escaped (ensure_ascii=False) — we ship
    Spanish content names; escaping them would diverge from any
    reasonable C# JsonSerializer default
  * No trailing newline

The signature itself is detached: the manifest file on disk is the
human-readable form (which may have trailing newlines or different
key order if hand-edited), and the .sig file authenticates the
re-canonicalized bytes. The C# verifier does the same canonicalize
step before checking the signature.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_bytes(manifest: Any) -> bytes:
    """Return the canonical UTF-8 bytes that get signed and verified.

    Input is any JSON-serializable structure (typically the dict that
    came out of json.load on a manifest file).
    """
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
