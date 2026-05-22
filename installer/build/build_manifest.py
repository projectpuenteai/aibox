"""
Build a release manifest from a YAML description.

The YAML lists items with placeholder revisions and per-source hints; this
script resolves them against the live upstream (Hugging Face commit SHA,
Kiwix catalog dump date, etc.) and writes a canonicalized manifest JSON
ready to be signed.

Usage:
    python build_manifest.py --version 1.0.0 --output dist/manifest-1.0.0.json

Inputs:
    aibox/installer/manifests/release-config.yaml — curated content list

Outputs:
    <output>            canonical manifest JSON
    (caller signs with sign-manifest.py afterward)

Flags:
    --compute-non-lfs-sha
        For non-LFS files in HF multi-file items (small configs, JSON, etc.),
        the HF tree API only returns a git SHA1, not a SHA256. When this flag
        is set, each non-LFS file is downloaded and hashed locally. A warning
        is printed for each such file. Files are cached in a temp dir so a
        second run (e.g. --dry-run) avoids re-downloading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow invocation from any CWD (e.g. the repo root in CI).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # PyYAML
import requests


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE.parent / "manifests" / "release-config.yaml"

# Temp dir used to cache downloaded non-LFS files across calls.
_NON_LFS_CACHE: dict[str, Path] = {}
_NON_LFS_TMPDIR: tempfile.TemporaryDirectory | None = None


def _get_tmpdir() -> Path:
    global _NON_LFS_TMPDIR
    if _NON_LFS_TMPDIR is None:
        _NON_LFS_TMPDIR = tempfile.TemporaryDirectory(prefix="aibox_manifest_")
    return Path(_NON_LFS_TMPDIR.name)


def resolve_hf_revision(repo: str, revision_hint: str) -> str:
    """Resolve 'main' (or any branch name) to a concrete commit SHA."""
    if len(revision_hint) == 40 and all(c in "0123456789abcdef" for c in revision_hint):
        return revision_hint
    resp = requests.get(
        f"https://huggingface.co/api/models/{repo}",
        params={"revision": revision_hint},
        timeout=20,
    )
    resp.raise_for_status()
    info = resp.json()
    sha = info.get("sha")
    if not sha:
        raise RuntimeError(f"Could not resolve {repo}@{revision_hint} to a commit SHA.")
    return sha


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_and_hash(url: str, cache_key: str) -> str:
    """Download URL, cache it in the temp dir, and return the SHA256 hex digest."""
    if cache_key in _NON_LFS_CACHE:
        cached = _NON_LFS_CACHE[cache_key]
        h = hashlib.sha256()
        with cached.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    tmpdir = _get_tmpdir()
    # Sanitize cache_key to a safe filename.
    safe_name = cache_key.replace("/", "_").replace(":", "_")
    dest = tmpdir / safe_name

    print(f"  WARNING: downloading non-LFS file for hashing: {url}", file=sys.stderr)
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    h = hashlib.sha256()
    with dest.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                h.update(chunk)
    _NON_LFS_CACHE[cache_key] = dest
    return h.hexdigest()


def list_hf_tree(
    repo: str,
    revision: str,
    compute_non_lfs_sha: bool = False,
) -> list[dict[str, Any]]:
    """List the file tree at a pinned commit; used to populate multi-file items.

    For LFS files: uses entry['lfs']['oid'] as the SHA256.
      (The HF tree API exposes 'lfs.oid', not 'lfs.sha256' — the field
       name is 'oid' but its value IS the SHA256 of the file content.)

    For non-LFS files: the top-level 'oid' is a git SHA1, not a content
      SHA256. When compute_non_lfs_sha=True, the file is downloaded and
      hashed locally. Otherwise sha256 is left as an empty string and a
      warning is printed.
    """
    files: list[dict[str, Any]] = []
    next_url: str | None = (
        f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true"
    )
    entries: list[dict[str, Any]] = []
    page = 0
    while next_url is not None and page < 100:
        resp = requests.get(next_url, timeout=30)
        resp.raise_for_status()
        entries.extend(resp.json())
        # HF pagination exposes Link: <url>; rel="next"
        link_hdr = resp.headers.get("Link", "")
        next_url = None
        for part in link_hdr.split(","):
            seg = part.strip()
            if 'rel="next"' in seg and "<" in seg and ">" in seg:
                next_url = seg[seg.index("<") + 1 : seg.index(">")]
                break
        page += 1
    if page == 100:
        raise RuntimeError(
            f"HF tree API for {repo}@{revision} returned more than 100 pages — refusing."
        )

    for entry in entries:
        if entry.get("type") != "file":
            continue

        path = entry["path"]
        size = int(entry.get("size", 0))
        lfs_block = entry.get("lfs")

        if lfs_block:
            # lfs.oid IS the SHA256 of the file content (not a git hash).
            sha256 = lfs_block.get("oid", "") or ""
        else:
            # Non-LFS: top-level 'oid' is git SHA1 — not useful as SHA256.
            sha256 = ""
            if compute_non_lfs_sha:
                dl_url = (
                    f"https://huggingface.co/{repo}/resolve/{revision}/{path}"
                )
                try:
                    sha256 = download_and_hash(dl_url, f"{repo}@{revision}/{path}")
                except Exception as exc:
                    print(
                        f"  WARNING: could not hash non-LFS file {path}: {exc}",
                        file=sys.stderr,
                    )
            else:
                print(
                    f"  WARNING: non-LFS file {path!r} in {repo} has no SHA256 "
                    "from the tree API. Pass --compute-non-lfs-sha to download "
                    "and hash it locally.",
                    file=sys.stderr,
                )

        files.append({
            "path": path,
            "size_bytes": size,
            "sha256": sha256,
        })

    return files


def fnmatch_any(name: str, patterns: list[str]) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def build_hf_single(item: dict[str, Any]) -> dict[str, Any]:
    revision = resolve_hf_revision(item["repo"], item.get("revision", "main"))
    out = {
        "id": item["id"],
        "source": "huggingface",
        "repo": item["repo"],
        "revision": revision,
        "path_in_repo": item["path_in_repo"],
        "target": item["target"],
        "size_bytes": int(item.get("size_bytes", 0)),
        "sha256": item.get("sha256", ""),
    }
    return out


def build_hf_multi(
    item: dict[str, Any], compute_non_lfs_sha: bool = False
) -> dict[str, Any]:
    revision = resolve_hf_revision(item["repo"], item.get("revision", "main"))
    include = item.get("include", ["**"])
    omit = item.get("omit", [])
    tree = list_hf_tree(item["repo"], revision, compute_non_lfs_sha)
    files = [
        f for f in tree
        if fnmatch_any(f["path"], include) and not fnmatch_any(f["path"], omit)
    ]
    sha_lines = sorted(f"{f['sha256']}  {f['path']}" for f in files if f["sha256"])
    bundle_sha = hashlib.sha256(("\n".join(sha_lines) + "\n").encode("utf-8")).hexdigest()
    return {
        "id": item["id"],
        "source": "huggingface",
        "repo": item["repo"],
        "revision": revision,
        "include": include,
        "target_dir": item["target_dir"],
        "files": files,
        "size_bytes_total": sum(f["size_bytes"] for f in files),
        "sha256_manifest": bundle_sha,
    }


def build_kiwix(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": item["id"],
        "source": "kiwix",
        "catalog_query": item.get("catalog_query", {}),
        "fallback_url": item.get("fallback_url", ""),
        "sha256_url": item.get("sha256_url", ""),
        "target": item["target"],
        "size_bytes": int(item.get("size_bytes", 0)),
    }
    return out


def build_kolibri(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "source": "kolibri_channel",
        "studio_base_url": item.get("studio_base_url", "https://studio.learningequality.org"),
        "channel_id": item["channel_id"],
        "include_node_ids": item.get("include_node_ids"),
        "approx_size_bytes": int(item.get("approx_size_bytes", 0)),
    }


def build_r2(item: dict[str, Any], r2_base: str) -> dict[str, Any]:
    url = (
        item["url"]
        if item.get("url", "").startswith("http")
        else f"{r2_base.rstrip('/')}/{item['url'].lstrip('/')}"
    )
    return {
        "id": item["id"],
        "source": "r2",
        "url": url,
        "target": item["target"],
        "extract_to": item.get("extract_to"),
        "size_bytes": int(item.get("size_bytes", 0)),
        "sha256": item["sha256"],
    }


def build_items(
    config: dict[str, Any],
    r2_base: str,
    compute_non_lfs_sha: bool = False,
) -> Iterable[dict[str, Any]]:
    for item in config["items"]:
        src = item["source"]
        if src == "r2":
            yield build_r2(item, r2_base)
        elif src == "huggingface":
            if "target_dir" in item:
                yield build_hf_multi(item, compute_non_lfs_sha)
            else:
                yield build_hf_single(item)
        elif src == "kiwix":
            yield build_kiwix(item)
        elif src == "kolibri_channel":
            yield build_kolibri(item)
        else:
            raise RuntimeError(f"Unknown source '{src}' in release-config.yaml")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", required=True, help="Release version string (e.g. 1.0.0).")
    p.add_argument("--output", required=True, type=Path, help="Output manifest JSON path.")
    p.add_argument("--config", default=DEFAULT_CONFIG, type=Path,
                   help="release-config.yaml path (default: manifests/release-config.yaml).")
    p.add_argument(
        "--r2-base",
        default=os.environ.get("AIBOX_R2_BASE", "https://cdn.projectpuenteai.org/aibox"),
        help="Base URL for R2 items without an explicit http:// URL. "
             "Also reads AIBOX_R2_BASE env var.",
    )
    p.add_argument("--min-installer-version", default=None,
                   help="Minimum installer version that can read this manifest.")
    p.add_argument(
        "--compute-non-lfs-sha",
        action="store_true",
        help="Download non-LFS files from HF and compute their SHA256 locally. "
             "Without this flag, non-LFS files get an empty sha256 string and a "
             "warning is printed.",
    )
    args = p.parse_args()

    if not args.config.exists():
        print(f"error: release-config.yaml not found at {args.config}", file=sys.stderr)
        return 2

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    items = list(build_items(config, args.r2_base, args.compute_non_lfs_sha))

    manifest = {
        "schema_version": 2,
        "release": args.version,
        "min_installer_version": args.min_installer_version or args.version,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": config.get("notes", ""),
        "items": items,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys=True so on-disk diffs are stable; the canonical bytes used
    # for signing are always sorted (see manifest_canonical.py), so this
    # also makes the on-disk form match the signed payload more closely.
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.output} with {len(items)} items.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
