"""End-to-end bilingual chat regression test.

Hits the live `aibox-ai-control` container through Caddy at
http://localhost/ai/api/v1/app/* — exercises the full pipeline:
auth, language-aware retrieval (ES collection vs EN collection), reranking,
llama generation, and language-correct response.

Run after the stack is up and both ChromaDB indexes are warm:
    docker compose -f aibox/stack/docker-compose.yaml up -d
    python -m tools.tests.test_bilingual_chat

Prints a per-query verdict and an aggregate PASS/FAIL summary. The two test
accounts (`test_bilingual_en`, `test_bilingual_es`) are created on first run
with predictable passwords; subsequent runs reuse them.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "http://localhost/ai/api"
MODEL = "qwen2.5-7b-instruct-q4_0"
TEST_USER_EN = "test_bilingual_en"
TEST_USER_ES = "test_bilingual_es"
TEST_PASS = "BiTest!9pass"

EN_QUERIES = [
    "What is photosynthesis?",
    "Explain the water cycle",
    "Who was Abraham Lincoln?",
    "How does an electric motor work?",
    "What causes earthquakes?",
]

ES_QUERIES = [
    "¿Qué es la Revolución Mexicana?",
    "Explícame la fotosíntesis paso a paso",
    "¿Quién fue Simón Bolívar?",
    "¿Cuál es la diferencia entre un volcán activo y uno inactivo?",
    "¿Cómo funciona el aparato digestivo humano?",
]

# Word sets for crude language heuristic on response text.
ES_TOKENS = {
    "el", "la", "los", "las", "un", "una", "y", "de", "del", "que", "en", "es",
    "se", "por", "con", "para", "como", "su", "sus", "lo", "más", "pero", "no",
    "este", "esta", "son", "fue", "ser", "tiene", "puede", "hace", "hacer",
    "según", "también", "cuando", "donde",
}
EN_TOKENS = {
    "the", "and", "of", "to", "in", "is", "it", "that", "for", "as", "with",
    "on", "be", "by", "this", "from", "or", "are", "was", "an", "which",
    "but", "not", "have", "has", "can", "will", "they", "their", "its",
}


def _ensure_user(username: str, language: str) -> requests.Session:
    """Sign up (idempotent) + login. Returns an authenticated session."""
    s = requests.Session()
    r = s.post(
        f"{BASE}/v1/app/auth/signup",
        json={"username": username, "password": TEST_PASS, "preferred_language": language},
        timeout=10,
    )
    if r.status_code not in (200, 409):
        raise RuntimeError(f"signup({username}) failed: {r.status_code} {r.text}")
    r = s.post(
        f"{BASE}/v1/app/auth/login",
        json={"username": username, "password": TEST_PASS},
        timeout=10,
    )
    if r.status_code != 200:
        raise RuntimeError(f"login({username}) failed: {r.status_code} {r.text}")
    # If the account existed with a different preferred_language, fix it.
    try:
        s.put(f"{BASE}/v1/app/preferences", json={"preferred_language": language}, timeout=5)
    except Exception:
        pass
    return s


def _send_chat(s: requests.Session, query: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """POST a non-streaming chat completion and return (text, raw_payload)."""
    r = s.post(
        f"{BASE}/v1/app/chat/completions",
        json={
            "model": MODEL,
            "stream": False,
            "messages": [{"role": "user", "content": query}],
        },
        timeout=180,
    )
    if r.status_code != 200:
        return None, {"status": r.status_code, "body": r.text[:500]}
    try:
        data = r.json()
    except Exception:
        return r.text, None
    text = ""
    try:
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            text = str(msg.get("content") or "")
    except Exception:
        text = ""
    return text, data


def _detect_language(text: str) -> str:
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    if not words:
        return "unknown"
    es_hits = sum(1 for w in words if w in ES_TOKENS)
    en_hits = sum(1 for w in words if w in EN_TOKENS)
    if es_hits >= en_hits + 2:
        return "es"
    if en_hits >= es_hits + 2:
        return "en"
    return "ambiguous"


def _looks_cited(text: str) -> bool:
    # The system prompt instructs the model to write phrases like
    # "According to the article on [Topic]..." or "Según el artículo sobre [Tema]...".
    # We accept any of those plus generic Wikipedia mentions.
    patterns = [
        r"according to (?:the )?article",
        r"según (?:el )?artículo",
        r"de acuerdo (?:con|al) (?:el )?artículo",
        r"wikipedia",
    ]
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def _run_lang(label: str, username: str, lang: str, queries: List[str]) -> Tuple[int, int, List[Dict[str, Any]]]:
    print(f"\n=== {label} ({username}, preferred_language={lang}) ===")
    s = _ensure_user(username, lang)
    passed = 0
    rows: List[Dict[str, Any]] = []
    for q in queries:
        t0 = time.perf_counter()
        text, raw = _send_chat(s, q)
        elapsed = time.perf_counter() - t0
        ok = bool(text) and len(text.strip()) > 40
        detected = _detect_language(text or "")
        cited = _looks_cited(text or "")
        lang_ok = (detected == lang) or (detected == "ambiguous" and ok)
        verdict = "PASS" if (ok and lang_ok) else "FAIL"
        if verdict == "PASS":
            passed += 1
        snippet = (text or "").strip().replace("\n", " ")[:120]
        print(f"  [{verdict}] {elapsed:5.1f}s  lang={detected} cited={cited}  q={q[:60]!r}")
        print(f"           reply: {snippet!r}")
        rows.append({
            "query": q,
            "elapsed_s": round(elapsed, 2),
            "ok": ok,
            "detected_language": detected,
            "cited": cited,
            "verdict": verdict,
            "reply_excerpt": snippet,
        })
    return passed, len(queries), rows


def main() -> int:
    print("Bilingual chat regression — hitting", BASE)
    en_pass, en_total, en_rows = _run_lang("ENGLISH", TEST_USER_EN, "en", EN_QUERIES)
    es_pass, es_total, es_rows = _run_lang("SPANISH", TEST_USER_ES, "es", ES_QUERIES)

    print("\n=== SUMMARY ===")
    print(f"  English: {en_pass}/{en_total} passed")
    print(f"  Spanish: {es_pass}/{es_total} passed")
    out = {
        "english": {"passed": en_pass, "total": en_total, "rows": en_rows},
        "spanish": {"passed": es_pass, "total": es_total, "rows": es_rows},
    }
    out_path = "C:/AIBox/aibox/backend-data/bilingual_chat_results.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"  Detail: {out_path}")
    except Exception as exc:
        print(f"  (could not write detail file: {exc})")
    overall_ok = (en_pass + es_pass) == (en_total + es_total)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
