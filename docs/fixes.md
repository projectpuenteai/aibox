You are operating inside my AIBox / Project Puente AI codebase. Your job is to perform a full bug, glitch, security, cleanup, and unused-functionality audit, then apply safe fixes directly.

Important context:
- This project is an offline AI learning server stack.
- It likely includes Docker Compose services, FastAPI/backend code, frontend/admin UI code, SQLite persistence, Chroma/RAG persistence, Kiwix/Kolibri integrations, llama server orchestration, Caddy/reverse proxy, Windows scripts, and possibly startup/service automation.
- MCPs available:
  - context7
  - filesystem
  - github
  - sqlite
- Use MCP tools whenever they are useful instead of relying only on shell commands.
- Use subagents whenever the task benefits from parallel or specialized review.
- Do not make risky destructive changes without first inspecting the affected files.
- Do not delete databases, model files, vector stores, user data, uploads, backups, or deployment configs.
- Favor small, correct, reviewable fixes over huge rewrites.
- After every major set of fixes, run the most relevant tests/checks available.

Model and effort strategy:
- If a specific area is especially bad, convoluted, fragile, security-sensitive, or architecture-heavy, open an Opus high-effort subagent to analyze that area before making major changes.
- Use Opus/high subagents especially for:
  - tangled authentication or authorization logic
  - admin/student permission boundaries
  - dangerous file deletion or cleanup logic
  - subprocess/runtime-control code
  - Docker orchestration that affects boot reliability
  - RAG startup validation and Chroma/embedding configuration
  - confusing code paths where a bad fix could break production
  - security issues involving path traversal, command injection, SQL injection, secrets, or user data exposure
- The Opus subagent should produce:
  - root cause analysis
  - safest minimal fix
  - risks of the fix
  - files that must be changed
  - validation commands to run afterward
- Do not use Opus/max or xhigh for the general audit.
- Reserve xhigh/max only for one extremely difficult, isolated bug if Sonnet/high and Opus/high cannot resolve it.
- Do not let the Opus subagent rewrite large systems unless the current implementation is clearly broken and a smaller fix is not possible.

Editing strategy:
- Automatically make simple, safe, localized edits when the issue and fix are clear.
- Examples of simple safe edits:
  - broken imports
  - typos
  - missing null checks
  - incorrect endpoint names
  - clearly wrong path constants
  - missing directory creation
  - missing timeout handling
  - obvious frontend error-state fixes
  - unused imports
  - small validation checks
  - obvious admin guard omissions
  - minor Docker healthcheck/config improvements
- Do not stop to ask me before making these simple safe edits.
- For hard, risky, ambiguous, or architecture-level issues, do not force a fix immediately.
- Instead, document them clearly in a markdown file named AUDIT_AND_FIX_REPORT.md.
- For each hard issue, include:
  - what the issue is
  - affected files
  - why it matters
  - severity
  - why it is risky to fix automatically
  - recommended fix
  - suggested validation commands
  - whether an Opus/high subagent reviewed it
- If a hard issue is critical or high severity, use an Opus/high subagent to analyze it and then either apply the smallest safe fix or document why it should be handled manually.

Your mission:
Go through the codebase and find bugs, glitches, broken paths, misconfigurations, security issues, unused functionality, dead code, fragile assumptions, broken Docker/runtime behavior, bad error handling, and anything that could cause the AIBox to fail in production. Then fix what is safe to fix. Save hard/risky issues in AUDIT_AND_FIX_REPORT.md with clear explanations and recommended next steps.

Start with a full reconnaissance phase.

PHASE 1 — Repository map and project understanding

1. Use filesystem tools to inspect the project structure.
2. Identify:
   - main backend app entrypoints
   - frontend/app entrypoints
   - Docker Compose files
   - Dockerfiles
   - Caddy/proxy config
   - Windows startup scripts / PowerShell scripts / NSSM scripts
   - SQLite database usage
   - Chroma/RAG usage
   - llama/model server integration
   - Kiwix/Kolibri integration
   - auth/session/user-account code
   - admin/runtime-control code
   - file upload / user document storage code
   - environment variable files or examples
   - tests, linters, package files, requirements, pyproject, package.json, etc.
3. Build a concise mental model of how the system boots and how a user request flows:
   - browser/client
   - Caddy/front proxy
   - backend/API
   - auth/session
   - AI generation
   - RAG retrieval
   - storage/database
   - admin diagnostics
4. Before editing anything, create an initial risk checklist inside AUDIT_AND_FIX_REPORT.md.

PHASE 2 — Create specialized subagents

Use subagents where helpful. At minimum, split the review into these tracks:

Subagent A: Backend/API audit
- FastAPI routes
- request validation
- authentication and authorization
- session handling
- user isolation
- file access paths
- AI-control endpoints
- admin-only endpoints
- error handling
- logging
- startup checks
- environment-variable handling

Subagent B: Frontend/UI audit
- login/account creation
- student portal
- admin console
- diagnostics UI
- API calls
- error states
- loading states
- dark/light theme persistence
- Spanish toggle if present
- broken links, missing buttons, dead components

Subagent C: Docker/runtime/orchestration audit
- docker-compose.yaml
- service dependencies
- volumes and mounts
- Windows path mapping
- model paths
- Chroma paths
- Kiwix/Kolibri paths
- Caddy proxy routes
- healthchecks
- restart policies
- startup order
- environment variables
- NSSM/PowerShell service behavior

Subagent D: RAG/AI pipeline audit
- embedding model path
- vector database path
- Chroma collection loading
- retrieval relevance
- reranking toggles
- prompt construction
- token counting
- context injection
- graceful fallback when no context is found
- diagnostics reporting
- latency/TPS instrumentation

Subagent E: Security and privacy audit
- default credentials
- hardcoded secrets
- API keys
- insecure CORS
- open admin routes
- path traversal
- arbitrary file read/write
- command injection
- SSRF
- unsafe subprocess usage
- SQL injection
- missing rate limits
- weak password policy
- sensitive logs
- exposed database/model paths
- user data retention/deletion issues

Subagent F: Dead code and unused functionality audit
- unused files
- unused components
- unused endpoints
- unused scripts
- stale backup code
- duplicate logic
- unreachable code
- old legacy AI container references
- unused dependencies
- commented-out code that should be removed or documented

Each subagent should report:
- files inspected
- issues found
- severity: critical / high / medium / low / cleanup
- proposed fix
- whether the fix is safe to apply now
- whether the issue should instead be saved in AUDIT_AND_FIX_REPORT.md for manual/higher-risk review

PHASE 3 — Use MCP tools deliberately

Use the available MCPs as follows:

Filesystem MCP:
- Inspect files and directories.
- Read config files.
- Search for risky patterns.
- Apply edits where appropriate.
- Check for duplicate or unused files.

SQLite MCP:
- Inspect database schema if a local SQLite database exists.
- Check tables, indexes, migrations, and relationships.
- Look for schema/code mismatches.
- Do not alter live data unless the project clearly uses migrations and the change is necessary.
- Prefer code-level compatibility fixes over destructive DB changes.

GitHub MCP:
- Check current branch/status if available.
- Inspect issues/PRs/history if useful.
- Do not push unless explicitly asked.
- Use git diff frequently to keep track of changes.

Context7 MCP:
- Use for current documentation on libraries/frameworks when needed.
- Prioritize official docs for FastAPI, Docker Compose, Chroma, Caddy, SQLite, React/Vite/Next, Tailwind, or other libraries actually used by the codebase.
- Do not guess library behavior if docs can clarify it.

PHASE 4 — Bug and glitch search strategy

Search the repo for the following bug patterns:

General:
- TODO, FIXME, HACK, BUG, temporary, legacy, deprecated
- empty catch blocks
- broad exception swallowing
- print-only error handling
- inconsistent path separators
- hardcoded absolute paths
- missing directory creation
- missing file existence checks
- missing timeout handling
- missing retries around service calls
- startup code that silently fails
- race conditions during boot
- incorrect async usage
- blocking calls inside async endpoints
- unchecked None/null values
- inconsistent environment variable names
- broken imports
- duplicate functions
- stale config values
- unreachable code

Python/FastAPI:
- unvalidated request bodies
- endpoints missing auth dependencies
- endpoints missing admin guard
- direct string SQL
- unsafe subprocess calls
- file path joins without normalization
- missing CORS restrictions
- missing rate limiting
- missing exception handlers
- leaking stack traces to clients
- logging secrets or prompts with private data
- global mutable state problems
- resource cleanup issues
- startup/shutdown event problems
- dependency version incompatibilities

Frontend:
- API URLs hardcoded incorrectly
- missing error display
- broken forms
- buttons that call nonexistent endpoints
- theme state not persisted
- Spanish toggle partially implemented
- admin controls visible to students
- diagnostics panel exposed to non-admin users
- unsafe rendering of HTML/Markdown
- broken loading states
- stale mock data
- unused components
- console errors
- missing accessibility labels for important controls

Docker/runtime:
- broken bind mounts
- missing volumes
- paths that work on Linux but not Windows, or vice versa
- missing healthchecks
- services starting before dependencies are ready
- incorrect exposed ports
- Caddy reverse proxy routes mismatched with backend/frontend
- containers relying on local files that are not mounted
- llama/model paths inconsistent with backend expectations
- Chroma persistence not mounted
- SQLite DB not persisted
- secrets baked into images
- bloated images or unused services
- old/legacy services that conflict with current services

RAG/AI:
- embedding model path mismatch
- Chroma collection name mismatch
- Chroma persist directory mismatch
- collection exists but code points elsewhere
- retrieval returns no context due to bad config
- reranking toggle references missing dependency
- prompt injection risks from retrieved content
- unbounded context size
- missing token budget enforcement
- bad stop sequences
- no graceful fallback when llama is down
- diagnostics misreporting retrieval/generation status
- timing code causing large performance overhead

Security:
- default passwords
- weak session cookies
- cookies missing httponly/secure/samesite where applicable
- JWT secret fallback values
- admin endpoints protected only by frontend hiding
- CORS allowing all origins in production
- file upload path traversal
- serving arbitrary files
- SQL injection
- shell injection
- SSRF from user-provided URLs
- exposed environment variables
- secrets committed to repo
- logs containing tokens, passwords, private prompts, or student data
- missing rate limits on auth and AI endpoints
- no size limits on uploads/request bodies
- user data not isolated by user ID

PHASE 5 — Fixing rules

Apply fixes directly when they are safe, localized, and clearly correct.

Safe fixes include:
- broken imports
- typos
- incorrect path constants
- missing directory creation
- missing null checks
- broken API route paths
- frontend calling wrong endpoint
- missing error state handling
- obvious admin guard omissions
- basic path traversal protection
- obvious CORS tightening with dev/prod distinction
- missing timeouts
- missing healthchecks
- missing environment examples
- small Docker volume/path fixes
- missing startup validation
- duplicate unused imports
- clearly dead code removal when no references exist

Be more cautious with:
- database schema changes
- auth/session rewrites
- major Docker architecture changes
- deleting services
- replacing libraries
- changing data formats
- changing model/RAG behavior heavily
- deleting old scripts unless clearly obsolete
- anything involving destructive cleanup/deletion
- anything involving user data or student privacy

For cautious items:
- First document the issue in AUDIT_AND_FIX_REPORT.md.
- Use an Opus/high subagent if the issue is complex, high-risk, security-sensitive, or production-critical.
- Apply the smallest compatibility-preserving fix only if it is clearly safe.
- If the fix requires migration, operator action, or manual testing, leave it as a clearly documented recommendation instead of doing something destructive.

PHASE 6 — Specific things I want checked carefully

Pay special attention to these AIBox-specific concerns:

1. RAG startup validation
Make sure the backend checks:
- embedding model path exists
- Chroma persist directory exists
- Chroma collection exists
- collection document count is nonzero
- configured collection name matches actual collection
- retrieval failure is reported clearly
- “no relevant wiki context” is distinguishable from “RAG is misconfigured”
- diagnostics show whether RAG was used and why/why not

2. User isolation
Verify:
- users cannot access other users’ files
- user IDs cannot be spoofed through request parameters
- server enforces ownership
- admin-only actions require server-side admin validation
- frontend hiding is not the only protection

3. Runtime control
Verify:
- start/stop/restart/status endpoints cannot be used by students
- subprocess/service calls are safe
- Windows paths are handled correctly
- Docker/NSSM/PowerShell assumptions are documented
- status shown in UI reflects actual process/container state

4. Storage safety
Verify:
- SQLite DB is persisted
- Chroma DB is persisted
- uploaded/user files are persisted
- cleanup thresholds do not accidentally delete important data
- there are no dangerous recursive delete calls
- emergency cleanup is conservative

5. Docker boot reliability
Verify:
- containers restart appropriately
- healthchecks exist where useful
- backend waits for dependent services
- Caddy routes are correct
- environment variables are consistent
- local volumes point to the intended paths

6. Diagnostics overhead
Verify:
- diagnostics do not significantly slow down generation
- prompt/chunk snapshots are admin-only
- sensitive student data is not exposed broadly
- token/timing reporting is accurate enough

7. Frontend polish bugs
Verify:
- student login/create-account flow works
- admin console does not break if backend status endpoint fails
- loading/error states are clear
- dark/light theme persists
- Spanish toggle does not cause broken text or layout
- buttons map to real backend endpoints

PHASE 7 — Validation commands

Before making changes, detect what commands are available. Look for package.json, pyproject.toml, requirements.txt, pytest config, Docker files, etc.

Run only relevant commands, such as:
- git status
- python -m compileall
- pytest
- npm install only if needed and safe
- npm run build
- npm run lint
- npm test
- docker compose config
- docker compose build if reasonable
- docker compose up healthcheck validation if reasonable
- targeted script checks
- SQLite schema inspection

Do not run destructive scripts. Do not wipe volumes. Do not reset databases.

If a command fails:
- capture the failure
- determine whether it is caused by your changes or preexisting configuration
- fix if safe
- rerun the narrowest relevant check
- if not safe to fix automatically, document it in AUDIT_AND_FIX_REPORT.md

PHASE 8 — Reporting format

Create or update:

AUDIT_AND_FIX_REPORT.md

The report should include:

1. Executive summary
- total files inspected
- major systems inspected
- simple fixes applied automatically
- hard/risky issues saved for manual review
- unresolved risks
- commands run

2. Critical/high findings
For each:
- issue
- affected files
- impact
- fix applied or recommendation
- whether an Opus/high subagent reviewed it
- validation result

3. Medium/low findings
Same structure, shorter.

4. Automatically fixed issues
For each:
- issue
- affected files
- fix applied
- validation result

5. Hard/risky issues not automatically fixed
For each:
- issue
- affected files
- severity
- why it matters
- why it was not automatically fixed
- recommended fix
- suggested validation commands
- whether an Opus/high subagent should review it or already reviewed it

6. Security review
Include:
- auth/admin protections
- file access protections
- secrets/config
- CORS/network exposure
- subprocess/command safety
- database safety
- logging/privacy
- user-data isolation

7. RAG/AI review
Include:
- model path
- embedding path
- Chroma path
- collection name
- startup validation
- retrieval behavior
- fallback behavior
- diagnostics
- unresolved RAG risks

8. Docker/runtime review
Include:
- compose health
- volumes
- ports
- service dependencies
- Windows/Linux path notes
- startup reliability
- unresolved runtime risks

9. Dead code / unused functionality
Include:
- removed items
- items kept because uncertain
- recommended future cleanup

10. Validation commands and results
Include exact commands run and pass/fail status.

PHASE 9 — Edit workflow

Use this workflow:

1. Inspect.
2. Make a plan.
3. Apply a small batch of simple safe fixes.
4. Save hard/risky issues in AUDIT_AND_FIX_REPORT.md instead of blindly editing them.
5. Use Opus/high subagents for convoluted, fragile, security-sensitive, or architecture-heavy areas.
6. Show git diff summary.
7. Run targeted validation.
8. Repeat until no major safe fixes remain.
9. Write final report.
10. End with:
   - summary of what changed
   - remaining risks
   - exact commands I should run manually if something could not be run
   - whether the project appears safer/more stable than before

PHASE 10 — Final constraints

Do not stop after only surface-level linting.
Do not only produce a report; actually fix safe issues.
Do not rewrite the entire app.
Do not delete important data.
Do not expose secrets in the report.
Do not ignore security.
Do not trust frontend-only protections.
Do not assume paths are correct; verify them.
Do not assume RAG works; verify startup configuration and retrieval flow.
Do not assume Docker volumes persist data; verify compose configuration.
Do not use Opus/max or xhigh for broad auditing.
Do not apply risky fixes just to appear productive.
Do not make destructive changes to data, databases, Chroma stores, model files, uploaded files, or backups.

Begin now by mapping the repository, creating AUDIT_AND_FIX_REPORT.md, and writing the initial audit plan. Then proceed through the phases, automatically applying simple safe edits and documenting hard/risky issues for review. If, at any point, I am about to run out of tokens, stop what you are doing, and create a file called reviewSave.md that overviews what you have done, and what you still need to do, so you can continue later.