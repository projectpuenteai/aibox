# AIBox Installer

The supported install path is **USB + PowerShell** (offline content delivery). See the
"USB install" section in the repo `CLAUDE.md` and the active `INSTALLER_DOWNLOAD_AUDIT.md`
at the workspace root.

## What lives here now

- **`admin-console/`** — WPF operator console (`AIBox Admin Console.exe`). This is the
  live control panel the autostart shortcut launches (see the "Offline hotspot autostart"
  section in `CLAUDE.md`). Built with:
  `dotnet publish aibox/installer/admin-console/AIBoxAdminConsole.csproj -c Release -r win-x64 --no-self-contained`

## Retired: download-based installer

The two-stage download installer (Inno `.exe` + WPF First Run app + Cloudflare-R2
ed25519-signed manifest + the `release.yml` CI pipeline) was **retired on 2026-05-26** when
the project pivoted to USB delivery. Its source is archived at
`legacy/installer-download-path/` (see the `RETIRED.md` there) and is recoverable from git
history. Do not revive it without re-reading that note — its network fetchers, R2 bucket,
and signing pipeline are no longer maintained.
