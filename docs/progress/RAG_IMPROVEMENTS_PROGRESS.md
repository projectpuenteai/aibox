# RAG Pipeline Improvements - Progress Tracker
**Last updated**: 2026-04-05 01:30 AM

## Status: Phase 2 In Progress (Index Rebuild Running - did not finish, ended program)

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

## IN PROGRESS

### Phase 2: Multilingual Embedding Model (bge-m3)

#### Completed:
- Downloaded BAAI/bge-m3 to `models/embed-m3/` (1024 dims, multilingual, ~2.2GB)
- Added `detect_language()` to `chunk_pages_for_rag.py` - detects Spanish by function word frequency
- Added `"language"` field to chunk records in `chunk_pages_for_rag.py`
- Added language metadata storage to `build_chroma_index.py`
- Added Spanish `SKIP_SECTION_TITLES` to `build_chroma_index.py`
- Updated `index_settings.py` default embed model to `embed-m3`
- Updated `docker-compose.yaml` EMBED_MODEL to `/models/embed-m3`

#### Currently Running:
- **Chroma index rebuild** with bge-m3 embeddings (771K chunks, CPU with 4 workers)
- DB growing from ~4GB base, writing to `/c/AIBox/aibox/backend-data/chroma_db/`
- Estimated time: could be several hours on CPU

#### Still TODO after rebuild completes:
1. Rebuild ai-control container: `cd /c/AIBox/aibox/stack && docker compose build ai-control && docker compose up -d ai-control`
2. Re-run the 3 graded test questions and compare scores
3. Spanish retrieval should dramatically improve with multilingual embedder

---

## NOT YET STARTED

### Phase 2F: Optional Language-Aware Reranking Boost
- Add small boost in `_heuristic_rerank_score()` when chunk language matches query language

### Phase 3: Comprehensive Validation
- Expand `tools/tests/test_rag_pipeline_smoke.py` and `tools/tests/test_rag_pipeline_smoke_es.py` with full graded test suite
- End-to-end generation tests
- Comparison matrix across all phases
- Injection safety test

---

## Files Modified

| File | Changes |
|---|---|
| `aibox/tools/ai-control/app_storage.py` | Spanish stopwords, topic hints, skip logic, follow-up detection, injection filtering, normalize_messages hardening |
| `aibox/stack/docker-compose.yaml` | New system prompt, retrieval instruction, tuned parameters, embed model path |
| `aibox/tools/data_prep/chunk_pages_for_rag.py` | Language detection function, language field on chunks |
| `aibox/tools/index/build_chroma_index.py` | Language metadata in Chroma, Spanish skip section titles |
| `aibox/tools/config/index_settings.py` | Default embed model changed to embed-m3 |
