# Bugs Found

Run date: 2026-04-18 UTC

## Fixed During This Pass

### `BUG-runtime-admin-auth`
- Severity: High
- Area: `tools/ai-control/app.py`
- Repro: `POST /ai/api/v1/admin/runtime/start` without an authenticated admin session.
- Expected: `401` or `403`
- Actual before fix: `200 OK` with runtime state mutation.
- Fix: Added `_require_runtime_admin(...)` and enforced it on runtime alias/status endpoints and runtime mutation endpoints.
- Retest: `runtime-anon-mutate` passed with `401` in `live-results.json`.

### `BUG-lockout-persistence`
- Severity: High
- Area: `tools/ai-control/app_storage.py`
- Repro: Repeated bad logins for the same username/IP.
- Expected: lockout state should persist and escalate to `429`.
- Actual before fix: failed-login counters were rolled back and the flow stayed at `401`.
- Fix: Added `persist_and_raise(...)` and used it in the login failure path so lockout-side effects commit before the request aborts.
- Retest: `login-lockout` passed with `429` in `live-results.json`.

### `BUG-ai-block-persistence`
- Severity: High
- Area: `tools/ai-control/app_storage.py`
- Repro: Repeated heavy prompts through `/ai/api/v1/app/chat/completions`.
- Expected: warnings, cooldowns, and send blocks should persist to the user restrictions row.
- Actual before fix: security-event warnings existed, but cooldown/send-block fields were rolled back and stayed null.
- Fix: Reused `persist_and_raise(...)` in prompt-length, concurrency, docs, chat, and general rate-limit enforcement paths.
- Retest: `chat-abuse-controls` passed and returned a non-null `ai_send_blocked_until` in `live-results.json`.

### `BUG-ai-diagnostics-glyph`
- Severity: Low
- Area: `stack/portal/ai/index.html`
- Repro: Open AI diagnostics while a request id is shown.
- Expected: clean localized diagnostics banner text.
- Actual before fix: the banner included a broken replacement glyph.
- Fix: removed the bad text assignment and left the correct localized assignment in place.
- Retest: browser captures showed a clean AI interface; no mojibake was observed in the final screenshot set.

## Remaining Open Issues

### `BUG-rag-lincoln-ranking`
- Severity: Medium
- Area: retrieval / reranking
- Evidence: `rag/rag_test_20260418-030206Z.json`
- Repro: `core-en-lincoln` query `Who was Abraham Lincoln?`
- Expected: retrieval context headed by `Abraham Lincoln`.
- Actual: top titles were `Abraham (given name)`, `Lincoln (movie)`, `Lincoln`, and `The United States in the 19th century`.
- Impact: the retrieval layer can anchor on weak lexical matches for named-entity questions.

### `BUG-rag-water-cycle-ranking`
- Severity: Medium
- Area: retrieval / reranking
- Evidence: `rag/rag_test_20260418-030206Z.json`
- Repro: `edge-teaching-register` query `Explain the water cycle to a 10 year old`
- Expected: retrieval context headed by `Water cycle`.
- Actual: top titles were `Aquatic locomotion`, `Stream`, and `Water`.
- Impact: the retrieval layer can miss the intended pedagogical concept on explanatory prompts.
