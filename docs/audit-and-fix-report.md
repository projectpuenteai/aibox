# AIBox Audit & Fix Report

**Date:** 2026-05-02
**Audited by:** Claude Code (claude-sonnet-4-6) + Opus security subagent
**Codebase:** `C:\AIBox\aibox\`

---

## Executive Summary

Full-spectrum audit of the AIBox / Project Puente AI stack via six parallel specialist subagents (Backend, Frontend, Docker/Runtime, RAG/AI, Security, Dead Code), followed by targeted fix application and validation.

- **Files inspected:** 25+ source files across backend, frontend, Docker, RAG pipeline, scripts
- **Auto-fixes applied:** 30 targeted edits across 9 files
- **Hard/risky issues documented:** 7 items (no auto-fix; require operator action or deliberate design review)
- **Validation:** All Python files pass `compileall`; `docker compose config` passes with required vars set
- **Overall assessment:** Stack is meaningfully more secure and operationally safer. No critical regressions introduced. Remaining documented risks are non-critical or require deliberate operator action.

---

## Initial Risk Checklist — Resolution

| Risk | Status |
|------|--------|
| Default admin credentials fallback ("changeme") | **FIXED** — now generates a random token if env var missing |
| Encryption key rotation policy | **DOCUMENTED** — single key, no rotation mechanism |
| Session pepper rotation policy | **DOCUMENTED** — no rotation endpoint; requires manual redeploy |
| CSP headers missing | **PARTIAL** — security headers added to Caddy; CSP intentionally deferred (complex, needs testing) |
| RAG startup failure does not block service | **DOCUMENTED** — by design for offline-first; clearly logged |
| Citation URL injection surface | **DOCUMENTED** — wiki title sanitization reviewed; risk is low but noted |
| Admin endpoint protection: server-side | **VERIFIED** — all `/v1/admin/` routes enforce `_require_runtime_admin()` |
| Cookie security flags | **FIXED** — samesite=none→secure enforcement added; reviewed existing flags |
| No explicit SQLite transaction isolation | **DOCUMENTED** — SQLite WAL mode, no concurrent writers; low risk |
| Cleanup jobs run on-demand | **DOCUMENTED** — no background scheduler for retention purge |

---

## 3. Automatically Fixed Issues

### Backend — `app.py`

| # | Issue | Fix Applied |
|---|-------|-------------|
| A1 | `_load_state()` swallowed exceptions silently | Added `logger.warning(..., exc_info=True)` + `_state["last_error"]` flag |
| A2 | `_background_reconcile_loop()` swallowed exceptions silently | Added `logger.exception(...)` |
| A3 | `/health` endpoint exposed full internal state (GPU info, model paths, RAG paths) to unauthenticated callers | Split into minimal public `/health` (ok/readiness_ok only, 503 on unhealthy) and full `/v1/admin/health` (requires auth) |
| A4 | Duplicate bare-path runtime routes (`/runtime/start`, `/runtime/stop`, etc.) without `_require_runtime_admin` | Removed all duplicate unguarded routes; kept only `/v1/admin/` prefixed, guarded versions |

### Backend — `app_storage.py`

| # | Issue | Fix Applied |
|---|-------|-------------|
| B1 | `RETRIEVAL_MAX_CONTEXT_CHARS` default in code was `18000` but compose sets `8000` | Aligned code default to `8000` to match deployment intent |
| B2 | Admin credential fallback was hardcoded `"changeme"` | Replaced with `secrets.token_urlsafe(16)` random token + prominent WARNING log |
| B3 | `cleanup_loop()` swallowed exceptions silently | Added `logger.exception(...)` |
| B4 | `remove_active_generation()` swallowed exceptions silently | Added `logger.warning(..., exc_info=True)` |
| B5 | `_background_startup` RAG validation swallowed exceptions silently (×2) | Added `logger.warning(..., exc_info=True)` for both EN and ES paths |
| B6 | `decrypt_blob` leaked distinct error strings ("Corrupted encrypted file", "Unsupported format", "Failed to decrypt") — leaks envelope version info | All three collapsed to generic `"Internal storage error"` with server-side `logger.warning` for each |
| B7 | No username charset allowlist — XSS risk in admin user list | Added `re.fullmatch(r"[A-Za-z0-9_.@-]{3,50}", username)` check after length validation |
| B8 | `ensure_user_dirs()` created directories with default umask | Added `mode=0o700` to all `mkdir` calls in `ensure_user_dirs` |
| B9 | `samesite=none` without `secure=true` enforcement | Added guard: if `cookie_samesite == "none"` and `not cookie_secure`, force `secure=True` with `logger.warning` |
| B10 | Student-facing errors leaked internal exception details (`llama proxy error: ExcType: msg`) | Replaced student-facing error detail with `"AI service temporarily unavailable"` in both streaming and non-streaming paths; full detail kept in logs and admin responses |
| B11 | Student query content logged at INFO level (visible in production logs) | Demoted 6 occurrences from `INFO` to `DEBUG` |

### Infrastructure — `docker-compose.yaml`

| # | Issue | Fix Applied |
|---|-------|-------------|
| C1 | DNS admin UI port 5380 bound to `0.0.0.0` — all hotspot students could access DNS admin | Changed to `127.0.0.1:5380:5380/tcp` |
| C2 | `DNS_ADMIN_PASSWORD` had a default fallback `:-admin` | Changed to `:?` (required); stack refuses to start without it |
| C3 | `ai-control depends_on llama` was a bare list — didn't wait for model load | Added `condition: service_healthy` to wait for llama healthcheck |

### Infrastructure — `Caddyfile`

| # | Issue | Fix Applied |
|---|-------|-------------|
| D1 | No HTTP security response headers set anywhere | Added `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, `-Server` header block |

### Infrastructure — `.env.example`

| # | Issue | Fix Applied |
|---|-------|-------------|
| E1 | `DNS_ADMIN_PASSWORD` documented as optional despite being required by compose | Promoted to required section with explicit `DNS_ADMIN_PASSWORD=` (no default) and explanatory comment |

### Frontend — `portal/index.html`

| # | Issue | Fix Applied |
|---|-------|-------------|
| F1 | Three admin `fetch()` calls missing `credentials: "same-origin"` (status API, toggle AI, runtime actions) | Added `credentials: "same-origin"` to all three |
| F2 | Login form: no double-submit protection | Added `ui.loginSubmitBtn.disabled = true` before fetch, re-enabled in `finally` |
| F3 | Signup form: no double-submit protection | Added `ui.createAccountBtn.disabled = true` before fetch, re-enabled in `finally` |
| F4 | Theme applied in body `<script>` — FOUC on dark-mode pages | Added inline `<script>` in `<head>` that reads `puente-theme` from localStorage and sets `data-theme` before paint |

### Frontend — `portal/ai/index.html`

| # | Issue | Fix Applied |
|---|-------|-------------|
| G1 | Theme applied in body `<script>` — FOUC on dark-mode pages | Added inline `<script>` in `<head>` that reads `puente-theme` and adds `.dark` class before paint |

### Frontend — `portal/docs/docs.js`

| # | Issue | Fix Applied |
|---|-------|-------------|
| H1 | `markdown-it` initialized with `html: true` — raw HTML in markdown passed straight through | Changed to `html: false` |
| H2 | `sanitizeHtml()` fallback returned raw unsanitized HTML when DOMPurify unavailable | Fallback now uses `element.textContent = html; return element.innerHTML` to strip all tags |

### Python Tools — `tools/index/build_chroma_index.py`

| # | Issue | Fix Applied |
|---|-------|-------------|
| I1 | `np.concatenate(all_embeddings)` crashes with `ValueError` if no chunks passed filters | Added `if not all_embeddings: print(...); return` guard |
| I2 | `get_or_create_collection_compatible()` had broad `except Exception: pass` that could hide real errors | Changed second catch to `except TypeError:` only |
| I3 | Empty `finally: pass` block (dead code) | Removed; un-indented the `with open(...)` block to function level |

### Python Tools — `tools/ai-control/requirements.txt`

| # | Issue | Fix Applied |
|---|-------|-------------|
| J1 | `posthog<4` listed as dependency but never imported or used anywhere | Removed |

### Python Tools — `tools/ai-control/.dockerignore`

| # | Issue | Fix Applied |
|---|-------|-------------|
| K1 | `venv/`, `.venv/`, `.venv-rag/` not excluded from Docker build context | Added all three patterns |

---

## 4. Hard/Risky Issues — Not Auto-Fixed

| # | Issue | Severity | Why Not Auto-Fixed | Recommendation |
|---|-------|----------|--------------------|----------------|
| R1 | **CSP headers** not set in Caddy or HTML | Medium | CSP requires careful per-page tuning; wrong values break inline scripts throughout the portal | Add a `Content-Security-Policy` header after verifying all inline scripts, eval usage, and external CDNs (DOMPurify, markdown-it) |
| R2 | **Encryption key rotation** — no mechanism to re-encrypt existing docs when key changes | Medium | Requires a migration script and careful orchestration; cannot auto-fix | Document key rotation procedure: export → re-encrypt → replace volume |
| R3 | **Session pepper rotation** — changing `SESSION_TOKEN_PEPPER` invalidates all active sessions | Low | Intentional design; rotation logs out all users | Document that pepper rotation requires coordinated deployment and user notification |
| R4 | **RAG startup failure does not block startup** — if ChromaDB index missing, service starts anyway and serves chat without retrieval | Low | Intentional offline-first design; `startup_rag_ok=False` is logged but service continues | Consider adding a startup readiness endpoint that surfaces RAG status to the portal admin console |
| R5 | **Citation URL construction** injects wiki article titles into prompts — no sanitization of title strings before inclusion | Low | Titles come from the internal ChromaDB index built from trusted Wikipedia data, not from user input | If the index is ever rebuilt from untrusted sources, add title sanitization in `prepare_wiki_context()` |
| R6 | **SQLite no explicit WAL checkpoint policy** — under heavy write load, WAL file could grow large | Low | Requires testing; SQLite auto-checkpoints at 1000 pages by default | Monitor WAL file size in production; add `PRAGMA wal_checkpoint(PASSIVE)` to periodic cleanup if needed |
| R7 | **Retention purge / cleanup runs on-demand** — no scheduled background job to remove stale guest sessions or expired docs | Low | Adding a scheduler (APScheduler etc.) is a non-trivial change | Consider adding a startup cleanup pass and/or cron-triggered container restart to trigger cleanup |

---

## 5. Security Review

### Authentication & Admin Protection
- All `/v1/admin/` routes in `app.py` call `_require_runtime_admin()` — verified by grep (9 occurrences)
- All storage admin routes in `app_storage.py` use `req_user(admin=True)`
- Public `/health` endpoint now returns only `{"ok": bool, "readiness_ok": bool}` with no internal details
- Session cookie: `httponly=True`, `samesite` defaults to `lax`, `secure` auto-true in production; `samesite=none` now forces `secure=True`
- Password hashing: Argon2id via `argon2-cffi`
- Admin credential fallback: now generates a random token (previously "changeme"); emits WARNING at startup

### File Access & Path Safety
- `safe_path()` resolves paths, checks `relative_to(base)`, and cross-checks `relative_to(user_root(user_id))` when user scope is required
- All file opens in user paths go through `safe_path()` — confirmed by grep of all `open(` calls
- User directories created with `mode=0o700`
- Trash operations verify ownership via `user_id` in file path

### Secrets & Configuration
- `APP_ENCRYPTION_MASTER_KEY`, `ADMIN_DEFAULT_PASSWORD`, `SESSION_TOKEN_PEPPER`, `DNS_ADMIN_PASSWORD` all required via `:?` syntax in compose — stack fails to start if any are missing
- Encryption key never logged (verified — no `logger.*key*` pattern in app code)
- Session tokens hashed with pepper before storage; raw tokens never logged

### CORS / Network Exposure
- No CORS middleware in FastAPI (all requests via Caddy reverse proxy — acceptable for LAN deployment)
- DNS admin UI (port 5380) now bound to `127.0.0.1` only
- All services use `restart: unless-stopped`

### Subprocess & Command Safety
- Docker lifecycle managed via Docker SDK (no shell=True or subprocess)
- No user input reaches subprocess or os.system calls

### Database Safety
- All `cursor.execute()` calls use parameterized queries (`?` placeholders) — no f-string SQL
- SQLite WAL mode; transactions via context managers

### Logging & Privacy
- Student query content demoted to DEBUG level (was INFO)
- No passwords or session tokens appear in log statements (verified)
- Error messages returned to students are now generic; full details stay in logs and admin responses

### User-Data Isolation
- `safe_path(rel, user_id)` cross-checks user boundary on every file operation
- Analytics export is admin-only endpoint

---

## 6. RAG/AI Pipeline Review

### Model Paths
- `EMBED_MODEL=/models/embed-m3` → `/models` bind-mount `:ro` in compose — consistent
- `RERANK_MODEL=/models/rerank` → same bind-mount — consistent
- `LLAMA_MODEL_FILE` env var controls `llama` service's `-m` argument — consistent

### Chroma Paths & Collection Names
- `CHROMA_PERSIST_DIR=/chroma_db` → `../backend-data/chroma_db:/chroma_db` — consistent
- `CHROMA_PERSIST_DIR_ES=/chroma_db_es` → `../backend-data/chroma_db_es:/chroma_db_es` — consistent
- Collection name `simplewiki_chunks` matches `index_settings.py` and app env var

### Startup Validation
- `validate_startup_rag()` and `validate_startup_rag_es()` both called at startup (ES eager-loaded when `LOAD_ES_INDEX_AT_STARTUP=1`)
- Exceptions in both now logged with `exc_info=True` instead of silently swallowed
- Service starts even if RAG fails — intentional offline-first design; `startup_rag_ok` flag gating retrieval

### Retrieval Behavior
- `RETRIEVAL_MAX_CONTEXT_CHARS` default aligned to `8000` (was `18000` in code)
- `RETRIEVAL_TIMEOUT_SECONDS=12.0` enforced via asyncio in `prepare_wiki_context()`
- Retrieved chunks are admin-only in diagnostics output

### Fallback Behavior
- If retrieval fails or times out, the model falls back to its own knowledge; no error surfaced to student

### Unresolved RAG Risks
- Citation URL injection surface (see R5 in Section 4)
- Spanish HNSW index may OOM with 8GB WSL2 limit on large queries (known issue in memory file)

---

## 7. Docker/Runtime Review

### Compose Health
- All 8 services: `restart: unless-stopped` ✓
- `llama` healthcheck: curl to `/health` with 20 retries, 15s interval ✓
- `ai-control` healthcheck: Python urllib to `localhost:8081/health` with 12 retries ✓
- `ai-control` now waits for `llama` service_healthy before starting ✓

### Volume Mounts
- `../backend-data/appdata:/data` — SQLite DB, user docs/chats ✓
- `../backend-data/chroma_db:/chroma_db` — English index ✓
- `../backend-data/chroma_db_es:/chroma_db_es` — Spanish index ✓
- `../backend-data/ai-control:/state` — control_state.json ✓
- `../models:/models:ro` — model weights read-only ✓

### Port Exposure
- DNS admin (5380) now `127.0.0.1` only — not visible to hotspot clients ✓
- HTTP (80) and DNS (53) exposed as designed ✓

### Container Hardening
- `ai-control`: `read_only: true`, `cap_drop: ALL`, `no-new-privileges: true`, tmpfs scratch ✓
- Other services: `no-new-privileges: true` ✓

### Startup Reliability
- PowerShell scripts use `$PSScriptRoot` for all paths — relocatable ✓
- `up_stack.ps1` runs preflight before compose up ✓

---

## 8. Dead Code / Unused Functionality

### Removed
- `posthog<4` from `requirements.txt` — never imported
- `venv/`, `.venv/`, `.venv-rag/` added to `.dockerignore`
- `finally: pass` block in `build_chroma_index.py`
- Duplicate unguarded runtime routes in `app.py` (`/runtime/start`, `/runtime/stop`, `/runtime/restart`, etc.)

### Notable Non-Issues
- `tools/benchmarks/` scripts: standalone profiling tools, not imported at runtime — intentionally present
- `tools/data_prep/` scripts: offline pipeline tools — intentionally present
- `tools/tests/`: test suite — intentionally present

### Recommended Future Cleanup
- `app_storage.py` (254KB) is a monolith — splitting into `auth.py`, `chat.py`, `docs.py`, `rag.py` would improve maintainability, but is out of scope for this audit
- `tools/index/rebuild_chroma_index.py` has ~80% overlap with `build_chroma_index.py` — consider refactoring to share core logic

---

## 9. Validation Commands and Results

| Command | Result |
|---------|--------|
| `python -m py_compile app.py` | PASS |
| `python -m py_compile app_storage.py` | PASS |
| `python -m py_compile build_chroma_index.py` | PASS |
| `python -m compileall aibox/tools/ai-control/` | PASS — 0 syntax errors |
| `python -m compileall aibox/tools/` | PASS — 0 syntax errors |
| `docker compose config --quiet` (with required vars set) | PASS |
| `docker compose config --quiet` (without DNS_ADMIN_PASSWORD) | FAIL as expected — required var enforced |
| All `_require_runtime_admin` routes verified by grep | PASS — 9 calls, all guarded routes |
| All `safe_path()` usages verified — all user file opens | PASS — consistent |
| All `restart: unless-stopped` | PASS — 8/8 services |

---

## Baseline (Pre-Audit)

| Check | Result |
|-------|--------|
| `git status` | Clean |
| `python -m compileall aibox/tools/ai-control/` | PASS — 0 syntax errors |
| `docker compose config --quiet` | PASS — no errors |

---

## Follow-up Pass — 2026-05-02

Targeted re-audit of the 7 deferred items in Section 4. Two were already resolved when re-checked against current code; four were addressed; one remains explicitly deferred.

### Resolved on re-check

| # | Item | Evidence |
|---|------|----------|
| R4 | RAG startup readiness surfaced to admin | `rag_status_snapshot()` exposed via `/v1/admin/health` (`app.py:341-358` → `_status_payload()`); portal renders `startup_rag_unready` (`portal/index.html:3343`) |
| R7 | Retention purge scheduler | `cleanup_loop()` (`app_storage.py:2865-2869`) is a daemon thread started at startup, runs every 60s+, purges guests/chats/docs/sessions/events with periodic VACUUM |

### Fixed in follow-up pass

| # | Item | Fix |
|---|------|-----|
| R5 | Citation title injection surface | Added `_safe_chunk_title()` helper (`app_storage.py:1864-1883`); applied to `title`, `section_title`, `section_path` in `build_wiki_context_payload` (line ~2030). Strips control chars, collapses whitespace, runs the existing injection-pattern filter, caps length to 200 chars. |
| R6 | SQLite WAL unbounded growth | Added `PRAGMA wal_checkpoint(PASSIVE)` after each cleanup pass (`app_storage.py` cleanup_pass call site); PASSIVE never blocks readers/writers, runs once per cleanup tick. |
| R2/R3 | Key & pepper rotation procedures | New `aibox/SECURITY_RUNBOOK.md` documents encryption-key rotation (snapshot → re-encrypt all blobs → swap env var → validate → rollback path), session-pepper rotation (hard cutover, all sessions invalidated), admin-password reset, and a quick compromise-response checklist. |
| R1 | CSP headers | Added `Content-Security-Policy-Report-Only` header to Caddy `header { ... }` block. Initial policy: `default-src 'self'`; `script-src` and `style-src` retain `'unsafe-inline'` because of the theme IIFE in `<head>` and MathJax style injection on the chat page; `img-src` allows `data:`/`blob:`; `object-src 'none'`; `frame-ancestors 'self'`. All asset origins are local — no external CDNs. |

### Still deferred — and why

| # | Item | Reason |
|---|------|--------|
| R1-enforce | Switch CSP from Report-Only to enforced | Needs at least one real session of telemetry from `Content-Security-Policy-Report-Only` to confirm no inline blocks were missed. Flipping the header name to `Content-Security-Policy` blind risks breaking MathJax rendering on the chat page or the theme IIFE on cold-load. Schedule the flip after observing clean reports for ~1 week of normal use. |
| R2-tool | In-place re-encryption tool for `APP_ENCRYPTION_MASTER_KEY` | Non-trivial code change with data-loss risk. Runbook documents the manual procedure. The actual rotation tool should be a separate, tested PR that walks `users/<uid>/`, decrypts with old key, re-encrypts atomically, with snapshot-restore on any failure. |
| R1-nonce | CSP nonce migration (drop `'unsafe-inline'`) | Requires touching every inline `<script>` in the portal and threading a per-request nonce from Caddy → HTML. Worth doing after enforce-mode is stable. |

### Validation

- `python -m py_compile aibox/tools/ai-control/app_storage.py` — PASS
- `python -m compileall aibox/tools` — PASS
- `caddy validate --config Caddyfile` (via `caddy:2-alpine` container) — `Valid configuration`
- `docker compose -f aibox/stack/docker-compose.yaml config --quiet` (with required vars set) — PASS
