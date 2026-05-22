# Installer Pre-Build Audit — 2026-05-19

Skeptical review of the entire `aibox/installer/` tree. Nothing has been
compiled or executed; this list is what must be fixed before the first
real build / release will succeed.

Findings come from four parallel haiku review agents (C# / Inno / GitHub
Actions / Python build tooling), plus direct live probes against
huggingface.co and download.kiwix.org from this session.

Severity tiers:

- **B0** — Won't compile / won't start
- **B1** — Will fail at build/release time
- **B2** — Will fail at runtime against real upstreams
- **B3** — Subtle bug, will fire eventually
- **N** — Nit

---

## B0 — won't compile / won't start

### Inno Setup (`inno/AIBox.iss`)

1. **Missing branding assets.** `Source: branding\banner.bmp`,
   `banner-small.bmp`, `branding\app.ico`, plus the
   `UninstallDisplayIcon` and `[Icons]` IconFilename entries reference
   files that don't exist. iscc.exe fails at `[Files]` enumeration.
   **Fix:** either commit placeholder bitmaps or guard the lines with
   `#ifexists`.

2. **`TNewMemo` is not an Inno class.** Should be `TMemo`.
   `InitializeWizard()` won't compile as-written. **RESOLVED** — Inno agent replaced with `TMemo`.

3. **`TMemoryStatusEx` is not a built-in record.** Inno Pascal Script
   doesn't expose a typed `TMemoryStatusEx`. Either declare a record
   with the right fields (`dwLength: DWORD; dwMemoryLoad: DWORD;
   ullTotalPhys: Int64; ...`) plus an `external` for
   `GlobalMemoryStatusEx`, or query RAM via WMI (`SELECT TotalPhysicalMemory FROM Win32_ComputerSystem`). **RESOLVED** — Inno agent rewrote `CheckRamGB` to use PowerShell/WMI.

4. **`GetSpaceOnDisk64` signature is wrong.** The actual Inno helper is
   `GetSpaceOnDisk(Path: String; var SizeBytes, FreeBytes: Cardinal): Boolean` for 32-bit, or `GetSpaceOnDisk64(Path: String; var FreeBytes, TotalBytes: Int64): Boolean`. My call has `Free64, Total64` but checks `Free64` — confirm parameter order against current Inno docs and rebuild. **RESOLVED** — current `CheckFreeDiskGB` uses `GetSpaceOnDisk64(Drive, Free64, Total64)` and checks `Free64`; parameter order matches Inno docs.

5. **`MemoryStatus.ullTotalPhys` field access.** Depends on (3). Tied to
   the same fix. **RESOLVED** — eliminated with the WMI rewrite (item 3).

6. **`function NeedRestart(): Boolean;` override.** Inno provides this
   as an event callback that's invoked at a specific point in the
   install flow. Defining a same-named function in `[Code]` works if
   declared with the right signature; mine looks OK syntactically but
   the `NeedReboot` global is set in `CurStepChanged(ssPostInstall)`,
   which fires *after* Inno has already decided whether to reboot.
   The reboot-resume design is wrong: should set `NeedReboot` from
   inside the Docker install step, not after.

### WPF First Run (`first-run/`)

7. **`uninstaller/MainWindow.xaml.cs:23-24` inverts intent.** Comment
   says "Default both boxes ON" when launched from Inno, code sets
   them OFF. The user gets the opposite of what Inno's `[UninstallRun]`
   expects.

8. **`ShortcutWriter.cs` IPersistFile reference.** I use
   `System.Runtime.InteropServices.ComTypes` for `IPersistFile` but
   never imported the namespace. `using System.Runtime.InteropServices.ComTypes;`
   is required at the top, or the cast fails.

9. **`StateStore.cs` snake_case naming.** The CLR `InstallState` class
   uses PascalCase properties (`PhaseAComplete`), and I set
   `PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower`. The Inno
   wizard's `WriteInstallState()` emits `"phase_a_complete": true`.
   Should round-trip — but only if the .NET 8 `SnakeCaseLower` policy
   exists. It does (added in .NET 8.0) — confirm before merging.

10. **`DownloadPhase.xaml.cs:142` `dotnet publish` SingleFile + dependencies.**
    `PublishSingleFile=true` + `SelfContained=false` won't bundle WPF's
    framework-dependent native deps cleanly. The published binary may
    fail to launch on machines without exactly the right .NET runtime.
    **Fix:** either `SelfContained=true` (bigger, but standalone) or
    drop `PublishSingleFile=true` and accept multiple files.

### GitHub Actions (`release.yml`)

11. **"Emit image ref + digest" step has no `id`.** The
    `outputs:` block on `build-ai-control` references
    `steps.push.outputs.digest` and the later `steps.<no-id>` shell
    step — but the shell step has no `id`. The job-level
    `outputs.image_digest` ends up empty, breaking the downstream
    `Bake build constants` step. **RESOLVED** — GHA agent added `id: refs` to the emit step.

12. **`if: ${{ env.AZURE_SIGNING_TENANT != '' }}` is always false.**
    GitHub Actions evaluates step-level `if:` *before* applying that
    step's `env:` block. The AZURE_* env vars are declared on the
    same step they gate. **Fix:** promote the secrets to job-level
    `env:` or write `if: ${{ secrets.AZURE_SIGNING_TENANT != '' }}`.
    (Note: GHA recently restricted `secrets.*` in `if:` — confirm
    current behavior; promoting to job-level env is safer.) **RESOLVED** — GHA agent promoted AZURE_* vars to job-level `env:` on both signing jobs.

### Python build tooling

13. **`sign-manifest.py` arg parser mismatch.** Workflow calls
    `--privkey-env` and `--output`; script accepts `--key-base64-env`
    and `--sig-out`. **Fix:** either update the workflow or add
    backwards-compatible argparse aliases. **RESOLVED** — Python agent updated the script to accept `--key-base64-env` and `--sig-out`; workflow updated to match; `--privkey-env` kept as alias.

14. **`verify-manifest.py` arg parser mismatch + missing feature.**
    Workflow calls `--signature` and `--pubkey-b64-env`; script
    accepts `--sig` and `--pubkey` (file path only). The
    `--pubkey-b64-env` form needs to be added to the script (decode
    base64, write to temp file, then read). **RESOLVED** — Python agent added `--pubkey-b64-env` support and `--sig`/`--signature` alias; workflow confirmed to match.

15. **`stage_r2_content.py` doesn't implement what the workflows
    call.** No `--upload-manifest`, `--upload-manifest-sig`,
    `--update-latest`, `--upload-shards`, `--target-prefix` arguments
    exist in the current script. Both `release.yml` and
    `stage-content.yml` invoke flags that aren't there. **Fix:**
    extend the script or write `stage_r2_manifest.py` for manifest
    operations and keep the shard uploader separate. **PARTIALLY RESOLVED** — stage-content.yml duplicate `--upload-shards` line removed (cleanup pass). The `--upload-manifest`/`--upload-manifest-sig`/`--update-latest` flags called by release.yml still missing from script; Python agent owns this.

16. **`release-config.yaml` ships `sha256: TBD`.** The
    `chroma-simplewiki-part-01` entry has `sha256: "TBD"` and
    `size_bytes: 0`. `build_manifest.py` passes both through
    verbatim. The signed manifest will reference a literal "TBD"
    sha; the C# downloader will fail integrity verification on every
    user's machine. **Fix:** either omit the item until shards exist,
    or have CI compute and substitute the real values. **RESOLVED** — Python agent commented out the entire chroma shard block with clear instructions to re-enable after staging.

---

## B1 — fails at build/release time

17. **`build_manifest.py:90` reads `lfs.sha256` — that field doesn't
    exist.** Confirmed live: HF's `/api/.../tree/...` exposes
    `lfs.oid` (which IS the SHA256), not `lfs.sha256`. For LFS files
    my code always gets an empty string, then the `if f["sha256"]`
    filter silently drops them from `sha256_manifest`. The bundle
    hash will be wrong. **Fix:** `entry.get("lfs", {}).get("oid", "")`.

18. **Non-LFS small files have no SHA256 from the HF tree API at all.**
    Top-level `oid` field is the git SHA1 of the blob, not a SHA256
    of the file content. Two options: download each small file in
    CI and hash it locally, or accept that small files won't
    contribute to the bundle hash and document the gap.

19. **`KiwixFetcher` OPDS catalog query returns `totalResults=0`.**
    Confirmed live: `https://opds.library.kiwix.org/catalog/v2/entries?count=-1&name=wikipedia_en_all_mini`
    returns zero results — the `name=` filter is broken upstream.
    My fetcher already has a directory-listing fallback, but the
    catalog path is dead weight. **Fix:** demote the OPDS query to
    a low-priority probe (skip if it returns 0 results) and rely on
    the directory scrape, which does work.

20. **`sign-files.ps1` signtool path is pinned to SDK 10.0.22621.0.**
    GitHub's `windows-latest` runner may have a different SDK
    version. **Fix:** glob `C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe`
    and pick the highest.

21. **`sign-files.ps1` never installs `Microsoft.Trusted.Signing.Client`.**
    The script references the DLL under `$USERPROFILE/.dotnet/tools/`
    but doesn't `dotnet tool install` it first. **Fix:** add the
    install step at the top of the script.

22. **`iscc.exe` path assumes Inno Setup v6 at `C:\Program Files (x86)\Inno Setup 6\`.**
    Chocolatey's `innosetup` package usually installs there but
    isn't guaranteed. **Fix:** `(Get-Command iscc.exe -ErrorAction SilentlyContinue).Source`
    after `choco install`, or set PATH.

23. **`docker/build-push-action@v6` digest output handling.** I read
    `steps.push.outputs.digest`. v6 does expose this, but the value
    only includes the algorithm prefix when pushed (`sha256:abc…`).
    My consumer in `Bake build constants` concatenates without
    handling that prefix. **Fix:** verify the embedded string is
    `<image>:<tag>@sha256:<hex>` and the WPF runtime can parse it.

24. **Inno `Permissions: admins-full system-full users-readexec`** is
    not Inno's syntax. Permissions specifiers use a constrained
    vocabulary (`Modify`, `FullAccess`, etc.). **Fix:** either drop
    `[Dirs]` Permissions and let SetupBuildState's ACL inheritance
    apply, or use the correct keyword set.

---

## B2 — fails at runtime against real upstreams

25. **`KiwixFetcher.QueryCatalogAsync` filter behavior.** Even when
    the catalog API does return entries (for other dump names), the
    XML parsing assumes `<link rel="alternate" href="..." type="application/x-zim">`
    style links. The actual feed uses `rel="http://opds-spec.org/acquisition/open-access"`
    with `.zim.meta4` metalink hrefs that need a second hop to
    resolve to a mirror. **Fix:** add a metalink resolution path,
    or rely entirely on the directory scrape.

26. **`HuggingFaceFetcher` multi-file hash bundle is unverifiable.**
    With (17)+(18) fixed for LFS files, small non-LFS files still
    have no SHA. So the C# subtree-manifest verification logic
    matches against the same set the builder uses, but anyone who
    tampers with a small non-LFS file passes through undetected.
    Risk: the threat model says "manifest is the trust root" — but
    individual files within a multi-file HF item are weakly bound to
    the manifest if non-LFS files are present.
    **Fix:** in CI, fetch every non-LFS file once, SHA256 it locally,
    pin in the `files: []` array that the manifest already supports.

27. **`HuggingFaceFetcher.ListRepoTreeAsync` does not paginate.**
    HF's tree API caps at 1000 entries per call; the repos we care
    about are small but a 1001st file silently disappears. **Fix:**
    follow `cursor` query param until empty, or assert the response
    is `< 1000` entries.

28. **`HuggingFaceFetcher` rate-limit propagation.** I catch 429 and
    return `IsRetryable = true`, but the engine's backoff (5s/15s/60s)
    is way too aggressive for HF's anonymous limit (which typically
    resets in minutes). **Fix:** override backoff for 429 specifically
    to `[30s, 2m, 5m]` per the installerplan.

29. **`R2Fetcher.ExtractTarZst` doesn't path-sanitize.** SharpZipLib's
    `TarArchive.ExtractContents` walks tar entries trusting the
    archived path. A malicious archive with `../../etc/passwd` could
    escape `destDir`. Since the manifest is signed and the archives
    come from R2, the practical risk is low — but defense in depth.
    **Fix:** validate each entry's path doesn't escape `destDir`.

30. **`HttpRangeDownloader` re-downloads on RangeNotSatisfiable.**
    When the server returns 416 (range not satisfiable), I warn and
    proceed to verify; but the existing `.part` may actually be
    correct + complete, in which case the verify-after-rename path
    works. If the file is corrupt and same length, we never
    re-fetch. **Fix:** on 416, if SHA mismatch after rename, delete
    `.part` and retry from byte 0 once.

31. **`EnvWriter.LockDownAcls` swallows all exceptions silently.**
    If we're not elevated and the ACL set fails, the `.env` is left
    world-readable on its parent ACL inheritance, which contradicts
    the documented guarantee. **Fix:** at minimum, log the failure
    at WARN and surface it in the bootstrap UI.

32. **`DockerCli.WaitForDaemonAsync` polls every 3s for 90s.** If
    Docker Desktop is starting cold, 90s is short on a laptop. Plan
    spec says 60s — bump to 180s or make it configurable.

33. **`Ed25519Verifier` signature acceptance check.** I check
    `s < L` but RFC 8032 also forbids signatures where the encoded
    R is "non-canonical." `DecodePoint` does enforce `y < P`. That's
    sufficient — but worth an Ed25519 KAT (known-answer test) before
    relying on it in production. **Fix:** add a unit test with the
    RFC 8032 §7.1 test vectors.

34. **`ManifestParser.Parse` accepts `revision: "main"` for HF items.**
    Production manifests should pin to a SHA. The C# parser doesn't
    enforce this. Risk: a misbuilt manifest ships and HF resolves
    `main` to a later commit than tested. **Fix:** validate every
    HF revision is 40-hex at parse time, or warn loudly.

35. **`ManifestClient.FetchAndVerifyAsync` no manifest size cap.**
    A malicious origin (pre-signature-check) could send a 1 GB
    "manifest" and exhaust memory before the sig check fails. **Fix:**
    cap the manifest fetch at 1 MB.

36. **`Manifest.cs:Canonicalize` float divergence vs Python.** Python
    `json.dumps(5.0)` emits `"5.0"`; my C# emits `"5"` because
    `JsonValue.TryGetValue<long>` succeeds on `5.0`. Manifest schema
    shouldn't have floats, but if anyone adds one, signatures will
    silently diverge.

---

## B3 — subtle bugs and rough edges

37. **`InstallContext.Discover()` calls `StateStore.Load` even when
    `phase_a_complete` is false.** Inno writes state with phase_a=true,
    so practically OK, but a corrupt-state path goes through the
    "fresh InstallState" branch and proceeds as if Phase A never
    ran. **Fix:** add an explicit "no install-state found" check
    before the legitimate fresh-dev path triggers.

38. **`MainWindow.xaml.cs` `ShowFatal` and `ShowError`.** `FindResource`
    can return null; the cast in BootstrapFlow.cs:210 and
    DownloadPhase.xaml.cs:126 will throw on a missing key. Add null
    coalesce.

39. **`DownloadPhase` cancel flow.** `OnPauseClick`/`OnCancelClick`
    cancel `_cts` but the engine task may still be writing to
    `state.json` mid-cancel, racing with the MainWindow closing
    handler. Risk of corrupted state. **Fix:** `await _engineTask`
    on cancel before returning.

40. **`KolibriFetcher` static `_kolibriUp` is process-wide.** If the
    First Run app is restarted, the static state resets; we'll
    re-run `docker compose up -d kolibri` redundantly. Harmless but
    wasteful. **Fix:** detect via `docker compose ps -q kolibri`
    before issuing up.

41. **`AutostartRegistrar` runs `install_autostart.ps1` without
    elevation check.** The PS1 self-elevates in some places but
    not all. If WPF is launched non-elevated and the user accepts
    UAC for First Run, the spawned `powershell.exe` inherits the
    same token — which is the elevated token we got from Inno's
    UninstallRun context. In a dev-launch path, the script may
    silently fail. **Fix:** assert `IsUserAnAdmin()` at the start
    of the bootstrap flow.

42. **`ShortcutWriter.SetRunAsAdminBit` flips byte 21.** That's the
    correct offset per MS-SHLLINK §2.5 (DataFlags > RunAsUser bit
    0x20). But the file may be smaller than 22 bytes only if the
    lnk write failed; otherwise the offset is correct.

43. **`ProgressBar.Maximum` long → double conversion.** For downloads
    > 2^53 bytes (~9 PB), precision is lost. Irrelevant.

44. **`MainWindow.xaml.cs` `_shutdownCts.Cancel()` on Closing.**
    Pattern is correct but `_engineTask` is not awaited on close;
    a download in flight will be abruptly killed. Acceptable for a
    user-driven cancel, but make sure the next launch resumes from
    the last `state.json` save (verified — engine saves every
    state transition).

45. **`build_manifest.py` outputs `indent=2, sort_keys=False`.** The
    file on disk is not canonical, which is fine because
    canonicalization happens at verify time. But a human reviewer
    diffing on-disk manifests will get confusing diffs. **Fix:**
    `sort_keys=True` for stable diffs.

46. **GHA `actions/download-artifact@v4` without a name filter** in
    `publish-release` downloads each artifact into its own
    subdirectory under `dist/`, so the `mv dist/installer/* dist/`
    only catches a fraction of the layout. **Fix:** explicitly
    download with name and `path` per upload, or list all artifacts
    and pattern-match.

47. **Inno `[UninstallRun]` references
    `{app}\AIBoxFirstRun\AIBoxUninstaller.exe`** but the
    `[Files]` block stages only `dist/stage/first-run/*` — which
    contains the WPF First Run binary. After today's release.yml
    fix to publish the uninstaller into the same directory, this
    works; without that fix, UninstallRun fails. **Fix:** the
    release.yml step is already updated to publish the uninstaller
    into `dist/stage/first-run/`; verify after build.

48. **`uninstaller/MainWindow.xaml.cs` deletes data via
    `..\AIBox\models` etc.** That double-uses `Path.GetFolderPath(CommonApplicationData)`
    then `..\` — fragile. **Fix:** compute `InstallContext`-equivalent
    paths directly.

---

## N — nits

- `release.yml` step names are inconsistent (`Emit image ref + digest`
  has no period; `Bake build constants` does not).
- `build_manifest.py` argument `--r2-base` is shadowed by
  `os.environ["AIBOX_R2_BASE"]` default; precedence isn't documented.
- `BuildConstants.cs` `AiControlImageRef` defaults to
  `:dev` — fine for dev, but the CI bake step regex won't match the
  initial value cleanly if the embedded image SHA has special
  characters (which it won't).
- The Inno `LicenseFile=branding\LICENSE.rtf` references an RTF that
  exists but uses `\b...\b0` markup that some Inno builds render with
  odd font fallback on non-English locales.

---

## Confirmed-OK by live probe

- HF `/api/models/{repo}?revision=main` returns the resolved SHA in
  the `sha` field. `build_manifest.py:resolve_hf_revision` will work.
- HF tree API at `/api/models/{repo}/tree/{sha}?recursive=true` does
  recurse and exposes file sizes correctly.
- `download.kiwix.org/zim/wikipedia/` directory listing returns the
  expected `wikipedia_en_all_mini_2026-03.zim` entry. Directory
  scrape path is viable.
- Python `manifest_canonical.canonical_bytes()` runs and produces
  deterministic output matching the schema we expect to sign.

---

## Suggested fix order (rough plan)

1. **All B0 first** — none of the rest matters until the build can
   produce binaries.
2. **B1 in this order:** Python script arg parsers (13, 14, 15) →
   release-config.yaml TBD placeholder (16) → HF lfs.oid fix (17) →
   per-file SHA256 strategy for non-LFS (18) → signtool/iscc path
   detection (20, 22) → Kiwix OPDS demotion (19, 25).
3. **B2 next, prioritized by likelihood:** rate-limit backoff (28),
   manifest size cap (35), pagination (27), HF revision SHA validation
   (34), R2 path traversal (29).
4. **B3** as code-review polish.

This list is the gate. None of these issues are individually hard;
the count is the real risk — every one of them needs to be touched
before the first end-to-end install attempt.
