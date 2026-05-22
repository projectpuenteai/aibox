# aibox/scripts/windows/

Operator-facing entry points for the **Windows demo networking layer**. The
substantive logic — hotspot engine, Docker lifecycle, autostart registration —
lives in `aibox/tools/llama-runtime/scripts/`; this folder holds the demo
orchestrator and the discoverable diagnostic scripts. For one-off lifecycle
actions (start/stop hotspot, install/uninstall autostart) call the engine
scripts directly.

## Scripts

| Script                         | Purpose                                                  |
|--------------------------------|----------------------------------------------------------|
| `check-hotspot-capability.ps1` | Pre-flight: verify Windows, Wi-Fi, Docker, ports, env    |
| `start-demo-stack.ps1`         | Capability check → Docker Compose → hotspot → summary    |
| `stop-demo-stack.ps1`          | Stop Docker; **keep hotspot** by default                 |
| `test-demo-network.ps1`        | Post-startup PASS/WARN/FAIL self-test with summary       |
| `diagnose-hotspot-failure.ps1` | Deep diagnostic when the hotspot won't start             |

Direct engine equivalents for one-shot actions (in
`aibox/tools/llama-runtime/scripts/`):
- `setup_hotspot.ps1` / `setup_hotspot.ps1 -Stop` — start / stop the hotspot
- `install_autostart.ps1` / `uninstall_autostart.ps1` — register / remove the
  logon scheduled task and shortcuts

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

The scripts here append timestamped lines to
`aibox/logs/windows-demo-startup.log`. The engine scripts write structured
JSON to `aibox/stack/portal/network-info.json` (consumed by the portal's
`connect.html` and by `test-demo-network.ps1`).

## See also

- `../../docs/windows-hotspot-demo.md` — operator-facing documentation with
  troubleshooting, known Windows limitations, and the Linux migration plan.
- `../../tools/llama-runtime/scripts/setup_hotspot.ps1` — the WinRT-based
  hotspot engine (handles firewall rules, Ethernet policy, hosts file).
- `../../tools/llama-runtime/scripts/get_network_info.ps1` — writes
  `network-info.json` with hotspot status, HTTP readiness, LAN IPs.
