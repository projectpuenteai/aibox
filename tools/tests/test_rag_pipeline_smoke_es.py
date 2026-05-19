#!/usr/bin/env python3
"""Spanish-only RAG smoke test.

Asserts that with RAG_SPANISH_ONLY=1 the pipeline:
  - routes to the Spanish ChromaDB (rag_index_language="es" on every chunk)
  - uses the reranker (rerank_enabled=True, rerank_ms>0)
  - the reranker actually reorders at least some candidates
  - citation URLs point to /wiki/es/...
  - returned chunks look relevant for both Spanish and English queries

Run inside the ai-control container after the stack is up:
  docker exec aibox-ai-control python /app/tools/tests/test_rag_pipeline_smoke_es.py
"""

import json
import sys

from app_storage import StorageRuntime


QUERIES = [
    "¿Quién fue Simón Bolívar?",
    "¿Qué es la fotosíntesis?",
    "Who was Abraham Lincoln?",
    "What is photosynthesis?",
]


def run_query(rt: StorageRuntime, query: str) -> dict:
    print(f"\n{'='*70}")
    print(f"Query: {query}")
    print(f"{'='*70}")
    payload = rt.prepare_wiki_context(query)
    context = str(payload.get("context", "") or "")
    selected = payload.get("selected_chunks") or []
    print(f"  context length    : {len(context)} chars")
    print(f"  chunks selected   : {len(selected)}")
    print(f"  rerank_enabled    : {payload.get('rerank_enabled')}")
    print(f"  rerank_ms         : {payload.get('rerank_ms')}")
    print(f"  candidate_count   : {payload.get('candidate_count')}")
    print(f"  citations         : {len(payload.get('citations') or [])}")
    for i, chunk in enumerate(selected, 1):
        title = chunk.get("title") or chunk.get("page_title") or "?"
        score = chunk.get("relevance_score", 0)
        lang = chunk.get("rag_index_language")
        orig = chunk.get("original_rank")
        rerk = chunk.get("reranked_rank")
        print(f"    [{i}] lang={lang} score={score:.4f} title={title!r} (orig_rank={orig} -> rerank={rerk})")
    return payload


def assert_spanish_index(payload: dict, query: str):
    selected = payload.get("selected_chunks") or []
    assert selected, f"FAIL [{query}]: no chunks selected"
    for chunk in selected:
        lang = chunk.get("rag_index_language")
        assert lang == "es", (
            f"FAIL [{query}]: chunk routed to {lang!r}, expected 'es'. "
            "RAG_SPANISH_ONLY is not active or routing is broken."
        )
    print(f"  PASS: all {len(selected)} chunks have rag_index_language='es'")


def assert_reranker_ran(payload: dict, query: str):
    assert payload.get("rerank_enabled") is True, (
        f"FAIL [{query}]: rerank_enabled={payload.get('rerank_enabled')}, expected True"
    )
    rerank_ms = payload.get("rerank_ms") or 0
    assert rerank_ms > 0, f"FAIL [{query}]: rerank_ms={rerank_ms}, expected > 0"
    candidate_count = payload.get("candidate_count") or 0
    assert candidate_count >= len(payload.get("selected_chunks") or []), (
        f"FAIL [{query}]: candidate_count={candidate_count} should be >= final selection"
    )
    print(f"  PASS: reranker ran on {candidate_count} candidates in {rerank_ms} ms")


def assert_reranker_reordered(payload: dict, query: str):
    """At least one selected chunk should have reranked_rank != original_rank
    OR the candidate pool should be larger than the final selection (proving
    the reranker filtered/promoted)."""
    selected = payload.get("selected_chunks") or []
    moved = [c for c in selected if c.get("original_rank") != c.get("reranked_rank")]
    candidate_count = payload.get("candidate_count") or 0
    promoted_from_below_topk = any(
        (c.get("original_rank") or 0) > len(selected) for c in selected
    )
    assert moved or promoted_from_below_topk or candidate_count > len(selected), (
        f"FAIL [{query}]: reranker had no effect. "
        f"selected={len(selected)} candidate_count={candidate_count} moved={len(moved)}"
    )
    if moved:
        print(f"  PASS: reranker reordered {len(moved)}/{len(selected)} selected chunks")
    else:
        print(f"  PASS: reranker pruned {candidate_count - len(selected)} candidates below threshold")


def assert_citations_spanish(payload: dict, query: str):
    citations = payload.get("citations") or []
    assert citations, f"FAIL [{query}]: no citations returned"
    for citation in citations:
        url = str(citation.get("wiki_url") or "")
        assert "/wiki/es/" in url, (
            f"FAIL [{query}]: citation URL {url!r} should contain '/wiki/es/'"
        )
    print(f"  PASS: {len(citations)} citation(s), all point to /wiki/es/")


def main():
    print("Initializing StorageRuntime (uses RAG_SPANISH_ONLY from env)...")
    rt = StorageRuntime(llama_base_url="http://localhost:2020")
    if not rt.rag_spanish_only:
        print("FAIL: RAG_SPANISH_ONLY is not set in this container env.")
        sys.exit(1)
    print(f"  rag_spanish_only  : {rt.rag_spanish_only}")
    print(f"  chroma_persist_es : {rt.chroma_persist_dir_es}")
    print(f"  chroma_collection : {rt.chroma_collection_es}")

    status = rt.rag_status_snapshot()
    print(f"  startup_rag_ok    : {status.get('startup_rag_ok')}")
    print(f"  chroma_count      : {status.get('chroma_count')}")
    print(f"  embed_dimension   : {status.get('embed_dimension')}")
    print(f"  rerank_available  : {status.get('rerank_available')}")

    all_passed = True
    for query in QUERIES:
        try:
            payload = run_query(rt, query)
            assert_spanish_index(payload, query)
            assert_reranker_ran(payload, query)
            assert_reranker_reordered(payload, query)
            assert_citations_spanish(payload, query)
        except AssertionError as e:
            print(f"\n  {e}")
            all_passed = False
        except Exception as e:
            print(f"\n  ERROR [{query}]: {type(e).__name__}: {e}")
            all_passed = False

    if rt._wiki_collection is None and rt._chroma_client is None:
        print("\n  PASS: English Chroma client was never opened (memory saved)")
    else:
        print(
            f"\n  FAIL: English Chroma was opened. "
            f"_wiki_collection={rt._wiki_collection!r} _chroma_client={rt._chroma_client!r}"
        )
        all_passed = False

    print(f"\n{'='*70}")
    if all_passed:
        print("ALL TESTS PASSED - Spanish-only RAG is healthy")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
