# AIBox Release Runbook

Step-by-step guide for operators cutting an AIBox installer release.
Cross-reference: [SECRETS.md](SECRETS.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) · `installerplan.txt §11`

---

## Prerequisites

Before the first release can ship, the following external resources must exist:

- **Cloudflare R2 bucket** — create a bucket (e.g. `aibox-content`) in the Cloudflare dashboard.
  Enable a custom domain (e.g. `cdn.projectpuenteai.org`) pointing at the bucket.
  Create an R2 API token with `Object Read & Write` on that bucket.

- **GitHub repository secrets & variables** — see [SECRETS.md](SECRETS.md) for the full table.

- **Azure Trusted Signing account** — create an account at `trustedsigning.azure.com`,
  create a certificate profile, and grant the CI service principal `Trusted Signing Certificate Profile Signer`.
  Alternatively, provision a DigiCert EV code-signing cert and adapt `sign-files.ps1`.

- **Python 3.12+ with `cryptography`** — required on any machine that runs the build helpers:
  ```
  pip install cryptography requests ruamel.yaml
  ```

- **Kiwix ZIM files staged to R2** — run `stage-content.yml` (manual dispatch) before building
  the manifest. The manifest CI will fail if R2 shard entries still contain `size_bytes: 0` or
  `sha256: TBD`.

---

## One-time Setup: ed25519 Signing Keypair

Run this **once**, ever. Running it again rotates the key, which invalidates all shipped installers.

```powershell
# From the repo root
python aibox\installer\build\generate-keypair.py `
    --private-out aibox\installer\build\.secrets\release.ed25519.sk `
    --public-out  aibox\installer\build\.secrets\release.ed25519.pk
```

The script prints three values. Use them as follows:

1. **`AIBOX_MANIFEST_PRIVKEY`** (GitHub secret) — the base64 private key seed printed by the script.
2. **`AIBOX_MANIFEST_PUBKEY_B64`** (GitHub secret) — the base64 public key printed by the script.
3. **Committed public key** — base64-encode the raw `.pk` file and write it to
   `first-run/Resources/release-pubkey.ed25519`, then commit:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes(
       'aibox\installer\build\.secrets\release.ed25519.pk'
   )) | Set-Content -NoNewline `
       'aibox\installer\first-run\Resources\release-pubkey.ed25519'
   git add aibox/installer/first-run/Resources/release-pubkey.ed25519
   git commit -m "chore: set release pubkey"
   ```

Delete or move `build/.secrets/release.ed25519.sk` to a password manager.
**Never commit `.secrets/`** — it is in `.gitignore`.

For local testing without touching the production key, use:
```powershell
python aibox\installer\build\setup_dev_keypair.py
```

---

## Per-release Workflow

### 1. Update `release-config.yaml` if needed

- Verify HF revision SHAs are still resolvable (see [Manual Interventions](#manual-interventions)).
- Verify Kiwix dump dates are current (see [Manual Interventions](#manual-interventions)).
- Update Kolibri channel IDs if channels have changed.
- Commit and push to `main`.

### 2. Stage Chroma index shards to R2 (if changed)

Dispatch the `stage-content.yml` workflow manually from the Actions tab, or run locally:

```powershell
python aibox\installer\build\stage_r2_content.py `
    --shard-dir aibox\backend-data\chroma_shards `
    --update-latest
```

Confirm the shards are reachable at `$AIBOX_R2_BASE/chroma/v1/`.

### 3. Create and push a version tag

```bash
git tag v1.0.0
git push origin v1.0.0
```

This triggers `release.yml`. The workflow runs six jobs in order:

| # | Job | What it does |
|---|-----|--------------|
| 1 | `build-ai-control` | Builds + pushes GHCR image; outputs digest |
| 2 | `build-first-run` | Publishes WPF app, bakes build constants, signs binaries |
| 3 | `build-inno` | Downloads Docker Desktop, builds NVIDIA probe, compiles Inno setup, signs `.exe` |
| 4 | `build-manifest` | Builds + signs `manifest-<v>.json`, verifies signature |
| 5 | `upload-r2` | Mirrors manifest + `.sig` to R2, bumps `/latest.json` |
| 6 | `publish-release` | Creates GitHub release with all artifacts |

### 4. Verify the release

```bash
# Inspect the published manifest
curl https://cdn.projectpuenteai.org/aibox/latest.json | jq .

# Verify the manifest signature locally
python aibox/installer/build/verify-manifest.py \
  --manifest dist/manifest-1.0.0.json \
  --sig      dist/manifest-1.0.0.json.sig \
  --pubkey-b64-env AIBOX_MANIFEST_PUBKEY_B64
```

Download `AIBox-Setup-<v>.exe` from the GitHub release and check its SHA-256 against the
`.sha256` sidecar file. Test in a clean VM.

### 5. Announce / distribute

Share the GitHub release URL. End users run the `.exe`, which chains to the WPF First Run app,
which fetches the manifest from R2 and begins the ~100 GB download.

---

## Manual Interventions

### When Kiwix dump dates rotate

Kiwix retires old ZIM files without notice (typically every 1–3 months). When the fallback URL
returns 404:

1. Run the rotation helper to find the latest available dump date:
   ```powershell
   python aibox\installer\build\rotate_kiwix_dates.py --dry-run
   ```
2. Review the proposed changes, then apply:
   ```powershell
   python aibox\installer\build\rotate_kiwix_dates.py
   ```
3. Commit `manifests/release-config.yaml` and re-run from step 3 of the per-release workflow.

### When HF SHAs need to be re-resolved

HF `revision: main` entries are resolved to concrete commit SHAs by `build_manifest.py` at CI time.
If a model repo is force-pushed or reorganised:

1. Find the current HEAD commit SHA for the affected repo on huggingface.co (`/tree/main` → commit hash).
2. Update `revision:` in `manifests/release-config.yaml`.
3. Commit and re-tag.

---

## Diagnostics When Something Fails

### Where logs live

- **CI logs** — GitHub Actions tab → select the failed run → expand the failed step.
- **First Run app logs** — written to `%LOCALAPPDATA%\AIBox\logs\first-run.log` on end-user machines.
  The diagnostics ZIP (if smoke test fails) lands at `%LOCALAPPDATA%\AIBox\logs\diagnostics-<ts>.zip`.

### Inspect `/latest.json`

```bash
curl https://cdn.projectpuenteai.org/aibox/latest.json
# Should return: { "version": "1.0.0", "manifest": "manifest-1.0.0.json", ... }
```

If the old version is still there, the `upload-r2` job failed — check its logs and re-run it
(`workflow_dispatch` on the tag, `dry_run: false`).

### Roll back a bad release

1. Edit the GitHub release to mark it as a pre-release (hides it from the default download link).
2. Overwrite `/latest.json` on R2 to point at the previous good version:
   ```bash
   echo '{"version":"0.9.0","manifest":"manifest-0.9.0.json"}' \
     | python aibox/installer/build/stage_r2_content.py --stdin-latest
   ```
3. Delete or retract the bad git tag if needed:
   ```bash
   git tag -d v1.0.0 && git push origin :refs/tags/v1.0.0
   ```

---

## Required GitHub Secrets and Variables

See [SECRETS.md](SECRETS.md) for the exhaustive table. Quick reference:

| Name | Kind | One-line description |
|------|------|----------------------|
| `AIBOX_MANIFEST_PRIVKEY` | Secret | Base64 ed25519 private key seed; signs manifests |
| `AIBOX_MANIFEST_PUBKEY_B64` | Secret | Base64 ed25519 public key; baked into WPF app at build time |
| `AIBOX_R2_ACCOUNT_ID` | Secret | Cloudflare account ID for R2 API auth |
| `AIBOX_R2_ACCESS_KEY_ID` | Secret | R2 API token key ID |
| `AIBOX_R2_SECRET_ACCESS_KEY` | Secret | R2 API token secret |
| `AIBOX_R2_BUCKET` | Var | R2 bucket name (e.g. `aibox-content`) |
| `AIBOX_R2_BASE` | Var | Public base URL (e.g. `https://cdn.projectpuenteai.org/aibox`) |
| `DOCKER_DESKTOP_SHA256` | Var | Expected SHA-256 of the staged Docker Desktop installer |
| `AZURE_SIGNING_TENANT` | Secret | Azure tenant ID for Trusted Signing |
| `AZURE_SIGNING_CLIENT_ID` | Secret | Service principal client ID |
| `AZURE_SIGNING_CLIENT_SECRET` | Secret | Service principal client secret |
| `AZURE_SIGNING_ACCOUNT` | Secret | Trusted Signing account name |
| `AZURE_SIGNING_PROFILE` | Secret | Certificate profile name |
| `AZURE_SIGNING_ENDPOINT` | Secret | Regional endpoint (e.g. `eus.codesigning.azure.net`) |
| `GITHUB_TOKEN` | Auto | Provided by Actions; used for GHCR push and release creation |
