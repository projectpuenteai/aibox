# AIBox / Project Puente AI Security Runbook

Operator procedures for rotating secrets, verifying backups, recovering admin access, and reacting to compromise. Commands assume the project root `C:\AIBox` on a Windows host running Docker Desktop.

Before any rotation, stop the stack and snapshot `backend-data/` to a separate disk. Encryption-key rotation is destructive on failure.

For routine startup, cleanup, and prohibited field-device commands, see `docs/operator-safe-commands.md`.

## Secrets Inventory

The stack requires these env vars to start. They live in `aibox/stack/.env`, which must stay out of git.

| Env var | Purpose | Rotation impact |
|---|---|---|
| `APP_ENCRYPTION_MASTER_KEY` | AES-256-GCM master key for stored docs/chats/JSON blobs | High: encrypted blobs must be re-encrypted before swap |
| `SESSION_TOKEN_PEPPER` | Server-side pepper mixed into session-token hashes | Medium: invalidates all active sessions |
| `ADMIN_DEFAULT_PASSWORD` | Initial admin password, only applied when the admin user is first created | Low: reset later through admin tooling |
| `DNS_ADMIN_PASSWORD` | Technitium DNS admin UI password | Low: bound to 127.0.0.1 only |

The encryption envelope format is JSON: `{"v": <enc_version>, "alg": "AES-256-GCM", "nonce": <b64>, "ciphertext": <b64>}`.

## 1. Rotate `APP_ENCRYPTION_MASTER_KEY`

Use `aibox/tools/storage/rotate_encryption_key.py`. Test on a copy of `backend-data/` first; never start with live data.

### 1.1 Pre-Flight

1. Notify users that the stack will be unavailable.
2. Stop the stack:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\down_stack.ps1
   ```
3. Snapshot:
   ```powershell
   $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
   Copy-Item -Recurse -Path .\aibox\backend-data -Destination ".\aibox\backend-data-snapshot-$stamp"
   ```
4. Generate a new 32-byte base64 key:
   ```powershell
   $bytes = New-Object byte[] 32
   [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
   [Convert]::ToBase64String($bytes)
   ```

### 1.2 Re-Encrypt Existing Blobs

Encrypted files live under `aibox/backend-data/appdata/users/<uid>/`. The rotation tool loads the old key from `AIBOX_OLD_ENCRYPTION_MASTER_KEY` or a hidden prompt, loads the new key from `AIBOX_NEW_ENCRYPTION_MASTER_KEY` or a hidden prompt, backs up originals, re-encrypts each file atomically, and verifies the new blob before replacing the source file.

Dry-run first:

```powershell
$env:AIBOX_OLD_ENCRYPTION_MASTER_KEY = "<OLD_BASE64_KEY>"
$env:AIBOX_NEW_ENCRYPTION_MASTER_KEY = "<NEW_BASE64_KEY>"
py -3 .\aibox\tools\storage\rotate_encryption_key.py --dry-run --users-root .\aibox\backend-data\appdata\users
```

Apply after the dry-run reports zero failures:

```powershell
py -3 .\aibox\tools\storage\rotate_encryption_key.py `
  --users-root .\aibox\backend-data\appdata\users `
  --backup-root .\aibox\backend-data\appdata\backups\key-rotation `
  --manifest .\aibox\backend-data\appdata\backups\key-rotation\last-rotation.json `
  --stop-on-error
```

The script intentionally does not accept keys as command-line flags so secrets do not land in shell history. If you lose the old key, encrypted data is unrecoverable.

### 1.3 Roll Forward

1. Replace `APP_ENCRYPTION_MASTER_KEY` in `aibox/stack/.env` with the new key.
2. Bring the stack back up:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\aibox\tools\llama-runtime\scripts\up_stack.ps1
   ```
3. Validate by signing in as a known user, opening a saved chat or doc, and checking `docker logs aibox-ai-control` for `decrypt_blob` warnings.
4. Verify backup/appdata readability:
   ```powershell
   $env:APP_ENCRYPTION_MASTER_KEY = "<NEW_BASE64_KEY>"
   py -3 .\aibox\tools\storage\verify_storage_backup.py --appdata-root .\aibox\backend-data\appdata
   ```

### 1.4 Roll Back

If validation fails:

1. Run `down_stack.ps1`.
2. Remove `.\aibox\backend-data` only after confirming the snapshot is intact.
3. Restore the snapshot to `.\aibox\backend-data`.
4. Restore the old `APP_ENCRYPTION_MASTER_KEY` value in `.env`.
5. Run `up_stack.ps1`.

## 2. Rotate `SESSION_TOKEN_PEPPER`

Effect: every active session token becomes invalid; every signed-in user must sign in again. Stored passwords are unaffected.

1. Announce a maintenance window.
2. Generate a new pepper:
   ```powershell
   $bytes = New-Object byte[] 32
   [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
   [Convert]::ToBase64String($bytes)
   ```
3. Update `SESSION_TOKEN_PEPPER` in `aibox/stack/.env`.
4. Restart `ai-control`:
   ```powershell
   docker compose -f aibox/stack/docker-compose.yaml up -d ai-control
   ```
5. Validate that old sessions require login and new sessions work.

## 3. Cleanup Safety

Cleanup now writes manifests and can be dry-run from the admin API:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1/ai/api/v1/app/admin/storage-cleanup" `
  -WebSession $session `
  -ContentType "application/json" `
  -Body '{"dry_run":true,"reason":"pre-maintenance-check"}'
```

To require a recent verified backup before cleanup deletes anything, set these in `aibox/stack/.env`:

```env
CLEANUP_REQUIRE_BACKUP_MARKER=true
CLEANUP_BACKUP_MARKER_PATH=/data/backups/latest_verified_backup.json
CLEANUP_BACKUP_MARKER_MAX_HOURS=72
```

The marker file must contain:

```json
{"verified": true, "verified_at": "2026-05-15T00:00:00+00:00"}
```

Orphaned user directories are now moved to `/data/orphan-user-quarantine` with a manifest instead of being immediately deleted.

## 4. Verify A Backup

Run this against a copied appdata backup or the live appdata volume while the stack is stopped:

```powershell
$env:APP_ENCRYPTION_MASTER_KEY = "<CURRENT_BASE64_KEY>"
py -3 .\aibox\tools\storage\verify_storage_backup.py --appdata-root .\aibox\backend-data\appdata
```

When the verification passes, write/update the cleanup marker in the appdata backup directory:

```powershell
$marker = @{
  verified = $true
  verified_at = (Get-Date).ToUniversalTime().ToString("o")
}
$marker | ConvertTo-Json -Compress | Set-Content .\aibox\backend-data\appdata\backups\latest_verified_backup.json -Encoding UTF8
```

## 5. Reset Admin Password

`ADMIN_DEFAULT_PASSWORD` is only honored when the admin user is first created. After that, use the admin portal reset action when possible. If portal access is lost, reset through SQLite only after stopping `ai-control` and taking a DB snapshot.

## 6. Compromise Response

1. Disconnect from the network.
2. Run `down_stack.ps1`.
3. Snapshot `backend-data/` for forensics.
4. Rotate `SESSION_TOKEN_PEPPER`.
5. Rotate `APP_ENCRYPTION_MASTER_KEY` if the master key may be exposed.
6. Reset admin password.
7. Review `security_events` and `docker logs aibox-ai-control`.

## References

- Encryption helpers: `aibox/tools/ai-control/app_storage.py` and `aibox/tools/storage/crypto_blobs.py`
- Key rotation tool: `aibox/tools/storage/rotate_encryption_key.py`
- Backup verifier: `aibox/tools/storage/verify_storage_backup.py`
- Required env vars: `aibox/stack/.env.example`
- Disaster drill: `aibox/docs/storage-disaster-drill.md`
