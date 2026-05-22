# AIBox Installer

Two-stage Windows installer for Project Puente AI. See `../../installerplan.txt`
for the full design. This directory holds the source for the installer
artifacts: the Inno Setup wizard, the WPF First Run app, and the build
pipeline that produces them.

## Layout

```
installer/
  inno/                Inno Setup script (Phase A wizard)
  first-run/           WPF .NET 8 app (Phase B + C)
  uninstaller/         Custom uninstaller for the data side
  build/               Local build + signing + R2 staging helpers
  manifests/           Release manifests (manifest-<version>.json)
  .github/workflows/   CI for release builds and content staging
```

## Build artifacts (high level)

| Artifact                       | Size      | Source              | Audience               |
| ------------------------------ | --------- | ------------------- | ---------------------- |
| `AIBox-Setup-<v>.exe`          | ~700 MB   | Inno Setup          | End users (GitHub rel) |
| `AIBox First Run.exe`          | ~10 MB    | WPF .NET 8          | Bundled inside above   |
| `manifest-<v>.json` + `.sig`   | ~30 KB    | `build/build-manifest.py` | GitHub + R2      |

## Content sources

The First Run app pulls ~100 GB of payload from four upstreams. Only the
Chroma index lives on infrastructure we operate.

| Source              | Carries                       | Hosting                |
| ------------------- | ----------------------------- | ---------------------- |
| Hugging Face        | LLM, embedding, reranker      | huggingface.co (pinned)|
| Kiwix               | Wikipedia ZIMs                | download.kiwix.org     |
| Kolibri Studio      | Curated courses               | studio.learningequality.org |
| Cloudflare R2       | Chroma index shards           | `cdn.<ourdomain>`      |

See `manifests/README.md` for the manifest schema and `build/README.md`
for how to cut a release.

## Trust model

The manifest is the trust root: it's signed with the project's ed25519
private key (held only in GitHub Actions secrets), and the WPF First Run
app verifies it against an embedded public key
(`first-run/Resources/release-pubkey.ed25519`) before trusting any URL or
checksum inside it. Once the manifest is trusted, every per-file SHA-256
and every `.sha256` sidecar URL it points at becomes trusted by
extension.

A single compromised upstream cannot inject malicious content unless the
attacker also forges a signed manifest.

## Current status

All source is in place. Release infrastructure has landed. Outstanding
provisioning items before a real release ships are documented in
[RELEASING.md](RELEASING.md).

What landed in the last update:

- `RELEASING.md` — step-by-step operator runbook (keypair setup, per-release
  workflow, Kiwix/HF manual interventions, diagnostics, rollback)
- `SECRETS.md` — exhaustive secrets contract (every GitHub secret and variable
  the workflows consume)
- `TROUBLESHOOTING.md` — first-aid for common installer failures
- `build/setup_dev_keypair.py` — local dev keypair generator (wraps
  `generate-keypair.py`; writes to `build/.secrets/` which is gitignored)
- `build/rotate_kiwix_dates.py` — scrapes `download.kiwix.org` and patches
  `release-config.yaml` when ZIM dump dates rotate
- `inno/nvidia-probe/` — proper C# NVIDIA detection shim replacing the
  CI `csc` stub (see `inno/nvidia-probe/README.md` for build instructions)
- `release.yml` "Stage NVIDIA probe" step updated to use `dotnet publish`
  against `inno/nvidia-probe/nvidia-probe.csproj`

External provisioning still needed before a real release (see [RELEASING.md](RELEASING.md)
for detailed steps):

- Cloudflare R2 bucket + `cdn.<domain>` custom domain
- ed25519 release keypair (run `build/generate-keypair.py`)
- Azure Trusted Signing account for code signing
- Branding assets in `inno/branding/` and `first-run/Resources/`
- Curated Kolibri channel IDs (see `manifests/release-config.yaml`)

See `installerplan.txt` §11 for the rollout order and [RELEASING.md](RELEASING.md)
for how to execute each step.
