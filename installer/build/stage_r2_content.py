"""
Stage AIBox content to Cloudflare R2.

Two operating modes (can combine --upload-shards with --upload-manifest in
one invocation, but they are independent):

--- MODE 1: Shard upload ---
Upload pre-built *.tar.zst shard files from a local directory to R2.

  python stage_r2_content.py \\
      --upload-shards ./dist/shards/ \\
      --target-prefix aibox/chroma/v1

Idempotent: shards whose key already exists in R2 with matching content-length
are skipped. Skipped uploads are logged.

--- MODE 2: Manifest + signature mirror ---
Upload a signed manifest JSON and its .sig to R2, then optionally write
a latest.json pointer.

  python stage_r2_content.py \\
      --upload-manifest dist/manifest-1.0.0.json \\
      --upload-manifest-sig dist/manifest-1.0.0.json.sig \\
      --update-latest

  The manifest is uploaded to:
    s3://<bucket>/aibox/manifest-<release>.json   (versioned copy)
    s3://<bucket>/aibox/manifest-latest.json      (convenience alias)

  And --update-latest writes:
    s3://<bucket>/aibox/latest.json   {"release": "1.0.0", "manifest_url": "..."}

--- LEGACY: Streaming shard builder ---
The original streaming tar|zstd->R2 pipeline is still available via --source.
Use --upload-shards for already-built shards (faster, CI-friendly).

Environment variables (all required):
  AIBOX_R2_ACCOUNT_ID       Cloudflare account ID
  AIBOX_R2_ACCESS_KEY_ID    R2 API token access key
  AIBOX_R2_SECRET_ACCESS_KEY R2 API token secret
  AIBOX_R2_BUCKET           Bucket name (e.g. puentechromadb)

Optional:
  AIBOX_R2_ENDPOINT_URL     Override https://<account_id>.r2.cloudflarestorage.com
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError
except ImportError as exc:
    print(
        f"ERROR: missing dependency '{exc.name}'. Install with:\n"
        "  pip install boto3",
        file=sys.stderr,
    )
    sys.exit(2)

# zstandard is only needed for the legacy streaming mode.
_zstd = None


def _require_zstd():
    global _zstd
    if _zstd is None:
        try:
            import zstandard
            _zstd = zstandard
        except ImportError:
            print(
                "ERROR: 'zstandard' is required for --source (streaming mode). "
                "Install with: pip install zstandard",
                file=sys.stderr,
            )
            sys.exit(2)
    return _zstd


DEFAULT_SHARD_SIZE = 4 * 1024 * 1024 * 1024  # 4 GiB
DEFAULT_ZSTD_LEVEL = 9
DEFAULT_BUF_SIZE = 4 * 1024 * 1024  # 4 MiB writes through the pipeline


@dataclass
class ShardRecord:
    index: int
    key: str
    size_bytes: int
    sha256: str
    uploaded: bool


def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


def read_env(name: str, required: bool = True) -> str | None:
    val = os.environ.get(name)
    if required and not val:
        print(f"ERROR: env var {name} is unset.", file=sys.stderr)
        sys.exit(2)
    return val


def make_r2_client() -> tuple[object, str]:
    """Build a boto3 S3 client pointed at R2 and return (client, bucket)."""
    account_id = read_env("AIBOX_R2_ACCOUNT_ID")
    access_key = read_env("AIBOX_R2_ACCESS_KEY_ID")
    secret_key = read_env("AIBOX_R2_SECRET_ACCESS_KEY")
    bucket = read_env("AIBOX_R2_BUCKET")
    endpoint = os.environ.get("AIBOX_R2_ENDPOINT_URL") or (
        f"https://{account_id}.r2.cloudflarestorage.com"
    )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    # Probe: head the bucket once to fail fast on bad credentials.
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as e:
        print(
            f"ERROR: cannot access bucket {bucket!r} at {endpoint}: "
            f"{e.response['Error']['Code']} {e.response['Error']['Message']}",
            file=sys.stderr,
        )
        sys.exit(2)
    return client, bucket


# ─── Idempotent single-object upload ─────────────────────────────────────────

def upload_object(
    client,
    bucket: str,
    key: str,
    local_path: Path,
    content_type: str = "application/octet-stream",
    extra_metadata: dict | None = None,
) -> None:
    """Upload a local file to R2.

    Idempotent: if an object with the same key and content-length already
    exists it is skipped.  Logs the outcome either way.
    """
    size = local_path.stat().st_size
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] == size:
            print(f"  skip (already in bucket, size matches): {key}")
            return
        print(
            f"  re-uploading (size mismatch: "
            f"{head['ContentLength']} vs {size}): {key}"
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchKey", "NotFound"):
            raise

    extra: dict = {"ContentType": content_type}
    if extra_metadata:
        extra["Metadata"] = extra_metadata

    start = time.time()
    client.upload_file(
        Filename=str(local_path),
        Bucket=bucket,
        Key=key,
        ExtraArgs=extra,
    )
    elapsed = max(time.time() - start, 0.001)
    rate = size / elapsed / (1024 * 1024)
    print(f"  uploaded {key} ({human_bytes(size)}, {rate:.1f} MB/s)")


def put_json_object(client, bucket: str, key: str, obj: dict) -> None:
    """PUT a JSON dict directly (no local file needed).

    Uses compact canonical JSON (sorted keys, no whitespace) so the
    object is byte-stable and reproducible across runs. latest.json is
    not on the signed trust path today, but a stable representation
    avoids spurious diffs and is one less surprise if anyone later
    decides to attach a SHA256 to it.
    """
    body = json.dumps(
        obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    print(f"  wrote {key} ({len(body)} bytes)")


# ─── Mode 1: upload pre-built shards ─────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_shards(args: argparse.Namespace, client, bucket: str) -> list[dict]:
    """Upload every *.tar.zst in --upload-shards dir to --target-prefix."""
    shard_dir = args.upload_shards.resolve()
    if not shard_dir.is_dir():
        print(
            f"ERROR: --upload-shards {shard_dir} is not a directory.",
            file=sys.stderr,
        )
        sys.exit(2)

    prefix = args.target_prefix.rstrip("/")
    shard_files = sorted(shard_dir.glob("*.tar.zst"))
    if not shard_files:
        print(f"WARNING: no *.tar.zst files found in {shard_dir}", file=sys.stderr)
        return []

    print(f"Uploading {len(shard_files)} shard(s) from {shard_dir}")
    print(f"Target prefix: s3://{bucket}/{prefix}/")
    print()

    records = []
    for path in shard_files:
        key = f"{prefix}/{path.name}"
        digest = sha256_file(path)
        upload_object(
            client,
            bucket,
            key,
            path,
            content_type="application/zstd",
            extra_metadata={"sha256": digest},
        )
        records.append({
            "key": key,
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": digest,
        })

    return records


# ─── Mode 2: manifest + sig mirror ───────────────────────────────────────────

def _release_from_manifest(manifest_path: Path) -> str:
    """Extract the release version from the manifest JSON or filename.

    Tries the JSON 'release' field first; falls back to the filename stem
    after stripping a leading 'manifest-' prefix (e.g. manifest-1.0.0.json
    → 1.0.0).
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        release = data.get("release", "")
        if release:
            return str(release)
    except Exception:
        pass
    # Fallback: strip "manifest-" prefix and extension.
    stem = manifest_path.stem  # e.g. "manifest-1.0.0"
    if stem.startswith("manifest-"):
        return stem[len("manifest-"):]
    return stem


def upload_manifest(args: argparse.Namespace, client, bucket: str) -> str:
    """Upload manifest JSON (and optionally its .sig) to R2.

    Returns the versioned manifest key so the caller can build latest.json.
    """
    manifest_path: Path = args.upload_manifest.resolve()
    release = _release_from_manifest(manifest_path)
    if not release:
        print(
            "ERROR: could not determine release version from manifest.",
            file=sys.stderr,
        )
        sys.exit(2)

    versioned_key = f"aibox/manifest-{release}.json"
    latest_key = "aibox/manifest-latest.json"

    print(f"Uploading manifest (release={release})")
    upload_object(client, bucket, versioned_key, manifest_path, "application/json")
    upload_object(client, bucket, latest_key, manifest_path, "application/json")

    if args.upload_manifest_sig is not None:
        sig_path: Path = args.upload_manifest_sig.resolve()
        upload_object(
            client,
            bucket,
            versioned_key + ".sig",
            sig_path,
            "application/octet-stream",
        )
        upload_object(
            client,
            bucket,
            latest_key + ".sig",
            sig_path,
            "application/octet-stream",
        )

    return versioned_key


def write_latest_pointer(client, bucket: str, release: str, manifest_key: str) -> None:
    """Write aibox/latest.json pointing at the current release."""
    # Build a public URL by convention; callers can override by inspecting the key.
    latest_obj = {
        "release": release,
        "manifest_key": manifest_key,
    }
    put_json_object(client, bucket, "aibox/latest.json", latest_obj)


# ─── Legacy streaming mode (--source) ────────────────────────────────────────

class ChunkedZstdSink:
    """
    Streaming sink. Receives compressed bytes from the zstd encoder and
    spills them to disk in chunks of exactly --shard-size each (last
    chunk may be smaller). Each chunk file is closed, sha256-finalized,
    and yielded so the caller can upload it before the next chunk is
    written. This caps peak local disk usage at ~one shard.
    """

    def __init__(self, workdir: Path, prefix: str, shard_size: int):
        self.workdir = workdir
        self.prefix = prefix
        self.shard_size = shard_size
        self._idx = 0
        self._fp: io.BufferedWriter | None = None
        self._path: Path | None = None
        self._h = None
        self._written = 0
        self._completed: list[tuple[int, Path, int, str]] = []

    def _open_next(self) -> None:
        self._idx += 1
        name = f"{self.prefix}{self._idx:02d}.tar.zst"
        self._path = self.workdir / name
        self._fp = self._path.open("wb")
        self._h = hashlib.sha256()
        self._written = 0

    def _close_current(self) -> tuple[int, Path, int, str]:
        assert self._fp and self._path and self._h is not None
        self._fp.flush()
        os.fsync(self._fp.fileno())
        self._fp.close()
        record = (self._idx, self._path, self._written, self._h.hexdigest())
        self._completed.append(record)
        self._fp = None
        self._path = None
        self._h = None
        self._written = 0
        return record

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        if self._fp is None:
            self._open_next()
        view = memoryview(data)
        remaining = len(view)
        while remaining:
            assert self._fp and self._h is not None
            room = self.shard_size - self._written
            if room <= 0:
                yield_record = self._close_current()
                self._open_next()
                room = self.shard_size
                _ = yield_record  # suppress flake8 unused
            take = min(room, remaining)
            chunk = view[:take]
            self._fp.write(chunk)
            self._h.update(chunk)
            self._written += take
            view = view[take:]
            remaining -= take
        return len(data)

    def finish(self) -> None:
        if self._fp is not None:
            self._close_current()

    def completed_shards(self) -> Iterator[tuple[int, Path, int, str]]:
        """Yield completed shard records and clear them."""
        while self._completed:
            yield self._completed.pop(0)


def iter_source_files(source: Path) -> Iterator[Path]:
    """Deterministic file enumeration: sorted by relative posix path."""
    files: list[Path] = []
    for root, dirs, names in os.walk(source):
        dirs.sort()
        for n in sorted(names):
            files.append(Path(root) / n)
    yield from files


def stream_into_sink(
    source: Path, sink: ChunkedZstdSink, zstd_level: int, on_progress
) -> int:
    """Streaming tar -> zstd -> sink. Returns total uncompressed bytes."""
    zstd = _require_zstd()
    cctx = zstd.ZstdCompressor(level=zstd_level, threads=-1)

    class _SinkAdapter(io.RawIOBase):
        def writable(self) -> bool:
            return True

        def write(self, b):  # type: ignore[override]
            sink.write(bytes(b))
            return len(b)

    raw_total = 0

    with cctx.stream_writer(_SinkAdapter(), closefd=False) as zwriter:
        with tarfile.open(fileobj=zwriter, mode="w|", bufsize=DEFAULT_BUF_SIZE) as tar:
            for f in iter_source_files(source):
                arcname = f.relative_to(source.parent).as_posix()
                info = tar.gettarinfo(name=str(f), arcname=arcname)
                if info is None:
                    continue
                if info.isfile():
                    with f.open("rb") as fp:
                        tar.addfile(info, fileobj=fp)
                    raw_total += info.size
                    on_progress(raw_total, arcname)
                else:
                    tar.addfile(info)

    sink.finish()
    return raw_total


def upload_shard_legacy(
    client, bucket: str, key: str, path: Path, size: int, sha256: str
) -> None:
    """Upload a single streaming shard; idempotent by size check."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] == size:
            print(f"  skip (already in bucket, size matches): {key}")
            return
        print(
            f"  re-uploading (size mismatch: {head['ContentLength']} vs {size}): {key}"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            raise

    start = time.time()
    client.upload_file(
        Filename=str(path),
        Bucket=bucket,
        Key=key,
        ExtraArgs={
            "Metadata": {"sha256": sha256},
            "ContentType": "application/zstd",
        },
    )
    elapsed = max(time.time() - start, 0.001)
    rate = size / elapsed / (1024 * 1024)
    print(f"  uploaded {key} ({human_bytes(size)}, {rate:.1f} MB/s)")


def run_legacy_stream(args: argparse.Namespace, client, bucket: str) -> None:
    """Original streaming tar|zstd pipeline (--source mode)."""
    source = args.source.resolve()
    if not source.is_dir():
        print(f"ERROR: --source {source} is not a directory.", file=sys.stderr)
        sys.exit(2)

    workdir = (args.workdir or source.parent / "_r2_staging").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    receipt_path = (args.receipt or workdir / "staging-receipt.json").resolve()

    print(f"source:       {source}")
    print(f"workdir:      {workdir}")
    print(f"bucket:       {bucket}")
    print(f"key prefix:   {args.prefix}")
    print(f"shard size:   {human_bytes(args.shard_size)}")
    print(f"zstd level:   {args.zstd_level}")
    print(f"dry run:      {args.dry_run}")
    print()

    sink = ChunkedZstdSink(workdir, args.shard_prefix, args.shard_size)
    shards: list[ShardRecord] = []
    last_log = time.time()

    def on_progress(raw_total: int, name: str) -> None:
        nonlocal last_log
        now = time.time()
        if now - last_log >= 5:
            print(f"  ... {human_bytes(raw_total)} read, current: {name[-60:]}")
            last_log = now
        for idx, path, size, digest in sink.completed_shards():
            key = f"{args.prefix.rstrip('/')}/{path.name}"
            if not args.dry_run:
                upload_shard_legacy(client, bucket, key, path, size, digest)
                if not args.keep_shards:
                    path.unlink()
            shards.append(
                ShardRecord(idx, key, size, digest, uploaded=not args.dry_run)
            )

    print("Streaming tar | zstd into shards ...")
    t0 = time.time()
    raw_total = stream_into_sink(source, sink, args.zstd_level, on_progress)

    # Drain final shard(s).
    for idx, path, size, digest in sink.completed_shards():
        key = f"{args.prefix.rstrip('/')}/{path.name}"
        if not args.dry_run:
            upload_shard_legacy(client, bucket, key, path, size, digest)
            if not args.keep_shards:
                path.unlink()
        shards.append(
            ShardRecord(idx, key, size, digest, uploaded=not args.dry_run)
        )

    elapsed = time.time() - t0
    total_compressed = sum(s.size_bytes for s in shards)
    ratio = total_compressed / raw_total if raw_total else 0.0
    print()
    print(f"Source uncompressed: {human_bytes(raw_total)}")
    print(f"Total compressed:    {human_bytes(total_compressed)}")
    print(f"Ratio:               {ratio:.2%}")
    print(f"Shards:              {len(shards)}")
    print(f"Elapsed:             {elapsed:.1f}s "
          f"({(raw_total / max(elapsed, 0.001)) / (1024*1024):.1f} MB/s raw)")

    receipt = {
        "source": str(source),
        "bucket": bucket,
        "prefix": args.prefix,
        "shard_prefix": args.shard_prefix,
        "shard_size_bytes": args.shard_size,
        "zstd_level": args.zstd_level,
        "raw_bytes": raw_total,
        "total_compressed_bytes": total_compressed,
        "elapsed_seconds": round(elapsed, 1),
        "shards": [asdict(s) for s in shards],
    }
    receipt_path.write_text(json.dumps(receipt, indent=2))
    print(f"Receipt written to:  {receipt_path}")


# ─── Argument parser ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Mode 1: upload pre-built shards ──────────────────────────────────────
    shards_g = parser.add_argument_group("Shard upload (--upload-shards)")
    shards_g.add_argument(
        "--upload-shards",
        type=Path,
        metavar="DIR",
        help="Directory of *.tar.zst shard files to upload.",
    )
    shards_g.add_argument(
        "--target-prefix",
        metavar="PREFIX",
        default="aibox/chroma/v1",
        help="R2 key prefix for shard uploads (default: aibox/chroma/v1).",
    )

    # ── Mode 2: manifest mirror ───────────────────────────────────────────────
    mf_g = parser.add_argument_group("Manifest upload (--upload-manifest)")
    mf_g.add_argument(
        "--upload-manifest",
        type=Path,
        metavar="PATH",
        help="Manifest JSON to upload to R2.",
    )
    mf_g.add_argument(
        "--upload-manifest-sig",
        type=Path,
        metavar="PATH",
        help="Detached .sig for the manifest (uploaded alongside it).",
    )
    mf_g.add_argument(
        "--update-latest",
        action="store_true",
        help="After uploading the manifest, write aibox/latest.json pointer.",
    )

    # ── Legacy streaming mode (--source) ──────────────────────────────────────
    legacy_g = parser.add_argument_group("Legacy streaming mode (--source)")
    legacy_g.add_argument(
        "--source",
        type=Path,
        help="Source directory to archive and upload as streaming shards.",
    )
    legacy_g.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Local staging directory. Default: <source-parent>/_r2_staging/",
    )
    legacy_g.add_argument(
        "--prefix",
        default="chroma_es/v1/",
        help="Key prefix inside the R2 bucket for streaming shards.",
    )
    legacy_g.add_argument(
        "--shard-prefix",
        default="simplewiki_es_chunks_part_",
        help="Filename prefix for each streaming shard.",
    )
    legacy_g.add_argument(
        "--shard-size",
        type=int,
        default=DEFAULT_SHARD_SIZE,
        help=f"Bytes per shard (default {DEFAULT_SHARD_SIZE}).",
    )
    legacy_g.add_argument(
        "--zstd-level",
        type=int,
        default=DEFAULT_ZSTD_LEVEL,
        help=f"zstd compression level (default {DEFAULT_ZSTD_LEVEL}).",
    )
    legacy_g.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="Output JSON receipt path (default: <workdir>/staging-receipt.json).",
    )
    legacy_g.add_argument(
        "--keep-shards",
        action="store_true",
        help="Do not delete local shard files after upload.",
    )
    legacy_g.add_argument(
        "--dry-run",
        action="store_true",
        help="Stage shards locally but don't upload (legacy mode only).",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Validate: at least one mode must be requested.
    if not args.upload_shards and not args.upload_manifest and not args.source:
        parser.print_help()
        print(
            "\nERROR: specify at least one of --upload-shards, "
            "--upload-manifest, or --source.",
            file=sys.stderr,
        )
        return 2

    # Build R2 client (skipped in dry-run legacy mode to allow offline testing).
    if args.dry_run and args.source and not args.upload_shards and not args.upload_manifest:
        client = None
        bucket = "<dry-run>"
    else:
        client, bucket = make_r2_client()

    exit_code = 0

    # ── Mode 1: pre-built shard upload ───────────────────────────────────────
    if args.upload_shards:
        records = upload_shards(args, client, bucket)
        print(f"\nUploaded {len(records)} shard(s).")

    # ── Mode 2: manifest + sig ────────────────────────────────────────────────
    if args.upload_manifest:
        manifest_path = args.upload_manifest.resolve()
        release = _release_from_manifest(manifest_path)

        versioned_key = upload_manifest(args, client, bucket)

        if args.update_latest:
            write_latest_pointer(client, bucket, release, versioned_key)
            print(f"  latest.json updated → {release}")

    # ── Legacy streaming mode ─────────────────────────────────────────────────
    if args.source:
        run_legacy_stream(args, client, bucket)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
