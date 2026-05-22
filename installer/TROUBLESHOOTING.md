# AIBox Installer — Troubleshooting

First-aid for common installer failures. For operator-level runbook and rollback
procedures, see [RELEASING.md](RELEASING.md).

---

## "Preflight failed: NVIDIA GPU not detected"

**Symptom:** The Inno Setup wizard shows a hard-block dialog and refuses to continue.

**Cause:** `nvidia-probe.exe` exited with code 1 — neither `nvidia-smi` nor `nvcuda.dll`
was found. This means the NVIDIA driver is missing or badly broken.

**Fix:**
1. Download and install the latest Game Ready or Studio driver from
   [nvidia.com/drivers](https://www.nvidia.com/drivers).
2. Reboot when prompted.
3. Verify the driver is working: open Device Manager → Display adapters → your GPU
   should appear without a warning triangle. Optionally run `nvidia-smi` in a terminal
   to confirm it prints a GPU name.
4. Re-run `AIBox-Setup-<v>.exe`.

If the GPU is present but `nvidia-smi` is not on `PATH` and `nvcuda.dll` is absent,
the driver installation may be partial. Uninstall via DDU (Display Driver Uninstaller)
and reinstall cleanly.

---

## "Manifest signature did NOT verify"

**Symptom:** The WPF First Run app shows "Manifest signature verification failed" and
stops downloading.

**Cause:** The downloaded manifest does not match the embedded public key. This can happen
if:
- The file was corrupted in transit.
- You are testing with a dev keypair that does not match the embedded `release-pubkey.ed25519`.
- The installer was downloaded from an unofficial mirror.

**Fix:**
1. Delete any partial download of the installer.
2. Download `AIBox-Setup-<v>.exe` directly from the official GitHub release page:
   `https://github.com/projectpuenteai/aibox/releases`
3. Verify the `.sha256` sidecar matches before running:
   ```powershell
   (Get-FileHash AIBox-Setup-1.0.0.exe -Algorithm SHA256).Hash.ToLowerInvariant()
   # compare to the contents of AIBox-Setup-1.0.0.exe.sha256
   ```
4. Re-run the installer.

**For operators/developers:** If you are testing locally with a dev keypair, make sure
the pubkey baked into the WPF app at build time (`first-run/Resources/release-pubkey.ed25519`)
matches the private key you are signing the manifest with. Run `build/setup_dev_keypair.py`
and follow its printed instructions.

---

## "Docker Desktop is not running"

**Symptom:** The WPF First Run app's "Docker" phase shows a red status and cannot pull images.

**Cause:** Docker Desktop is installed but not started, or its engine has crashed.

**Fix:**
1. Open Docker Desktop from the Start Menu or System Tray.
2. Wait for the green "Docker Desktop is running" indicator in the tray icon tooltip.
   This can take 30–60 seconds on first launch after a reboot.
3. Click **Retry** in the First Run app, or close and re-run it — it resumes from where it left off.

If Docker Desktop shows an error on startup:
- Run it as Administrator once: right-click → "Run as administrator".
- If WSL 2 errors appear, open PowerShell as admin and run:
  ```powershell
  wsl --update
  wsl --shutdown
  ```
  Then restart Docker Desktop.

---

## "Hugging Face rate-limiting / 429 errors"

**Symptom:** The First Run app stalls on a model download with repeated retry messages
referencing HTTP 429.

**Cause:** Anonymous HF downloads are rate-limited. Large GGUF files are particularly
affected.

**Fix:**
1. Create a free Hugging Face account at [huggingface.co/join](https://huggingface.co/join).
2. Generate a read token at `huggingface.co/settings/tokens`.
3. Set the token in the First Run app's environment before launching it, or set it as a
   system environment variable:
   ```powershell
   $env:AIBOX_HF_TOKEN = "hf_..."
   # then re-run the First Run app
   ```
4. The download will resume using authenticated requests, which have a much higher rate limit.

For operators staging a release: add `AIBOX_HF_TOKEN` as a GitHub Actions secret to avoid
rate-limiting during manifest builds (see [SECRETS.md](SECRETS.md)).

---

## "Smoke test failed"

**Symptom:** At the end of installation, the First Run app reports that the smoke test failed
and offers to generate a diagnostics bundle.

**What the smoke test checks:**
- Docker stack is up and all containers are healthy.
- The `ai-control` API responds on `/health`.
- The Kiwix server is reachable.
- The Chroma index is loaded and queryable.

**Fix:**
1. Click **Save diagnostics** in the app. The bundle is saved to:
   ```
   %LOCALAPPDATA%\AIBox\logs\diagnostics-<timestamp>.zip
   ```
2. Open the zip and check `first-run.log` for the first `ERROR` line after "Starting smoke test".
3. Common sub-failures:
   - **Container unhealthy** — run `docker ps -a` and `docker logs <container>` to see why.
   - **`ai-control` not responding** — check `docker logs aibox-ai-control-1`; VRAM exhaustion
     is common on 6 GB GPUs. Try reducing `N_GPU_LAYERS` in `aibox/stack/.env`.
   - **Chroma empty** — the Chroma shard may have failed to extract; check `backend-data/chroma_db/`.

4. After fixing the root cause, re-run the smoke test without reinstalling:
   ```powershell
   powershell -ExecutionPolicy Bypass -File aibox\tools\llama-runtime\scripts\up_stack.ps1
   ```

If you cannot resolve the issue, open a GitHub issue and attach the diagnostics zip (redact
any personal data first).

---

## First Run app crashes on launch

**Symptom:** The WPF First Run app closes immediately or shows an unhandled exception dialog.

**Fix:**
1. Check the log at `%LOCALAPPDATA%\AIBox\logs\first-run.log`.
2. Ensure .NET 8 Desktop Runtime (x64) is installed:
   [dotnet.microsoft.com/download/dotnet/8.0](https://dotnet.microsoft.com/download/dotnet/8.0).
3. Run as Administrator if the log shows access-denied errors writing to `%ProgramFiles%\AIBox`.

---

## General: how to collect logs

```powershell
# All First Run logs
Get-Content "$env:LOCALAPPDATA\AIBox\logs\first-run.log" | Select-Object -Last 100

# Docker container logs (run after docker compose up)
docker compose -f aibox/stack/docker-compose.yaml logs --tail=50
```
