# Installer build helpers

Local PowerShell + Python helpers that mirror what the GitHub Actions
release workflow does. Useful for testing the pipeline without cutting a
real release.

## Scripts

| Script                  | Purpose                                            |
| ----------------------- | -------------------------------------------------- |
| `generate-keypair.py`   | Create an ed25519 signing keypair (one-time setup) |
| `build-manifest.py`     | Assemble a manifest from inputs + sign it          |
| `verify-manifest.py`    | Verify a manifest signature (parity with the WPF verifier) |
| `build-installer.ps1`   | Local Inno + WPF + signing run                     |
| `sign.ps1`              | Code-sign the .exe (Authenticode)                  |
| `stage-r2-content.ps1`  | Upload Chroma index shards to R2                   |

## One-time setup

```powershell
# 1. Generate the release ed25519 keypair (do this ONCE, ever)
python aibox\installer\build\generate-keypair.py `
    --private-out aibox\installer\build\.secrets\release.ed25519.sk `
    --public-out aibox\installer\first-run\Resources\release-pubkey.ed25519

# 2. Stash the private key in GitHub Actions secrets as
#    AIBOX_MANIFEST_SIGNING_KEY (base64 of the 32-byte private key).
#    Delete the local .secrets\ copy or move it to a password manager.
#    DO NOT commit .secrets\ — see .gitignore.
```

The PUBLIC key under `first-run/Resources/release-pubkey.ed25519` IS
committed (32 raw bytes) — it's the trust root baked into every shipped
installer.

## Cutting a release manifest

```powershell
python aibox\installer\build\build-manifest.py `
    --version 1.0.0 `
    --output aibox\installer\manifests\manifest-1.0.0.json `
    --sign-key aibox\installer\build\.secrets\release.ed25519.sk
```

This will:
1. Resolve each HF repo's current `main` commit SHA via the HF API
   (or use SHAs from the input config if pinned).
2. Compute SHA-256 for every R2 shard listed in the input config.
3. Verify Kiwix `.sha256` sidecar URLs are reachable (no inlining).
4. Verify Kolibri channel IDs are listed on Studio (HEAD check).
5. Emit `manifest-<version>.json` and `manifest-<version>.json.sig`.

## R2 staging

```powershell
# Upload all built Chroma shards from backend-data/ to R2.
# Requires R2 credentials in the environment.
.\stage-r2-content.ps1 -ShardDir aibox\backend-data\chroma_shards `
                       -Bucket aibox-content `
                       -Prefix aibox/chroma/v1/
```

Status: `generate-keypair.py`, `build-manifest.py`, `verify-manifest.py`
implemented. Others are skeletons.
