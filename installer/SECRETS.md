# AIBox Secrets Contract

Every secret and repository variable consumed by `.github/workflows/release.yml`
and `.github/workflows/stage-content.yml`. Configure these in the GitHub repo under
**Settings → Secrets and variables → Actions** before pushing the first tag.

See [RELEASING.md](RELEASING.md) for how to generate values that require local tooling.

---

## Secrets

| Name | Type | Purpose | Where to get |
|------|------|---------|--------------|
| `AIBOX_MANIFEST_PRIVKEY` | Secret | Base64-encoded 32-byte ed25519 private key seed. Used by `sign-manifest.py` to sign `manifest-<v>.json`. **Never share or log this value.** | Run `build/generate-keypair.py`; copy the printed base64 private value. |
| `AIBOX_MANIFEST_PUBKEY_B64` | Secret | Base64-encoded 32-byte ed25519 public key. Baked into the WPF First Run resource file at build time by the `build-first-run` CI job. | Same `generate-keypair.py` run; copy the printed base64 public value. Also commit the decoded bytes to `first-run/Resources/release-pubkey.ed25519`. |
| `AIBOX_R2_ACCOUNT_ID` | Secret | Cloudflare account ID used to construct the R2 S3-compatible endpoint URL (`https://<account_id>.r2.cloudflarestorage.com`). | Cloudflare dashboard → R2 → Overview → Account ID (top right). |
| `AIBOX_R2_ACCESS_KEY_ID` | Secret | API token key ID for R2 object writes. | Cloudflare dashboard → R2 → Manage R2 API tokens → Create token (Object Read & Write on the target bucket). |
| `AIBOX_R2_SECRET_ACCESS_KEY` | Secret | API token secret paired with `AIBOX_R2_ACCESS_KEY_ID`. | Shown once at token creation time in the Cloudflare dashboard. |
| `AZURE_SIGNING_TENANT` | Secret | Azure Active Directory tenant ID (GUID) for the subscription that holds the Trusted Signing account. | Azure portal → Azure Active Directory → Overview → Tenant ID. |
| `AZURE_SIGNING_CLIENT_ID` | Secret | App registration (service principal) client ID used by the CI to authenticate against Azure. | Azure portal → App registrations → your CI app → Application (client) ID. |
| `AZURE_SIGNING_CLIENT_SECRET` | Secret | Client secret for the service principal above. | Azure portal → App registrations → Certificates & secrets → New client secret. |
| `AZURE_SIGNING_ACCOUNT` | Secret | Name of the Azure Trusted Signing account resource (not the Azure account). | Azure portal → Trusted Signing → your account → Overview → Name. |
| `AZURE_SIGNING_PROFILE` | Secret | Name of the certificate profile within the Trusted Signing account. | Azure portal → Trusted Signing → your account → Certificate profiles → profile name. |
| `AZURE_SIGNING_ENDPOINT` | Secret | Regional endpoint for the Trusted Signing service (e.g. `eus.codesigning.azure.net`). | [Azure Trusted Signing regional endpoints](https://learn.microsoft.com/azure/trusted-signing/concept-trusted-signing-resources-roles#supported-regions). |
| `GITHUB_TOKEN` | Auto (Actions) | Automatically injected by GitHub Actions. Used to push images to GHCR (`packages: write`) and to create GitHub releases (`contents: write`). | No action required — GitHub provides this automatically per run. |

---

## Repository Variables

Repository variables are non-secret configuration values. Set them under
**Settings → Secrets and variables → Actions → Variables**.

| Name | Type | Purpose | Where to get |
|------|------|---------|--------------|
| `AIBOX_R2_BUCKET` | Variable | Name of the Cloudflare R2 bucket that holds installer content (e.g. `aibox-content`). | The bucket name you chose when creating the R2 bucket. |
| `AIBOX_R2_BASE` | Variable | Public base URL for R2-hosted content, used by `build_manifest.py` to construct item URLs (e.g. `https://cdn.projectpuenteai.org/aibox`). Must not have a trailing slash. | The custom domain you configured on the R2 bucket + your preferred path prefix. |
| `DOCKER_DESKTOP_SHA256` | Variable | Expected lowercase SHA-256 hex digest of the Docker Desktop installer downloaded during the `build-inno` job. If unset, the hash check is skipped (warning only). | Download Docker Desktop from `desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe`, run `Get-FileHash ... -Algorithm SHA256`, and paste the lowercase result. Refresh whenever you want to pin a new Docker Desktop version. |

---

## Optional / Future Secrets

| Name | Type | Purpose |
|------|------|---------|
| `AIBOX_HF_TOKEN` | Secret | Hugging Face API token. RESERVED — not currently referenced by any workflow; included for future HF rate-limit headroom. Not required for public repos. Generate at `huggingface.co/settings/tokens`. |

---

## Notes

- **Rotation policy** — `AIBOX_MANIFEST_PRIVKEY` / `AIBOX_MANIFEST_PUBKEY_B64` must be rotated
  together. Rotating them invalidates all previously shipped installers' signature verification.
  Coordinate a new `first-run/Resources/release-pubkey.ed25519` commit before any rotation.
- **Signing is soft-required** — if the `AZURE_SIGNING_*` secrets are absent, the CI workflow
  skips code signing and emits unsigned artifacts. The manifest signing step (`AIBOX_MANIFEST_PRIVKEY`)
  is hard-required and will fail the build if missing.
- **R2 credentials scope** — grant the R2 API token the minimum scope: Object Read & Write on
  the target bucket only. Do not use the global Cloudflare API key.
