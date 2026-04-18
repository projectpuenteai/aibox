# AIBox / Puente AI Full Test Pass

Run date: 2026-04-18 UTC
Base URL: `http://localhost`
Result bundle: `tools/tests/results/puente-e2e/20260418T025328Z`

## Live Stack
- `[PASS]` Public runtime `health`, `ready`, `live`, and `status` endpoints responded through Caddy.
- `[PASS]` Admin login worked with the configured Puente admin account.
- `[PASS]` Anonymous runtime mutation was rejected with `401`.
- `[PASS]` Normal, guest, lockout, delete-test, limits, saved-chat, and spam test accounts were created.
- `[PASS]` Normal login, `auth/me`, and logout worked.
- `[PASS]` Guest preferences stayed guest-scoped and forced light theme.
- `[PASS]` Repeated invalid logins escalated to lockout with `429`.
- `[PASS]` Admin user listing included the created test accounts.
- `[PASS]` Admin password reset, lock, unlock, self-delete protection, and user deletion worked.
- `[PASS]` Docs create, edit, star, delete, restore, and trash clear worked.
- `[PASS]` Doc max-count and repeated offense blocking worked.
- `[PASS]` Saved-chat capacity and folder create/update/delete worked.
- `[PASS]` Chat completions persisted messages, citations, delete, and restore.
- `[PASS]` Prompt-length guard, heavy-prompt warning path, and AI send block escalation worked.
- `[PASS]` Admin security events, storage insights, analytics summary/timeseries, and analytics export responded.
- `[PASS]` Admin runtime `stop`, `start`, `restart`, and `clear-override` worked.
- `[PASS]` Adjacent `wiki` and `learn` shells were reachable.

## Retention Clone
- `[PASS]` Old deleted docs were hard-deleted from the cloned state.
- `[PASS]` Old deleted chats were hard-deleted from the cloned state.
- `[PASS]` Inactive guest accounts were deleted from the cloned state.
- `[PASS]` Old unstarred docs were deleted under cleanup pressure.
- `[PASS]` Starred docs survived cleanup pressure.
- `[PASS]` Old unsaved chats were deleted under cleanup pressure.
- `[PASS]` Saved chats survived cleanup pressure.
- `[PASS]` Cleanup execution recorded a cleanup event.

## Browser Evidence
- `[PASS]` Welcome screen rendered correctly. Visual note: auth cards and branding were visible. Evidence: `screenshots/welcome-screen.png`
- `[PASS]` Login form rendered correctly. Visual note: username/password controls were visible. Evidence: `screenshots/login-screen.png`
- `[PASS]` Signup form rendered correctly. Visual note: create-account flow was visible and complete. Evidence: `screenshots/signup-screen.png`
- `[PASS]` Portal dashboard rendered after login. Visual note: main portal shell loaded without broken layout. Evidence: `screenshots/portal-dashboard.png`
- `[PASS]` Docs editor rendered after navigation. Visual note: editor shell loaded and accepted text input. Evidence: `screenshots/docs-editor.png`
- `[PASS]` Docs trash state rendered. Visual note: restore control and clear-trash state were visible in trash scope. Evidence: `screenshots/docs-trash-state.png`
- `[PASS]` AI chat rendered. Visual note: sidebar, composer, and response area loaded correctly. Evidence: `screenshots/ai-chat.png`
- `[PASS]` Admin console rendered. Visual note: runtime, accounts, storage, security, and analytics panels loaded together. Evidence: `screenshots/admin-console.png`

## Adjacent RAG Regression
- `[FAIL]` `core-en-lincoln` returned `Abraham (given name)` instead of `Abraham Lincoln`. Evidence: `rag/rag_test_20260418-030206Z.json`
- `[FAIL]` `edge-teaching-register` returned `Aquatic locomotion`, `Stream`, and `Water` instead of `Water cycle`. Evidence: `rag/rag_test_20260418-030206Z.json`
