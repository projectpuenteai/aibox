# Retest Results

Run date: 2026-04-18 UTC

## Initial Failures Observed Earlier In The Pass

### Runtime control auth
- Initial evidence: the earlier live run reported `BUG-runtime-admin-auth`.
- Original behavior: anonymous runtime mutation returned `200 OK`.
- Fix applied: runtime admin auth enforcement in `tools/ai-control/app.py`.
- Retest result: final `runtime-anon-mutate` check passed with `401`.

### Login lockout
- Initial evidence: the earlier live run reported `Expected 429, got 401`.
- Original behavior: repeated bad logins did not persist lockout state.
- Fix applied: committed enforcement-side effects in `tools/ai-control/app_storage.py`.
- Retest result: final `login-lockout` check passed with `429`.

### AI abuse escalation
- Initial evidence: the earlier live run reported `AI send block was not escalated`.
- Original behavior: heavy-prompt warnings were logged but `ai_send_blocked_until` stayed null.
- Fix applied: committed enforcement-side effects in `tools/ai-control/app_storage.py`.
- Retest result: final `chat-abuse-controls` check passed with `ai_send_blocked_until=2026-04-18T02:59:54.937311+00:00`.

### Browser docs capture
- Initial evidence: the browser lane initially failed on the docs flow.
- Original behavior: the screenshot runner navigated before browser auth had settled, then used a brittle trash-state path.
- Fix applied: stabilized browser auth waits and switched the trash-state screenshot to a reliable authenticated setup path.
- Retest result: final browser lane passed `8/8`.

## Final Rerun Summary

- Live lane: `17/17 PASS`
- Browser lane: `8/8 PASS`
- Retention clone lane: `8/8 PASS`
- Adjacent RAG regression lane: `18/20 PASS`, `2/20 FAIL`

## Acceptance Status

- Checklist generated: yes
- Pass/fail results generated: yes
- Bugs found documented: yes
- Fixes applied documented: yes
- Retest results documented: yes
- Screenshots captured: yes
- Remaining blockers: no core portal/runtime blockers remain; two retrieval-quality failures remain in the adjacent RAG suite.
