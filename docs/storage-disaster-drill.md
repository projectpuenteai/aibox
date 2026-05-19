# AIBox Storage Disaster Drill

Use this drill before field deployments and after major storage changes. The goal is to prove that a backup can restore a working AIBox on a clean machine without depending on the original disk.

## Assets That Must Be Backed Up

- `aibox/backend-data/appdata`: SQLite DB, encrypted docs/chats, cleanup manifests, backup markers.
- `aibox/backend-data/chroma_db`: English Chroma index.
- `aibox/backend-data/chroma_db_es`: Spanish Chroma index.
- `aibox/backend-data/ai-control`: control-plane state.
- `aibox/models`: LLM, embedding, and rerank models.
- `aibox/kiwix`: ZIM files.
- `aibox/kolibri-data`: Kolibri runtime data.
- Docker named volumes: `dns_data`, `caddy_data`, `caddy_config` if preserving DNS/Caddy state matters.
- `aibox/stack/.env`: secrets and deployment configuration. Store separately from normal content backups.

## Drill Steps

1. Stop the source stack.
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\down_stack.ps1
   ```

2. Copy the required assets to external storage. Keep `.env` encrypted or offline.

3. Verify the appdata backup.
   ```powershell
   $env:APP_ENCRYPTION_MASTER_KEY = "<CURRENT_BASE64_KEY>"
   py -3 .\aibox\tools\storage\verify_storage_backup.py --appdata-root .\aibox\backend-data\appdata
   ```

4. Write a verified-backup marker after verification passes.
   ```powershell
   $marker = @{
     verified = $true
     verified_at = (Get-Date).ToUniversalTime().ToString("o")
     source = "manual-disaster-drill"
   }
   New-Item -ItemType Directory -Force .\aibox\backend-data\appdata\backups | Out-Null
   $marker | ConvertTo-Json -Compress | Set-Content .\aibox\backend-data\appdata\backups\latest_verified_backup.json -Encoding UTF8
   ```

5. Restore onto a clean test machine or clean checkout.

6. Start the stack.
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\up_stack.ps1
   ```

7. Validate core workflows:
   - Sign in as admin.
   - Sign in as a normal user.
   - Open an existing document.
   - Open an existing chat.
   - Send a short AI message.
   - Confirm English and Spanish RAG diagnostics show the expected collection paths/counts.
   - Open Kiwix English and Spanish pages.
   - Open Kolibri.
   - Connect a client through the hotspot and load the portal.

8. Run a cleanup dry-run from the admin API or portal tooling and confirm a manifest appears under `appdata/cleanup-manifests`.

## Pass Criteria

- SQLite `integrity_check` passes.
- At least one encrypted doc/chat decrypts.
- No `decrypt_blob` warnings appear in `ai-control` logs during validation.
- Portal, chat, docs, wiki, learn, and admin status load.
- RAG status distinguishes ready, missing index, and no-context states.
- Cleanup dry-run produces a manifest and does not delete files.

## Failure Handling

Do not continue a failed drill by editing live data. Record the failed step, preserve logs and manifests, and restore from the previous known-good backup.
