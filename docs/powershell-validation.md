# AIBox PowerShell Validation Notes

Use this checklist after changing scripts under `aibox/tools/llama-runtime/scripts` or `aibox/scripts/windows`.

## Static Checks

Install PSScriptAnalyzer on a development machine when internet access is available:

```powershell
Install-Module PSScriptAnalyzer -Scope CurrentUser
Invoke-ScriptAnalyzer -Path .\aibox\tools\llama-runtime\scripts -Recurse
Invoke-ScriptAnalyzer -Path .\aibox\scripts\windows -Recurse
```

## Manual Edge Cases

Run the startup/control scripts from:

- a path with spaces
- a non-admin PowerShell session
- an admin PowerShell session
- a Windows user profile where Docker Desktop is not already running
- a machine with no Wi-Fi adapter or with hotspot disabled
- a machine where elevation is cancelled

## Encoding Rules

- Write JSON with `ConvertTo-Json` and `Set-Content -Encoding UTF8`.
- Use `-LiteralPath` when deleting or reading a path that came from a variable.
- Avoid constructing commands as strings; prefer argument arrays.
- Never add `--volumes`, `docker system prune`, or `docker image prune -a` to cleanup scripts.
