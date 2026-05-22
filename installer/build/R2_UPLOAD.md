# R2 upload runbook

How to upload a Chroma index to Cloudflare R2 so the AIBox installer
can pull it on first-run. Uses
[`stage_r2_content.py`](stage_r2_content.py).

## 1. Mint R2 API credentials

In the Cloudflare dashboard:

1. Go to **R2** in the left nav.
2. Click your bucket (e.g. `puentechromadb`) to verify it exists, or
   create it with **Create bucket**. The bucket region should be set to
   **Automatic** unless you have a specific reason.
3. From the **R2** overview page, click **Manage R2 API Tokens**.
4. **Create API token**:
   - Permissions: **Object Read & Write**
   - Specify bucket: just `puentechromadb` (least-privilege; do not
     grant access to all buckets)
   - TTL: leave open-ended for now, but rotate after first successful
     upload
5. Copy the four values it shows. You only see them once:
   - **Access Key ID**
   - **Secret Access Key**
   - **Account ID** (shown in the URL bar even without the token — it's
     the 32-char hex in `dash.cloudflare.com/<account_id>/...`)
   - **Endpoint** (e.g. `https://<account>.r2.cloudflarestorage.com`)
     — the script constructs this from the account ID by default, so
     you usually only need the account ID

## 2. Set environment variables

PowerShell session (Windows):

```powershell
$env:R2_ACCOUNT_ID       = "abc123def4567890abc123def4567890"
$env:R2_ACCESS_KEY_ID    = "..."
$env:R2_SECRET_ACCESS_KEY = "..."
$env:R2_BUCKET           = "puentechromadb"
```

Verify with `echo $env:R2_BUCKET` before continuing. **Do not** add
these to a shell profile or commit them — they grant write access to
your bucket.

## 3. Run a dry-run first (no upload, ~2 minutes)

This compresses the index locally to verify everything works without
spending any R2 bandwidth:

```powershell
# from C:\AIBox
.\.venv-rag\Scripts\activate
python aibox\installer\build\stage_r2_content.py `
    --source aibox\backend-data\chroma_db_es `
    --prefix chroma_es/v1/ `
    --shard-prefix simplewiki_es_chunks_part_ `
    --dry-run --keep-shards
```

Expected output: ~8 shards written to
`aibox\backend-data\_r2_staging\`, each ~4 GiB except the last, plus a
`staging-receipt.json`. Total disk used during the run is bounded to
roughly one shard size (~4 GiB) because each shard is deleted after
"upload" — but with `--dry-run --keep-shards` they all stick around
for inspection.

For chroma_db_es (30 GB raw): expect ~20-25 GB compressed, 5-7 shards.

## 4. Real upload

Remove `--dry-run` and `--keep-shards`:

```powershell
python aibox\installer\build\stage_r2_content.py `
    --source aibox\backend-data\chroma_db_es `
    --prefix chroma_es/v1/ `
    --shard-prefix simplewiki_es_chunks_part_
```

While it runs, you'll see lines like:

```
  ... 4.1 GB read, current: chroma_db_es/chroma.sqlite3
  uploaded chroma_es/v1/simplewiki_es_chunks_part_01.tar.zst (4.0 GB, 18.3 MB/s)
```

The script streams `tar | zstd | shard | upload` in a single pass, so
local disk never holds more than one shard at a time.

## 5. Resumption

If the upload is interrupted (closed terminal, network drop, ctrl+C),
just re-run the same command. The script:

- Re-streams the tar+zstd from scratch (compression is faster than
  upload anyway).
- For each shard, checks if an object of matching size already exists
  in R2 (HEAD request, ~1 KB).
- Skips upload if it does, re-uploads if size differs.

Net effect: a re-run after a 50%-complete first run uploads only the
remaining shards.

## 6. Verify in the dashboard

After completion, the bucket should contain:

```
chroma_es/v1/simplewiki_es_chunks_part_01.tar.zst
chroma_es/v1/simplewiki_es_chunks_part_02.tar.zst
...
chroma_es/v1/simplewiki_es_chunks_part_NN.tar.zst
```

Each object has a `sha256` custom metadata header set by the script.
The `staging-receipt.json` left in `_r2_staging\` records the same
information — that's the file the manifest builder will consume in a
future step.

## 7. (Later) Public access

Right now the bucket is private. To let the installer download
shards without R2 credentials, you have two options:

1. **R2 Custom Domain** (recommended for production): attach a
   subdomain like `cdn.<yourdomain>` to the bucket via the Cloudflare
   dashboard. Free, unlimited egress, automatic SSL. The installer
   manifest URLs become
   `https://cdn.<yourdomain>/chroma_es/v1/simplewiki_es_chunks_part_01.tar.zst`.

2. **r2.dev public URL** (development only): toggle "Allow Public
   Access" on the bucket. Cloudflare gives a `*.r2.dev` URL. **Not
   for production** — rate-limited and bandwidth-throttled.

You don't need either for the upload itself. Set it up when you're
ready to cut the first signed manifest.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `cannot access bucket ...: 403 InvalidAccessKeyId` | The R2 API token doesn't grant access to that bucket. Re-mint with bucket-scoped permission. |
| `cannot access bucket ...: 404 NoSuchBucket` | Bucket name typo, or token is scoped to a different account. Check `R2_BUCKET` and `R2_ACCOUNT_ID`. |
| Upload speed << your link speed | R2's per-account upload is shaped around ~250 Mbps for single streams. boto3 multipart already uses 10 parallel uploads per file, so this is upstream limit. |
| `botocore.errorfactory.NoSuchKey` during HEAD check | Benign — first run, the object doesn't exist yet. Script handles this. |
| Out of disk in `_r2_staging\` | Pass `--workdir D:\some\drive\with\space` to relocate. Default workdir is `<source-parent>/_r2_staging`. |
