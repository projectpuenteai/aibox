# AIBox Maintenance

AIBox devices are expected to run offline for long stretches. Perform dependency
and vulnerability reviews on an online development machine, then ship only tested
and reversible updates to field devices.

## Monthly Dependency Review

1. Create a branch and snapshot the current working stack.
2. Review Python dependencies:
   ```powershell
   py -3 -m pip install --upgrade pip-audit
   py -3 -m pip_audit -r .\aibox\tools\ai-control\requirements.txt
   py -3 -m pip_audit -r .\aibox\requirements.txt
   ```
3. Review frontend/test dependencies:
   ```powershell
   Push-Location .\aibox\tools\tests
   npm audit
   Pop-Location
   ```
4. Review container images using an image scanner available on the online build
   machine, such as Docker Scout, Trivy, or Grype. Record image digest changes in
   the **Container Image Update Policy** section below.
5. Review vendored browser assets under `stack/portal/assets/vendor/` against
   their upstream releases.
6. Apply one dependency family at a time. Avoid mixing Python, image, and
   frontend updates in one field release unless a security fix requires it.

## Required Validation

Run these checks before exporting images or copying updates to an offline device:

```powershell
py -3 -m pip install -r .\aibox\tools\tests\requirements-test.txt
powershell -ExecutionPolicy Bypass -File .\aibox\tools\tests\validate_python.ps1
py -3 -m pytest .\aibox\tools\tests\test_storage_migrations.py
py -3 -m pytest .\aibox\tools\tests\test_security_controls.py
py -3 -m pytest .\aibox\tools\tests\test_rag_durability.py
powershell -ExecutionPolicy Bypass -File .\aibox\tools\tests\compose_config_redacted.ps1
docker compose -f .\aibox\stack\docker-compose.yaml up -d
docker compose -f .\aibox\stack\docker-compose.yaml ps
```

Do not paste raw `docker compose config` output into issues or chat. It expands
real values from `stack/.env`, including deployment secrets.

Then manually verify login, admin status, chat, saved chats, docs, English and
Spanish RAG, Kiwix, Kolibri, and hotspot client access.

## Rollback Notes

- Keep the previous compose file, `.env`, image digests, and `backend-data`
  snapshot until the replacement has passed a storage disaster drill.
- Do not upgrade the live field database without a tested backup and restore
  path.
- If an update fails after deployment, restore the previous code/image bundle and
  rerun the startup preflight before opening the device to students.

## Encoding Hygiene

- Keep hand-written docs and scripts UTF-8 encoded.
- Prefer ASCII punctuation in operational docs and comments unless names or UI
  copy require Unicode.
- When editing PowerShell, write JSON with `Set-Content -Encoding UTF8` or an
  equivalent explicit encoding.
- Do not edit vendored minified assets only to normalize formatting or encoding.

## Container Image Update Policy

AIBox is designed for offline field deployments, so image updates must be deliberate and reversible. Do not replace digest-pinned images during field work unless a fix depends on it.

### Current Image Pins

| Service | Image reference | Policy |
|---|---|---|
| `ai-control` base | `python:3.11-slim@sha256:9a7765b36773a37061455b332f18e265e7f58f6fea9c419a550d2a8b0e9db834` | Multi-arch digest pin for Python 3.11.15-slim-trixie. Rebuild and run Python validation before changing. |
| `dns` | `technitium/dns-server:latest@sha256:85c2...` | Digest pinned. Review upstream release notes before changing the digest. |
| `caddy` | `caddy:2@sha256:ec18ee54aab3315c22e25f3b2babda73ff8007d39b13b3bd1bfffa2f0444c7d9` | Multi-arch digest pin for Caddy 2.11.3-alpine. Refresh during monthly image review. |
| `llama` | `ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:5c92...` | Digest pinned. Update only after GPU smoke test and chat/RAG validation. |
| `chat` | `ghcr.io/open-webui/open-webui:main@sha256:fd69...` | Digest pinned but branch label is moving. Treat digest as authoritative. |
| `kiwix-en` / `kiwix-es` | `ghcr.io/kiwix/kiwix-serve:latest@sha256:e21b...` | Digest pinned. Update only after both ZIM routes are tested. |
| `kolibri` | `treehouses/kolibri:latest@sha256:a68b...` | Digest pinned. Update only after content library and login checks. |

### Monthly Image Review

1. On an online development machine, check upstream release notes for security fixes.
2. Pull candidate images by explicit digest.
3. Update `aibox/stack/docker-compose.yaml` in a branch.
4. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File aibox\tools\tests\compose_config_redacted.ps1
   docker compose -f aibox\stack\docker-compose.yaml up -d
   docker compose -f aibox\stack\docker-compose.yaml ps
   ```
5. Validate portal, AI chat, RAG, Kiwix English/Spanish, Kolibri, DNS, and hotspot access.
6. Export or cache the tested images before shipping to offline devices.

### Image Rollback

Keep the previous compose file and image cache until the new build has passed a full storage disaster drill and field-style startup. Roll back by restoring the previous digest references and running:

```powershell
docker compose -f aibox\stack\docker-compose.yaml up -d --no-recreate
```

## PowerShell Validation

Use this checklist after changing scripts under `aibox/tools/llama-runtime/scripts` or `aibox/scripts/windows`.

### Static Checks

Install PSScriptAnalyzer on a development machine when internet access is available:

```powershell
Install-Module PSScriptAnalyzer -Scope CurrentUser
Invoke-ScriptAnalyzer -Path .\aibox\tools\llama-runtime\scripts -Recurse
Invoke-ScriptAnalyzer -Path .\aibox\scripts\windows -Recurse
```

### Manual Edge Cases

Run the startup/control scripts from:

- a path with spaces
- a non-admin PowerShell session
- an admin PowerShell session
- a Windows user profile where Docker Desktop is not already running
- a machine with no Wi-Fi adapter or with hotspot disabled
- a machine where elevation is cancelled

### Encoding Rules

- Write JSON with `ConvertTo-Json` and `Set-Content -Encoding UTF8`.
- Use `-LiteralPath` when deleting or reading a path that came from a variable.
- Avoid constructing commands as strings; prefer argument arrays.
- Never add `--volumes`, `docker system prune`, or `docker image prune -a` to cleanup scripts.
