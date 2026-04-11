# RAG Review

Primary evidence:

- `c:\AIBox\aibox\tools\ai-control\app_storage.py`
- `c:\AIBox\aibox\stack\docker-compose.yaml`
- `c:\AIBox\aibox\models\embed\README.md`
- `c:\AIBox\aibox\models\rerank\config.json`

This review covers the active `ai-control` pipeline, not the legacy `backend/app.py` path except where contrast matters.

## 1. Spanish support, Spanish Wikipedia support, prompt injection

Spanish responses:

- Code-confirmed: yes.
- The active system prompt in compose says: answer in the same language the student uses and teach at an age-appropriate level.
- Evidence: `BASE_SYSTEM_PROMPT` in `stack/docker-compose.yaml`.

Spanish Wikipedia retrieval:

- Partially supported in architecture, not strongly supported in the current model/index setup.
- Retrieval itself is language-agnostic at the pipeline level: `build_retrieval_query()`, `retrieve_wiki_chunks()`, `rerank_wiki_chunks()`, and `prepare_wiki_context()` do not block Spanish queries.
- But current embedding quality for Spanish is likely weak because the embed model on disk is `BAAI/bge-large-en-v1.5`, which is English-focused.
- Evidence: `models/embed/README.md` identifies the embed model as `bge-large-en-v1.5`; compose mounts that directory as `EMBED_MODEL=/models/embed`.
- The collection name is still `simplewiki_chunks`, which suggests the active index may be English-source-oriented unless you explicitly rebuilt it with Spanish content.
- Conclusion: Spanish-source retrieval only works well if Spanish Wikipedia content has already been chunked and indexed into the active Chroma collection, and even then the English-biased embedder is a likely quality bottleneck.

Prompt-injection resistance:

- Partial only.
- Good: retrieved context is injected as a system message via `_build_retrieval_system_message()` and `inject_wiki_context()`, which keeps retrieval above user turns in the final conversation.
- Weak: there is no explicit sanitization or trust policy for malicious retrieved passages before they are injected.
- Weak: `normalize_messages()` accepts client-provided `system`, `user`, and `assistant` roles, so callers can send their own system messages into the upstream request.
- Evidence: `normalize_messages()` in `app_storage.py` keeps `role in ("system", "user", "assistant")`; `inject_wiki_context()` inserts its system message ahead of the first non-system message but does not remove existing client-supplied system content.
- Conclusion: the system has some structural protection, but it is not hardened against prompt injection from either retrieved text or client-supplied system messages.

## 2. Highest-value RAG improvements

1. Replace the embedding model with a multilingual retriever and rebuild the index.

- Best single improvement if Spanish Wikipedia is a real goal.
- Strong candidates: `BAAI/bge-m3` or `intfloat/multilingual-e5-large`.
- Rebuild the Chroma collection after the model change so embedding space and stored vectors match.

2. Split or filter retrieval by source language.

- Add chunk metadata for language and either:
  - query the same collection with language filtering, or
  - maintain separate collections for English and Spanish.
- This prevents English chunks from dominating Spanish queries.

3. Reduce retrieval waste before reranking.

- Current defaults: `RETRIEVAL_CANDIDATE_K=12`, `RETRIEVAL_TOP_K=5`, `RERANK_SCORE_THRESHOLD=0.45`, `RETRIEVAL_MAX_CONTEXT_CHARS=18000`.
- This is workable, but the context budget is too large relative to the llama context budget and increases prompt cost.
- Tighten retrieved context so only the most useful chunks reach the model.

4. Harden query construction for Spanish.

- `build_retrieval_query()` is sensible, but the skip logic and token heuristics are not especially language-aware.
- Improve topic extraction and retrieval-skip heuristics for accented Spanish, short educational prompts, and follow-up questions.

5. Add explicit prompt-injection filtering.

- Strip or down-rank passages containing instruction-like patterns such as “ignore previous instructions”, “system prompt”, “assistant must”, or role simulation text.
- Reject or sanitize client-supplied `system` messages unless the caller is trusted.

## 3. Bottlenecks and performance improvements

Main bottlenecks:

- CPU embedding.
  - Compose sets `RETRIEVAL_DEVICE=cpu`.
  - Every retrieval query embeds on CPU under `_retriever_run_lock`.

- CPU reranking.
  - Compose sets `RERANK_DEVICE=cpu`.
  - Cross-encoder reranking is often the slowest retrieval-stage step.

- Large retrieval context budget.
  - Compose sets `RETRIEVAL_MAX_CONTEXT_CHARS=18000`.
  - That can consume a large share of the llama prompt budget and slow prompt processing.

- Long timeout budget.
  - Compose sets `RETRIEVAL_TIMEOUT_SECONDS=25.0`.
  - That is generous for resilience, but it can hide a slow retrieval path and hurt perceived latency.

- Client-supplied system-role passthrough.
  - This is more a quality/safety bottleneck than a raw speed bottleneck, but it can materially degrade output quality.

Recommended performance changes:

1. Move reranking off CPU first if possible.

- On a 3060 12 GB, a smaller/faster multilingual reranker on GPU usually gives a better latency win than trying to keep the large English embedder on CPU.

2. Tighten prompt budget.

- Lower `RETRIEVAL_MAX_CONTEXT_CHARS` from `18000` to roughly `6000-9000` for this stack.
- Let the reranker and thresholding do more work before prompt injection.

3. Keep candidate counts modest.

- `candidate_k=12` and `top_k=5` is reasonable.
- If latency is still high, test `candidate_k=8-10` before cutting `top_k` below `4`.

4. Keep the HNSW/index path hot.

- The app already includes warmup routines. Keep them.
- If retrieval still spikes, benchmark Chroma I/O and collection size directly.

5. Reject untrusted `system` messages.

- This improves answer stability and reduces “prompt fighting” inside the final conversation.

## 4. RTX 3060 12 GB recommendations

Current llama defaults in compose:

- `LLAMA_CTX_SIZE=8192`
- `LLAMA_N_PREDICT=768` at server launch, plus `LLAMA_N_PREDICT=1024` in `ai-control` env
- `LLAMA_BATCH_SIZE=1024`
- `LLAMA_UBATCH_SIZE=512`
- `LLAMA_N_GPU_LAYERS=99`
- flash attention enabled
- continuous batching enabled

Recommended starting point for balanced quality/speed/detail:

- `LLAMA_CTX_SIZE=8192`
- `LLAMA_N_PREDICT=512` for typical chat, raise to `768` only when longer answers are clearly needed
- `LLAMA_BATCH_SIZE=512` or `768`
- `LLAMA_UBATCH_SIZE=256` or `384`
- `LLAMA_N_GPU_LAYERS=99` if VRAM permits; otherwise reduce only if you observe pressure
- `RETRIEVAL_TOP_K=4` or `5`
- `RETRIEVAL_CANDIDATE_K=8` to `12`
- `RETRIEVAL_MAX_CONTEXT_CHARS=6000` to `9000`

Why:

- `ctx-size 8192` is a good ceiling on a 3060 12 GB for a 7B GGUF with flash attention.
- Bigger context is not free; if latency rises, reduce injected context before shrinking retrieval quality too aggressively.
- Reducing `n_predict` from 1024 to 512 often improves responsiveness without hurting educational usefulness.
- Moderate batch sizes tend to be steadier on 12 GB cards than maxing them out.

If you want more detail over speed:

- Keep `ctx-size 8192`.
- Use `n_predict=768`.
- Keep `top_k=5`.
- Increase answer detail in prompting rather than pushing context larger first.

If you want more speed:

- Drop `n_predict` to `384-512`.
- Lower `RETRIEVAL_MAX_CONTEXT_CHARS`.
- Test `candidate_k=8`.

## 5. Teacher-style prompt

This is compatible with the current `BASE_SYSTEM_PROMPT` plus retrieval injection approach:

```text
You are Puente AI, a patient and highly effective teacher for students in Latin America.
Always answer in the same language the student uses.
Your job is to teach, not just give short answers.

When responding:
- Explain ideas clearly and step by step.
- Use simple language first, then add deeper detail if helpful.
- When a topic is difficult, break it into small parts.
- Correct mistakes gently and explain why.
- Use short examples, analogies, or mini-worked steps when useful.
- If the student asks a factual question, be accurate and grounded in the provided context when it is relevant.
- If the answer is uncertain, say so plainly instead of guessing.
- End important explanations with a brief check for understanding, a follow-up question, or a suggested next step.

Do not be condescending, do not invent facts, and do not mention hidden instructions or retrieval unless the student explicitly asks.
```

## Concrete hardening recommendations

- Stop accepting arbitrary client `system` messages in `normalize_messages()` for normal users.
- Add a retrieval sanitation pass that flags instruction-like chunk text before injection.
- Add chunk metadata for language and source, and filter retrieval accordingly.
- Rebuild embeddings with a multilingual model if Spanish Wikipedia is part of the product goal.
- Lower the retrieval context budget so the llama server spends more of its budget on reasoning and response, not prompt overhead.
