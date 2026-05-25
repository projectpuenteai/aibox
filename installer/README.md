# AIBox Installer

Two-stage Windows installer for Project Puente AI. See `../../installerplan.txt`
for the full design and finish-and-ship runbook.

This single document replaces every other `.md` that used to live under
`aibox/installer/` (per-subdirectory READMEs, RELEASING, SECRETS, TROUBLESHOOTING,
the two pre-build audit files). Topics are grouped: overview → architecture →
build/sign pipeline → release runbook → secrets → troubleshooting → status.

---

## 1. What this builds

| Artifact                       | Size      | Source                       | Audience               |
| ------------------------------ | --------- | ---------------------------- | ---------------------- |
| `AIBox-Setup-<v>.exe`          | ~700 MB   | Inno Setup (`inno/AIBox.iss`)| End users (GitHub rel) |
| `AIBox First Run.exe`          | ~10 MB    | WPF .NET 8 (`first-run/`)    | Bundled inside above   |
| `AIBox Uninstaller.exe`        | ~10 MB    | WPF .NET 8 (`uninstaller/`)  | Bundled inside above   |
| `nvidia-probe.exe`             | ~180 KB   | C# console (`inno/nvidia-probe/`) | Inno preflight    |
| `manifest-<v>.json` + `.sig`   | ~30 KB    | `build/build_manifest.py`    | GitHub + R2            |

Content sources the First Run app pulls (~100 GB total):

| Source         | Carries                  | Hosting                       |
| -------------- | ------------------------ | ----------------------------- |
| Hugging Face   | LLM, embedding, reranker | huggingface.co (pinned SHAs)  |
| Kiwix          | Wikipedia ZIMs           | download.kiwix.org            |
| Kolibri Studio | Curated courses          | studio.learningequality.org   |
| Cloudflare R2  | Chroma index shards      | `cdn.projectpuenteai.org`     |

Only the Chroma index lives on infrastructure we operate.

---

## 2. Layout

```
aibox/installer/
  inno/                  Inno Setup script + branding + NVIDIA probe sub-project
    AIBox.iss            single .iss with preflight + reboot-resume inline
    branding/            banner.bmp, banner-small.bmp, app.ico, LICENSE.rtf,
                         PREINSTALL.rtf  (all committed; replace with real
                         assets before public release)
    nvidia-probe/        nvidia-probe.csproj — see §7
  first-run/             WPF .NET 8 First Run app (Phase B + C)
    Services/            ManifestClient, DownloadEngine, EnvWriter, DockerCli, ...
    Services/Fetchers/   R2 / HuggingFace / Kiwix / Kolibri / HttpRangeDownloader
    Phases/              DownloadPhase, BootstrapFlow, AdminPromptView, SummaryView
    Resources/release-pubkey.ed25519   embedded ed25519 pubkey (CI overwrites)
    Tests/               89 xUnit tests (passing locally)
  uninstaller/           Custom uninstaller for the data side
  admin-console/         WPF Admin Console (post-install operator UI)
  build/                 Python build/sign helpers + Cloudflare R2 uploader
  manifests/             release-config.yaml + manifest-0.0.1.json sample
  .github/workflows/     release.yml, stage-content.yml, sign-files.ps1
  dist/                  build outputs (gitignored except where noted)
    stage/               CI staging area for Inno [Files] sources
```

---

## 3. Architecture — three phases

**Phase A — Inno wizard (`AIBox-Setup-<v>.exe`):**
preflight (NVIDIA via `nvidia-probe.exe`, RAM/disk via WMI), install bits to
`C:\AIBox`, run bundled Docker Desktop MSI, write
`%ProgramData%\AIBox\install-state.json` with `phase_a_complete: true`, drop
the First Run shortcut, optionally request a reboot via the `NeedRestart()`
callback (driven by `{tmp}\docker_install_result.txt` sentinel).
Phase A does NOT generate `.env`, does NOT pull images, does NOT auto-start
Phase B.

**Phase B — First Run, content download:**
fetches `manifest-<v>.json` and `.sig` from R2 (with a GitHub Releases
fallback), verifies the signature against the embedded ed25519 pubkey,
then runs four fetchers behind one scheduler:
- `HuggingFaceFetcher` — pinned `/resolve/{sha}/{path}` + tree API + glob
  filter + optional `AIBOX_HF_TOKEN`
- `KiwixFetcher` — OPDS catalog (low priority) + directory-listing scrape +
  `.sha256` sidecar trust
- `KolibriFetcher` — `docker compose exec kolibri kolibri manage
  importchannel/importcontent` with JSON progress parsing
- `R2Fetcher` — range download + SHA verify + `tar.zst` extract
All fetchers share `HttpRangeDownloader` (atomic `.part` → rename, range
resume, SHA-256 verify) and persist resume state every 5 s.

**Phase C — First Run, bootstrap:**
admin password prompt → `EnvWriter` generates secrets and writes ACL-locked
`stack/.env` (DPAPI-encrypted admin copy at
`%ProgramData%\AIBox\admin-credentials.dpapi`) → `docker compose pull` →
smoke test against `ai-control:/health` → autostart task registration via
`install_autostart.ps1` → shortcut rewrite → summary card.

State machine persisted to `install-state.json`:
`fresh → ready_for_b → ready_for_c → done`. Every phase is resumable;
the First Run app picks up where it left off.

---

## 4. Trust model

The manifest is the trust root. It is signed with the project's ed25519
private key (held only in GitHub Actions secrets), and the First Run app
verifies it against the public key embedded at
`first-run/Resources/release-pubkey.ed25519` before trusting any URL or
checksum inside it. Once the manifest is trusted, every per-file SHA-256
and every `.sha256` sidecar URL it references becomes trusted by extension.

A single compromised upstream cannot inject malicious content unless the
attacker also forges a signed manifest.

The pointer file `latest.json` at `cdn.projectpuenteai.org/aibox/latest.json`
tells installers which version is current:

```json
{
  "current_release": "1.0.0",
  "min_installer_version": "1.0.0",
  "manifest_url": "https://cdn.projectpuenteai.org/aibox/manifest-1.0.0.json"
}
```

`min_installer_version` blocks an outdated `.exe` from a manifest that
needs newer features.

---

## 5. Manifest schema (`schema_version: 2`)

Top-level:

| Field                   | Type        | Required | Notes                          |
| ----------------------- | ----------- | -------- | ------------------------------ |
| `schema_version`        | int         | yes      | Must be `2`                    |
| `release`               | string      | yes      | Semantic version               |
| `min_installer_version` | string      | yes      | Lowest setup.exe that can read it |
| `built_at`              | RFC 3339    | yes      | UTC build timestamp            |
| `items`                 | array       | yes      | Payload to fetch               |

Each item has a `source` field selecting the fetcher:

- **`huggingface` single file** — `repo`, `revision` (40-hex SHA, enforced),
  `path_in_repo`, `target`, `size_bytes`, `sha256`.
- **`huggingface` multi-file (tree)** — `repo`, `revision`, `include` (globs),
  `target_dir`, `size_bytes_total`, `files` (per-file `path` + `sha256`),
  `sha256_manifest` (meta-hash over the sorted `<path> <sha256>` lines —
  rejects a tampered tree response).
- **`kiwix`** — `catalog_query`, `fallback_url`, `sha256_url`, `target`,
  `size_bytes`. No `sha256` field; the `.sha256` sidecar is authoritative
  (Kiwix sometimes rebuilds dumps byte-identically without renaming).
- **`kolibri_channel`** — `studio_base_url`, `channel_id`, `include_node_ids`,
  `approx_size_bytes`. Not a file download; `kolibri manage
  importchannel/importcontent` runs inside the kolibri container.
- **`r2`** — `url`, `target`, `extract_to`, `size_bytes`, `sha256`. Tar.zst
  shards extracted in place after verification.

The full schema and canonicalization rules live in the Python reference
implementation (`build/manifest_canonical.py`) and the C# parser
(`first-run/Services/Manifest.cs`); 19 xUnit canonicalization-parity tests
confirm byte-for-byte equivalence including non-ASCII, control chars, and
integer-valued doubles (`5.0` ↔ `"5.0"`).

The sample `manifests/manifest-0.0.1.json` exercises every fetcher with
tiny payloads; not for shipping.

The content list that becomes the next real release lives in
`manifests/release-config.yaml`.

---

## 6. Build helpers (Python, under `build/`)

All scripts are implemented (older docs claimed several were "skeletons" —
they aren't).

| Script                  | Purpose                                            |
| ----------------------- | -------------------------------------------------- |
| `generate-keypair.py`   | Create an ed25519 signing keypair (one-time)       |
| `setup_dev_keypair.py`  | Local dev keypair generator (writes to `.secrets/`)|
| `build_manifest.py`     | Assemble a manifest from `release-config.yaml`     |
| `sign-manifest.py`      | Sign a manifest with the ed25519 private key       |
| `verify-manifest.py`    | Verify a manifest signature (parity with WPF)      |
| `manifest_canonical.py` | Canonicalizer used by build + sign + verify        |
| `stage_r2_content.py`   | Upload Chroma shards (and the manifest) to R2      |
| `rotate_kiwix_dates.py` | Scrape `download.kiwix.org` and patch dates in     |
|                         | `release-config.yaml` when Kiwix dumps rotate      |

There are no PowerShell `sign.ps1` / `build-installer.ps1` /
`stage-r2-content.ps1` scripts — the equivalent is the Python set above
plus the Azure-Trusted-Signing wrapper at `.github/workflows/sign-files.ps1`.

### Repeatable local build (after a C# or `.iss` change)

```powershell
cd C:\AIBox\aibox\installer
& "C:\Program Files\dotnet\dotnet.exe" publish first-run\AIBoxFirstRun.csproj `
    -c Release -r win-x64 --self-contained false -o dist\stage\first-run
& "C:\Program Files\dotnet\dotnet.exe" publish uninstaller\AIBoxUninstaller.csproj `
    -c Release -r win-x64 --self-contained false -o dist\stage\first-run
& "C:\Program Files\dotnet\dotnet.exe" publish inno\nvidia-probe\nvidia-probe.csproj `
    -c Release -r win-x64 --self-contained true -o dist\stage
& "$env:LocalAppData\Programs\Inno Setup 6\ISCC.exe" inno\AIBox.iss
```

### Repeatable local manifest cut (uses the dev keypair)

```powershell
cd C:\AIBox\aibox\installer
& C:\AIBox\.venv-rag\Scripts\python.exe build\build_manifest.py `
    --version 1.0.0 --output dist\manifest-1.0.0.json --compute-non-lfs-sha
& C:\AIBox\.venv-rag\Scripts\python.exe build\sign-manifest.py `
    --manifest dist\manifest-1.0.0.json `
    --key-file build\.secrets\dev.ed25519.sk
& C:\AIBox\.venv-rag\Scripts\python.exe build\verify-manifest.py `
    --manifest dist\manifest-1.0.0.json `
    --sig      dist\manifest-1.0.0.json.sig `
    --pubkey   build\.secrets\dev.ed25519.pk
```

### Tests (xUnit)

```powershell
& "C:\Program Files\dotnet\dotnet.exe" test `
    aibox\installer\first-run\Tests\AIBoxFirstRunTests.csproj `
    -c Release --logger "console;verbosity=minimal"
```

Expected: 89/89 passing (includes 9 Ed25519 RFC 8032 §7.1 KAT vectors and
19 canonicalization-parity tests).

---

## 7. Inno wizard (`inno/AIBox.iss`)

Single `.iss` script with preflight + reboot-resume inlined. Builds
`AIBox-Setup-<v>.exe`. Lays down the AIBox source tree, runs the bundled
Docker Desktop MSI silently (if Docker is not already installed), writes
the initial `install-state.json`. Does NOT write `.env`, pull images, or
download content — all of that is the First Run app's job.

CI pre-stages these under `dist/stage/`:

- `first-run/` — `dotnet publish` output of the WPF app + uninstaller
- `DockerDesktopInstaller.exe` — bundled MSI, hash-pinned via
  `DOCKER_DESKTOP_SHA256` repo variable
- `nvidia-probe.exe` — CUDA-detection shim
- `RELEASE_COMMIT.txt` — git SHA being built

Local build:

```powershell
iscc.exe /Dversion=1.0.0 inno\AIBox.iss
```

If `/Dversion=` is mangled by your shell's quoting, edit
`#define MyAppVersion` near the top of `AIBox.iss` directly, or use the
`/F` form to override `OutputBaseFilename` instead.

The reboot decision uses `NeedRestart()` callback driven by
`{tmp}\docker_install_result.txt` written by the Docker MSI step:
exit `0` → no reboot, exit `3010` → reboot, anything else / missing file →
reboot defensively.

### NVIDIA probe (`inno/nvidia-probe/`)

Minimal C# console app used by the Inno preflight. Detection:

1. Runs `nvidia-smi --query-gpu=name --format=csv,noheader` and checks
   for at least one non-empty line.
2. Falls back to `LoadLibrary("nvcuda.dll")` via P/Invoke (System32
   carries the CUDA runtime DLL even when `nvidia-smi` is not on PATH).

Exit codes: `0` = GPU detected, `1` = not detected. A non-zero exit
blocks the install with a hard error — there is no "continue anyway"
button per design.

Build (one-liner from repo root):

```powershell
dotnet publish aibox/installer/inno/nvidia-probe/nvidia-probe.csproj `
  -c Release -r win-x64 --self-contained false `
  -o dist/stage/nvidia-probe-staging
Copy-Item dist/stage/nvidia-probe-staging/nvidia-probe.exe dist/stage/
```

---

## 8. Cloudflare R2 upload runbook

For staging the Chroma index (the one artifact we self-host). Uses
`build/stage_r2_content.py`.

### Mint R2 credentials

Cloudflare dashboard → **R2** → bucket (e.g. `puentechromadb`) →
**Manage R2 API Tokens** → **Create API token**, scope
**Object Read & Write** on that bucket only. Capture Access Key ID,
Secret Access Key, Account ID, and the S3-compatible endpoint.

### Set environment variables (PowerShell)

```powershell
$env:R2_ACCOUNT_ID        = "abc123def4567890abc123def4567890"
$env:R2_ACCESS_KEY_ID     = "..."
$env:R2_SECRET_ACCESS_KEY = "..."
$env:R2_BUCKET            = "puentechromadb"
```

Do not commit or persist these.

### Dry run first

```powershell
.\.venv-rag\Scripts\activate
python aibox\installer\build\stage_r2_content.py `
    --source aibox\backend-data\chroma_db_es `
    --prefix chroma_es/v1/ `
    --shard-prefix simplewiki_es_chunks_part_ `
    --dry-run --keep-shards
```

Produces ~8 shards (~4 GiB each) under `aibox\backend-data\_r2_staging\`
plus a `staging-receipt.json`. The pipeline is `tar | zstd | shard |
upload`, single pass, so local disk holds at most one shard at a time.

### Real upload

Drop `--dry-run --keep-shards`. Interrupt-safe — re-running checks
shard size in R2 via HEAD and skips already-uploaded objects.

### Public access

For production, attach `cdn.<yourdomain>` to the bucket via R2 Custom
Domains (free, unlimited egress, automatic SSL). The installer manifest
URLs then become `https://cdn.<yourdomain>/chroma_es/v1/...`. The
`r2.dev` public URL is rate-limited and unsuitable for production.

| Symptom | Cause / fix |
| --- | --- |
| `403 InvalidAccessKeyId` | Token doesn't grant access to this bucket — re-mint with bucket-scoped permission |
| `404 NoSuchBucket` | Bucket-name typo or token scoped to a different account |
| Upload speed << link | R2 shapes single-stream uploads ~250 Mbps; boto3 multipart already parallelizes |
| Out of disk in `_r2_staging\` | `--workdir D:\some\drive` to relocate |

---

## 9. Release runbook

### Prerequisites (one-time, infra)

- Cloudflare R2 bucket (`aibox-content`) with `cdn.projectpuenteai.org`
  custom domain — provisioned and DNS resolves; bucket empty until first
  release.
- GitHub repository secrets + variables — see §10 for the table.
- Azure Trusted Signing account (or equivalent EV cert + adapt
  `sign-files.ps1`) for code signing the `.exe`s.
- Python 3.12+ with `cryptography`, `requests`, `ruamel.yaml`, `pyyaml`,
  `zstandard`, `boto3` on any machine running the build helpers.

### One-time: ed25519 signing keypair

Run **once** ever — re-running rotates the key and invalidates every
shipped installer.

```powershell
python aibox\installer\build\generate-keypair.py `
    --private-out aibox\installer\build\.secrets\release.ed25519.sk `
    --public-out  aibox\installer\build\.secrets\release.ed25519.pk
```

Then:

1. **`AIBOX_MANIFEST_PRIVKEY`** (GitHub secret) — base64 private key seed
   printed by the script.
2. **`AIBOX_MANIFEST_PUBKEY_B64`** (GitHub secret) — base64 public key
   printed by the script.
3. **Committed pubkey** — base64-encode the raw `.pk` file and write it
   to `first-run/Resources/release-pubkey.ed25519`:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes(
       'aibox\installer\build\.secrets\release.ed25519.pk'
   )) | Set-Content -NoNewline `
       'aibox\installer\first-run\Resources\release-pubkey.ed25519'
   git add aibox/installer/first-run/Resources/release-pubkey.ed25519
   git commit -m "chore: set release pubkey"
   ```

Delete or move the local `.sk` to a password manager.

For local-only testing (without touching production key):

```powershell
python aibox\installer\build\setup_dev_keypair.py
```

### Per-release workflow

1. Update `manifests/release-config.yaml` if HF revisions, Kiwix dates,
   or Kolibri channels have moved. `python build\rotate_kiwix_dates.py
   --dry-run` automates the Kiwix-date refresh.
2. Stage Chroma shards to R2 if changed — dispatch `stage-content.yml`,
   or run `stage_r2_content.py --update-latest` locally. Confirm shards
   reach `$AIBOX_R2_BASE/chroma/v1/`.
3. Tag and push:
   ```bash
   git tag v1.0.0 && git push origin v1.0.0
   ```
4. Watch `release.yml` (Actions tab). It runs six jobs:

   | # | Job | Output |
   |---|-----|--------|
   | 1 | `build-ai-control`  | GHCR image push + digest output |
   | 2 | `build-first-run`   | Published WPF binary; rewrites `BuildConstants.cs`; signs |
   | 3 | `build-inno`        | Downloads Docker Desktop, builds NVIDIA probe, compiles + signs `.exe` |
   | 4 | `build-manifest`    | Builds + signs `manifest-<v>.json`, verifies signature |
   | 5 | `upload-r2`         | Mirrors manifest + `.sig` to R2; bumps `/latest.json` |
   | 6 | `publish-release`   | Creates GitHub release with all artifacts |

5. Verify:
   ```bash
   curl https://cdn.projectpuenteai.org/aibox/latest.json | jq .
   python aibox/installer/build/verify-manifest.py \
     --manifest dist/manifest-1.0.0.json \
     --sig      dist/manifest-1.0.0.json.sig \
     --pubkey-b64-env AIBOX_MANIFEST_PUBKEY_B64
   ```
   Download `AIBox-Setup-<v>.exe` and `.sha256` from the GitHub release.
   Smoke-test in a clean VM.

### Manual interventions

- **Kiwix dump rotated** — `rotate_kiwix_dates.py --dry-run`, review,
  then run without `--dry-run`, commit `release-config.yaml`, re-tag.
- **HF SHAs need re-resolving** — look up the current `tree/main`
  commit, update `revision:` in `release-config.yaml`, re-tag.

### Rollback

1. Mark the bad GitHub release as a pre-release (hides it from default
   download links).
2. Overwrite `/latest.json` to point at the previous good manifest:
   ```bash
   echo '{"version":"0.9.0","manifest":"manifest-0.9.0.json"}' \
     | python aibox/installer/build/stage_r2_content.py --stdin-latest
   ```
3. Optionally delete the bad git tag.

---

## 10. Secrets contract

GitHub repo settings → **Secrets and variables → Actions**.

### Secrets

| Name | Purpose |
|------|---------|
| `AIBOX_MANIFEST_PRIVKEY`     | Base64 ed25519 private seed; signs the manifest. Never share or log. |
| `AIBOX_MANIFEST_PUBKEY_B64`  | Base64 ed25519 public key; baked into the WPF binary at build time. |
| `AIBOX_R2_ACCOUNT_ID`        | Cloudflare account ID for the R2 S3 endpoint. |
| `AIBOX_R2_ACCESS_KEY_ID`     | R2 API token key ID. |
| `AIBOX_R2_SECRET_ACCESS_KEY` | R2 API token secret. |
| `AZURE_SIGNING_TENANT`       | Azure AAD tenant ID for Trusted Signing. |
| `AZURE_SIGNING_CLIENT_ID`    | Service principal client ID. |
| `AZURE_SIGNING_CLIENT_SECRET`| Service principal client secret. |
| `AZURE_SIGNING_ACCOUNT`      | Trusted Signing account name. |
| `AZURE_SIGNING_PROFILE`      | Certificate profile name. |
| `AZURE_SIGNING_ENDPOINT`     | Regional endpoint (e.g. `eus.codesigning.azure.net`). |
| `GITHUB_TOKEN`               | Auto-provided; used for GHCR push and release creation. |

### Variables

| Name | Purpose |
|------|---------|
| `AIBOX_R2_BUCKET`      | R2 bucket name (e.g. `aibox-content`). |
| `AIBOX_R2_BASE`        | Public base URL (e.g. `https://cdn.projectpuenteai.org/aibox`), no trailing slash. |
| `DOCKER_DESKTOP_SHA256`| Expected SHA-256 of the Docker Desktop installer pinned by CI. Optional — when unset, hash check is skipped with a warning. |

### Optional / reserved

| Name | Purpose |
|------|---------|
| `AIBOX_HF_TOKEN` | HF token. **Not currently referenced** by any workflow — reserved for future HF rate-limit headroom. |

### Rotation / scope notes

- `AIBOX_MANIFEST_PRIVKEY` and `AIBOX_MANIFEST_PUBKEY_B64` rotate together.
  Rotation invalidates every shipped installer's signature verification —
  coordinate a new `first-run/Resources/release-pubkey.ed25519` commit
  alongside.
- Manifest signing is **hard-required**: missing `AIBOX_MANIFEST_PRIVKEY`
  fails the build. Code signing is **soft-required**: missing
  `AZURE_SIGNING_*` produces unsigned `.exe`s and SmartScreen warnings;
  the `build-first-run` job throws on tag builds when
  `AIBOX_MANIFEST_PUBKEY_B64` is absent.
- R2 token: scope to **Object Read & Write on the target bucket only**.
  Do not use the global Cloudflare API key.

---

## 11. Troubleshooting (first-aid for common install failures)

### "Preflight failed: NVIDIA GPU not detected"

`nvidia-probe.exe` exited 1 — neither `nvidia-smi` nor `nvcuda.dll`
was found. Install the latest NVIDIA driver from nvidia.com/drivers,
reboot, verify with `nvidia-smi`, re-run setup. If the GPU is there
but the driver is partial, clean-reinstall via DDU.

### "Manifest signature did NOT verify"

The downloaded manifest doesn't match the embedded public key. Either
corruption in transit, or the installer came from an unofficial mirror.
Re-download `AIBox-Setup-<v>.exe` from the official GitHub release and
verify the `.sha256` sidecar before running.

For developers testing locally with a dev keypair: make sure the pubkey
baked into the binary (`first-run/Resources/release-pubkey.ed25519`)
matches the key that signed the manifest. `build/setup_dev_keypair.py`
prints both halves.

### "Docker Desktop is not running"

Open Docker Desktop and wait for the tray icon to show "running"
(30–60 s on first launch after reboot). Then click **Retry** in the
First Run app, or close and reopen — it resumes. WSL2 errors:
`wsl --update && wsl --shutdown` in an admin PowerShell, then restart
Docker Desktop.

### Hugging Face 429 / rate-limit stalls

Anonymous HF downloads are rate-limited. Create a free HF account, mint
a read token at huggingface.co/settings/tokens, then set
`AIBOX_HF_TOKEN` before re-launching the First Run app:

```powershell
$env:AIBOX_HF_TOKEN = "hf_..."
```

### "Smoke test failed"

The First Run app saves a diagnostics bundle to
`%LOCALAPPDATA%\AIBox\logs\diagnostics-<ts>.zip`. Inside, check
`first-run.log` for the first `ERROR` line after "Starting smoke test".
Common sub-failures:

- Container unhealthy → `docker ps -a` and `docker logs <container>`
- `ai-control` not responding → VRAM exhaustion on small GPUs;
  reduce `N_GPU_LAYERS` in `aibox/stack/.env`
- Chroma empty → shard failed to extract; check
  `backend-data/chroma_db/`

Re-run the smoke test without reinstalling:

```powershell
powershell -ExecutionPolicy Bypass -File aibox\tools\llama-runtime\scripts\up_stack.ps1
```

### First Run app crashes on launch

Check `%LOCALAPPDATA%\AIBox\logs\first-run.log`. Ensure
.NET 8 Desktop Runtime (x64) is installed. Run as Administrator if the
log shows access-denied writing to `%ProgramFiles%\AIBox`.

### Collecting logs

```powershell
Get-Content "$env:LOCALAPPDATA\AIBox\logs\first-run.log" | Select-Object -Last 100
docker compose -f aibox/stack/docker-compose.yaml logs --tail=50
```

---

## 12. Current status

What works locally (verified by the most recent build pass):

- All four .NET projects (`AIBoxFirstRun`, `AIBoxUninstaller`,
  `AIBoxFirstRunTests`, `nvidia-probe`) build with 0 warnings, 0 errors.
- All 89 xUnit tests pass.
- All Python build scripts compile and accept the args the workflows pass.
- `inno/AIBox.iss` compiles to `AIBox-Setup-1.0.0.exe` locally.
- `build_manifest.py → sign-manifest.py → verify-manifest.py` round-trips
  with the dev keypair.
- `dist/stage/DockerDesktopInstaller.exe` is the real ~655 MB Docker
  Desktop installer, not a placeholder.
- `dist/stage/nvidia-probe.exe`, `RELEASE_COMMIT.txt`, and
  `first-run/` are populated.
- `BuildConstants.ManifestBaseUrl` defaults to
  `https://cdn.projectpuenteai.org/aibox` (no longer the
  `.invalid` sentinel).
- Branding bitmaps + icon + RTFs are committed in `inno/branding/`
  (placeholder graphics — replace before public release).

What's still blocking a real, signed, end-user release:

1. **Production ed25519 keypair** — `generate-keypair.py` produces it,
   but `AIBOX_MANIFEST_PRIVKEY` / `AIBOX_MANIFEST_PUBKEY_B64` are not yet
   set in GitHub Secrets, and `first-run/Resources/release-pubkey.ed25519`
   still holds the dev placeholder.
2. **Cloudflare R2 secrets** — bucket + custom domain are provisioned, but
   `AIBOX_R2_*` secrets are not yet set in GitHub Secrets.
3. **Azure Trusted Signing** — the signing job is gated on
   `AZURE_SIGNING_TENANT != ''`. Without it the build still succeeds but
   ships unsigned `.exe`s, and SmartScreen will block most users.
4. **Kolibri channel ID verification** — the only channel pinned in
   `release-config.yaml` is `c1f2b7e6ac9f56a2bb44fa7a48b66dce`, which
   404s on Studio's public API. Confirm with
   `docker compose exec kolibri kolibri manage importchannel network <id>`
   in a test environment before tagging a release.
5. **Real branding** — the committed bitmaps are placeholders. Drop in
   real Project Puente assets before public release. Sizes:
   `banner.bmp` 497×314 (24-bit), `banner-small.bmp` 55×55 (24-bit),
   `app.ico` multi-res (16/32/48/256).
6. **Chroma shard staging** — the chroma-shard block in
   `release-config.yaml` is commented out. Either ship the
   pre-built index (run `build_chroma_index.py`, shard, upload via
   `stage_r2_content.py`, fill real sizes/SHAs, uncomment) or
   accept that users will build the index on first launch
   (~2 h on the laptop spec, vs ~5 min from a staged shard).

See `../../installerplan.txt` for the design rationale, the
finish-and-ship checklist, and section-12 upstream-pinned URLs.
