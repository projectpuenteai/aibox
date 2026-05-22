# Inno Setup wizard (Phase A)

Builds `AIBox-Setup-<version>.exe`. This is the ~700 MB installer the
end user downloads from a GitHub release. It lays down the AIBox source
tree, installs the bundled Docker Desktop MSI if Docker isn't already
present, and writes the initial `install-state.json`.

## What this stage does NOT do

- No `.env` generation — waits for the admin password in Phase C.
- No `docker compose pull` — Docker may not be running yet.
- No content download — that's the First Run app's job (~100 GB).
- No autostart registration — also Phase C.

## Files

- `AIBox.iss` — main Inno Setup script with preflight + reboot-resume
  inlined (the design originally called for split `.iss` files but
  Inno's `[Code]` section is simpler kept in one place).
- `branding/PREINSTALL.rtf`, `branding/LICENSE.rtf` — wizard text.
- `branding/banner.bmp`, `banner-small.bmp`, `app.ico` — visuals,
  supplied by the design team. Not checked into git yet.

## Build prerequisites

- Inno Setup 6+ on a Windows runner (`iscc.exe` on PATH).
- The CI workflow pre-stages these artifacts under `dist/stage/`:
  - `first-run/` — compiled WPF First Run binaries (from `dotnet publish`)
  - `DockerDesktopInstaller.exe` — bundled MSI (fresh per build, hash-verified)
  - `nvidia-probe.exe` — tiny CUDA-detection shim
  - `RELEASE_COMMIT.txt` — the git SHA being built

## Local build

```powershell
iscc.exe /Dversion=0.0.1 AIBox.iss
```

Produces `dist/AIBox-Setup-0.0.1.exe` (unsigned). The CI release workflow
does the same with `/Dversion=$RELEASE_VERSION` and runs signtool on the
output before publishing to a GitHub release.

## Manual smoke test

1. On a clean Windows VM, run the produced setup `.exe`.
2. Verify the preflight page reports OK for an NVIDIA-equipped machine
   and FAIL with a hard block for one without.
3. Verify `%ProgramData%\AIBox\install-state.json` after install:
   `phase_a_complete: true`, `phase_b_complete: false`.
4. Verify `AIBox First Run` desktop shortcut points to
   `%ProgramFiles%\AIBox\AIBoxFirstRun\AIBox First Run.exe`.

## Reboot resume

If Docker Desktop's MSI requests a reboot, the wizard registers a
`RunOnce` value that re-invokes itself after login so the user lands
back on the final page without manual intervention.
