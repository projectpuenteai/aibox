"""Legacy offline RAG backend used by the optional `legacy-ai` Docker profile.

This service loads the local embedding model, Chroma vector store, and local
Hugging Face language model, then exposes OpenAI-style chat routes plus admin
and debug endpoints. The default stack now prefers the llama.cpp path wired up
through `tools/ai-control/app.py`, but this file still shows the full Python
reference flow for retrieval, prompting, streaming, and warmup behavior.
"""

import math
import os
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, TextIteratorStreamer
try:
    from transformers import BitsAndBytesConfig
except Exception:
    BitsAndBytesConfig = None

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse


# -----------------------------
# Offline safety
# -----------------------------
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_ENABLE_PARALLEL_LOADING", "true")
os.environ.setdefault("HF_PARALLEL_LOADING_WORKERS", "8")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")


# -----------------------------
# Config
# -----------------------------
PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "chroma_db")
COLLECTION = os.getenv("CHROMA_COLLECTION", "simplewiki_chunks")

EMBED_MODEL = os.getenv("EMBED_MODEL", "/models/embed")
EMBED_DEVICE = os.getenv("EMBED_DEVICE", "auto")

LLM_PATH = os.getenv("LLM_PATH", "/models/llm")
LLM_FALLBACK_PATH = os.getenv("LLM_FALLBACK_PATH", "").strip()
LLM_MODEL_PROFILE = os.getenv("LLM_MODEL_PROFILE", "primary").strip().lower()
LLM_DEVICE = os.getenv("LLM_DEVICE", "auto")
LLM_CPU_DTYPE = os.getenv("LLM_CPU_DTYPE", "float16")
LLM_MAX_CPU_MEMORY = os.getenv("LLM_MAX_CPU_MEMORY", "10GiB")
LLM_OFFLOAD_DIR = os.getenv("LLM_OFFLOAD_DIR", "/tmp/llm_offload")
LLM_QUANTIZATION = os.getenv("LLM_QUANTIZATION", "4bit").strip().lower()
LLM_COMPUTE_DTYPE = os.getenv("LLM_COMPUTE_DTYPE", "float16").strip().lower()

RETRIEVAL_DEVICE = os.getenv("RETRIEVAL_DEVICE", "auto")
CPU_THREADS = int(os.getenv("CPU_THREADS", str(os.cpu_count() or 4)))
ADMIN_STATE_PATH = os.getenv("ADMIN_STATE_PATH", "/data/admin_state.json")
AI_ENABLED_DEFAULT = os.getenv("AI_ENABLED_DEFAULT", "1") == "1"
WARMUP_ON_STARTUP = os.getenv("WARMUP_ON_STARTUP", "1") == "1"
WARMUP_RETRIEVER_ON_STARTUP = os.getenv("WARMUP_RETRIEVER_ON_STARTUP", "0") == "1"
WARMUP_GENERATE_ON_STARTUP = os.getenv("WARMUP_GENERATE_ON_STARTUP", "1") == "1"
RETRIEVAL_ENABLED_DEFAULT = os.getenv("RETRIEVAL_ENABLED_DEFAULT", "0") == "1"

TOP_K = int(os.getenv("TOP_K", "5"))
RETRIEVAL_CANDIDATE_K = max(TOP_K, int(os.getenv("RETRIEVAL_CANDIDATE_K", str(max(TOP_K * 3, 12)))))
RETRIEVAL_MIN_RELEVANCE_SCORE = min(1.0, max(0.0, float(os.getenv("RETRIEVAL_MIN_RELEVANCE_SCORE", "0.14"))))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "1800"))
RETRIEVAL_MAX_RERANK_BODY_CHARS = max(400, int(os.getenv("RETRIEVAL_MAX_RERANK_BODY_CHARS", "1400")))
RETRIEVAL_DEBUG_PREVIEW_CHARS = max(80, int(os.getenv("RETRIEVAL_DEBUG_PREVIEW_CHARS", "160")))
RERANK_MODEL = os.getenv("RERANK_MODEL", "/models/rerank").strip()
RERANK_DEVICE = os.getenv("RERANK_DEVICE", "cpu").strip()
MAX_NEW_TOKENS = int(os.getenv("MAX_NEW_TOKENS", "256"))
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "1536"))
STREAM_TIMEOUT_SECONDS = float(os.getenv("STREAM_TIMEOUT_SECONDS", "120"))
RETRIEVAL_TIMEOUT_SECONDS = float(os.getenv("RETRIEVAL_TIMEOUT_SECONDS", "3.0"))
GENERATION_TIMEOUT_SECONDS = float(os.getenv("GENERATION_TIMEOUT_SECONDS", "90.0"))

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
TOP_P = float(os.getenv("TOP_P", "0.95"))
REPETITION_PENALTY = float(os.getenv("REPETITION_PENALTY", "1.05"))

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a concise, helpful assistant. Use the provided context to answer.\n"
    "If insufficient context is available, say so.\n"
    "Do not use emojis unless the user explicitly asks for emojis."
)

CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
CHROMA_HNSW_SPACE = os.getenv("CHROMA_HNSW_SPACE", "cosine")


# -----------------------------
# App
# -----------------------------
app = FastAPI(title="Offline RAG Backend", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ALLOW_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Globals
# -----------------------------
_embedder: Optional[SentenceTransformer] = None
_chroma_client = None
_collection = None
_reranker = None

_tokenizer: Optional[AutoTokenizer] = None
_model: Optional[AutoModelForCausalLM] = None
_retrieval_device: Optional[str] = None
_active_llm_path: Optional[str] = None
_llm_load_mode = "unknown"

_retriever_init_lock = threading.Lock()
_reranker_init_lock = threading.Lock()
_llm_init_lock = threading.Lock()
_admin_lock = threading.Lock()
_admin_state = {"ai_enabled": AI_ENABLED_DEFAULT}
_retrieval_health_lock = threading.Lock()
_retrieval_health: Dict[str, Any] = {
    "ok": True,
    "checked_at": None,
    "error_class": None,
    "error_message": None,
}


# -----------------------------
# Utilities
# -----------------------------
def now_ts() -> int:
    """Return a Unix timestamp for response payloads and health probes."""
    return int(time.time())


def resolve_device(name: str) -> str:
    """Resolve a requested device name to a device this machine can actually use."""
    if name == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if name == "cuda" and not torch.cuda.is_available():
        print("[warn] CUDA requested but unavailable, falling back to CPU")
        return "cpu"
    return name


def iso_now() -> str:
    """Return the current UTC time in ISO format for status metadata."""
    return datetime.now(timezone.utc).isoformat()


def torch_dtype_from_name(name: str, default: torch.dtype = torch.float16) -> torch.dtype:
    """Map string dtype names from env vars to the matching PyTorch dtype."""
    return {
        "float16": torch.float16,
        "fp16": torch.float16,
        "float32": torch.float32,
        "fp32": torch.float32,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
    }.get((name or "").strip().lower(), default)


def active_llm_path() -> str:
    """Choose the primary or fallback local model directory for the LLM."""
    if LLM_MODEL_PROFILE == "fallback" and LLM_FALLBACK_PATH and os.path.isdir(LLM_FALLBACK_PATH):
        return LLM_FALLBACK_PATH
    return LLM_PATH


def retrieval_health_snapshot() -> Dict[str, Any]:
    """Return a thread-safe copy of the last retrieval self-check result."""
    with _retrieval_health_lock:
        return dict(_retrieval_health)


def set_retrieval_health(ok: bool, exc: Optional[Exception] = None, message: Optional[str] = None) -> None:
    """Update shared retrieval health state for health and debug endpoints."""
    with _retrieval_health_lock:
        _retrieval_health["ok"] = bool(ok)
        _retrieval_health["checked_at"] = iso_now()
        if exc is not None:
            _retrieval_health["error_class"] = type(exc).__name__
            _retrieval_health["error_message"] = message or str(exc)
            return
        _retrieval_health["error_class"] = None
        _retrieval_health["error_message"] = message


def _choose_finish_reason(stop_reason: str) -> str:
    """Translate internal stop labels into OpenAI-style finish reasons."""
    if stop_reason == "max_new_tokens":
        return "length"
    return "stop"


def _is_rebuild_worthy_retrieval_error(exc: Exception) -> bool:
    """Detect errors that usually mean the persisted Chroma data is incompatible."""
    text = str(exc).lower()
    return (
        "dimensionality" in text
        or "hnsw" in text
        or "config" in text
        or "collection" in text
    )


def _compact_text(value: Any, limit: int) -> str:
    """Collapse whitespace and trim text for previews, reranking, and logs."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _informative_terms(text: str) -> List[str]:
    """Extract simple search terms for heuristic overlap-based scoring."""
    stopwords = {
        "about", "after", "also", "an", "and", "are", "been", "being", "between", "both", "but",
        "can", "could", "did", "does", "for", "from", "had", "has", "have", "her", "here", "him",
        "his", "how", "into", "its", "just", "more", "most", "much", "not", "now", "off", "onto",
        "our", "out", "over", "she", "should", "some", "than", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "those", "through", "under", "very", "was", "were",
        "what", "when", "where", "which", "while", "who", "with", "would", "your",
    }
    return [
        token
        for token in re.findall(r"[A-Za-z0-9']+", str(text or "").lower())
        if len(token) > 2 and token not in stopwords
    ]


def _sigmoid_score(value: Any) -> float:
    """Normalize raw reranker outputs into a predictable 0-1 score range."""
    try:
        score = float(value)
    except Exception:
        return 0.0
    if 0.0 <= score <= 1.0:
        return score
    clamped = max(-12.0, min(12.0, score))
    return 1.0 / (1.0 + math.exp(-clamped))


def _build_rerank_text(chunk: Dict[str, Any]) -> str:
    """Combine chunk metadata and body into the text passed to the reranker."""
    meta = chunk.get("meta") or {}
    title = _compact_text(meta.get("title", ""), 120)
    section_path = _compact_text(meta.get("section_path", ""), 140)
    body = _compact_text(chunk.get("doc", ""), RETRIEVAL_MAX_RERANK_BODY_CHARS)
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if section_path:
        parts.append(f"Section: {section_path}")
    if body:
        parts.append(body)
    return "\n".join(parts).strip()


def _chunk_relevance_features(query: str, chunk: Dict[str, Any]) -> Dict[str, float]:
    """Compute explainable heuristic signals for one retrieved chunk."""
    query_terms = set(_informative_terms(query))
    if not query_terms:
        return {
            "term_overlap": 0.0,
            "anchor_overlap": 0.0,
            "distance_score": 0.0,
            "phrase_match": 0.0,
        }
    meta = chunk.get("meta") or {}
    title = _compact_text(meta.get("title", ""), 120)
    section_path = _compact_text(meta.get("section_path", ""), 140)
    body = _compact_text(chunk.get("doc", ""), 900)
    anchor_terms = set(_informative_terms(" ".join(part for part in (title, section_path) if part)))
    body_terms = set(_informative_terms(body))
    overlap = len(query_terms & body_terms) / max(1, len(query_terms))
    anchor_overlap = len(query_terms & anchor_terms) / max(1, min(len(query_terms), 5))
    phrase_match = 1.0 if _compact_text(query, 180).lower() in _build_rerank_text(chunk).lower() else 0.0
    try:
        distance = float(chunk.get("distance")) if chunk.get("distance") is not None else 1.0
    except Exception:
        distance = 1.0
    distance_score = max(0.0, 1.0 - min(distance, 1.5) / 1.5)
    return {
        "term_overlap": round(overlap, 4),
        "anchor_overlap": round(anchor_overlap, 4),
        "distance_score": round(distance_score, 4),
        "phrase_match": round(phrase_match, 4),
    }


def _heuristic_rerank_score(query: str, chunk: Dict[str, Any]) -> Tuple[float, Dict[str, float]]:
    """Fallback chunk scorer used when the ML reranker is unavailable."""
    features = _chunk_relevance_features(query, chunk)
    score = min(
        1.0,
        (0.42 * features["term_overlap"])
        + (0.23 * features["anchor_overlap"])
        + (0.25 * features["distance_score"])
        + (0.10 * features["phrase_match"]),
    )
    return round(score, 4), features


def ensure_reranker() -> Tuple[Optional[Any], str]:
    """Load the local cross-encoder reranker or fall back to heuristic mode."""
    global _reranker
    if _reranker is not None:
        return _reranker, "cross_encoder"
    if not RERANK_MODEL or not os.path.isdir(RERANK_MODEL):
        return None, "heuristic"
    with _reranker_init_lock:
        if _reranker is not None:
            return _reranker, "cross_encoder"
        try:
            from sentence_transformers import CrossEncoder
        except Exception as exc:
            print(f"[warn] reranker unavailable, using heuristic fallback: {type(exc).__name__}: {exc}")
            return None, "heuristic"
        try:
            try:
                _reranker = CrossEncoder(RERANK_MODEL, device=RERANK_DEVICE, local_files_only=True)
            except TypeError:
                _reranker = CrossEncoder(RERANK_MODEL, device=RERANK_DEVICE)
            return _reranker, "cross_encoder"
        except Exception as exc:
            print(f"[warn] reranker load failed, using heuristic fallback: {type(exc).__name__}: {exc}")
            return None, "heuristic"
def configure_cpu_threads() -> None:
    """Tune PyTorch CPU threading to reduce oversubscription during local inference."""
    threads = max(1, CPU_THREADS)
    try:
        torch.set_num_threads(threads)
    except Exception:
        pass
    # Keep interop lower to reduce oversubscription.
    try:
        torch.set_num_interop_threads(max(1, min(4, threads)))
    except Exception:
        pass


def configure_cuda_runtime() -> None:
    """Enable safe CUDA backend optimizations when a GPU is available."""
    if not torch.cuda.is_available():
        return
    try:
        torch.backends.cuda.matmul.allow_tf32 = True
    except Exception:
        pass
    try:
        torch.backends.cudnn.allow_tf32 = True
        torch.backends.cudnn.benchmark = True
    except Exception:
        pass


def choose_retrieval_device() -> str:
    """Choose a retrieval device that avoids competing with generation when possible."""
    mode = RETRIEVAL_DEVICE.strip().lower()
    if mode == "cpu":
        return "cpu"
    if mode == "cuda":
        return "cuda" if torch.cuda.is_available() else "cpu"
    # auto: prefer CPU when LLM is on CUDA to avoid GPU contention and keep
    # generation throughput high. Retrieval remains multithreaded on CPU.
    if resolve_device(LLM_DEVICE) == "cuda":
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def read_admin_state() -> Dict[str, Any]:
    """Read the persisted admin on/off toggle from disk."""
    if not os.path.isfile(ADMIN_STATE_PATH):
        return {"ai_enabled": AI_ENABLED_DEFAULT}
    try:
        with open(ADMIN_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {
                    "ai_enabled": bool(data.get("ai_enabled", AI_ENABLED_DEFAULT))
                }
    except Exception:
        pass
    return {"ai_enabled": AI_ENABLED_DEFAULT}


def write_admin_state(state: Dict[str, Any]) -> None:
    """Write the admin toggle back to disk so restarts keep the same state."""
    dirpath = os.path.dirname(ADMIN_STATE_PATH)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    with open(ADMIN_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def load_admin_state() -> None:
    """Load admin state into memory during service startup."""
    global _admin_state
    with _admin_lock:
        _admin_state = read_admin_state()
        write_admin_state(_admin_state)


def is_ai_enabled() -> bool:
    """Return whether this backend is currently allowed to answer requests."""
    with _admin_lock:
        return bool(_admin_state.get("ai_enabled", True))


def set_ai_enabled(enabled: bool) -> Dict[str, Any]:
    """Update the admin toggle in memory and persist it immediately."""
    with _admin_lock:
        _admin_state["ai_enabled"] = bool(enabled)
        write_admin_state(_admin_state)
        return dict(_admin_state)


def _default_collection_config(space: str) -> Dict[str, Any]:
    """Return the Chroma HNSW config this backend expects for new collections."""
    return {
        "_type": "CollectionConfigurationInternal",
        "hnsw_configuration": {
            "_type": "HNSWConfigurationInternal",
            "space": space,
            "ef_construction": 100,
            "ef_search": 10,
            "num_threads": 16,
            "M": 16,
            "resize_factor": 1.2,
            "batch_size": 100,
            "sync_threshold": 1000,
        },
    }


def _normalize_collection_config_json(raw: Optional[str], space: str) -> str:
    """Normalize stored Chroma config JSON so old collections remain readable."""
    default_cfg = _default_collection_config(space)
    if not raw or not raw.strip():
        return json.dumps(default_cfg)

    try:
        cfg = json.loads(raw)
    except Exception:
        return json.dumps(default_cfg)

    if not isinstance(cfg, dict):
        return json.dumps(default_cfg)

    cfg.setdefault("_type", "CollectionConfigurationInternal")

    hnsw = cfg.get("hnsw_configuration")
    if not isinstance(hnsw, dict):
        hnsw = {}
        cfg["hnsw_configuration"] = hnsw

    hnsw.setdefault("_type", "HNSWConfigurationInternal")
    hnsw.setdefault("space", space)
    hnsw.setdefault("ef_construction", 100)
    hnsw.setdefault("ef_search", 10)
    hnsw.setdefault("num_threads", 16)
    hnsw.setdefault("M", 16)
    hnsw.setdefault("resize_factor", 1.2)
    hnsw.setdefault("batch_size", 100)
    hnsw.setdefault("sync_threshold", 1000)

    return json.dumps(cfg)


def migrate_legacy_chroma_config(persist_dir: str, space: str) -> None:
    """Patch older Chroma SQLite metadata in place before opening the collection."""
    sqlite_path = os.path.join(persist_dir, "chroma.sqlite3")
    if not os.path.isfile(sqlite_path):
        return

    conn = sqlite3.connect(sqlite_path)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, config_json_str FROM collections")
        except sqlite3.Error:
            return
        rows = cur.fetchall()
        updates = []
        for collection_id, config_json_str in rows:
            normalized = _normalize_collection_config_json(config_json_str, space)
            if normalized != config_json_str:
                updates.append((normalized, collection_id))

        if updates:
            cur.executemany(
                "UPDATE collections SET config_json_str = ? WHERE id = ?",
                updates,
            )
            conn.commit()
    finally:
        conn.close()


def _load_embedder_with_fallback(device: str) -> Tuple[SentenceTransformer, str]:
    """Load the embedder on the requested device, then fall back to CPU if needed."""
    attempts = [device]
    if device != "cpu":
        attempts.append("cpu")

    last_exc: Optional[Exception] = None
    for attempt in attempts:
        try:
            if attempt == "cpu":
                configure_cpu_threads()
            model = SentenceTransformer(
                EMBED_MODEL,
                device=attempt,
                local_files_only=True,
            )
            if attempt != device:
                print(f"[warn] retrieval embedder fallback to {attempt}")
            return model, attempt
        except Exception as exc:
            last_exc = exc
            print(f"[warn] embedder load failed on {attempt}: {type(exc).__name__}: {exc}")
    assert last_exc is not None
    raise last_exc


def run_retrieval_self_check() -> None:
    """Run a minimal query against the collection to confirm retrieval works."""
    if _embedder is None or _collection is None:
        set_retrieval_health(False, message="retriever_not_initialized")
        return
    try:
        emb = _embedder.encode(["retrieval health check"], normalize_embeddings=True).tolist()
        _collection.query(
            query_embeddings=emb,
            n_results=1,
            include=["documents", "metadatas", "distances"],
        )
        set_retrieval_health(True, message="ok")
    except Exception as exc:
        set_retrieval_health(False, exc=exc)
        print(f"[warn] retrieval self-check failed: {type(exc).__name__}: {exc}")


# -----------------------------
# Retriever Init (dimension-safe)
# -----------------------------
def ensure_retriever():
    """Initialize the embedder and Chroma collection exactly once per process."""
    global _embedder, _chroma_client, _collection, _retrieval_device

    if _collection is not None:
        return

    with _retriever_init_lock:
        if _collection is not None:
            return

        # Load the embedder before opening Chroma so we know the expected vector size.
        desired = choose_retrieval_device() if EMBED_DEVICE == "auto" else resolve_device(EMBED_DEVICE)
        _embedder, _retrieval_device = _load_embedder_with_fallback(desired)
        embed_dim = _embedder.get_sentence_embedding_dimension()

        # Older local databases may store incomplete HNSW config JSON that newer
        # Chroma versions reject, so normalize it before opening the collection.
        migrate_legacy_chroma_config(PERSIST_DIR, CHROMA_HNSW_SPACE)

        # Connect to the persisted vector store that was built by the indexing tools.
        _chroma_client = chromadb.PersistentClient(
            path=PERSIST_DIR,
            settings=Settings(anonymized_telemetry=False),
        )

        try:
            col = _chroma_client.get_collection(COLLECTION)
            # Compare the persisted collection's declared dimension with the
            # current embedder so stale indexes do not silently produce bad results.
            meta = col.metadata or {}
            existing_dim = meta.get("dimension")
            if existing_dim and existing_dim != embed_dim:
                _chroma_client.delete_collection(COLLECTION)
                col = None
        except Exception:
            col = None

        if col is None:
            col = _chroma_client.create_collection(
                name=COLLECTION,
                metadata={"dimension": embed_dim},
            )

        _collection = col
        run_retrieval_self_check()


# -----------------------------
# LLM Init
# -----------------------------
def _build_quantization_config() -> Optional[Any]:
    """Build a bitsandbytes config when CUDA 4-bit loading is enabled."""
    mode = LLM_QUANTIZATION
    if mode in ("", "none", "off", "false", "0"):
        return None
    if mode != "4bit":
        print(f"[warn] unsupported LLM_QUANTIZATION={mode}; continuing without quantization")
        return None
    if BitsAndBytesConfig is None:
        print("[warn] BitsAndBytesConfig unavailable; continuing without quantization")
        return None
    compute_dtype = torch_dtype_from_name(LLM_COMPUTE_DTYPE, torch.float16)
    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
    )


def _load_llm_on_cuda(model_path: str) -> Tuple[AutoModelForCausalLM, str]:
    """Load the LLM onto CUDA, trying 4-bit first and broader fallbacks after that."""
    quant_cfg = _build_quantization_config()
    if quant_cfg is not None:
        try:
            model = AutoModelForCausalLM.from_pretrained(
                model_path,
                local_files_only=True,
                low_cpu_mem_usage=True,
                device_map={"": 0},
                quantization_config=quant_cfg,
                torch_dtype=torch_dtype_from_name(LLM_COMPUTE_DTYPE, torch.float16),
            )
            return model, "cuda_4bit"
        except Exception as exc:
            print(f"[warn] 4bit load failed ({type(exc).__name__}); falling back to fp16")
    try:
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            local_files_only=True,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            device_map={"": 0},
        )
        return model, "cuda_fp16"
    except Exception as exc:
        print(f"[warn] full GPU load failed ({type(exc).__name__}); falling back to auto device_map")
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            local_files_only=True,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            device_map="auto",
        )
        return model, "auto_device_map"


def ensure_llm():
    """Load the tokenizer and model lazily from the active local model directory."""
    global _tokenizer, _model, _active_llm_path, _llm_load_mode

    if _model is not None:
        return

    with _llm_init_lock:
        if _model is not None:
            return

        device = resolve_device(LLM_DEVICE)
        model_path = active_llm_path()

        _tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            local_files_only=True,
            use_fast=True,
        )

        if _tokenizer.pad_token is None and _tokenizer.eos_token:
            _tokenizer.pad_token = _tokenizer.eos_token

        if device == "cuda":
            _model, _llm_load_mode = _load_llm_on_cuda(model_path)
        else:
            configure_cpu_threads()
            cpu_dtype = torch_dtype_from_name(LLM_CPU_DTYPE, torch.float16)

            _model = AutoModelForCausalLM.from_pretrained(
                model_path,
                local_files_only=True,
                torch_dtype=cpu_dtype,
                low_cpu_mem_usage=True,
                device_map="auto",
                offload_folder=LLM_OFFLOAD_DIR,
                offload_state_dict=True,
                max_memory={"cpu": LLM_MAX_CPU_MEMORY},
            )
            _llm_load_mode = f"cpu_{LLM_CPU_DTYPE.lower()}"

        _active_llm_path = model_path
        _model.eval()


def get_generation_input_device() -> torch.device:
    """Find the device prompt tensors should be moved onto before generation."""
    if _model is None:
        return torch.device("cpu")

    device_map = getattr(_model, "hf_device_map", None)
    if isinstance(device_map, dict):
        for mapped in device_map.values():
            if isinstance(mapped, int):
                return torch.device(f"cuda:{mapped}")
            if isinstance(mapped, str):
                low = mapped.lower()
                if low.startswith("cuda"):
                    return torch.device(mapped)
        if any(str(v).lower() == "cpu" for v in device_map.values()):
            return torch.device("cpu")

    try:
        for param in _model.parameters():
            if param.device.type != "meta":
                return param.device
    except Exception:
        pass
    return torch.device("cpu")


def summarize_llm_device_map(limit: int = 8) -> Dict[str, Any]:
    """Return a compact summary of where the model's layers ended up loading."""
    if _model is None:
        return {"loaded": False}
    device_map = getattr(_model, "hf_device_map", None)
    if not isinstance(device_map, dict):
        return {"loaded": True, "device_map": None}

    counts: Dict[str, int] = {}
    sample: List[Dict[str, Any]] = []
    for key, value in device_map.items():
        label = str(value)
        counts[label] = counts.get(label, 0) + 1
        if len(sample) < limit:
            sample.append({"module": str(key), "device": label})
    return {
        "loaded": True,
        "counts": counts,
        "sample": sample,
    }


def infer_stop_reason(
    generation_state: Dict[str, Any],
    output_tokens: int,
    max_tokens: int,
    generation_ms: int,
) -> str:
    """Infer why generation ended so responses can expose a stable stop reason."""
    if generation_state.get("error_class"):
        return "error"

    result = generation_state.get("result")
    eos_token_id = _tokenizer.eos_token_id if _tokenizer is not None else None
    generated_tokens = generation_state.get("generated_tokens")
    if isinstance(generated_tokens, int) and generated_tokens >= max_tokens:
        return "max_new_tokens"

    try:
        sequence = None
        if hasattr(result, "sequences") and result.sequences is not None and len(result.sequences) > 0:
            sequence = result.sequences[0]
        elif torch.is_tensor(result) and result.ndim >= 2 and result.shape[0] > 0:
            sequence = result[0]

        if sequence is not None and eos_token_id is not None:
            last_id = int(sequence[-1].item()) if hasattr(sequence[-1], "item") else int(sequence[-1])
            if isinstance(eos_token_id, list):
                if last_id in eos_token_id:
                    return "eos"
            elif last_id == int(eos_token_id):
                return "eos"
    except Exception:
        pass

    timeout_ms = int(max(0.0, GENERATION_TIMEOUT_SECONDS) * 1000)
    if timeout_ms > 0 and generation_ms >= max(0, timeout_ms - 350):
        return "max_time"
    if output_tokens >= max_tokens:
        return "max_new_tokens"
    return "eos"


# -----------------------------
# Retrieval
# -----------------------------
def rerank_chunks(query: str, chunks: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str, Optional[str], int]:
    """Rerank retrieved chunks with the ML reranker or the heuristic fallback."""
    if not chunks:
        return [], "none", None, 0

    reranker, mode = ensure_reranker()
    ranked = [{"meta": dict(chunk.get("meta") or {}), **{k: v for k, v in chunk.items() if k != "meta"}} for chunk in chunks]
    start_t = time.perf_counter()
    scores: Optional[List[float]] = None
    rerank_error: Optional[str] = None

    if reranker is not None and mode == "cross_encoder":
        try:
            pairs = [[query, _build_rerank_text(chunk)] for chunk in ranked]
            raw_scores = reranker.predict(pairs)
            scores = [_sigmoid_score(score) for score in raw_scores]
        except Exception as exc:
            rerank_error = f"{type(exc).__name__}: {exc}"
            mode = "heuristic"

    if scores is None:
        scores = []
        for chunk in ranked:
            score, features = _heuristic_rerank_score(query, chunk)
            chunk.update(features)
            scores.append(score)

    for chunk, score in zip(ranked, scores):
        if "term_overlap" not in chunk:
            chunk.update(_chunk_relevance_features(query, chunk))
        chunk["relevance_score"] = round(float(score), 4)

    ranked.sort(
        key=lambda chunk: (
            -float(chunk.get("relevance_score") or 0.0),
            float(chunk.get("distance")) if chunk.get("distance") is not None else 999999.0,
        )
    )
    return ranked, mode, rerank_error, int((time.perf_counter() - start_t) * 1000)


def select_chunks(ranked: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pick the final chunks that fit the answer prompt and diversity rules."""
    if not ranked:
        return []

    selected = []
    seen = set()
    for index, chunk in enumerate(ranked):
        meta = chunk.get("meta") or {}
        title = _compact_text(meta.get("title", ""), 120).lower()
        key = (meta.get("page_id"), title)
        if key in seen:
            continue
        score = float(chunk.get("relevance_score") or 0.0)
        term_overlap = float(chunk.get("term_overlap") or 0.0)
        anchor_overlap = float(chunk.get("anchor_overlap") or 0.0)
        distance_score = float(chunk.get("distance_score") or 0.0)
        if index > 0 and score < RETRIEVAL_MIN_RELEVANCE_SCORE and term_overlap <= 0.0 and anchor_overlap <= 0.0 and distance_score < 0.35:
            continue
        selected.append(chunk)
        seen.add(key)
        if len(selected) >= TOP_K:
            break

    if not selected and ranked:
        selected.append(ranked[0])
    return selected


def retrieve(query: str) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str], Dict[str, Any]]:
    """Run vector search plus reranking and return chunks with debug metadata."""
    ensure_retriever()
    health = retrieval_health_snapshot()
    if not health.get("ok"):
        return [], health.get("error_class") or "RetrievalUnhealthy", health.get("error_message"), {
            "candidate_count": 0,
            "rerank_mode": "none",
            "rerank_ms": 0,
            "rerank_error": health.get("error_message"),
        }

    emb = _embedder.encode([query], normalize_embeddings=True).tolist()

    try:
        result = _collection.query(
            query_embeddings=emb,
            n_results=RETRIEVAL_CANDIDATE_K,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        print(f"[warn] retrieval disabled for this request: {type(exc).__name__}: {exc}")
        if _is_rebuild_worthy_retrieval_error(exc):
            set_retrieval_health(False, exc=exc)
        return [], type(exc).__name__, str(exc), {
            "candidate_count": 0,
            "rerank_mode": "none",
            "rerank_ms": 0,
            "rerank_error": str(exc),
        }

    chunks = []
    for doc, meta, dist in zip(
        result.get("documents", [[]])[0],
        result.get("metadatas", [[]])[0],
        result.get("distances", [[]])[0],
    ):
        chunks.append({
            "doc": doc,
            "meta": meta or {},
            "distance": float(dist) if dist is not None else None,
        })

    ranked, rerank_mode, rerank_error, rerank_ms = rerank_chunks(query, chunks)
    selected = select_chunks(ranked)
    set_retrieval_health(True, message="ok")
    return selected, None, None, {
        "candidate_count": len(chunks),
        "rerank_mode": rerank_mode,
        "rerank_ms": rerank_ms,
        "rerank_error": rerank_error,
    }


def retrieve_with_timeout(query: str):
    """Run retrieval in a worker thread so slow vector search can time out cleanly."""
    start = time.perf_counter()
    if RETRIEVAL_TIMEOUT_SECONDS <= 0:
        chunks, error_class, error_message, meta = retrieve(query)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return chunks, False, elapsed_ms, error_class, error_message, meta

    result: Dict[str, Any] = {}
    done = threading.Event()

    def _run():
        """Execute retrieval off-thread and capture structured errors for the caller."""
        try:
            chunks, error_class, error_message, meta = retrieve(query)
            result["chunks"] = chunks
            result["error_class"] = error_class
            result["error_message"] = error_message
            result["meta"] = meta
        except Exception as exc:
            result["error_class"] = type(exc).__name__
            result["error_message"] = str(exc)
            result["meta"] = {
                "candidate_count": 0,
                "rerank_mode": "none",
                "rerank_ms": 0,
                "rerank_error": str(exc),
            }
        finally:
            done.set()

    worker = threading.Thread(target=_run, daemon=True, name="retrieve-query")
    worker.start()
    finished = done.wait(RETRIEVAL_TIMEOUT_SECONDS)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    if not finished:
        print(f"[warn] retrieval timed out after {elapsed_ms}ms; continuing without context")
        return [], True, elapsed_ms, "TimeoutError", "retrieval_timeout", {
            "candidate_count": 0,
            "rerank_mode": "none",
            "rerank_ms": 0,
            "rerank_error": "retrieval_timeout",
        }
    return (
        result.get("chunks", []),
        False,
        elapsed_ms,
        result.get("error_class"),
        result.get("error_message"),
        result.get("meta", {}),
    )


def build_context(chunks: List[Dict[str, Any]]) -> str:
    """Format selected chunks into the context block inserted into the prompt."""
    parts = []
    total = 0

    for i, ch in enumerate(chunks, 1):
        meta = ch.get("meta") or {}
        title = _compact_text(meta.get("title", ""), 140) or "Untitled"
        section_path = _compact_text(meta.get("section_path", ""), 160)
        details = []
        if section_path:
            details.append(f"Section: {section_path}")
        if ch.get("relevance_score") is not None:
            details.append(f"Relevance: {float(ch['relevance_score']):.4f}")
        if ch.get("distance") is not None:
            details.append(f"Distance: {float(ch['distance']):.4f}")
        block = f"[{i}] {title}"
        if details:
            block += "\n" + " | ".join(details)
        block += f"\n{ch['doc']}"
        if total + len(block) > MAX_CONTEXT_CHARS:
            break
        parts.append(block)
        total += len(block)

    return "\n\n---\n\n".join(parts)


# -----------------------------
# Generation
# -----------------------------
def generate_stream(
    prompt: str,
    temperature: float,
    max_tokens: int,
    generation_state: Optional[Dict[str, Any]] = None,
):
    """Stream model tokens for one prompt while tracking generation metadata."""
    ensure_llm()

    inputs = _tokenizer(
        prompt,
        return_tensors="pt",
        truncation=True,
        max_length=MAX_INPUT_TOKENS,
    )

    input_device = get_generation_input_device()
    inputs = {k: v.to(input_device) for k, v in inputs.items()}
    input_length = int(inputs["input_ids"].shape[-1]) if "input_ids" in inputs else 0
    do_sample = temperature > 0.0

    generate_kwargs = {
        "do_sample": do_sample,
        "repetition_penalty": REPETITION_PENALTY,
        "max_new_tokens": max_tokens,
        "max_time": GENERATION_TIMEOUT_SECONDS,
        "use_cache": True,
        "pad_token_id": _tokenizer.pad_token_id,
        "eos_token_id": _tokenizer.eos_token_id,
    }
    if do_sample:
        generate_kwargs["temperature"] = max(temperature, 1e-5)
        generate_kwargs["top_p"] = TOP_P

    streamer = TextIteratorStreamer(
        _tokenizer,
        skip_prompt=True,
        skip_special_tokens=True,
        timeout=STREAM_TIMEOUT_SECONDS,
    )
    generate_kwargs["streamer"] = streamer

    err: Dict[str, Any] = {}
    result: Dict[str, Any] = {}

    def _run_generate():
        """Run the blocking `model.generate` call on a worker thread."""
        try:
            with torch.inference_mode():
                try:
                    result["output"] = _model.generate(
                        **inputs,
                        **generate_kwargs,
                    )
                except RuntimeError as exc:
                    # Some partially offloaded device maps still reject CUDA input
                    # tensors, so retry from CPU instead of failing the request.
                    if "device meta" not in str(exc).lower():
                        raise
                    cpu_inputs = {k: v.to("cpu") for k, v in inputs.items()}
                    result["output"] = _model.generate(
                        **cpu_inputs,
                        **generate_kwargs,
                    )
        except Exception as exc:
            err["error"] = exc

    worker = threading.Thread(target=_run_generate, daemon=True, name="llm-generate")
    worker.start()

    for text in streamer:
        yield text

    worker.join()
    if "error" in err:
        if generation_state is not None:
            generation_state["error_class"] = type(err["error"]).__name__
            generation_state["error_message"] = str(err["error"])
            generation_state["input_length"] = input_length
        raise err["error"]
    if generation_state is not None:
        generation_state["result"] = result.get("output")
        generation_state["input_length"] = input_length
        try:
            output = result.get("output")
            sequence = None
            if hasattr(output, "sequences") and output.sequences is not None and len(output.sequences) > 0:
                sequence = output.sequences[0]
            elif torch.is_tensor(output) and output.ndim >= 2 and output.shape[0] > 0:
                sequence = output[0]
            if sequence is not None:
                sequence_len = int(sequence.shape[-1]) if hasattr(sequence, "shape") else len(sequence)
                generation_state["generated_tokens"] = max(0, sequence_len - input_length)
        except Exception:
            pass


def generate(prompt: str, temperature: float, max_tokens: int) -> str:
    """Collect the streamed output into one final string response."""
    answer_parts: List[str] = []
    for piece in generate_stream(prompt, temperature, max_tokens):
        answer_parts.append(piece)
    return "".join(answer_parts).strip()


def generate_with_meta(prompt: str, temperature: float, max_tokens: int) -> Tuple[str, Dict[str, Any]]:
    """Generate a full answer and calculate timing/token metadata for it."""
    generation_state: Dict[str, Any] = {}
    generation_start = time.perf_counter()
    answer_parts: List[str] = []
    for piece in generate_stream(prompt, temperature, max_tokens, generation_state=generation_state):
        answer_parts.append(piece)
    answer = "".join(answer_parts).strip()
    generation_ms = int((time.perf_counter() - generation_start) * 1000)
    try:
        output_tokens = len(_tokenizer.encode(answer, add_special_tokens=False))
    except Exception:
        output_tokens = 0
    stop_reason = infer_stop_reason(generation_state, output_tokens, max_tokens, generation_ms)
    return answer, {
        "generation_ms": generation_ms,
        "output_tokens": output_tokens,
        "stop_reason": stop_reason,
    }


def warmup_llm_generation() -> None:
    """Run a tiny generation request so the first real user request is less cold."""
    try:
        prompt = make_prompt("hello", "")
        for _ in generate_stream(prompt, 0.0, 16):
            pass
    except Exception as exc:
        print(f"[warn] llm generation warmup failed: {type(exc).__name__}: {exc}")


def warmup_retriever_safe() -> None:
    """Warm the retrieval path without letting startup crash on a retrieval error."""
    try:
        ensure_retriever()
    except Exception as exc:
        set_retrieval_health(False, exc=exc)
        print(f"[warn] retriever warmup failed: {type(exc).__name__}: {exc}")


def warmup_llm_safe() -> None:
    """Warm the LLM load path without crashing startup if model load fails."""
    try:
        ensure_llm()
    except Exception as exc:
        print(f"[warn] llm warmup failed: {type(exc).__name__}: {exc}")


def summarize_chunks(chunks: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    """Prepare a short debug-friendly summary of the top retrieved chunks."""
    items = []
    for ch in chunks[:limit]:
        meta = ch.get("meta") or {}
        items.append({
            "title": meta.get("title"),
            "section_title": meta.get("section_title"),
            "section_path": meta.get("section_path"),
            "page_id": meta.get("page_id"),
            "distance": ch.get("distance"),
            "relevance_score": ch.get("relevance_score"),
            "preview": (ch.get("doc") or "")[:RETRIEVAL_DEBUG_PREVIEW_CHARS],
        })
    return items


def make_prompt(user_msg: str, context: str) -> str:
    """Assemble the final chat prompt given the user message and retrieval context."""
    ensure_llm()
    context_block = context.strip() if context else ""
    if context_block:
        user_content = (
            f"{user_msg}\n\n"
            "Context snippets:\n"
            f"{context_block}\n\n"
            "Answer only using helpful facts from the context when relevant."
        )
    else:
        user_content = (
            f"{user_msg}\n\n"
            "No retrieval context was found. Answer briefly and clearly."
        )
    chat_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    if hasattr(_tokenizer, "apply_chat_template"):
        return _tokenizer.apply_chat_template(
            chat_messages,
            tokenize=False,
            add_generation_prompt=True,
        )
    return f"SYSTEM:\n{SYSTEM_PROMPT}\n\nUSER:\n{user_content}\n\nASSISTANT:\n"


def run_chat_pipeline(
    user_msg: str,
    temperature: float,
    max_tokens: int,
    retrieval_enabled: bool = RETRIEVAL_ENABLED_DEFAULT,
) -> Dict[str, Any]:
    """Run retrieval, prompt building, and generation for non-streaming chat calls."""
    if retrieval_enabled:
        (
            chunks,
            timed_out,
            retrieval_ms,
            retrieval_error_class,
            retrieval_error_message,
            retrieval_meta,
        ) = retrieve_with_timeout(user_msg)
    else:
        chunks, timed_out, retrieval_ms, retrieval_error_class, retrieval_error_message, retrieval_meta = [], False, 0, None, None, {
            "candidate_count": 0,
            "rerank_mode": "none",
            "rerank_ms": 0,
            "rerank_error": None,
        }
    context = build_context(chunks)
    prompt = make_prompt(user_msg, context)
    answer, gen_meta = generate_with_meta(prompt, temperature, max_tokens)
    return {
        "chunks": chunks,
        "retrieval_timed_out": timed_out,
        "retrieval_ms": retrieval_ms,
        "retrieval_error_class": retrieval_error_class,
        "retrieval_error_message": retrieval_error_message,
        "retrieval_candidate_count": retrieval_meta.get("candidate_count", 0),
        "retrieval_rerank_mode": retrieval_meta.get("rerank_mode"),
        "retrieval_rerank_ms": retrieval_meta.get("rerank_ms", 0),
        "retrieval_rerank_error": retrieval_meta.get("rerank_error"),
        "answer": answer,
        "stop_reason": gen_meta["stop_reason"],
        "generation_ms": gen_meta["generation_ms"],
        "output_tokens": gen_meta["output_tokens"],
    }


def sse_event(payload: Dict[str, Any]) -> str:
    """Encode a payload as one server-sent events message."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stream_chat_events(
    user_msg: str,
    temperature: float,
    max_tokens: int,
    debug: bool,
    retrieval_enabled: bool = RETRIEVAL_ENABLED_DEFAULT,
):
    """Yield debug and token events for the streaming chat endpoint."""
    start = time.perf_counter()
    last_stage_ms = 0

    def t_ms() -> int:
        """Measure elapsed time from the start of this request in milliseconds."""
        return int((time.perf_counter() - start) * 1000)

    def emit(stage: str, **payload: Any) -> str:
        """Emit one structured SSE event and track time since the previous stage."""
        nonlocal last_stage_ms
        current_ms = t_ms()
        evt = {
            "stage": stage,
            "t_ms": current_ms,
            "delta_ms": max(0, current_ms - last_stage_ms),
        }
        evt.update(payload)
        last_stage_ms = current_ms
        return sse_event(evt)

    try:
        if debug:
            yield emit("prompt received", prompt=user_msg)

        if not is_ai_enabled():
            raise RuntimeError("AI is disabled by admin.")

        if retrieval_enabled:
            if debug:
                yield emit("retrieval running", device=_retrieval_device or choose_retrieval_device())

            (
                chunks,
                retrieval_timed_out,
                retrieval_ms,
                retrieval_error_class,
                retrieval_error_message,
                retrieval_meta,
            ) = retrieve_with_timeout(user_msg)
            if debug:
                yield emit(
                    "retrieval results",
                    count=len(chunks),
                    candidate_count=retrieval_meta.get("candidate_count", 0),
                    rerank_mode=retrieval_meta.get("rerank_mode"),
                    rerank_ms=retrieval_meta.get("rerank_ms", 0),
                    rerank_error=retrieval_meta.get("rerank_error"),
                    items=summarize_chunks(chunks),
                    timed_out=retrieval_timed_out,
                    retrieval_ms=retrieval_ms,
                    error_class=retrieval_error_class,
                )
        else:
            chunks, retrieval_timed_out, retrieval_ms, retrieval_error_class, retrieval_error_message, retrieval_meta = [], False, 0, None, None, {
                "candidate_count": 0,
                "rerank_mode": "none",
                "rerank_ms": 0,
                "rerank_error": None,
            }
            if debug:
                yield emit("retrieval skipped", count=0, retrieval_ms=0)

        prompt_build_start = time.perf_counter()
        context = build_context(chunks)
        prompt = make_prompt(user_msg, context)
        prompt_build_ms = int((time.perf_counter() - prompt_build_start) * 1000)

        if debug:
            yield emit(
                "prompt built",
                prompt_build_ms=prompt_build_ms,
                context_chars=len(context),
                prompt_chars=len(prompt),
                retrieval_enabled=retrieval_enabled,
            )

        if debug:
            yield emit(
                "AI processing",
                device=resolve_device(LLM_DEVICE),
                generation_max_tokens=max_tokens,
                temperature=temperature,
            )

        generation_start = time.perf_counter()
        first_token_at_ms: Optional[int] = None
        stream_chunk_count = 0
        answer_parts: List[str] = []
        generation_state: Dict[str, Any] = {}
        for token in generate_stream(prompt, temperature, max_tokens, generation_state=generation_state):
            if not token:
                continue
            if first_token_at_ms is None:
                first_token_at_ms = t_ms()
                if debug:
                    yield emit("first token", ttft_ms=first_token_at_ms)
            answer_parts.append(token)
            stream_chunk_count += 1
            yield sse_event({
                "stage": "token",
                "token": token,
                "t_ms": t_ms(),
            })
        answer = "".join(answer_parts).strip()
        generation_ms = int((time.perf_counter() - generation_start) * 1000)
        total_ms = t_ms()
        output_chars = len(answer)

        output_tokens = 0
        try:
            output_tokens = len(_tokenizer.encode(answer, add_special_tokens=False))
        except Exception:
            output_tokens = 0
        tokens_per_sec = round((output_tokens * 1000.0) / max(generation_ms, 1), 2) if output_tokens > 0 else 0.0
        first_token_delay_ms = first_token_at_ms if first_token_at_ms is not None else total_ms
        stop_reason = infer_stop_reason(generation_state, output_tokens, max_tokens, generation_ms)

        timing_metrics = {
            "retrieval_enabled": retrieval_enabled,
            "retrieval_ms": retrieval_ms,
            "retrieval_timed_out": retrieval_timed_out,
            "retrieval_error_class": retrieval_error_class,
            "retrieval_error_message": retrieval_error_message,
            "retrieval_candidate_count": retrieval_meta.get("candidate_count", 0),
            "retrieval_rerank_mode": retrieval_meta.get("rerank_mode"),
            "retrieval_rerank_ms": retrieval_meta.get("rerank_ms", 0),
            "retrieval_rerank_error": retrieval_meta.get("rerank_error"),
            "prompt_build_ms": prompt_build_ms,
            "ttft_ms": first_token_delay_ms,
            "generation_ms": generation_ms,
            "total_ms": total_ms,
            "output_chars": output_chars,
            "output_tokens": output_tokens,
            "tokens_per_sec": tokens_per_sec,
            "stream_chunk_count": stream_chunk_count,
            "stop_reason": stop_reason,
        }

        if debug:
            yield emit("timing summary", **timing_metrics)

        yield emit("final output", answer=answer, **timing_metrics)
    except Exception as exc:
        current_ms = t_ms()
        yield sse_event({
            "stage": "error messages",
            "error": f"{type(exc).__name__}: {exc}",
            "stop_reason": "error",
            "t_ms": current_ms,
            "delta_ms": max(0, current_ms - last_stage_ms),
        })


# -----------------------------
# Routes
# -----------------------------
@app.on_event("startup")
def startup_init():
    """Load persistent state and optionally warm key runtime paths on startup."""
    load_admin_state()
    configure_cpu_threads()
    configure_cuda_runtime()
    if WARMUP_ON_STARTUP:
        if WARMUP_RETRIEVER_ON_STARTUP:
            threading.Thread(target=warmup_retriever_safe, daemon=True, name="warmup-retriever").start()
        threading.Thread(target=warmup_llm_safe, daemon=True, name="warmup-llm").start()
        if WARMUP_GENERATE_ON_STARTUP:
            threading.Thread(target=warmup_llm_generation, daemon=True, name="warmup-generate").start()


@app.get("/health")
def health():
    """Expose a lightweight health payload for the legacy backend container."""
    return {
        "ok": True,
        "ts": now_ts(),
        "ai_enabled": is_ai_enabled(),
        "llm_device": resolve_device(LLM_DEVICE),
        "retrieval_device": _retrieval_device or choose_retrieval_device(),
        "retrieval_health": retrieval_health_snapshot(),
        "retrieval_candidate_k": RETRIEVAL_CANDIDATE_K,
        "retrieval_min_relevance_score": RETRIEVAL_MIN_RELEVANCE_SCORE,
    }


@app.get("/v1/debug/runtime")
def runtime_debug():
    """Expose a detailed runtime snapshot for troubleshooting local model loading."""
    cuda_available = torch.cuda.is_available()
    payload = {
        "ok": True,
        "ts": now_ts(),
        "ai_enabled": is_ai_enabled(),
        "llm": {
            "active_path": _active_llm_path or active_llm_path(),
            "primary_path": LLM_PATH,
            "fallback_path": LLM_FALLBACK_PATH or None,
            "fallback_available": bool(LLM_FALLBACK_PATH and os.path.isdir(LLM_FALLBACK_PATH)),
            "model_profile": LLM_MODEL_PROFILE,
            "load_mode": _llm_load_mode,
            "requested_device": LLM_DEVICE,
            "resolved_device": resolve_device(LLM_DEVICE),
            "quantization": LLM_QUANTIZATION,
            "compute_dtype": LLM_COMPUTE_DTYPE,
            "device_map": summarize_llm_device_map(),
        },
        "retrieval": {
            "embed_model": EMBED_MODEL,
            "requested_device": EMBED_DEVICE,
            "resolved_device": _retrieval_device or choose_retrieval_device(),
            "health": retrieval_health_snapshot(),
            "chroma_persist_dir": PERSIST_DIR,
            "collection": COLLECTION,
            "candidate_k": RETRIEVAL_CANDIDATE_K,
            "top_k": TOP_K,
            "min_relevance_score": RETRIEVAL_MIN_RELEVANCE_SCORE,
            "rerank_model": RERANK_MODEL or None,
            "rerank_device": RERANK_DEVICE,
            "rerank_available": bool(RERANK_MODEL and os.path.isdir(RERANK_MODEL)),
        },
        "limits": {
            "max_new_tokens": MAX_NEW_TOKENS,
            "max_input_tokens": MAX_INPUT_TOKENS,
            "max_context_chars": MAX_CONTEXT_CHARS,
            "retrieval_timeout_seconds": RETRIEVAL_TIMEOUT_SECONDS,
            "stream_timeout_seconds": STREAM_TIMEOUT_SECONDS,
            "generation_timeout_seconds": GENERATION_TIMEOUT_SECONDS,
        },
        "cuda": {
            "available": cuda_available,
            "device_count": torch.cuda.device_count() if cuda_available else 0,
            "device_name": torch.cuda.get_device_name(0) if cuda_available and torch.cuda.device_count() > 0 else None,
        },
    }
    return payload


@app.get("/v1/admin/ai-enabled")
def get_ai_enabled():
    """Return whether admins currently allow this backend to answer requests."""
    return {"enabled": is_ai_enabled()}


@app.post("/v1/admin/ai-enabled")
async def post_ai_enabled(req: Request):
    """Update the persisted admin toggle via a small JSON request body."""
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if "enabled" not in payload:
        raise HTTPException(400, "Missing enabled")

    state = set_ai_enabled(bool(payload.get("enabled")))
    return {"enabled": bool(state["ai_enabled"])}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    """Handle OpenAI-style chat completion requests in stream or JSON mode."""
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    messages = payload.get("messages")
    if not messages:
        raise HTTPException(400, "Missing messages")

    temperature = float(payload.get("temperature", TEMPERATURE))
    max_tokens = min(int(payload.get("max_tokens", MAX_NEW_TOKENS)), MAX_NEW_TOKENS)
    stream = bool(payload.get("stream", False))
    debug = bool(payload.get("debug", False))
    retrieval_enabled = bool(payload.get("retrieval_enabled", RETRIEVAL_ENABLED_DEFAULT))

    # The backend answers only from the latest user turn, so earlier messages
    # are used only to find that final user prompt in the request payload.
    user_msg = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        ""
    )

    if not user_msg:
        raise HTTPException(400, "No user message found")

    if stream or debug:
        return StreamingResponse(
            stream_chat_events(user_msg, temperature, max_tokens, debug, retrieval_enabled),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if not is_ai_enabled():
        raise HTTPException(status_code=503, detail="AI is currently disabled by admin.")

    try:
        result = run_chat_pipeline(user_msg, temperature, max_tokens, retrieval_enabled)
        answer = result["answer"]
        stop_reason = result.get("stop_reason", "eos")

        return JSONResponse({
            "id": f"chatcmpl_{now_ts()}",
            "object": "chat.completion",
            "created": now_ts(),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": _choose_finish_reason(stop_reason),
            }],
            "usage": {
                "completion_tokens": result.get("output_tokens", 0),
            },
            "stop_reason": stop_reason,
            "timing": {
                "generation_ms": result.get("generation_ms"),
                "retrieval_ms": result.get("retrieval_ms"),
            },
        })
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")






