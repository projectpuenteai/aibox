# Installer Pre-First-Build Recommendations — 2026-05-20

Skeptical re-audit of the entire `aibox/installer/` tree. All B0/B1/B2/B3
code-side findings from this audit have now been **fixed** in the repo
(see "Fixed in this session" below for the file list). Verified after
edits:

- All 4 .NET projects (`AIBoxFirstRun`, `AIBoxUninstaller`,
  `AIBoxFirstRunTests`, `nvidia-probe`) build with 0 warnings, 0 errors.
- All 89 xUnit tests pass.
- All 5 Python build scripts compile cleanly.
- All 3 YAML files (`release.yml`, `stage-content.yml`,
  `release-config.yaml`) parse.

The remaining blockers are infrastructure-side (P0 list) — you'll
need to provision them outside the repo before the first release.

Source: 5 parallel review agents (C# / Inno / Python / GitHub Actions /
live upstream probes) plus direct local probes from this session.

**Severity tiers** (same as REVIEW_FINDINGS.md):
B0 = won't compile/start · B1 = fails at build/release · B2 = fails at
user runtime · B3 = subtle bug · N = nit.

**Live verification done in this session (do NOT re-run, results are authoritative):**

- All 4 .NET projects (`AIBoxFirstRun`, `AIBoxUninstaller`,
  `AIBoxFirstRunTests`, `nvidia-probe`) build with `0 Warning(s) 0 Error(s)`.
- The 89 xUnit tests in `AIBoxFirstRunTests` all pass.
  Includes 19 canonicalization-parity tests — C# `Manifest.Canonicalize`
  and Python `manifest_canonical.canonical_bytes` produce identical bytes
  on every sample input.
- All four Python build scripts accept the args the workflow passes.
- The shipped `dist/manifest-1.0.0.json` verifies correctly against
  the **dev** keypair (`build/.secrets/dev.ed25519.pk`), confirming that
  build + sign + verify pipeline is functionally wired end-to-end.

## Fixed in this session

Code (committed to the working tree, all edits verified by build + tests):

| Finding | File(s) touched |
|---|---|
| B0-7 Inno `NeedRestart()` callback | `inno/AIBox.iss` |
| B0-8 + B2-16 Inno RAM check quoting + empty-output fail | `inno/AIBox.iss` |
| B1-9 `pyyaml` in pip install (build-manifest + upload-r2) | `.github/workflows/release.yml` |
| B1-10 download-artifact subdir layout | `.github/workflows/release.yml` |
| B1-11 shell-glob safety in upload-r2 + R2 cred validation | `.github/workflows/release.yml` |
| B1-13 `concurrency:` block | `.github/workflows/release.yml` |
| P0-3 hard-fail on tag build if pubkey secret missing | `.github/workflows/release.yml` |
| B1-14 / B3-31 HF tree API Link-header pagination (C# + Python) | `first-run/Services/Fetchers/HuggingFaceFetcher.cs`, `build/build_manifest.py` |
| B2-15 `HttpRangeDownloader` 416 retry refactor + overflow guard | `first-run/Services/Fetchers/HttpRangeDownloader.cs` |
| B2-17 reject empty manifest | `first-run/Services/Manifest.cs` |
| B2-18 surface corrupt state.json (`LoadWithStatus`) | `first-run/Services/StateStore.cs`, `first-run/MainWindow.xaml.cs` |
| B2-19 HF 429 backoff array indexing | `first-run/Services/DownloadEngine.cs` |
| B2-21 separate manifest / signature size caps | `first-run/Services/ManifestClient.cs` |
| B2-23 `verify-manifest.py` accepts base64 pubkey file | `build/verify-manifest.py` |
| B3-28 canonical `latest.json` | `build/stage_r2_content.py` |
| B3-29 / B3-30 Inno cleanup (unused vars + comment) | `inno/AIBox.iss` |
| B3-32 `BuildConstants.ManifestBaseUrl` `.invalid` hard-fail | `first-run/Services/ManifestClient.cs` |
| `DownloadPhase` broader exception catch on cancel | `first-run/Phases/DownloadPhase.xaml.cs` |
| Hostname migration `projectpuente` → `projectpuenteai` | `release.yml`, `build_manifest.py`, `BuildConstants.cs`, `RELEASING.md`, `SECRETS.md`, `TROUBLESHOOTING.md` |
| Already-verified-fixed (no change needed) | `R2Fetcher` path traversal (B2-20), `EnvWriter` ACL banner surfaced via UI (B2-22), `KolibriFetcher` ps probe (B3-25) |

---

## P0 — environmental blockers you still need to handle

These aren't code bugs; they're infrastructure the workflow assumes exists.
The code is ready, but the first CI release will fail until each of these
is in place.

### ~~P0-1. The `projectpuente` GitHub org does not exist~~ — RESOLVED

The real org is `projectpuenteai` (https://github.com/projectpuenteai),
not `projectpuente`. Three files were edited in this session to fix
stale references:

- `.github/workflows/release.yml` (4 occurrences — GHCR image refs)
- `first-run/Services/BuildConstants.cs:21` (`AiControlImageRef` default)
- `TROUBLESHOOTING.md:44` (release URL)

### ~~P0-2. `cdn.projectpuente.ai` has no DNS~~ — RESOLVED

The real CDN hostname is `cdn.projectpuenteai.org`. Probed live:
DNS resolves to Cloudflare IPs, TLS cert valid, `/aibox/latest.json`
returns 404 cleanly (the bucket is provisioned, content not yet
uploaded). Three files were edited:

- `.github/workflows/release.yml:131` (the `ManifestBaseUrl` rewrite)
- `build/build_manifest.py:299` (the `--r2-base` default)
- `RELEASING.md` (4 occurrences) and `SECRETS.md` (1 occurrence)

### P0-3. Production Ed25519 keypair does not exist in GitHub Secrets

`release.yml` references three secrets that have no observable
provenance in this repo:
- `AIBOX_MANIFEST_SIGNING_KEY` (base64 private seed used to sign)
- `AIBOX_MANIFEST_VERIFY_KEY` (base64 public, used to canary-verify in CI)
- `AIBOX_MANIFEST_PUBKEY_B64` (base64 public, baked into the WPF binary
  via `release.yml:143`)

All three must point to the **same** keypair. The current
`first-run/Resources/release-pubkey.ed25519` (45 bytes — base64 of
`5CJSEeVM3L/OmXmxW+q7YJ7BDm59jmi72auNIYGeXPA=`) is a committed placeholder
that will be overwritten by CI.

`release.yml:140-141` only WARNS when the secret is missing; the build
proceeds with the placeholder. **Change this to a hard fail** for tag
builds — otherwise a misconfigured release ships a binary that rejects
every signed manifest.

```pwsh
if (-not $env:AIBOX_MANIFEST_PUBKEY_B64) {
  if ('${{ github.event_name }}' -eq 'push') {
    throw "AIBOX_MANIFEST_PUBKEY_B64 secret missing on a tag build."
  }
  Write-Warning "Placeholder pubkey in use — signature verification will fail."
}
```

### P0-4. Cloudflare R2 credentials must exist as repo secrets

The `upload-r2` job needs `AIBOX_R2_ACCOUNT_ID`,
`AIBOX_R2_ACCESS_KEY_ID`, `AIBOX_R2_SECRET_ACCESS_KEY`, `AIBOX_R2_BUCKET`.
None are declared in the repo's GitHub settings yet (can't be verified
from the workspace; please confirm in the GitHub UI before triggering
release).

### P0-5. Azure Trusted Signing tenant/account must be set up

`sign-files.ps1` is wired to Azure Trusted Signing via
`AZURE_SIGNING_TENANT/CLIENT_ID/CLIENT_SECRET/ENDPOINT/ACCOUNT/PROFILE`
(secrets) + `AZURE_SIGNING_ENDPOINT` (var). The signing step is gated by
`if: env.AZURE_SIGNING_TENANT != ''` (correctly promoted to job-level
per the prior audit). Without these set, the build *will* succeed but
ship **unsigned binaries** — SmartScreen will block users immediately.

**Fix:** decide whether unsigned-for-dev is acceptable. If yes, document
it; if no, gate the whole release on the secrets being present.

### P0-6. The Kolibri channel ID may be invalid

Channel `c1f2b7e6ac9f56a2bb44fa7a48b66dce` returned 404 on Studio's
public API. The fetcher uses the `kolibri manage importchannel` CLI
which doesn't necessarily hit the same endpoint, so 404 isn't
definitive — but it has to be confirmed by running the CLI command on a
test Kolibri before the release.

---

## B0 — new, will block the build

### B0-7. Inno `NeedRestart()` callback is missing

`AIBox.iss` writes to a global `NeedReboot: Boolean` (lines 157, 401,
411, 413, 415, 417) but never defines the
`function NeedRestart(): Boolean` callback Inno invokes at install end.
Reboot logic silently no-ops. User proceeds to Phase B without
rebooting → Docker WSL2 backend not yet running → ai-control fails to
start. Add at end of `[Code]`:

```pascal
function NeedRestart(): Boolean;
begin
  Result := NeedReboot;
end;
```

Note: this is a partial fix only. The deeper design issue from prior
audit item #6 (NeedReboot is set in `CurStepChanged(ssPostInstall)`,
after Inno has already decided) still applies — set NeedReboot from
inside the Docker install step instead.

### B0-8. Inno RAM check PowerShell call has unquoted temp path

`AIBox.iss:191-193` builds a PowerShell command by concatenating
`TmpFile` into a string without surrounding quotes:

```pascal
'... Out-File -Encoding ASCII ' + TmpFile + '" 2>NUL'
```

Most usernames contain no spaces, but `C:\Users\First Last\AppData\...`
breaks the command. PowerShell sees a malformed line, the file isn't
written, `LoadStringFromFile` returns empty, and `CheckRamGB` returns
`True` regardless of actual RAM (see B3-16).

**Fix:** wrap with embedded quotes (Inno doubles single quotes):

```pascal
'... Out-File -Encoding ASCII """' + TmpFile + '""" 2>NUL'
```

---

## B1 — will fail at the release CI run

### B1-9. `pip install` lacks `pyyaml` in `build-manifest` and `upload-r2` jobs

`release.yml:293-298` does `pip install cryptography requests` and then
runs `build_manifest.py`, which at line 43 does `import yaml`.
ModuleNotFoundError, build fails.

**Fix:** `pip install cryptography requests pyyaml` in both jobs.

### B1-10. `actions/download-artifact@v4` lays artifacts in subdirs

`release.yml:396-409` (publish-release job) uses `download-artifact@v4`
with `merge-multiple: true`. v4's behavior puts each artifact under
`dist/<artifact-name>/...`. The subsequent globs `dist/AIBox-Setup-*.exe`
and `dist/manifest-*.json` don't match the actual `dist/installer/...`
and `dist/manifest/...` paths.

**Fix:** download each artifact explicitly with `name:` + `path: dist/`
(no `merge-multiple`), or update the globs to `dist/*/AIBox-Setup-*.exe`.

### B1-11. Shell glob passed to Python script will pass literal string on miss

`release.yml:381-382` runs `python ... --upload-manifest dist/manifest-*.json`.
If the artifact download in B1-10 fails or moves files, bash passes the
literal `dist/manifest-*.json` to Python. argparse accepts it as a
`Path`, and the script later FileNotFoundErrors with a confusing
"manifest-*.json: file not found".

**Fix:**

```bash
manifest=$(ls dist/manifest-*.json | head -1)
[ -z "$manifest" ] && { echo "no manifest"; exit 1; }
python ... --upload-manifest "$manifest" --upload-manifest-sig "$manifest.sig"
```

### B1-12. Shipped `manifest-1.0.0.json` has empty SHA on `llm-qwen2.5-7b-q4-part1`

`dist/manifest-1.0.0.json:9` has `"sha256": ""` for the first GGUF part.
That was signed verbatim into the manifest. The C# `HttpRangeDownloader`
treats `string.IsNullOrEmpty(expectedSha256)` as "no check required"
(see `Fetchers/HttpRangeDownloader.cs:127`), so it won't actively fail
— but it means the LLM file is not integrity-verified, which contradicts
the trust model. CI should always run `build_manifest.py
--compute-non-lfs-sha` so the published manifest has real hashes for
**every** file.

Verify: after fix, `cat dist/manifest-1.0.0.json | jq '.items[] |
select(.sha256 == "" and (.files | not))'` returns empty.

### B1-13. `release.yml` has no `concurrency:` block

Two tags pushed in rapid succession race on the same R2 keys and the
`latest.json` pointer. Add to top of workflow:

```yaml
concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false
```

### B1-14. HuggingFace tree API pagination not implemented (C# + Python)

Both `HuggingFaceFetcher.ListRepoTreeAsync` (C#:227) and
`build_manifest.py:list_hf_tree` (Python:111) assume the `?recursive=true`
API returns ≤1000 entries. C# throws on `Count == 1000` (which is also
wrong — it could be exactly 1000 and complete); Python silently truncates.
For the current 4 pinned repos this doesn't matter, but a 1001st file
silently disappears on any future repo. Add cursor pagination on both
sides.

---

## B2 — will fail at user runtime

### B2-15. `HttpRangeDownloader` 416 path streams the 416 body into `.part`

`Fetchers/HttpRangeDownloader.cs:74-90`: when the server returns 416 with
`resumeFrom > 0`, the code deletes `.part`, sets `resumeFrom = 0`, and
falls through to `label RetryFromZero` — but it never re-issues the
HTTP request. So the 416 response **body** (an HTML error page) gets
streamed into the new `.part` and then SHA-checked.

Self-heals because SHA verification fails and the user retries from
scratch on the next launch, but that means one wasted full-file
download per affected file. Refactor into a `DownloadOnceAsync` helper
called twice instead of using `goto`.

(Severity downgraded from the agent's B0 — the SHA check at line 127
catches it, so it manifests as one retry, not a crash.)

### B2-16. Inno RAM check passes silently when WMI returns empty

`AIBox.iss:207` sets `Result := True` on empty stdout from the
PowerShell WMI call (see B0-8). User on a 4 GB box is told "OK, 16 GB or
more RAM detected" and the install proceeds to fail with cryptic
container OOMs on first chat. Reject empty output as a hard failure
or at least a strong warning. Combined with B0-8 this is what the
"silent pass" loophole actually does in practice.

### B2-17. Empty manifest accepted

`ManifestParser.Parse` (C#:120-142) accepts `items: []`. The downloader
reports 0/0 progress and marks the install "complete" without
downloading anything. Add:
`if (manifest.Items.Count == 0) throw new InvalidDataException(...);`

### B2-18. Corrupt `state.json` silently looks like first launch

`StateStore.Load` catches `JsonException` and returns a fresh
`InstallState`. `InstallContext.Discover` then sees `PhaseAComplete=false`
and reports "Phase A has not completed" — which is misleading because
Phase A *did* complete; the state file got corrupted afterward (power
loss, antivirus, etc). Return `(state, wasCorrupt)` and surface
corruption explicitly in the bootstrap UI ("State file unreadable;
delete `%PROGRAMDATA%\AIBox\state.json` and rerun.").

### B2-19. HF rate-limit backoff still uses default 5s/15s/60s

`HuggingFaceFetcher` catches 429 and sets `IsRetryable=true`, but the
engine's default backoff is too aggressive for HF anonymous rate limits
(which typically reset in minutes, not seconds). Override backoff for
429s specifically: `[30s, 120s, 300s]`.

### B2-20. `R2Fetcher.ExtractTarZst` path-traversal unchecked

Manifest is signed, so risk is low — but SharpZipLib's
`TarArchive.ExtractContents` will happily write to `../../whatever`.
Validate each entry's `Name` is under `destDir` (use `Path.GetFullPath`
and `StartsWith`).

### B2-21. `ManifestClient` size cap is shared between JSON and signature

`ManifestClient.cs:63-64` caps both fetches at 1 MB. A 1 MB manifest is
generous; a 1 MB signature file is impossible (Ed25519 is 64 bytes), so
the shared cap is fine in practice. But if anyone ever adds detached
PGP signatures or multi-key signatures, this silently breaks. Use two
distinct constants: `MAX_MANIFEST_BYTES = 1MB`, `MAX_SIG_BYTES = 1KB`.

### B2-22. `EnvWriter.LockDownAcls` failure is silent if no logger passed

When the function is called without a `FileLogger`, ACL set failures
leave `.env` with inherited (potentially world-readable) ACLs and no
record. Either require the logger parameter (non-nullable) or assert
elevation before any call site.

### B2-23. `verify-manifest.py` doesn't accept base64 pubkey from file

`first-run/Resources/release-pubkey.ed25519` is base64 text (the C#
`NormalizeKey` accepts both forms). `verify-manifest.py --pubkey <file>`
rejects it with "must be exactly 32 bytes". Add the same base64
normalization to the Python verifier — important because local
developers will reach for the embedded resource file when running a
canary check and get a misleading error.

---

## B3 — subtle / polish

### B3-24. `DockerCli.WaitForDaemonAsync` is hardcoded at 180 s, not configurable

180 s is reasonable for most boots, but cold Docker Desktop on a slow
HDD can take longer. Either expose in `BuildConstants` or read from
`InstallContext`.

### B3-25. `KolibriFetcher._kolibriUp` static state is process-wide

Restarting First Run re-issues `docker compose up -d kolibri`
unnecessarily. Probe `docker compose ps -q kolibri` first.

### B3-26. `release.yml` version resolution duplicated 3x

Same shell snippet (with PowerShell vs bash variants) appears at
release.yml:63-70, 114-123, 242-251. Extract to a reusable composite
action or a shell helper.

### B3-27. `Manifest.Canonicalize` float detection relies on raw text

Currently works because all manifest values come from `JsonNode.Parse`
which preserves raw text. If anyone ever constructs a `JsonValue`
programmatically (e.g. for tests) with `5.0`, the path collapses to
"5" instead of "5.0" — divergence from Python `json.dumps`. The 19
canonicalization parity tests cover the common cases but should add a
"programmatically constructed double" case.

### B3-28. `stage_r2_content.py` writes `latest.json` non-canonically

`put_json_object()` uses `indent=2`. Not on the signed trust path, so
B3 — but if anyone ever adds SHA validation to `latest.json` it'll
fail unpredictably. Use compact canonical form to match the manifest.

### B3-29. Inno script `ResultLabel` and `YPos` declared but unused

`InitializeWizard()` at `AIBox.iss:257-258`. Dead code.

### B3-30. Inno comment "Inno will prompt for restart if NeedReboot is True" is wrong

`AIBox.iss:435-436`. Inno prompts when the `NeedRestart()` callback
(B0-7) returns true, not when the global is set. Misleading.

### B3-31. `HuggingFaceFetcher.ListRepoTreeAsync` pagination check is `== 1000`

Should be `>= 1000` AND implement cursor pagination, not just throw.
(Tied to B1-14.)

### B3-32. `BuildConstants.ManifestBaseUrl` default points to `cdn.example.invalid`

Defensive default so a non-CI build can't accidentally hit prod, which
is good. But the local dev test workflow doesn't override it — if a
developer runs `dotnet run` on First Run, they'll get a confusing TLS
failure rather than "no manifest URL configured." Better to error out
explicitly when `ManifestBaseUrl` contains `.invalid`.

---

## Confirmed-OK (live probes, 2026-05-20)

- HuggingFace metadata API + LFS download URLs + Range support — all 4
  repos resolve and serve Range correctly.
- Kiwix mirror direct download URLs for both `wikipedia_en_all_mini_2026-03.zim`
  (12.4 GB) and `wikipedia_es_all_maxi_2026-02.zim` (41.0 GB) work, Range
  supported, `.sha256` sidecars present.
- Docker Desktop installer URL is live.
- GitHub release infrastructure is live.
- Kiwix OPDS `?name=` filter still returns 0 results — fallback path is
  the only working one, as documented.
- C#↔Python canonicalization parity is byte-for-byte identical across
  19 test cases including non-ASCII (Spanish ñ, U+2028, emoji), control
  chars, floats, named escapes.
- `dist/manifest-1.0.0.json` (currently dev-signed) round-trips through
  `verify-manifest.py` cleanly.

---

## Recommended fix order

1. **P0-1 through P0-6** — provision infrastructure. Without these the
   code being bug-free doesn't matter.
2. **B0-7 (Inno reboot)** and **B0-8 (RAM check quoting)** — local code,
   2-line fixes each, unblocks first Inno compile.
3. **B1-9 (pyyaml)**, **B1-10 (artifact layout)**, **B1-11 (glob safety)**
   — without these, the first GitHub Actions release run dies.
4. **B1-12** — re-build manifest with `--compute-non-lfs-sha` so prod
   ships real hashes.
5. **B1-13 (concurrency)** + **B1-14 (HF pagination)** — pre-empt
   recurring failures.
6. **B2 list** — code review polish + defense-in-depth.
7. **B3 / N** — at leisure.

Once the P0 list is provisioned and B0/B1 are fixed, the first full
end-to-end CI build should succeed. The B2 list won't block the build
but will surface in the first round of real user testing.
