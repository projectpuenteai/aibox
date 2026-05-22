# nvidia-probe

Minimal C# console app used by the AIBox Inno Setup wizard to detect whether
an NVIDIA GPU is present before allowing the installation to proceed.

## Detection logic

1. Runs `nvidia-smi --query-gpu=name --format=csv,noheader` and checks for at
   least one non-empty output line (standard path when the full driver is installed).
2. Falls back to `LoadLibrary("nvcuda.dll")` via P/Invoke — the CUDA runtime DLL
   is present in System32 even when `nvidia-smi` is not on `PATH`.

**Exit codes:** `0` = GPU detected, `1` = not detected.

## Build

```powershell
dotnet publish nvidia-probe.csproj `
  -c Release `
  -r win-x64 `
  --self-contained false `
  -o ../../../dist/stage/nvidia-probe-staging
```

The CI release workflow (`release.yml`) copies the output `.exe` to `dist/stage/nvidia-probe.exe`
so the Inno script can find it at `{#SourcePath}\..\dist\stage\nvidia-probe.exe`.

## Local build (one-liner from repo root)

```powershell
dotnet publish aibox/installer/inno/nvidia-probe/nvidia-probe.csproj `
  -c Release -r win-x64 --self-contained false `
  -o dist/stage/nvidia-probe-staging
Copy-Item dist/stage/nvidia-probe-staging/nvidia-probe.exe dist/stage/
```

## Placement in the Inno script

`AIBox.iss` references the probe at `dist\stage\nvidia-probe.exe`. The Inno
`[Files]` section copies it into the installer and the `[Code]` section
executes it during the preflight check. A non-zero exit code blocks the
installation with a "NVIDIA GPU required" message.
