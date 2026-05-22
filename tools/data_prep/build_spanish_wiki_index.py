"""End-to-end Spanish Wikipedia subset builder for the local RAG stack.

NOTE (2026-05-20): the LIVE Spanish index at backend-data/chroma_db_es/
(rehydrated into the chroma_db_es_native named volume) uses the collection
name `simplewiki_chunks` — the same name the ai-control runtime reads via
CHROMA_COLLECTION_ES in stack/docker-compose.yaml. This script's
COLLECTION_NAME_ES below is the historical name used during prototyping; if
you re-run this end-to-end builder you must either:

  (a) set COLLECTION_NAME_ES = "simplewiki_chunks" before running, OR
  (b) update CHROMA_COLLECTION_ES in stack/docker-compose.yaml to match
      whatever this script produces.

Mismatching the two will surface as a `collection not found` error in
ai-control logs and a permanently-503 /health endpoint.

Runs four stages, each resumable:
  1. Build a title allowlist (vital articles + top pageviews).
  2. Download article plain text via the MediaWiki API.
  3. Chunk pages (reuses tools.data_prep.chunk_pages_for_rag).
  4. Embed chunks into Chroma (reuses tools.index.build_chroma_index).

Designed to run on any machine with Python + network. Output files:
  backend-data/allowlist_es.txt
  backend-data/pages_es.jsonl
  backend-data/chunks_es.jsonl
  backend-data/chroma_db/  (collection: see COLLECTION_NAME_ES below)

Usage:
  python -m tools.data_prep.build_spanish_wiki_index
  python -m tools.data_prep.build_spanish_wiki_index --stage fetch
  python -m tools.data_prep.build_spanish_wiki_index --stage download --max-articles 20000
  python -m tools.data_prep.build_spanish_wiki_index --stage chunk
  python -m tools.data_prep.build_spanish_wiki_index --stage embed --device cuda

Each stage is skipped if its output already exists unless --force is passed.
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote

import requests

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import BACKEND_DATA_DIR, PERSIST_DIR
from tools.data_prep import chunk_pages_for_rag
from tools.index import build_chroma_index as indexer

USER_AGENT = "PuenteAI-indexer/1.0 (https://github.com/; contact via repo)"
API = "https://es.wikipedia.org/w/api.php"
REST = "https://wikimedia.org/api/rest_v1"

ALLOWLIST_FILE = BACKEND_DATA_DIR / "allowlist_es.txt"
PAGES_FILE_ES = BACKEND_DATA_DIR / "pages_es.jsonl"
CHUNKS_FILE_ES = BACKEND_DATA_DIR / "chunks_es.jsonl"
DOWNLOAD_STATE = BACKEND_DATA_DIR / "pages_es.state.json"
COLLECTION_NAME_ES = os.getenv("CHROMA_COLLECTION_ES", "eswiki_chunks")

VITAL_ROOTS = [
    "Wikipedia:Artículos vitales/Nivel 3",
    "Wikipedia:Artículos vitales/Nivel 4",
]

SKIP_TITLE_PREFIXES = (
    "Anexo:", "Wikipedia:", "Especial:", "Ayuda:", "Archivo:",
    "Plantilla:", "Categoría:", "Portal:", "Usuario:",
)

MIN_PAGE_CHARS = 1500


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    return s


def _is_junk_title(title: str) -> bool:
    t = title.strip()
    if not t or t.startswith(SKIP_TITLE_PREFIXES):
        return True
    if "(desambiguación)" in t.lower():
        return True
    if t.isdigit() or (len(t) == 4 and t[:2] in ("19", "20") and t.isdigit()):
        return True
    return False


# ── Stage 1: allowlist ───────────────────────────────────────────────────

def fetch_links_on_page(session: requests.Session, page_title: str) -> list[str]:
    """Pull all article-namespace links from a page via the MediaWiki API."""
    titles: list[str] = []
    params = {
        "action": "query", "format": "json", "prop": "links",
        "titles": page_title, "pllimit": "max", "plnamespace": 0,
    }
    while True:
        r = session.get(API, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for page in data.get("query", {}).get("pages", {}).values():
            for link in page.get("links", []) or []:
                t = link.get("title", "").strip()
                if t and not _is_junk_title(t):
                    titles.append(t)
        cont = data.get("continue")
        if not cont:
            break
        params.update(cont)
        time.sleep(0.1)
    return titles


def fetch_subpages(session: requests.Session, root: str) -> list[str]:
    """List subpages of a Wikipedia: page (for the Nivel 5 multi-page list)."""
    titles = []
    params = {
        "action": "query", "format": "json", "list": "allpages",
        "apprefix": root.split(":", 1)[1] + "/", "apnamespace": 4,
        "aplimit": "max",
    }
    while True:
        r = session.get(API, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for p in data.get("query", {}).get("allpages", []) or []:
            titles.append(p["title"])
        cont = data.get("continue")
        if not cont:
            break
        params.update(cont)
    return titles


def fetch_vital_titles(session: requests.Session) -> set[str]:
    """Union of all article links from vital-article list pages."""
    print("[allowlist] fetching vital articles...")
    all_pages = list(VITAL_ROOTS)
    for root in VITAL_ROOTS:
        try:
            all_pages.extend(fetch_subpages(session, root))
        except Exception as exc:
            print(f"[allowlist] subpage fetch failed for {root}: {exc}")

    titles: set[str] = set()
    for p in sorted(set(all_pages)):
        try:
            found = fetch_links_on_page(session, p)
            titles.update(found)
            print(f"[allowlist]   {p}: +{len(found)} (total {len(titles):,})")
        except Exception as exc:
            print(f"[allowlist]   {p}: skipped ({exc})")
    return titles


def fetch_top_pageviews(session: requests.Session, months: int = 12) -> set[str]:
    """Top-1000 eswiki pageviews per month for the last `months` months."""
    print(f"[allowlist] fetching top pageviews for last {months} months...")
    titles: set[str] = set()
    today = date.today().replace(day=1)
    for i in range(1, months + 1):
        d = today - timedelta(days=30 * i)
        url = f"{REST}/metrics/pageviews/top/es.wikipedia/all-access/{d.year}/{d.month:02d}/all-days"
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            for art in data.get("items", [{}])[0].get("articles", []):
                t = unquote(art["article"]).replace("_", " ")
                if not _is_junk_title(t):
                    titles.add(t)
        except Exception as exc:
            print(f"[allowlist]   {d.year}-{d.month:02d} skipped ({exc})")
    print(f"[allowlist] pageviews yielded {len(titles):,} unique titles")
    return titles


def stage_build_allowlist(force: bool) -> Path:
    if ALLOWLIST_FILE.exists() and not force:
        count = sum(1 for _ in ALLOWLIST_FILE.open(encoding="utf-8"))
        print(f"[allowlist] using existing {ALLOWLIST_FILE.name} ({count:,} titles)")
        return ALLOWLIST_FILE

    ALLOWLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    session = _session()
    titles: set[str] = set()
    titles.update(fetch_vital_titles(session))
    titles.update(fetch_top_pageviews(session))
    clean = sorted(t for t in titles if not _is_junk_title(t))
    ALLOWLIST_FILE.write_text("\n".join(clean), encoding="utf-8")
    print(f"[allowlist] wrote {len(clean):,} titles -> {ALLOWLIST_FILE}")
    return ALLOWLIST_FILE


# ── Stage 2: download articles ───────────────────────────────────────────

def _load_state() -> dict:
    if DOWNLOAD_STATE.exists():
        try:
            return json.loads(DOWNLOAD_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": []}


def _save_state(state: dict) -> None:
    DOWNLOAD_STATE.write_text(json.dumps(state), encoding="utf-8")


def fetch_extracts_batch(session: requests.Session, titles: list[str]) -> list[dict]:
    """Get plain-text extract for up to 20 titles in one API call."""
    params = {
        "action": "query", "format": "json", "prop": "extracts",
        "explaintext": 1, "exlimit": "max", "redirects": 1,
        "titles": "|".join(titles),
    }
    r = session.get(API, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    out = []
    for page in data.get("query", {}).get("pages", {}).values():
        if "missing" in page or page.get("ns", 0) != 0:
            continue
        text = (page.get("extract") or "").strip()
        if len(text) < MIN_PAGE_CHARS:
            continue
        out.append({
            "page_id": int(page.get("pageid", 0)),
            "title": page.get("title", ""),
            "text": text,
        })
    return out


def stage_download(
    allowlist: Path,
    max_articles: int,
    workers: int,
    force: bool,
) -> Path:
    if PAGES_FILE_ES.exists() and not force:
        count = sum(1 for _ in PAGES_FILE_ES.open(encoding="utf-8"))
        print(f"[download] using existing {PAGES_FILE_ES.name} ({count:,} pages)")
        return PAGES_FILE_ES

    titles = [t.strip() for t in allowlist.read_text(encoding="utf-8").splitlines() if t.strip()]
    if max_articles > 0:
        titles = titles[:max_articles]
    print(f"[download] {len(titles):,} titles to download")

    state = _load_state()
    done_set = set(state.get("done", []))
    pending = [t for t in titles if t not in done_set]
    print(f"[download] {len(pending):,} pending after resume")

    PAGES_FILE_ES.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if done_set and PAGES_FILE_ES.exists() else "w"
    if mode == "w":
        DOWNLOAD_STATE.unlink(missing_ok=True)
        state = {"done": []}
        done_set = set()

    BATCH = 20
    batches = [pending[i:i + BATCH] for i in range(0, len(pending), BATCH)]

    written = 0
    t0 = time.time()
    session_pool = [_session() for _ in range(max(1, workers))]
    lock_f = PAGES_FILE_ES.open(mode, encoding="utf-8")
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(fetch_extracts_batch, session_pool[i % workers], batch): batch
                for i, batch in enumerate(batches)
            }
            for idx, fut in enumerate(as_completed(futures), start=1):
                batch = futures[fut]
                try:
                    pages = fut.result()
                except Exception as exc:
                    print(f"[download] batch failed ({exc}); will retry later")
                    continue
                for rec in pages:
                    lock_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
                done_set.update(batch)
                state["done"] = list(done_set)
                if idx % 50 == 0:
                    lock_f.flush()
                    _save_state(state)
                    rate = written / max(1e-9, time.time() - t0)
                    print(f"[download] batches {idx:,}/{len(batches):,} | "
                          f"pages written: {written:,} | {rate:,.1f} pages/sec")
    finally:
        lock_f.close()
        _save_state(state)

    print(f"[download] done: {written:,} pages written -> {PAGES_FILE_ES}")
    return PAGES_FILE_ES


# ── Stage 3: chunk ───────────────────────────────────────────────────────

def stage_chunk(pages_file: Path, workers: int, force: bool) -> Path:
    if CHUNKS_FILE_ES.exists() and not force:
        count = sum(1 for _ in CHUNKS_FILE_ES.open(encoding="utf-8"))
        print(f"[chunk] using existing {CHUNKS_FILE_ES.name} ({count:,} chunks)")
        return CHUNKS_FILE_ES

    chunk_pages_for_rag.main(
        input_file=str(pages_file),
        output_file=str(CHUNKS_FILE_ES),
        chunk_words_limit=chunk_pages_for_rag.DEFAULT_CHUNK_WORDS,
        chunk_chars_limit=chunk_pages_for_rag.DEFAULT_CHUNK_CHARS,
        overlap_words=chunk_pages_for_rag.DEFAULT_OVERLAP_WORDS,
        min_chunk_words=chunk_pages_for_rag.DEFAULT_MIN_CHUNK_WORDS,
        workers=workers,
        map_chunksize=chunk_pages_for_rag.DEFAULT_MAP_CHUNKSIZE,
    )
    return CHUNKS_FILE_ES


# ── Stage 4: embed + load into Chroma ────────────────────────────────────

def stage_embed(
    chunks_file: Path,
    device: str,
    collection: str,
    embed_batch: int,
    chroma_batch: int,
) -> None:
    import torch
    if embed_batch <= 0:
        if device.lower() == "cuda" and torch.cuda.is_available():
            vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            embed_batch = 16 if vram_gb < 10 else 64 if vram_gb < 16 else 128
            print(f"[embed] GPU VRAM: {vram_gb:.1f} GB -> embed_batch={embed_batch}")
        else:
            embed_batch = indexer.EMBED_BATCH

    indexer.main(
        chunks_file=str(chunks_file),
        persist_dir=PERSIST_DIR,
        collection_name=collection,
        device=device,
        workers=max(1, (os.cpu_count() or 1)),
        cpu_threads=max(1, (os.cpu_count() or 1)),
        chroma_batch=chroma_batch,
        embed_batch=embed_batch,
    )


# ── Orchestration ────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="End-to-end Spanish Wikipedia subset indexer.")
    p.add_argument("--stage", choices=["all", "fetch", "download", "chunk", "embed"], default="all")
    p.add_argument("--max-articles", type=int, default=0,
                   help="Cap number of titles to download (0 = all in allowlist).")
    p.add_argument("--download-workers", type=int, default=4)
    p.add_argument("--chunk-workers", type=int, default=chunk_pages_for_rag.DEFAULT_WORKERS)
    p.add_argument("--device", default="cuda",
                   help="'cuda' or 'cpu'. Falls back to cpu if CUDA unavailable.")
    p.add_argument("--collection", default=COLLECTION_NAME_ES,
                   help=f"Chroma collection name (default {COLLECTION_NAME_ES}).")
    p.add_argument("--embed-batch", type=int, default=0, help="0 = auto by VRAM.")
    p.add_argument("--chroma-batch", type=int, default=indexer.CHROMA_BATCH)
    p.add_argument("--force", action="store_true", help="Re-run stages even if outputs exist.")
    args = p.parse_args()

    if args.device.lower() == "cuda":
        try:
            import torch
            if not torch.cuda.is_available():
                print("[init] CUDA not available, falling back to CPU")
                args.device = "cpu"
        except Exception:
            args.device = "cpu"

    BACKEND_DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.stage in ("all", "fetch"):
        stage_build_allowlist(args.force)
    if args.stage in ("all", "download"):
        stage_download(ALLOWLIST_FILE, args.max_articles, args.download_workers, args.force)
    if args.stage in ("all", "chunk"):
        stage_chunk(PAGES_FILE_ES, args.chunk_workers, args.force)
    if args.stage in ("all", "embed"):
        stage_embed(CHUNKS_FILE_ES, args.device, args.collection, args.embed_batch, args.chroma_batch)

    print("[done] Spanish wiki pipeline complete.")
    print(f"[done] Chroma persist dir: {PERSIST_DIR}")
    print(f"[done] Collection: {args.collection}")


if __name__ == "__main__":
    main()
