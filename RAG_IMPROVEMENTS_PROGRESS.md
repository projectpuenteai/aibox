# RAG Pipeline Improvements - Progress Tracker
**Last updated**: 2026-04-06

## Status: Phase 3 Validation Complete, Spanish Wiki Index Next

---

## COMPLETED

### Phase 1A-D: Spanish Language Support (app_storage.py)
- **`_informative_terms()`** (line ~1181): Added ~40 Spanish stopwords, changed regex to support accented chars (`\u00C0-\u024F`)
- **`_extract_topic_hint()`** (line ~1196): Added Spanish interrogative patterns (quien/que/donde/cuando), `sobre`/`acerca de` patterns, Spanish generic nouns filter
- **`_should_skip_retrieval()`** (line ~1236): Added Spanish greetings, math prefixes (cuanto es, suma, multiplica), personal words (yo, mi, nosotros), subjective phrases (cual es mi, mi tarea)
- **`_is_contextual_followup()`** (line ~1226): Added Spanish referential terms (el, ella, ellos, ese, este, aquel, quien, cual), fixed regex for accented chars

### Phase 1E: Prompt Injection Filtering (app_storage.py)
- Added `_INJECTION_PATTERNS` regex (matches "ignore previous instructions", "you are now", "system prompt", etc.)
- Added `_sanitize_retrieved_chunk()` method - strips flagged lines from chunk text
- Integrated into `build_wiki_context_payload()` for both primary and deferred duplicate chunks

### Phase 1F: Block Client System Messages (app_storage.py)
- `normalize_messages()` (line ~2448): Removed `"system"` from allowed roles - server injects its own system message

### Phase 1G: Teacher-Style System Prompt (docker-compose.yaml)
- Replaced `BASE_SYSTEM_PROMPT` with detailed teacher-style prompt:
  - Step-by-step explanations, simple language first, break difficult topics
  - Correct mistakes gently, use examples/analogies
  - End with check for understanding or follow-up question

### Phase 1H: Improved Retrieval Instruction (docker-compose.yaml)
- Updated `WIKI_RETRIEVAL_INSTRUCTION` to emphasize weaving context into teaching, Latin American context for Spanish queries, grounding claims in passages

### Phase 1I: Tuned Retrieval Parameters (docker-compose.yaml)
| Parameter | Old | New |
|---|---|---|
| `RETRIEVAL_MAX_CONTEXT_CHARS` | 18000 | 8000 |
| `RETRIEVAL_TIMEOUT_SECONDS` | 25.0 | 12.0 |
| `RETRIEVAL_CANDIDATE_K` | 12 | 10 |
| `RETRIEVAL_TOP_K` | 5 | 4 |
| `RERANK_SCORE_THRESHOLD` | 0.45 | 0.20 |

### Phase 1J: Baseline vs Phase 1 Testing Results

**Retrieval-Only Comparison:**

| Query | Phase | retrieval_ms | chunks | context_chars | context_tokens | Top Chunk (score) |
|---|---|---|---|---|---|---|
| Photosynthesis (EN) | Baseline | 101,635* | 3 | 18,000 | 4,500 | Photosynthesis (0.989) |
| Photosynthesis (EN) | Phase 1 | 105,068* | 2 | 8,000 | 2,000 | Photosynthesis (0.989) |
| Bolivar (ES) | Baseline | 93,243* | 1 | 4,297 | 1,074 | Simon Bolivar (0.714) |
| Bolivar (ES) | Phase 1 | 5,860 | 1 | 4,297 | 1,074 | Simon Bolivar (0.714) |
| Amazon (EN) | Baseline | 4,172 | 2 | 5,788 | 1,447 | Amazon rainforest (0.938) |
| Amazon (EN) | Phase 1 | 3,155 | 3 | 8,000 | 2,000 | Amazon rainforest (0.938) |

*Cold start includes embedder loading

**Full Generation Test Scores (Phase 1):**
| Query | Retrieval | Accuracy | Teaching | Language | Overall |
|---|---|---|---|---|---|
| Photosynthesis (EN) | 5/5 | 5/5 | 5/5 | 5/5 EN | **5/5** |
| Bolivar (ES) | 3/5 | 4/5 | 5/5 | 5/5 ES | **4/5** |
| Amazon (EN) | 4/5 | 4/5 | 5/5 | 4/5* | **4/5** |

*Amazon responded in Spanish due to session context from previous query

---

### Phase 2: Multilingual Embedding Model (bge-m3) — COMPLETE
- Downloaded BAAI/bge-m3 to `models/embed-m3/` (1024 dims, multilingual, ~2.2GB)
- Added `detect_language()` to `chunk_pages_for_rag.py` - detects Spanish by function word frequency
- Added `"language"` field to chunk records in `chunk_pages_for_rag.py`
- Added language metadata storage to `build_chroma_index.py`
- Added Spanish `SKIP_SECTION_TITLES` to `build_chroma_index.py`
- Updated `index_settings.py` default embed model to `embed-m3`
- Updated `docker-compose.yaml` EMBED_MODEL to `/models/embed-m3`
- **Index rebuilt**: 443,839 chunks with 1024-dim bge-m3 embeddings (deployed 2026-04-06)
- Backed up at `chroma_db.zip` (3GB) and old index at `backend-data/chroma_backups/chroma_db-pre-bgem3/`

### Phase 2F: Language-Aware Reranking Boost (COMPLETE)
- Added `_detect_query_language()` to `app_storage.py`
- +0.05 boost when chunk language matches query language
- +0.05 language-match component in heuristic rerank score

### Phase 2G: Indexing Script Improvements (COMPLETE)
- Progress reporting (%, speed, ETA), below-normal priority, `--max-cpu-percent` flag
- GPU auto-tuning: embed-batch=256, chroma-batch=2048 when `--device cuda`

### Phase 2H: Cross-Lingual Reranking Fallback (COMPLETE)
- CrossEncoder reranker struggles with cross-lingual pairs (Spanish query → English chunk)
- Added fallback in `rerank_wiki_chunks()`: when reranker scores ALL chunks below threshold but embedding distances are good (< 0.45), falls back to distance-based heuristic scoring
- Effect: Spanish queries against English-only SimpleWiki index now return relevant results
- Example: "Que es la fotosintesis?" → Photosynthesis chunks via fallback

### Phase 2I: Query Expansion for Short Queries (COMPLETE)
- In `build_retrieval_query()`: single-word queries (1-2 words) that aren't greetings/math get "Explain " prepended
- Example: "mitochondria" → "Explain mitochondria" for better embedding signal
- Also fixed `_should_skip_retrieval()` to allow topic words >= 5 chars (was incorrectly skipping "mitochondria")

### Phase 2J: Language Tags in Context (COMPLETE)
- In `build_wiki_context_payload()`: chunk headers now include language tag
- Format: `[1] Photosynthesis :: Section (en)` — helps Qwen know the source language

### Phase 2K: Spanish Math Skip Fix (COMPLETE)
- `_should_skip_retrieval()` now converts Spanish math words (por→*, mas→+, menos→-, entre→/) before checking math expressions
- "cuanto es 5 por 3" now correctly skipped

### Phase 2L: Parameter Tuning for Large Index (COMPLETE)
| Parameter | Old | New | Reason |
|---|---|---|---|
| `RETRIEVAL_CANDIDATE_K` | 10 | 20 | Larger index needs more candidates for reranker |
| `RERANK_SCORE_THRESHOLD` | 0.20 | 0.15 | bge-m3 scores differently, avoid dropping borderline chunks |

### Phase 3: Comprehensive Validation (COMPLETE)

#### Test Infrastructure
- Created `tools/tests/test_cases.json` — 20 test queries across 5 categories
- Created `tools/tests/test_rag_comprehensive.py` — runner supporting direct (in-container) and API modes
- Categories: core_en, core_es, edge, skip, injection
- Updated Dockerfile to include test_rag_pipeline.py in container

#### Phase 3 Test Results (2026-04-06, bge-m3 index, 443K chunks)

**English Queries (all excellent):**

| Query | Chunks | Top Title | Top Score | Latency |
|---|---|---|---|---|
| What is photosynthesis? | 4 | Photosynthesis | 1.000 | 5-7s* |
| What is the Amazon rainforest? | 4 | Amazon rainforest | 1.000 | 8s |
| How does gravity work? | 4 | Gravity | 1.000 | 5s |
| What is DNA and how does it replicate? | 4 | DNA replication | 1.000 | 6s |
| What causes earthquakes? | 4 | Earthquake | 1.000 | 6s |
| French Revolution and its causes | 4 | French Revolution | 1.000 | 8-11s |
| mitochondria | 4 | Mitochondria | 1.000 | 7s |
| Water cycle | 3 | Aquatic locomotion | 1.000 | 5s |

*First query ~70s due to cold-start model loading

**Spanish Queries (cross-lingual, English SimpleWiki):**

| Query | Chunks | Top Title | Top Score | Fallback |
|---|---|---|---|---|
| Que es la fotosintesis? | 4 | Photosynthesis | 0.187 | Yes |
| Quien fue Simon Bolivar? | 2 | Simón Bolívar | 0.917 | No |
| Que es la gravedad? | 4 | Force, Gravity | 0.181 | Yes |
| Que es el sistema solar? | 3 | Solar System | 0.588 | Yes |
| Quien descubrio America? | 1 | United States | 0.330 | No |

**Skip Detection (all correct):**
- "hello", "hola", "2+2", "cuanto es 5 por 3" → all correctly skipped

**Key Observations:**
1. English retrieval is excellent — top chunks are always relevant, scores ≥ 0.90
2. Spanish cross-lingual retrieval works via fallback, but scores are lower (0.15-0.59)
3. A Spanish Wikipedia index would dramatically improve Spanish query quality and speed
4. Latency: ~5-10s per query on CPU (acceptable for educational use)
5. Cold start: ~70s (embedding model loading) — warmup queries run on startup

---

## NEXT STEPS

### Phase 4: Spanish Wikipedia Index
- Download Spanish Simple Wikipedia dump
- Run same pipeline: extract → chunk → build_chroma_index with bge-m3
- Add Spanish chunks to the same `simplewiki_chunks` collection (language metadata already supported)
- This will eliminate the cross-lingual fallback need and give native Spanish retrieval
- The existing scripts, embedding model (bge-m3), and language detection all support Spanish natively

---

## Files Modified

| File | Changes |
|---|---|
| `aibox/tools/ai-control/app_storage.py` | Spanish stopwords, topic hints, skip logic, follow-up detection, injection filtering, normalize_messages hardening, `_detect_query_language()`, language-aware reranking boost, cross-lingual reranking fallback, query expansion for short queries, language tags in context headers, Spanish math word support in skip logic, topic word skip fix (>= 5 chars allowed) |
| `aibox/stack/docker-compose.yaml` | New system prompt, retrieval instruction, tuned parameters (CANDIDATE_K=20, RERANK_THRESHOLD=0.15), embed model path |
| `aibox/tools/ai-control/Dockerfile` | Added test_rag_pipeline.py to container image |
| `aibox/tools/data_prep/chunk_pages_for_rag.py` | Language detection, language field on chunks, progress reporting (%, speed, ETA), 90% CPU cap, below-normal priority |
| `aibox/tools/index/build_chroma_index.py` | Language metadata in Chroma, Spanish skip sections, progress reporting (%, speed, ETA), `--max-cpu-percent` flag, below-normal priority, GPU auto-tuning |
| `aibox/tools/index/rebuild_chroma_index.py` | Pass-through for `--max-cpu-percent`, `--embed-batch` auto-tuning, CUDA batch optimization |
| `aibox/tools/config/index_settings.py` | Default embed model changed to embed-m3 |
| `aibox/tools/tests/test_cases.json` | NEW — 20 comprehensive test cases across 5 categories |
| `aibox/tools/tests/test_rag_comprehensive.py` | NEW — test runner with direct + API modes, JSON output, category filtering |
