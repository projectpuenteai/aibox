#!/usr/bin/env python3
"""Comprehensive RAG pipeline test suite for Project Puente AI.

Supports two modes:
  --mode direct   Run inside the ai-control container (imports app_storage directly)
  --mode api      Run externally via HTTP against the ai-control API (retrieval-only)

Usage inside container:
  docker exec aibox-ai-control python /app/tests/test_rag_comprehensive.py --mode direct

Usage from host (requires running ai-control):
  python -m tools.tests.test_rag_comprehensive --mode api --base-url http://localhost:8081
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Test-case loader
# ---------------------------------------------------------------------------

CASES_FILE = Path(__file__).parent / "test_cases.json"


def load_test_cases(path: Optional[str] = None) -> List[Dict[str, Any]]:
    p = Path(path) if path else CASES_FILE
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"]


# ---------------------------------------------------------------------------
# Direct mode: import StorageRuntime and call prepare_wiki_context
# ---------------------------------------------------------------------------

def run_direct(cases: List[Dict[str, Any]], verbose: bool = False) -> List[Dict[str, Any]]:
    """Run test cases by importing StorageRuntime directly (in-container)."""
    # Add ai-control source to path so we can import app_storage
    ai_control_dir = Path(__file__).resolve().parents[1] / "ai-control"
    if str(ai_control_dir) not in sys.path:
        sys.path.insert(0, str(ai_control_dir))

    from app_storage import StorageRuntime  # type: ignore

    print("Initializing StorageRuntime...")
    rt = StorageRuntime(llama_base_url="http://localhost:2020")

    rag_status = rt.rag_status_snapshot()
    print(f"  chroma_count     : {rag_status.get('chroma_count')}")
    print(f"  startup_rag_ok   : {rag_status.get('startup_rag_ok')}")
    print(f"  rerank_available : {rag_status.get('rerank_available')}")
    print(f"  embed_dimension  : {rag_status.get('embed_dimension')}")
    print()

    results = []
    for case in cases:
        result = run_single_direct(rt, case, verbose)
        results.append(result)

    return results


def run_single_direct(rt: Any, case: Dict[str, Any], verbose: bool) -> Dict[str, Any]:
    """Run a single test case in direct mode."""
    case_id = case["id"]
    query = case["query"]
    expect_skip = case.get("expect_skip", False)
    expect_retrieval = case.get("expect_retrieval", True)
    expect_min_chunks = case.get("expect_min_chunks", 0)
    expect_title = case.get("expect_title_contains")

    print(f"  [{case_id}] {query!r}")

    result: Dict[str, Any] = {
        "id": case_id,
        "query": query,
        "category": case.get("category"),
        "language": case.get("language"),
        "passed": True,
        "failures": [],
    }

    # Check skip detection
    skip_detected = rt._should_skip_retrieval(query)
    result["skip_detected"] = skip_detected

    if expect_skip:
        if not skip_detected:
            result["passed"] = False
            result["failures"].append(f"Expected skip but retrieval was NOT skipped")
        else:
            result["retrieval_ms"] = 0
            result["chunk_count"] = 0
            result["top_title"] = None
            result["top_score"] = None
            _print_result(result, verbose)
            return result

    if skip_detected and not expect_skip:
        result["passed"] = False
        result["failures"].append(f"Query was skipped but should NOT have been")
        result["retrieval_ms"] = 0
        result["chunk_count"] = 0
        result["top_title"] = None
        result["top_score"] = None
        _print_result(result, verbose)
        return result

    # Run retrieval
    start = time.perf_counter()
    try:
        payload = rt.prepare_wiki_context(query)
    except Exception as exc:
        result["passed"] = False
        result["failures"].append(f"Exception: {type(exc).__name__}: {exc}")
        result["retrieval_ms"] = int((time.perf_counter() - start) * 1000)
        result["chunk_count"] = 0
        result["top_title"] = None
        result["top_score"] = None
        _print_result(result, verbose)
        return result

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    result["retrieval_ms"] = elapsed_ms

    context = str(payload.get("context", "") or "")
    selected = payload.get("selected_chunks") or []
    included = [c for c in selected if c.get("inclusion_decision") == "included"]

    result["chunk_count"] = len(included)
    result["context_chars"] = len(context)
    result["context_tokens_est"] = payload.get("context_tokens_estimate", 0)
    result["rerank_enabled"] = payload.get("rerank_enabled", False)
    result["rerank_ms"] = payload.get("rerank_ms", 0)
    result["candidate_count"] = payload.get("candidate_count", 0)

    if included:
        top = included[0]
        result["top_title"] = top.get("title", "")
        result["top_score"] = top.get("relevance_score", 0)
        result["all_titles"] = [c.get("title", "") for c in included]
    else:
        result["top_title"] = None
        result["top_score"] = None
        result["all_titles"] = []

    # Validate expectations
    if expect_retrieval and not context:
        result["passed"] = False
        result["failures"].append("Expected retrieval but context is empty")

    if expect_min_chunks > 0 and len(included) < expect_min_chunks:
        result["passed"] = False
        result["failures"].append(
            f"Expected >= {expect_min_chunks} chunks, got {len(included)}"
        )

    if expect_title and included:
        title_match = any(
            expect_title.lower() in str(c.get("title", "")).lower()
            for c in included
        )
        if not title_match:
            result["passed"] = False
            result["failures"].append(
                f"Expected title containing {expect_title!r}, got {[c.get('title') for c in included]}"
            )

    # Check injection sanitization for injection test cases
    if case.get("category") == "injection" and context:
        injection_phrases = ["ignore previous", "reveal the system", "system prompt"]
        for phrase in injection_phrases:
            if phrase.lower() in context.lower():
                result["passed"] = False
                result["failures"].append(
                    f"Injection phrase {phrase!r} found in context (should be sanitized)"
                )

    if verbose and context:
        result["context_preview"] = context[:500]

    _print_result(result, verbose)
    return result


# ---------------------------------------------------------------------------
# API mode: hit ai-control HTTP endpoints
# ---------------------------------------------------------------------------

def run_api(cases: List[Dict[str, Any]], base_url: str, verbose: bool = False) -> List[Dict[str, Any]]:
    """Run test cases via the ai-control HTTP API (retrieval health check only)."""
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed. Install with: pip install httpx")
        sys.exit(1)

    print(f"Connecting to {base_url}...")
    client = httpx.Client(base_url=base_url, timeout=30)

    # Health check
    resp = client.get("/health")
    if resp.status_code != 200:
        print(f"ERROR: Health check failed: {resp.status_code}")
        sys.exit(1)
    health = resp.json()
    print(f"  status: {health.get('status')}")

    # RAG status
    resp = client.get("/v1/app/admin/rag-status")
    if resp.status_code == 200:
        rag = resp.json()
        print(f"  chroma_count: {rag.get('chroma_count')}")
        print(f"  embed_dimension: {rag.get('embed_dimension')}")
        print(f"  rerank_available: {rag.get('rerank_available')}")
    print()

    print("NOTE: API mode only validates health/status. For full retrieval tests,")
    print("      use --mode direct inside the Docker container.")
    print()

    return []


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_result(result: Dict[str, Any], verbose: bool) -> None:
    status = "PASS" if result["passed"] else "FAIL"
    ms = result.get("retrieval_ms", 0)
    chunks = result.get("chunk_count", 0)
    top = result.get("top_title") or "-"
    score = result.get("top_score")
    score_str = f"{score:.4f}" if score is not None else "-"

    line = f"    {status}  {ms:>6}ms  chunks={chunks}  top={top!r} score={score_str}"
    if result.get("skip_detected"):
        line = f"    {status}  (skipped)"

    print(line)
    for fail in result.get("failures", []):
        print(f"      FAIL: {fail}")

    if verbose and result.get("context_preview"):
        print(f"      context: {result['context_preview'][:200]}...")


def print_summary(results: List[Dict[str, Any]]) -> None:
    if not results:
        return

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    print(f"\n{'='*70}")
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")
    print(f"{'='*70}")

    # Per-category breakdown
    categories: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        cat = r.get("category", "unknown")
        categories.setdefault(cat, []).append(r)

    for cat, cat_results in sorted(categories.items()):
        cat_passed = sum(1 for r in cat_results if r["passed"])
        cat_total = len(cat_results)
        print(f"  {cat}: {cat_passed}/{cat_total}")

    # Latency stats for retrieval tests
    retrieval_results = [r for r in results if not r.get("skip_detected") and r.get("retrieval_ms")]
    if retrieval_results:
        latencies = [r["retrieval_ms"] for r in retrieval_results]
        avg = sum(latencies) / len(latencies)
        mn = min(latencies)
        mx = max(latencies)
        print(f"\n  Retrieval latency: avg={avg:.0f}ms  min={mn}ms  max={mx}ms")

    # Failed tests detail
    if failed:
        print(f"\nFailed tests:")
        for r in results:
            if not r["passed"]:
                print(f"  [{r['id']}] {r['query']!r}")
                for f in r.get("failures", []):
                    print(f"    - {f}")

    print()


def save_results(results: List[Dict[str, Any]], output_dir: Optional[str] = None) -> str:
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    out_file = out_dir / f"rag_test_{ts}.json"

    output = {
        "timestamp": ts,
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Results saved to: {out_file}")
    return str(out_file)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Comprehensive RAG pipeline test suite")
    parser.add_argument("--mode", choices=["direct", "api"], default="direct",
                        help="Test mode: direct (in-container) or api (external HTTP)")
    parser.add_argument("--base-url", default="http://localhost:8081",
                        help="ai-control base URL for API mode")
    parser.add_argument("--cases", default=None,
                        help="Path to test_cases.json (default: auto-detect)")
    parser.add_argument("--category", default=None,
                        help="Run only tests in this category")
    parser.add_argument("--id", default=None,
                        help="Run only a specific test by ID")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show context previews and extra detail")
    parser.add_argument("--save", action="store_true",
                        help="Save results to JSON file")
    parser.add_argument("--output-dir", default=None,
                        help="Directory for result files")
    args = parser.parse_args()

    cases = load_test_cases(args.cases)

    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
    if args.id:
        cases = [c for c in cases if c["id"] == args.id]

    if not cases:
        print("No test cases matched filters.")
        sys.exit(1)

    print(f"Running {len(cases)} test cases in {args.mode} mode\n")

    if args.mode == "direct":
        results = run_direct(cases, verbose=args.verbose)
    else:
        results = run_api(cases, args.base_url, verbose=args.verbose)

    if results:
        print_summary(results)

        if args.save:
            save_results(results, args.output_dir)

        failed = sum(1 for r in results if not r["passed"])
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
