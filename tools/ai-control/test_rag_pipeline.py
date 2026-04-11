#!/usr/bin/env python3
"""Verification script for the RAG pipeline.

Requires the ChromaDB index and embedding/reranker models to be available
(run inside the ai-control Docker container).
"""

import json
import sys

from app_storage import StorageRuntime


def run_query(rt: StorageRuntime, query: str) -> dict:
    """Run a single retrieval query and return the payload."""
    print(f"\n{'='*60}")
    print(f"Query: {query}")
    print(f"{'='*60}")
    payload = rt.prepare_wiki_context(query)
    context = str(payload.get("context", "") or "")
    selected = payload.get("selected_chunks") or []
    print(f"  context length  : {len(context)} chars")
    print(f"  chunks selected : {len(selected)}")
    print(f"  rerank_enabled  : {payload.get('rerank_enabled')}")
    print(f"  rerank_ms       : {payload.get('rerank_ms')}")
    print(f"  rerank_error    : {payload.get('rerank_error')}")
    print(f"  tokens estimate : {payload.get('context_tokens_estimate')}")
    print(f"  truncation count: {payload.get('chunk_truncation_count')}")
    print(f"  citations       : {len(payload.get('citations') or [])}")
    for i, chunk in enumerate(selected, 1):
        status = chunk.get("inclusion_decision", "?")
        title = chunk.get("title") or "?"
        score = chunk.get("relevance_score", 0)
        reason = chunk.get("dropped_reason")
        print(f"    [{i}] {status:8s} score={score:.4f} title={title!r}" +
              (f" dropped_reason={reason}" if reason else ""))
    print(f"\n--- Injected context (first 2000 chars) ---")
    print(context[:2000])
    if len(context) > 2000:
        print(f"  ... ({len(context) - 2000} more chars)")
    return payload


def assert_retrieval_works(payload: dict, query: str):
    """Assert basic expectations on a retrieval result."""
    context = str(payload.get("context", "") or "")
    selected = payload.get("selected_chunks") or []
    citations = payload.get("citations") or []
    top_k = max(1, int(payload.get("chunks_after_budget_trim") or payload.get("chunks_after_rerank") or len(selected) or 1))

    assert context, f"FAIL [{query}]: context is empty (retrieval_used would be False)"
    assert 1 <= len(selected) <= top_k, f"FAIL [{query}]: expected 1..{top_k} selected chunks, got {len(selected)}"
    included = [c for c in selected if c.get("inclusion_decision") == "included"]
    assert len(included) == len(selected), f"FAIL [{query}]: expected all selected chunks to be included"
    selected_titles = [str(chunk.get("page_title") or chunk.get("title") or "").strip() for chunk in included if str(chunk.get("page_title") or chunk.get("title") or "").strip()]
    unique_titles = []
    seen = set()
    for title in selected_titles:
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_titles.append(title)
    assert len(citations) == len(unique_titles), (
        f"FAIL [{query}]: expected {len(unique_titles)} distinct article citations, got {len(citations)}"
    )
    citation_titles = [str(citation.get("page_title") or "").strip() for citation in citations]
    assert citation_titles == unique_titles, (
        f"FAIL [{query}]: citation titles {citation_titles!r} did not match selected article order {unique_titles!r}"
    )
    for citation in citations:
        assert citation.get("page_title"), f"FAIL [{query}]: citation missing page_title"
        assert citation.get("wiki_url"), f"FAIL [{query}]: citation missing wiki_url"
        assert "/wiki/viewer#wiki/" in str(citation.get("wiki_url")), f"FAIL [{query}]: citation url has wrong format"
    for chunk in selected:
        assert not chunk.get("duplicate_removed"), (
            f"FAIL [{query}]: selected chunk incorrectly marked duplicate_removed: {chunk.get('title')}"
        )
    print(f"  PASS: {len(selected)} chunks included, {len(citations)} distinct article citations returned, context={len(context)} chars")


def assert_context_stats(payload: dict, query: str):
    """Assert context_stats dict is present with expected keys from build_wiki_context_payload."""
    stats_keys_in_payload = {"context_tokens_estimate", "chunk_truncation_count"}
    for key in stats_keys_in_payload:
        assert key in payload, f"FAIL [{query}]: missing key {key!r} in payload (from context_stats)"
    assert isinstance(payload.get("context_tokens_estimate"), int), (
        f"FAIL [{query}]: context_tokens_estimate should be int"
    )
    print(f"  PASS: context_stats keys present in payload")


def assert_citation_encoding(rt: StorageRuntime):
    sample_title = "Caf\u00e9 con leche / a\u00f1o"
    citation = rt._build_wiki_citation(sample_title, "http://localhost")
    assert citation is not None, "FAIL [citation]: helper returned None"
    assert citation["page_title"] == sample_title, "FAIL [citation]: page_title mismatch"
    assert citation["wiki_url"] == "http://localhost/wiki/viewer#wiki/Caf%C3%A9%20con%20leche%20%2F%20a%C3%B1o", (
        f"FAIL [citation]: unexpected url {citation['wiki_url']!r}"
    )
    print("  PASS: citation helper encodes spaces, slash, and accents")


def assert_citation_dedup(rt: StorageRuntime):
    chunks = [
        {"title": "Photosynthesis"},
        {"title": "Photosynthesis"},
        {"title": "Plant"},
    ]
    citations = rt._citations_from_chunks(chunks, "http://localhost")
    titles = [citation["page_title"] for citation in citations]
    assert titles == ["Photosynthesis", "Plant"], (
        f"FAIL [citation_dedup]: unexpected citation titles {titles!r}"
    )
    print("  PASS: citation list dedupes repeated article titles while preserving first-seen order")


def main():
    print("Initializing StorageRuntime...")
    rt = StorageRuntime(llama_base_url="http://localhost:2020")

    rag_status = rt.rag_status_snapshot()
    print(f"  chroma_count     : {rag_status.get('chroma_count')}")
    print(f"  startup_rag_ok   : {rag_status.get('startup_rag_ok')}")
    print(f"  rerank_available : {rag_status.get('rerank_available')}")
    print(f"  embed_dimension  : {rag_status.get('embed_dimension')}")

    queries = [
        "What is photosynthesis?",
        "Who was Abraham Lincoln?",
    ]

    all_passed = True
    for query in queries:
        try:
            payload = run_query(rt, query)
            assert_retrieval_works(payload, query)
            assert_context_stats(payload, query)
        except AssertionError as e:
            print(f"\n  {e}")
            all_passed = False
        except Exception as e:
            print(f"\n  ERROR [{query}]: {type(e).__name__}: {e}")
            all_passed = False

    try:
        assert_citation_encoding(rt)
    except AssertionError as e:
        print(f"\n  {e}")
        all_passed = False
    except Exception as e:
        print(f"\n  ERROR [citation]: {type(e).__name__}: {e}")
        all_passed = False

    try:
        assert_citation_dedup(rt)
    except AssertionError as e:
        print(f"\n  {e}")
        all_passed = False
    except Exception as e:
        print(f"\n  ERROR [citation_dedup]: {type(e).__name__}: {e}")
        all_passed = False

    print(f"\n{'='*60}")
    if all_passed:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
