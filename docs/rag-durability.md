# RAG Durability

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
