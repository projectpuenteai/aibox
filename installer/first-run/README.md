# AIBox First Run (Phase B + C)

WPF .NET 8 application that runs after Inno Setup completes. Pulls the
~100 GB content payload from four upstreams (HF, Kiwix, Kolibri, R2),
prompts the admin password, writes `stack/.env`, pulls Docker images,
smoke-tests the stack, and registers autostart.

## Project layout

```
first-run/
  AIBoxFirstRun.csproj
  app.manifest                  asInvoker; PerMonitorV2 DPI; longPath aware
  App.xaml, App.xaml.cs         single-instance mutex; file logger
  MainWindow.xaml, .xaml.cs     phase-switching shell
  Phases/
    DownloadPhase.xaml(.cs)     Phase B — manifest fetch + 4-fetcher engine
    BootstrapFlow.xaml(.cs)     Phase C — admin prompt → env → pull → smoke → finalize
    AdminPromptView.xaml(.cs)   the only user input in the installer
    SummaryView.xaml(.cs)       final credentials reveal + next steps
    DonePhase.cs                terminal state; opens Control Panel
  Services/
    BuildConstants.cs           CI-baked constants (manifest URL, image pin)
    InstallContext.cs           paths discovery from install-state.json
    StateStore.cs               atomic install-state.json read/write
    FileLogger.cs               redaction-aware logger
    Manifest.cs                 parser + RFC-compatible canonicalizer
    ManifestClient.cs           fetch from R2 with GitHub fallback + verify
    Ed25519Verifier.cs          pure managed RFC 8032 verify
    EnvWriter.cs                secrets gen + DPAPI blob + ACL lock
    DockerCli.cs                docker / docker compose wrapper
    AutostartRegistrar.cs       runs install_autostart.ps1
    ShortcutWriter.cs           IShellLinkW + Run-as-admin bit
    DownloadEngine.cs           scheduler with per-source caps + retries
    Fetchers/
      IFetcher.cs               shared progress + result types
      HttpRangeDownloader.cs    canonical range-resume + SHA verify + atomic rename
      R2Fetcher.cs              + tar.zst extract
      HuggingFaceFetcher.cs     /resolve/{sha}, tree API, GlobMatcher, HF token
      KiwixFetcher.cs           OPDS catalog + mirror probe + sidecar SHA
      KolibriFetcher.cs         compose exec import* + JSON progress parse
  Resources/
    release-pubkey.ed25519      base64-encoded ed25519 pubkey (rewritten in CI)
```

## Build

Requires .NET 8 SDK.

```powershell
dotnet build aibox\installer\first-run\AIBoxFirstRun.csproj -c Release
```

## State

All resumable state lives in `%ProgramData%\AIBox\install-state.json`.
The phase state machine is:

```
fresh -> ready_for_b -> ready_for_c -> done
```

See installerplan.txt §6 for the full failure-and-recovery matrix.

## Status

All source is in place; not yet built/tested. Next steps:

1. Install .NET 8 SDK locally and run `dotnet build`. Expect a handful of
   nullability warnings to surface — the project sets
   `TreatWarningsAsErrors=true`, so fix them before merging.
2. Generate a real ed25519 keypair with `build/generate-keypair.py`, base64
   the public half into `Resources/release-pubkey.ed25519`, store the
   private half as the `AIBOX_MANIFEST_PRIVKEY` GitHub Actions secret.
3. Stand up a Cloudflare R2 bucket + custom domain so the manifest URL in
   `BuildConstants.cs` resolves.
4. Run `dotnet publish ... -r win-x64` from CI; integrate with `release.yml`.
