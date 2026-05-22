# RAG Durability and Storage Module Notes

## Index Manifests

`tools/index/build_chroma_index.py` writes `index_manifest.json` into each
Chroma persist directory after a successful build. The manifest records:

- source chunk file path, size, and SHA-256
- embedding model path/name and dimension
- Chroma collection name, document count, and HNSW space
- chunk filter settings
- build batch sizes, skipped counts, and timing
- index tool version and build timestamp

`ai-control` reads the manifest during RAG startup validation and surfaces a
summary in admin diagnostics. Missing manifests do not block older deployed
indexes, but new rebuilds should always include one.

## Smoke Checks

Startup validation now runs a known-answer retrieval check:

- English: `What was the War of 1812?`, expecting `war` and `1812`
- Spanish: `¿Quién fue Simón Bolívar?`, expecting `bolívar`

The comprehensive local RAG suite also runs the same startup smoke path before
the broader retrieval cases:

```powershell
docker exec aibox-ai-control python /tmp/puente-rag/tests/test_rag_comprehensive.py --mode direct --save --output-dir /data/test-clones/<run>/rag
```

The E2E wrapper copies this script into the running `ai-control` container and
runs it after stack startup.

## Diagnostics Limits

Admin diagnostics are bounded before they are returned to the browser. The
defaults can be tuned in `stack/.env`:

```env
ADMIN_DIAGNOSTICS_MAX_BYTES=120000
ADMIN_DIAGNOSTICS_MAX_STRING_CHARS=4000
ADMIN_DIAGNOSTICS_MAX_LIST_ITEMS=50
```

Large strings, lists, and whole payloads include explicit truncation markers.

## `app_storage.py` Module Split Plan

`tools/ai-control/app_storage.py` still owns auth, storage, cleanup, analytics,
RAG, chat, and route mounting. A full split should be staged to avoid changing
runtime behavior on field devices.

### Completed First Boundary

- SQLite schema management now lives in `tools/ai-control/storage_migrations.py`.
- `StorageRuntime.init_db()` calls the migration runner and keeps post-migration
  data backfill behavior in the runtime.
- `tools/tests/test_storage_migrations.py` covers fresh database initialization,
  idempotent migration recording, and upgrade from an old ad hoc schema.

### Recommended Next Boundaries

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
