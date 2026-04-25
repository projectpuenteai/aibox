# Windows Hotspot Demo — Operator Guide

This guide covers the Windows-only demo workflow for Project Puente AI: a
laptop running the full Docker stack broadcasts its own Wi-Fi hotspot, and
student devices connect directly to it — no external router and no internet
required.

Linux with `hostapd`/`dnsmasq` is the long-term target for production devices;
see the [Linux migration path](#linux-migration-path) section below.

---

## What this does

1. Registers a Windows **Mobile Hotspot** (Wi-Fi access point) broadcasting a
   configurable SSID and WPA2 passphrase. The laptop becomes the gateway at
   `192.168.137.1`.
2. Starts the AIBox Docker stack (`caddy`, `ai-control`, `llama`, `chat`,
   `kolibri`, `kiwix`, `dns`) bound to `0.0.0.0:80` so connected devices can
   reach it.
3. Maps the offline hostname (default `puente.link`) → `192.168.137.1` in
   both the Windows `hosts` file and the Technitium DNS container so students
   can type a friendly URL instead of the raw IP.
4. Creates Windows Firewall inbound rules for HTTP (port 80) and DNS (port
   53) scoped to the hotspot subnet.
5. Provides PASS/WARN/FAIL diagnostics before and after startup so you know
   whether the demo is actually ready.

---

## One-time setup

Run once, on the laptop that will host the demo:

1. **Install prerequisites**
   - Windows 10 or Windows 11 (Home, Pro, or Education)
   - Docker Desktop (WSL2 backend), running
   - A working Wi-Fi adapter that supports hosted network / Mobile Hotspot
     (see [Recommended Wi-Fi adapter specs](#recommended-wi-fi-adapter-specs))

2. **Configure secrets**. Copy `aibox/stack/.env.example` to
   `aibox/stack/.env` and fill the **required** values
   (`APP_ENCRYPTION_MASTER_KEY`, `ADMIN_DEFAULT_PASSWORD`,
   `SESSION_TOKEN_PEPPER`). Also set, at minimum:

   ```env
   HOTSPOT_SSID=AIBox-Puente
   HOTSPOT_KEY=change-this-password
   OFFLINE_HOSTNAME=puente.link
   SESSION_COOKIE_SECURE=false
   DNS_ADMIN_PASSWORD=change-this-dns-admin-password
   ```

   Change the `HOTSPOT_KEY` from the example default before any real demo.
   `SESSION_COOKIE_SECURE=false` is required for HTTP-only LAN use — without
   it, browsers silently drop the session cookie over plain HTTP.

3. **Run the capability check**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File aibox\scripts\windows\check-hotspot-capability.ps1
   ```
   Resolve any `[FAIL]` items before continuing. `[WARN]` items are usually
   informational (e.g., "not running as Administrator" — the start scripts
   self-elevate).

4. **(Optional) Install autostart**, so the stack + hotspot come up at every
   logon without a UAC prompt:
   ```powershell
   powershell -ExecutionPolicy Bypass -File aibox\scripts\windows\install-startup-task.ps1
   ```
   This registers the `AIBox-Puente-Startup` scheduled task (logon trigger,
   45 s delay) and creates Desktop + Start Menu shortcuts to the WPF control
   panel.

---

## Daily startup

With autostart installed: **boot the laptop, log in, wait about a minute**.
Verify from another command window:

```powershell
powershell -ExecutionPolicy Bypass -File aibox\scripts\windows\test-demo-network.ps1
```

The expected output ends with:

```
==========================================
  Demo-readiness summary
==========================================
  Ready for demo : YES
  Student Wi-Fi  : AIBox-Puente
  Password       : (hidden — use -ShowPassword to display)
  Portal URL     : http://puente.link/
  Fallback URL   : http://192.168.137.1/
  Counts         : N PASS, 0-2 WARN, 0 FAIL
  Fixes needed   : none
==========================================
```

Without autostart, start manually:

```powershell
powershell -ExecutionPolicy Bypass -File aibox\scripts\windows\start-demo-stack.ps1
```

---

## How students connect

Tell students:

1. Open Wi-Fi settings, join `AIBox-Puente` (or whatever you set `HOTSPOT_SSID`
   to), password from `HOTSPOT_KEY`.
2. Open a browser, go to `http://puente.link/`.
3. If the hostname doesn't resolve, fall back to `http://192.168.137.1/`.

No internet is needed. If the laptop is on Ethernet upstream, students
connecting to the hotspot will be routed through Windows Internet Connection
Sharing automatically — this is Windows' default behavior for a Mobile
Hotspot. The demo stack does not depend on upstream connectivity either way.

---

## Known Windows limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| Mobile Hotspot requires admin | First run prompts for UAC elevation | `start-demo-stack.ps1` self-elevates. Autostart runs under `BUILTIN\Administrators` with `RunLevel Highest` so no UAC fires after install. |
| Windows 10 Home Mobile Hotspot turns off when upstream Wi-Fi disconnects | Not an issue when upstream is Ethernet or "none" | Use Ethernet upstream or run without internet; Windows keeps the hotspot up in that case. |
| **MT7921 (and similar single-radio chipsets) cannot do STA+AP simultaneously on different bands** | **Hotspot start times out / never broadcasts when the laptop is also connected as a Wi-Fi client to a network on a different channel.** Capability check passes; `StartTetheringAsync` succeeds but `TetheringOperationalState` never reaches `On`. | **Either** disconnect from upstream Wi-Fi before starting the hotspot, **or** plug in Ethernet so the Wi-Fi radio is free for AP mode, **or** add a USB Wi-Fi dongle to act as a second radio. The deployment scenario (offline classroom, no upstream) avoids this entirely. |
| **Legacy `Hosted network supported : No` from `netsh wlan show drivers`** | Looks alarming but is **not decisive**. Microsoft deprecated the legacy hosted-network OIDs around Win10 1607; modern Mobile Hotspot uses the Wi-Fi Direct path instead. Almost every modern Wi-Fi 6/6E adapter (Intel AX2xx, MediaTek MT79xx, Qualcomm) reports "No" here even when Mobile Hotspot works. | Ignore unless `Wi-Fi Direct Virtual Adapter` is also missing in `Get-NetAdapter -IncludeHidden`. |
| Some Realtek built-in drivers advertise AP capability but fail to broadcast | Silent fail: SSID does not appear | Check `check-hotspot-capability.ps1` output; replace with Intel AX201/AX210 or a SoftAP-capable USB dongle if unreliable. |
| Windows Defender Firewall profiles may reset after major feature updates | Students lose portal access after Windows update | Re-run `start-demo-stack.ps1` — `setup_hotspot.ps1` re-creates the firewall rules idempotently. |
| Mobile Hotspot SSID is 2.4 GHz by default on most adapters | Slower, more congested | Set `HOTSPOT_WIFI_BAND=5ghz` in `.env` if the adapter supports it (many budget adapters do not). |
| Hotspot password visible in `aibox/stack/.env` | Secret on disk | Use a unique `HOTSPOT_KEY` per device and treat `.env` like any other secrets file. |
| WSL2 Docker networking is NAT-bridged, not bridged | Caddy must bind `0.0.0.0`, not a specific IP | Already configured in `docker-compose.yaml` (`- "80:80"`) and `Caddyfile` (`:80 { ... }`). |

---

## Troubleshooting

### "SSID is not visible on student devices"

1. Run `check-hotspot-capability.ps1` — look for FAIL on `Driver hosted-network
   support`. If it says the driver reports "No," the adapter cannot broadcast
   regardless of what the WinRT capability check says (this is the MediaTek
   MT79xx case — see Known Limitations).
2. Run `diagnose-hotspot-failure.ps1` to capture the exact failure reason —
   it invokes `setup_hotspot.ps1` standalone with full JSON output and
   inspects WLAN-AutoConfig event log entries.
3. Check the Windows Settings → Network & Internet → Mobile Hotspot toggle
   state. If it's off, something stopped it between startup and now
   (Windows 10 Home auto-off being the most common cause).
4. Run `start-hotspot.ps1` to retry.

### "Students see the SSID but cannot load the portal"

1. Run `test-demo-network.ps1` — look for WARN/FAIL on:
   - **Portal responds on 127.0.0.1** → Docker issue. Check
     `docker compose -f aibox/stack/docker-compose.yaml logs caddy`.
   - **Portal responds via hotspot IP** → firewall issue. Re-run
     `start-hotspot.ps1` to re-create the `AIBox Hotspot HTTP` rule.
   - **Hosts file offline-hostname mapping** → DNS issue.
     Students should still reach `http://192.168.137.1/` as fallback.

### "Port 80 already in use"

The capability check flags this as FAIL with the offending process name.
Common culprits: World Wide Web Publishing Service (IIS), Skype (older
versions), BranchCache. Stop the service, free port 80, and retry.

### "Hotspot ethernet_policy is blocking startup"

When the laptop is on Ethernet, `setup_hotspot.ps1` defaults to `warn` mode
(the hotspot starts anyway, with the wired adapter as its upstream source).
If the demo environment strictly forbids sharing an Ethernet connection with
the hotspot, set `HOTSPOT_ETHERNET_POLICY=disable` in `.env`. The engine will
then refuse to start the hotspot over Ethernet and will disable the wired
adapter instead. Most demos should keep the default.

### "I need to see what happened at startup"

Tail the wrapper log:
```powershell
Get-Content aibox\logs\windows-demo-startup.log -Tail 50
```

Or check Task Scheduler → `AIBox-Puente-Startup` → History for the autostart
task.

---

## Linux migration path

The Windows demo is intentionally minimal so the long-term Linux deployment
can replace it cleanly. The mapping:

| Windows (now)                                                | Linux (later)                                      |
|--------------------------------------------------------------|----------------------------------------------------|
| Windows Mobile Hotspot (WinRT `NetworkOperatorTetheringManager`) | `hostapd` as a `systemd` unit                      |
| Windows ICS DHCP + DNS proxy on 192.168.137.0/24             | `dnsmasq` with a static lease pool                 |
| Windows `hosts` file entry for `puente.link → 192.168.137.1` | `dnsmasq` `address=/puente.link/192.168.4.1`       |
| Windows Defender Firewall rules                              | `iptables` / `nftables` inbound rules              |
| Task Scheduler `AIBox-Puente-Startup`                        | `systemd` target pulling in `aibox-stack.service` + `hostapd.service` |
| `start-demo-stack.ps1`, `stop-demo-stack.ps1`, etc.          | `systemctl start/stop aibox.target` + shell wrappers |
| `check-hotspot-capability.ps1`                               | An `aibox-check` CLI that inspects the same things |
| `test-demo-network.ps1`                                      | An `aibox-test` CLI with the same PASS/WARN/FAIL output |

The Docker Compose stack, the Caddy reverse proxy, and the portal JS do not
need to change — they already bind `0.0.0.0` and use relative API paths, so
they are network-agnostic.

Suggested Linux subnet: **192.168.4.0/24** with the gateway at
`192.168.4.1`. Using a different subnet than Windows' `192.168.137.0/24`
avoids confusion if both platforms are tested side by side.

---

## Recommended Wi-Fi adapter specs

For reliable multi-client performance in a classroom:

**Internal cards (best, when you control the hardware):**
- **Chipset**: Intel AX201 / AX210 (Wi-Fi 6) or Intel 8265 (Wi-Fi 5).
  These have first-class Windows AP/hotspot support and stable drivers.
- **Connection**: **PCIe M.2** preferred over USB. USB adapters often
  reset under load and are sensitive to cable quality.

**Caveats (work, but with constraints):**
- **MediaTek MT79xx family (MT7921, MT7922)**. Mobile Hotspot does work
  (verified by Windows event log entries on real units), but the chip is
  single-radio and cannot do STA+AP simultaneously on different bands.
  In practice: don't be connected as a Wi-Fi client when starting the
  hotspot — disconnect first, or use Ethernet upstream, or use a USB
  dongle as a second radio. The legacy `netsh wlan show drivers` will
  report "Hosted network supported : No" — this is normal for modern
  drivers and does NOT mean Mobile Hotspot is broken.
- Most cheap Realtek USB dongles. Many advertise "hosted network supported"
  but drop students under load or fail to broadcast on 5 GHz.

**USB dongle workaround (when the internal card is broken):**
The cleanest pattern is to use the broken internal Wi-Fi as the *upstream*
(Wi-Fi client) and let a SoftAP-capable USB dongle host `puente.link`.
Tested USB options for Mobile Hotspot:
- **TP-Link Archer T3U Plus** (~$20): RTL8812BU chipset, USB 3.0,
  external 5 dBi antenna, dual-band. Best balance of price + range.
- **TP-Link Archer T2U Plus** (~$15): RTL8811AU chipset, USB 2.0,
  5 dBi external antenna. Cheapest reliable option.
- Either works on Windows 10/11 with the in-box Microsoft drivers; install
  the TP-Link driver only if hosted network shows "No" after plugging it in.

**Bands**: 5 GHz capable (AC or AX). 2.4 GHz works but is crowded;
video streaming with 10+ clients on 2.4 GHz is unreliable.

**Client density**: Plan for 6–10 concurrent student devices per
hotspot. Beyond that, prefer a dedicated access point on the same
LAN subnet and switch the stack to LAN mode (unset
`OFFLINE_ACCESS_IP` or set it to the laptop's LAN IP).

Tested-OK on Project Puente: Intel AX201 (Lenovo ThinkPad X1 Carbon),
Intel AX211 (Dell Latitude 7440), Intel 8265 (Lenovo T480s),
MediaTek MT7921 (with no Wi-Fi client connection upstream).

---

## Safety notes

- **Secrets**. `HOTSPOT_KEY` is a WPA2 passphrase. Treat it like any
  credential; do not commit it to git. `aibox/stack/.env.example` uses
  placeholder values only.
- **No destructive commands**. The scripts here only create or modify
  hotspot state, firewall rules, the `hosts` file tag lines (prefixed
  `# AIBox-Puente`), and Docker containers defined in the project's
  compose file. They do not wipe adapters, change default routes, or
  disable arbitrary Windows services.
- **Idempotency**. Running any script twice is safe. `start-demo-stack.ps1`
  re-runs the capability check and re-invokes the engine, which checks
  state before mutating.
- **Rollback**. `stop-demo-stack.ps1 -StopHotspot` fully reverses the
  networking changes: the hotspot is torn down, the firewall rules
  remain (they're harmless when the hotspot is off), and the `hosts`
  entry for `puente.link` is removed.
