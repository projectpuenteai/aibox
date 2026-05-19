# `app_storage.py` Module Split Plan

`tools/ai-control/app_storage.py` still owns auth, storage, cleanup, analytics,
RAG, chat, and route mounting. A full split should be staged to avoid changing
runtime behavior on field devices.

## Completed First Boundary

- SQLite schema management now lives in `tools/ai-control/storage_migrations.py`.
- `StorageRuntime.init_db()` calls the migration runner and keeps post-migration
  data backfill behavior in the runtime.
- `tools/tests/test_storage_migrations.py` covers fresh database initialization,
  idempotent migration recording, and upgrade from an old ad hoc schema.

## Recommended Next Boundaries

1. Move request/response Pydantic models into `storage_models.py`.
2. Move analytics helpers into `storage_analytics.py`.
3. Move cleanup manifest and deletion planning helpers into `storage_cleanup.py`.
4. Move RAG index loading, validation, and prompt-context construction into
   `storage_rag.py`.
5. Move auth/session helpers into `storage_auth.py`.
6. Keep route registration in `app_storage.py` until each helper module has
   focused tests, then split route groups last.

Each step should preserve public route behavior and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\aibox\tools\tests\validate_python.ps1
py -3 -m pytest .\aibox\tools\tests\test_security_controls.py
py -3 -m pytest .\aibox\tools\tests\test_storage_migrations.py
```
