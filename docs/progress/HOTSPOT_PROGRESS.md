# Hotspot + Auto-Start + Control UI — Progress Tracker

## Part 1 — Hotspot working end-to-end

- [x] Fix WinRT projection failure in `setup_hotspot.ps1` (poll `TetheringOperationalState` instead of unwrapping async)
- [x] Confirm hotspot turns On at 192.168.137.1, HTTP responds on :80
- [x] Diagnose ICS DNS proxy hijacking 192.168.137.1:53 (NXDOMAINs `puente.link`)
- [x] First fix attempt: prepend `127.0.0.1` to Ethernet DNS — ICS skips loopback, falls back to router (NXDOMAIN)
- [x] Second attempt: use Ethernet's own LAN IP — ICS skips host-owned IPs too
- [x] **Working fix**: add `192.168.137.1 puente.link # AIBox-Puente` to `C:\Windows\System32\drivers\etc\hosts`. ICS DNS proxy reads hosts file before forwarding upstream. Verified with `nslookup puente.link 192.168.137.1` → 192.168.137.1.
- [x] Replace ICS-DNS-pivot helpers in `setup_hotspot.ps1` with `Set-HostsEntry` / `Remove-HostsEntry` (using `[System.IO.File]::WriteAllLines` for reliability)
- [x] Wire `Set-HostsEntry` into validate step (after $hostIp known), `Remove-HostsEntry` into Stop path
- [x] Remove Technitium as a hotspot startup dependency; hotspot readiness now relies on Windows Mobile Hotspot + the host `hosts` entry for `puente.link`
- [ ] Re-run `setup_hotspot.ps1` end-to-end with hardened Set-HostsEntry, confirm `hosts_entry_added: true` and JSON `status: ready`
- [ ] Phone-device test: connect to `AIBox-Puente` Wi-Fi, open `http://puente.link/`, verify portal + `/chat/` load

### Key fix (memorize)
**ICS DNS proxy on 192.168.137.1:53 reads the host hosts file before forwarding.** Don't fight ICS — just add the offline domain there. Cleanup on Stop removes `# AIBox-Puente`-tagged lines.

## Part 2 — Stop script, Control UI, Autostart

- [x] `down_stack.ps1` — self-elevates, calls `setup_hotspot.ps1 -Stop`, then `docker compose down`. Emits JSON for UI consumption.
- [x] `aibox_control_ui.ps1` — WPF window with Start / Pause / Stop, status panel reading `network-info.json` every 3s, log textarea, "Copy connect URL" button. Self-elevates on launch. Opening the UI now auto-starts the stack/hotspot if they are not already ready.
- [x] `install_autostart.ps1` — registers Scheduled Task `AIBox-Puente-Startup` at logon (Highest privileges, 45 s delay, `AllowStartIfOnBatteries`), creates Desktop + Start Menu shortcuts with the `RunAsAdministrator` byte flag (offset 21, bit 0x20) set so the UI gets admin on double-click. All paths derived from `$PSScriptRoot` — nothing hard-coded.
- [x] `uninstall_autostart.ps1` — removes task, Desktop shortcut, Start Menu shortcut + empty folder.

## V2–V4 Verification

- [ ] V2: down_stack works (`docker ps` empty, hotspot off). Opening the UI auto-starts to Ready, Pause → off + window stays, Start again → up, Stop → off + window closes. Phone test while UI shows Ready.
- [ ] V3: install autostart, reboot, confirm phone can reach `puente.link` with no PowerShell window or UAC prompt visible. Double-click shortcut → single UAC → UI opens with Ready. Run uninstall, reboot, confirm hotspot does NOT auto-start and shortcut is gone.
- [ ] V4: regression — `py -3 -m pytest aibox/tools/tests/test_rag_pipeline_smoke.py aibox/tools/tests/test_rag_pipeline_smoke_es.py` passes; `docker compose ps` shows all services healthy/running.

## Docs

- [x] One-paragraph "Offline hotspot autostart" section in root `CLAUDE.md` (next to the stack operations block).

---

## Key technical notes (so future-me doesn't re-derive)

- **PowerShell 5.1 cannot project WinRT `IAsyncOperation<T>`**. Async results return as bare `System.__ComObject`. Workaround: poll `$Manager.TetheringOperationalState` until target reached.
- **ICS DNS proxy** binds 192.168.137.1:53 unconditionally and consults the host `hosts` file before forwarding upstream. The working hotspot fix is to write `192.168.137.1 puente.link # AIBox-Puente` there during startup and remove it on stop.
- **Hotspot validation order:** start the hotspot, wait for `192.168.137.1`, verify HTTP on `:80`, write the `hosts` entry, then confirm `puente.link` resolves through the hotspot DNS proxy.
- **Technitium DNS remains optional** for LAN mode and manual local-DNS setups, but hotspot success must not depend on it.
