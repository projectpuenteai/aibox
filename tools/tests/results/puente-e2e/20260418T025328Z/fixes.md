# Fixes Applied

Run date: 2026-04-18 UTC

## Product Code

### `tools/ai-control/app.py`
- Added `_require_runtime_admin(req, write=False)` for runtime-control protection.
- Applied admin-session enforcement to runtime status aliases and runtime mutation routes:
  - `/v1/admin/health`
  - `/v1/admin/ready`
  - `/v1/admin/live`
  - `/v1/admin/status`
  - `/v1/admin/ai-enabled`
  - `/v1/admin/runtime/start`
  - `/v1/admin/runtime/stop`
  - `/v1/admin/runtime/restart`
  - `/v1/admin/runtime/clear-override`

### `tools/ai-control/app_storage.py`
- Added `persist_and_raise(...)` to commit intended enforcement-side effects before raising `HTTPException`.
- Updated transaction rollback handling so a deliberate pre-raise commit does not create a second failure on rollback.
- Moved login lockout, generic limit checks, docs/chat limit checks, concurrent-generation checks, and prompt-length enforcement onto the committed enforcement path.
- Result: lockouts, docs write blocks, AI cooldowns, and AI send blocks now persist instead of being lost when the request aborts.

### `stack/portal/ai/index.html`
- Removed the bad diagnostics string assignment that produced a broken replacement glyph in the UI.

## Test Harness and Runner

### Added
- `tools/tests/puente_e2e/common.mjs`
- `tools/tests/puente_e2e/live_checks.mjs`
- `tools/tests/puente_e2e/browser_checks.mjs`
- `tools/tests/puente_e2e/retention_clone_check.py`

### Updated
- `tools/tests/run_puente_e2e.ps1`
  - Uses the local Playwright install in `tools/tests`
  - Copies retention results back into the timestamped result bundle
  - Copies the RAG suite into a temporary in-container workspace and saves the output into the bundle

### Supporting setup
- Added `tools/tests/package.json` and installed Playwright locally for repeatable screenshot capture.
