# aibox/scripts/windows/

Operator-facing entry points for the **Windows demo networking layer**. Every
script here is a thin wrapper — the real hotspot engine, Docker lifecycle,
autostart registration, and network diagnostics live in
`aibox/tools/llama-runtime/scripts/`. If you are modifying behavior, edit the
engine first; these wrappers only exist to give the demo workflow discoverable
names and add logging + a consolidated pre-flight check.

## Scripts

| Script                         | Purpose                                                  | Delegates to                                |
|--------------------------------|----------------------------------------------------------|---------------------------------------------|
| `check-hotspot-capability.ps1` | Pre-flight: verify Windows, Wi-Fi, Docker, ports, env    | (standalone — no delegation)                |
| `start-hotspot.ps1`            | Start the Windows Mobile Hotspot (self-elevates)         | `setup_hotspot.ps1`                         |
| `stop-hotspot.ps1`             | Stop the hotspot and remove the hosts entry              | `setup_hotspot.ps1 -Stop`                   |
| `start-demo-stack.ps1`         | Capability check → Docker Compose → hotspot → summary    | `up_stack.ps1` (+ engine hotspot)           |
| `stop-demo-stack.ps1`          | Stop Docker; **keep hotspot** by default                 | `down_stack.ps1 -SkipHotspot` / full        |
| `install-startup-task.ps1`     | Register logon-trigger scheduled task + shortcuts        | `install_autostart.ps1`                     |
| `uninstall-startup-task.ps1`   | Remove the scheduled task and shortcuts                  | `uninstall_autostart.ps1`                   |
| `test-demo-network.ps1`        | Post-startup PASS/WARN/FAIL self-test with summary       | (standalone — reads `get_network_info.ps1`) |
| `diagnose-hotspot-failure.ps1` | Deep diagnostic when the hotspot won't start (full WLAN driver, ICS, WinRT, event log dump) | runs `setup_hotspot.ps1` standalone with -EmitJson |

## Typical sequences

**First-time demo setup:**
```
powershell -ExecutionPolicy Bypass -File .\check-hotspot-capability.ps1
# Resolve any FAIL items, then:
powershell -ExecutionPolicy Bypass -File .\start-demo-stack.ps1
powershell -ExecutionPolicy Bypass -File .\test-demo-network.ps1
```

**Daily operation (autostart registered):**
```
# Nothing to do — the scheduled task launches at logon.
# To verify:
powershell -ExecutionPolicy Bypass -File .\test-demo-network.ps1
```

**End of day:**
```
powershell -ExecutionPolicy Bypass -File .\stop-demo-stack.ps1            # keeps hotspot
# or
powershell -ExecutionPolicy Bypass -File .\stop-demo-stack.ps1 -StopHotspot
```

## Logs

Every wrapper appends a timestamped line to
`aibox/logs/windows-demo-startup.log`. The engine scripts write their own
structured JSON to `aibox/stack/portal/network-info.json` (consumed by the
portal's `connect.html` and by `test-demo-network.ps1`).

## See also

- `../../docs/windows-hotspot-demo.md` — operator-facing documentation with
  troubleshooting, known Windows limitations, and the Linux migration plan.
- `../../tools/llama-runtime/scripts/setup_hotspot.ps1` — the WinRT-based
  hotspot engine (handles firewall rules, Ethernet policy, hosts file).
- `../../tools/llama-runtime/scripts/get_network_info.ps1` — writes
  `network-info.json` with hotspot status, HTTP readiness, LAN IPs.
