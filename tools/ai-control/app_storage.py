"""Storage, auth, retrieval, and chat runtime mounted into the AI control service.

This file is the main application layer for the active stack. tools/ai-control/app.py
creates the FastAPI app and then calls mount_app_storage() from this file so the
same service can expose authentication, chat history, documents, admin tools,
persistence, and wiki-assisted chat generation.
"""
import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request
from urllib.request import Request as UrllibRequest

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from storage_migrations import ensure_column as migration_ensure_column
from storage_migrations import run_migrations
from storage_migrations import table_columns as migration_table_columns

logger = logging.getLogger("aibox.ai_control.storage")


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Normalize loose on/off input from env vars or JSON into a real boolean."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return bool(default)


def normalize_language_preference(value: Any, default: str = "en") -> str:
    """Clamp user-facing language choices to the supported portal locales."""
    text = str(value or "").strip().lower()
    return text if text in ("en", "es") else default


def normalize_theme_preference(value: Any, default: str = "light") -> str:
    """Clamp theme preference to the supported light/dark pair."""
    text = str(value or "").strip().lower()
    return text if text in ("light", "dark") else default


class LoginPayload(BaseModel):
    username: str
    password: str


class SignupPayload(BaseModel):
    username: str
    password: str
    role: Optional[str] = "user"
    preferred_language: Optional[str] = "en"
    preferred_theme: Optional[str] = "light"


class PreferencePayload(BaseModel):
    preferred_language: Optional[str] = None
    preferred_theme: Optional[str] = None


class ResetPasswordPayload(BaseModel):
    password: str


class RolePayload(BaseModel):
    role: str
    reason: Optional[str] = None
    confirm: Optional[str] = None


class UnlockPayload(BaseModel):
    reason: Optional[str] = None


class LockPayload(BaseModel):
    reason: str
    duration_minutes: Optional[int] = 30
    permanent: Optional[bool] = False


class CreateChatPayload(BaseModel):
    title: Optional[str] = "New Chat"


class UpdateChatPayload(BaseModel):
    title: Optional[str] = None
    is_saved: Optional[bool] = None
    folder_id: Optional[str] = None


class CreateChatFolderPayload(BaseModel):
    name: str


class UpdateChatFolderPayload(BaseModel):
    name: str


class CreateDocPayload(BaseModel):
    title: Optional[str] = "Untitled Document"
    type: Optional[str] = "markdown"
    content_markdown: Optional[str] = ""


class UpdateDocPayload(BaseModel):
    title: Optional[str] = None
    type: Optional[str] = None
    content_markdown: Optional[str] = None


class StarDocPayload(BaseModel):
    starred: bool


class PasteAbusePayload(BaseModel):
    doc_id: Optional[str] = None
    abuse_type: str  # "paste_cooldown" | "paste_duplicate" | "paste_too_long"
    detail: Optional[str] = None


class AnalyticsEventPayload(BaseModel):
    event_name: str
    surface: str
    metadata: Optional[Dict[str, Any]] = None


class CleanupPayload(BaseModel):
    dry_run: Optional[bool] = True
    reason: Optional[str] = "admin"
    required_bytes: Optional[int] = 0


class StorageRuntime:
    """Own the app's storage, auth, retrieval, and chat helper logic.

    The mounted FastAPI routes call into this runtime for almost all real work:
    file persistence under /data, SQLite access, session cookies, wiki retrieval,
    llama.cpp request shaping, abuse controls, and cleanup tasks.
    """

    def __init__(self, llama_base_url: str):
        """Load environment-driven settings and initialize shared runtime state."""
        self.llama_base_url = llama_base_url
        self.app_env = (os.getenv("APP_ENV", "production") or "production").strip().lower()
        self.data_root = Path(os.getenv("APP_DATA_ROOT", "/data"))
        self.db_path = Path(os.getenv("APP_DB_PATH", str(self.data_root / "db" / "app.db")))
        self.users_root = self.data_root / "users"
        self.tmp_root = self.data_root / "tmp"
        self.chroma_persist_dir = Path(os.getenv("CHROMA_PERSIST_DIR", str(self.data_root / "chroma_db")))
        self.chroma_collection = os.getenv("CHROMA_COLLECTION", "simplewiki_chunks").strip() or "simplewiki_chunks"
        self.chroma_persist_dir_es = Path(os.getenv("CHROMA_PERSIST_DIR_ES", str(self.data_root / "chroma_db_es")))
        self.chroma_collection_es = os.getenv("CHROMA_COLLECTION_ES", "simplewiki_chunks").strip() or "simplewiki_chunks"
        # Citation link verification — probe the Kiwix container for each cited
        # article before exposing the link to the user, so dead links (article
        # in chroma but pruned from the ZIM) are suppressed instead of 404ing.
        self.wiki_link_verify_enabled = _coerce_bool(os.getenv("WIKI_LINK_VERIFY_ENABLED", "1"), default=True)
        self.kiwix_base_en = (os.getenv("KIWIX_BASE_EN", "http://kiwix-en:8080") or "http://kiwix-en:8080").rstrip("/")
        self.kiwix_base_es = (os.getenv("KIWIX_BASE_ES", "http://kiwix-es:8080") or "http://kiwix-es:8080").rstrip("/")
        self.kiwix_book_en = (os.getenv("KIWIX_BOOK_EN", "wikipedia_en_all_mini_2026-03") or "wikipedia_en_all_mini_2026-03").strip()
        self.kiwix_book_es = (os.getenv("KIWIX_BOOK_ES", "wikipedia_es_all_maxi_2026-02") or "wikipedia_es_all_maxi_2026-02").strip()
        self.kiwix_probe_timeout = max(0.5, float(os.getenv("WIKI_LINK_VERIFY_TIMEOUT", "2.0")))
        self.kiwix_probe_cache_max = max(100, int(os.getenv("WIKI_LINK_VERIFY_CACHE_MAX", "5000")))
        self._wiki_exists_cache: "OrderedDict[Tuple[str, str], bool]" = OrderedDict()
        self._wiki_exists_lock = threading.Lock()
        self.load_es_index_at_startup = _coerce_bool(os.getenv("LOAD_ES_INDEX_AT_STARTUP", "0"), default=False)
        self.warmup_en_at_startup = _coerce_bool(os.getenv("WARMUP_EN_AT_STARTUP", "1"), default=True)
        # Spanish-only mode: never open the English Chroma client and route every
        # retrieve_wiki_chunks() call to the Spanish collection regardless of the
        # caller's user_language. bge-m3 is multilingual, so English queries still
        # retrieve relevant Spanish content.
        self.rag_spanish_only = _coerce_bool(os.getenv("RAG_SPANISH_ONLY", "0"), default=False)
        self.embed_model = os.getenv("EMBED_MODEL", "/models/embed").strip() or "/models/embed"
        self.retrieval_device = os.getenv("RETRIEVAL_DEVICE", "cpu").strip() or "cpu"
        self.retrieval_enabled_default = _coerce_bool(os.getenv("RETRIEVAL_ENABLED_DEFAULT", "1"), default=True)
        self.retrieval_top_k = max(1, int(os.getenv("RETRIEVAL_TOP_K", "8")))
        self.retrieval_candidate_k = max(self.retrieval_top_k, int(os.getenv("RETRIEVAL_CANDIDATE_K", "30")))
        self.retrieval_timeout_seconds = max(0.0, float(os.getenv("RETRIEVAL_TIMEOUT_SECONDS", "10.0")))
        self.retrieval_max_context_chars = max(400, int(os.getenv("RETRIEVAL_MAX_CONTEXT_CHARS", "8000")))
        self.retrieval_query_max_chars = max(120, int(os.getenv("RETRIEVAL_QUERY_MAX_CHARS", "400")))
        self.retrieval_preview_chars = max(240, int(os.getenv("RETRIEVAL_DEBUG_PREVIEW_CHARS", "800")))
        self.diagnostics_max_bytes = max(4096, int(os.getenv("ADMIN_DIAGNOSTICS_MAX_BYTES", "120000")))
        self.diagnostics_max_string_chars = max(200, int(os.getenv("ADMIN_DIAGNOSTICS_MAX_STRING_CHARS", "4000")))
        self.diagnostics_max_list_items = max(5, int(os.getenv("ADMIN_DIAGNOSTICS_MAX_LIST_ITEMS", "50")))
        self.rerank_model = os.getenv("RERANK_MODEL", "/models/rerank").strip() or "/models/rerank"
        self.rerank_device = os.getenv("RERANK_DEVICE", "cpu").strip() or "cpu"
        self.rerank_score_threshold = min(1.0, max(0.0, float(os.getenv("RERANK_SCORE_THRESHOLD", "0.16"))))
        self.retrieval_instruction = os.getenv(
            "WIKI_RETRIEVAL_INSTRUCTION",
            "You are a knowledgeable tutor helping a student learn. The following passages were "
            "retrieved from Wikipedia to help you give an accurate, well-grounded answer. Use them "
            "to provide factual detail, correct any misconceptions, and explain concepts clearly. "
            'If a passage is directly relevant, cite its title naturally in your response (e.g., '
            '"According to information on [Topic]..."). If the passages are not relevant to the '
            "question, answer from your own knowledge. Never mention retrieval, snippets, wiki "
            "support, or hidden context unless the user explicitly asks.",
        ).strip()
        self.retrieval_instruction_es = os.getenv(
            "WIKI_RETRIEVAL_INSTRUCTION_ES",
            "Eres un tutor experto que ayuda a un estudiante a aprender. Los siguientes pasajes "
            "fueron recuperados de Wikipedia para ayudarte a dar una respuesta precisa y bien "
            "fundamentada. Úsalos para aportar detalles factuales, corregir conceptos erróneos y "
            "explicar las ideas con claridad. Cuando un pasaje sea directamente relevante, cita "
            'su título de forma natural en tu respuesta (por ejemplo, "Según el artículo sobre '
            '[Tema]..."). Si los pasajes no son relevantes para la pregunta, responde con tu '
            "propio conocimiento. Nunca menciones la recuperación, los fragmentos, el soporte "
            "wiki ni el contexto oculto a menos que el estudiante lo pida explícitamente.",
        ).strip()
        self.base_system_prompt = (os.getenv("BASE_SYSTEM_PROMPT", "") or "").strip()
        self.base_system_prompt_es = (os.getenv("BASE_SYSTEM_PROMPT_ES", "") or "").strip()

        self.runtime_backend_name = (os.getenv("RUNTIME_BACKEND_NAME", "llama.cpp") or "llama.cpp").strip() or "llama.cpp"
        self.runtime_host = (os.getenv("RUNTIME_HOST", socket.gethostname()) or socket.gethostname()).strip() or socket.gethostname()
        self.llama_container_name = (os.getenv("LLAMA_CONTAINER_NAME", "aibox-llama") or "aibox-llama").strip() or "aibox-llama"
        self.llama_model_file = (os.getenv("LLAMA_MODEL_FILE", "qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf") or "qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf").strip() or "qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf"
        self.llama_model_path = (os.getenv("LLAMA_MODEL_PATH", f"/models/llm/gguf/{self.llama_model_file}") or f"/models/llm/gguf/{self.llama_model_file}").strip() or f"/models/llm/gguf/{self.llama_model_file}"
        self.llama_ctx_size = max(512, int(os.getenv("LLAMA_CTX_SIZE", "8192")))
        self.llama_n_predict = max(1, int(os.getenv("LLAMA_N_PREDICT", "768")))
        self.llama_threads = int(os.getenv("LLAMA_THREADS", "-1"))
        self.llama_threads_batch = int(os.getenv("LLAMA_THREADS_BATCH", str(self.llama_threads)))
        self.llama_batch_size = max(1, int(os.getenv("LLAMA_BATCH_SIZE", "1024")))
        self.llama_ubatch_size = max(1, int(os.getenv("LLAMA_UBATCH_SIZE", "512")))
        self.llama_n_gpu_layers = int(os.getenv("LLAMA_N_GPU_LAYERS", "99"))
        self.llama_context_shift_enabled = _coerce_bool(os.getenv("LLAMA_CONTEXT_SHIFT_ENABLED", "1"), default=True)
        self.llama_context_shift_keep = max(0, int(os.getenv("LLAMA_CONTEXT_SHIFT_KEEP", "1000")))
        self.llama_flash_attn = _coerce_bool(os.getenv("LLAMA_FLASH_ATTN", "1"), default=True)
        self.llama_temperature = float(os.getenv("TEMPERATURE", "0.0"))
        self.llama_top_p = float(os.getenv("TOP_P", "0.95"))
        self.llama_top_k = int(os.getenv("TOP_K", "40"))
        self.llama_min_p = float(os.getenv("MIN_P", "0.0"))
        self.llama_repeat_penalty = float(os.getenv("REPEAT_PENALTY", os.getenv("REPETITION_PENALTY", "1.0")))
        self.llama_seed = int(os.getenv("LLAMA_SEED", "-1"))
        stop_sequences_raw = (os.getenv("LLAMA_STOP_SEQUENCES", os.getenv("STOP_SEQUENCES", "")) or "").strip()
        if stop_sequences_raw:
            try:
                parsed_stop_sequences = json.loads(stop_sequences_raw)
                if isinstance(parsed_stop_sequences, list):
                    self.llama_stop_sequences = [str(item) for item in parsed_stop_sequences if str(item).strip()]
                else:
                    self.llama_stop_sequences = [segment.strip() for segment in stop_sequences_raw.split("||") if segment.strip()]
            except Exception:
                self.llama_stop_sequences = [segment.strip() for segment in stop_sequences_raw.split("||") if segment.strip()]
        else:
            self.llama_stop_sequences = []
        self.llama_grammar_mode = (os.getenv("LLAMA_GRAMMAR_MODE", "none") or "none").strip() or "none"
        self.llama_rope_scaling = (os.getenv("LLAMA_ROPE_SCALING", "") or "").strip() or None
        self.llama_yarn_scaling = (os.getenv("LLAMA_YARN_SCALING", "") or "").strip() or None

        self.cookie = os.getenv("SESSION_COOKIE_NAME", "aibox_session")

        # Cookie flag policy. Default: secure=True iff APP_ENV=production; samesite=lax.
        # Operators serving over plain HTTP on a LAN must set SESSION_COOKIE_SECURE=false,
        # otherwise browsers will silently drop the cookie.
        cookie_secure_raw = (os.getenv("SESSION_COOKIE_SECURE", "auto") or "auto").strip().lower()
        if cookie_secure_raw in ("1", "true", "yes", "on"):
            self.cookie_secure = True
        elif cookie_secure_raw in ("0", "false", "no", "off"):
            self.cookie_secure = False
        else:  # "auto" / unrecognized
            self.cookie_secure = (self.app_env == "production")
        samesite_raw = (os.getenv("SESSION_COOKIE_SAMESITE", "lax") or "lax").strip().lower()
        self.cookie_samesite = samesite_raw if samesite_raw in ("lax", "strict", "none") else "lax"
        # RFC 6265bis: samesite=none requires secure=true; silently upgrade secure if needed
        if self.cookie_samesite == "none" and not self.cookie_secure:
            logger.warning("SESSION_COOKIE_SAMESITE=none requires secure cookies; forcing secure=True")
            self.cookie_secure = True

        # Trust X-Forwarded-For only when explicitly enabled (e.g. behind Caddy+TLS).
        self.trust_proxy_headers = (os.getenv("TRUST_PROXY_HEADERS", "false") or "false").strip().lower() in ("1", "true", "yes", "on")
        # When running behind a reverse proxy (Caddy in the AIBox stack), every
        # connection arrives from the proxy IP unless we trust forwarded headers.
        # That collapses every rate-limit bucket onto a single key. Warn loudly
        # if we look like we're behind a proxy but the flag is off.
        if not self.trust_proxy_headers:
            in_container = os.path.exists("/.dockerenv") or os.getenv("RUNTIME_BEHIND_PROXY", "").strip().lower() in ("1", "true", "yes", "on")
            if in_container:
                logger.warning(
                    "TRUST_PROXY_HEADERS=false while running inside a container — "
                    "if Caddy or any reverse proxy fronts ai-control, all rate-limit "
                    "buckets will collapse onto the proxy IP. Set TRUST_PROXY_HEADERS=true "
                    "in stack/.env when ai-control runs behind Caddy."
                )

        self.token_pepper = os.getenv("SESSION_TOKEN_PEPPER", "")
        # Production must have a strong pepper (>= 32 chars / 128 bits of entropy
        # at minimum). secrets.token_hex(16) produces 32 chars, which is the
        # generator we document in README.md; reject anything shorter.
        if self.token_pepper and self.app_env == "production" and len(self.token_pepper) < 32:
            raise RuntimeError(
                f"SESSION_TOKEN_PEPPER is too short ({len(self.token_pepper)} chars); "
                "production requires >= 32 chars. Generate one with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if not self.token_pepper:
            if self.app_env == "production":
                raise RuntimeError("SESSION_TOKEN_PEPPER must be set for production. Add it to stack/.env")
            # Dev fallback: generate and persist a per-install random pepper so
            # restarts keep existing sessions valid but no two installs share a pepper.
            pepper_dir = self.data_root / "security"
            try:
                pepper_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pepper_dir = self.data_root
            pepper_file = pepper_dir / "session_pepper.dev"
            try:
                if pepper_file.exists():
                    self.token_pepper = pepper_file.read_text(encoding="utf-8").strip()
                if not self.token_pepper:
                    self.token_pepper = secrets.token_hex(32)
                    pepper_file.write_text(self.token_pepper, encoding="utf-8")
                    try:
                        os.chmod(pepper_file, 0o600)
                    except Exception:
                        pass
            except Exception:
                # If the filesystem is read-only for any reason, fall back to an
                # in-memory random pepper. Sessions will invalidate on restart, which
                # is acceptable in dev.
                self.token_pepper = secrets.token_hex(32)
                logger.warning("Could not persist dev session pepper; sessions will not survive restarts.")
            logger.warning(
                "SESSION_TOKEN_PEPPER not set. Using generated dev pepper at %s. "
                "Set SESSION_TOKEN_PEPPER explicitly for production.",
                pepper_file,
            )
        self.session_days = int(os.getenv("SESSION_DAYS", "7"))

        # Signup controls
        public_signup_default = "false" if self.app_env == "production" else "true"
        self.allow_public_signup = (os.getenv("ALLOW_PUBLIC_SIGNUP", public_signup_default) or public_signup_default).strip().lower() in ("1", "true", "yes", "on")
        self.signup_max_per_hour_per_ip = max(1, int(os.getenv("SIGNUP_MAX_PER_HOUR_PER_IP", "5")))
        self.user_password_min_length = max(4, int(os.getenv("USER_PASSWORD_MIN_LENGTH", "8")))
        self.guest_password_min_length = max(4, int(os.getenv("GUEST_PASSWORD_MIN_LENGTH", "4")))
        self.admin_password_min_length = max(8, int(os.getenv("ADMIN_PASSWORD_MIN_LENGTH", "8")))

        self.admin_username = os.getenv("ADMIN_USERNAME", "")
        self.admin_password = os.getenv("ADMIN_DEFAULT_PASSWORD", "")
        if not self.admin_username or not self.admin_password:
            if self.app_env == "production":
                raise RuntimeError(
                    "ADMIN_USERNAME and ADMIN_DEFAULT_PASSWORD must be set. "
                    "Add them to stack/.env or docker-compose environment."
                )
            self.admin_username = self.admin_username or "admin"
            if not self.admin_password:
                self.admin_password = secrets.token_urlsafe(16)
                logger.warning(
                    "ADMIN_DEFAULT_PASSWORD not set. Generated random admin password: %s  "
                    "(Set ADMIN_USERNAME and ADMIN_DEFAULT_PASSWORD in stack/.env for production.)",
                    self.admin_password,
                )
            else:
                logger.warning("Using development admin credentials. Set ADMIN_USERNAME and ADMIN_DEFAULT_PASSWORD for production.")

        self.chat_retention_days = int(os.getenv("CHAT_RETENTION_DAYS", "90"))
        self.doc_retention_days = int(os.getenv("DOC_RETENTION_DAYS", "180"))
        self.guest_retention_days = int(os.getenv("GUEST_RETENTION_DAYS", "30"))
        self.guest_logout_delete_minutes = int(os.getenv("GUEST_LOGOUT_DELETE_MINUTES", "5"))
        self.trash_retention_days = int(os.getenv("TRASH_RETENTION_DAYS", "3"))

        self.warn_pct = float(os.getenv("DISK_WARN_PERCENT", "75"))
        self.clean_pct = float(os.getenv("DISK_CLEANUP_PERCENT", "85"))
        self.emer_pct = float(os.getenv("DISK_EMERGENCY_PERCENT", "90"))

        self.warn_free = int(os.getenv("FREE_WARN_BYTES", str(5 * 1024 * 1024 * 1024)))
        self.clean_free = int(os.getenv("FREE_CLEANUP_BYTES", str(3 * 1024 * 1024 * 1024)))
        self.emer_free = int(os.getenv("FREE_EMERGENCY_BYTES", str(1 * 1024 * 1024 * 1024)))

        self.doc_create_per_min = int(os.getenv("DOC_CREATE_PER_MIN", "6"))
        self.chat_create_per_min = int(os.getenv("CHAT_CREATE_PER_MIN", "20"))
        self.doc_write_bpm = int(os.getenv("DOC_WRITE_BYTES_PER_MIN", str(2 * 1024 * 1024)))
        self.chat_write_bpm = int(os.getenv("CHAT_WRITE_BYTES_PER_MIN", str(8 * 1024 * 1024)))
        self.doc_max = int(os.getenv("DOC_FILE_MAX_BYTES", str(256_000)))
        self.chat_max = int(os.getenv("CHAT_FILE_MAX_BYTES", str(5 * 1024 * 1024)))
        self.max_chats = int(os.getenv("MAX_CHAT_SESSIONS_PER_USER", "200"))
        self.max_saved_chats = int(os.getenv("MAX_SAVED_CHATS_PER_USER", "10"))
        self.docs_offense_window = int(os.getenv("DOCS_OFFENSE_WINDOW_SECONDS", "600"))
        self.docs_offense_hits = int(os.getenv("DOCS_OFFENSE_HITS", "2"))
        self.docs_write_block_seconds = int(os.getenv("DOCS_WRITE_BLOCK_SECONDS", "1800"))
        self.ai_offense_window = int(os.getenv("AI_OFFENSE_WINDOW_SECONDS", "600"))
        self.ai_block_hits = int(os.getenv("AI_BLOCK_HITS", "3"))
        self.ai_prompt_cooldown_seconds = int(os.getenv("AI_PROMPT_COOLDOWN_SECONDS", "10"))
        self.ai_send_block_seconds = int(os.getenv("AI_SEND_BLOCK_SECONDS", "300"))
        self.ai_requests_per_min = max(1, int(os.getenv("AI_REQUESTS_PER_MIN", "12")))
        self.ai_requests_per_hour = max(self.ai_requests_per_min, int(os.getenv("AI_REQUESTS_PER_HOUR", "120")))
        self.ai_ip_requests_per_min = max(1, int(os.getenv("AI_IP_REQUESTS_PER_MIN", "30")))
        self.max_docs = int(os.getenv("MAX_DOCS_PER_USER", "150"))
        self.max_concurrent_generations = int(os.getenv("MAX_CONCURRENT_GENERATIONS_PER_USER", "2"))
        self.heavy_prompt_chars = int(os.getenv("HEAVY_PROMPT_CHARS", "5000"))
        self.heavy_prompt_hits = int(os.getenv("HEAVY_PROMPT_HITS", "3"))
        self.chat_continuation_limit = int(os.getenv("CHAT_CONTINUATION_LIMIT", "2"))
        self.chat_continue_prompt = os.getenv("CHAT_CONTINUE_PROMPT", "Continue exactly where you stopped. Do not restart or repeat prior text.")
        self.login_fail_limit = int(os.getenv("LOGIN_FAIL_LIMIT", "5"))
        self.login_window = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))

        self.cleanup_loop_seconds = int(os.getenv("CLEANUP_LOOP_SECONDS", "600"))

        self.rate_events_retention_days = int(os.getenv("RATE_EVENTS_RETENTION_DAYS", "7"))
        self.security_events_retention_days = int(os.getenv("SECURITY_EVENTS_RETENTION_DAYS", "90"))
        self.usage_events_retention_days = int(os.getenv("USAGE_EVENTS_RETENTION_DAYS", "14"))
        self.cleanup_events_retention_days = int(os.getenv("CLEANUP_EVENTS_RETENTION_DAYS", "30"))
        self.login_attempts_retention_days = int(os.getenv("LOGIN_ATTEMPTS_RETENTION_DAYS", "7"))
        self.session_retention_days = int(os.getenv("SESSION_RETENTION_DAYS", "30"))
        self.vacuum_interval_hours = int(os.getenv("VACUUM_INTERVAL_HOURS", "24"))
        cleanup_marker_default = self.app_env == "production"
        self.cleanup_require_backup_marker = _coerce_bool(os.getenv("CLEANUP_REQUIRE_BACKUP_MARKER", None), default=cleanup_marker_default)
        self.cleanup_backup_marker_path = Path(os.getenv("CLEANUP_BACKUP_MARKER_PATH", str(self.data_root / "backups" / "latest_verified_backup.json")))
        self.cleanup_backup_marker_max_hours = max(1, int(os.getenv("CLEANUP_BACKUP_MARKER_MAX_HOURS", "72")))
        self.cleanup_manifest_dir = Path(os.getenv("CLEANUP_MANIFEST_DIR", str(self.data_root / "cleanup-manifests")))
        self.orphan_quarantine_root = Path(os.getenv("ORPHAN_USER_QUARANTINE_ROOT", str(self.data_root / "orphan-user-quarantine")))
        self.orphan_quarantine_days = max(1, int(os.getenv("ORPHAN_USER_QUARANTINE_DAYS", "30")))
        # Confine both cleanup-manifest and orphan-quarantine destinations to
        # data_root. A misconfigured env var could otherwise write to (or move
        # users' data into) anywhere the process can reach. We resolve symlinks
        # before the containment check so a reparse point cannot point outside.
        self._validate_within_data_root(self.cleanup_manifest_dir, "CLEANUP_MANIFEST_DIR")
        self._validate_within_data_root(self.orphan_quarantine_root, "ORPHAN_USER_QUARANTINE_ROOT")
        self._last_vacuum_ts = 0

        key_b64 = (os.getenv("APP_ENCRYPTION_MASTER_KEY", "") or "").strip()
        if not key_b64:
            raise RuntimeError("APP_ENCRYPTION_MASTER_KEY is required")
        try:
            self.enc_key = base64.b64decode(key_b64, validate=True)
        except Exception as exc:
            raise RuntimeError("APP_ENCRYPTION_MASTER_KEY must be valid base64") from exc
        if len(self.enc_key) != 32:
            raise RuntimeError("APP_ENCRYPTION_MASTER_KEY must decode to exactly 32 bytes")
        self._aes = AESGCM(self.enc_key)
        self.enc_version = 1

        self._ph = PasswordHasher()
        self._cleanup_lock = threading.Lock()
        self._request_counter_lock = threading.Lock()
        self._request_counter = 0
        self._retriever_init_lock = threading.Lock()
        self._retriever_run_lock = threading.Lock()
        self._reranker_init_lock = threading.Lock()
        self._retriever_init_lock_es = threading.Lock()
        self._embedder: Any = None
        self._chroma_client: Any = None
        self._wiki_collection: Any = None
        self._chroma_client_es: Any = None
        self._wiki_collection_es: Any = None
        self._es_load_started: bool = False
        self._reranker: Any = None
        self.analytics_export_version = 1
        self.analytics_frontend_events = {
            "portal_tool_open",
            "wiki_shell_open",
            "wiki_open_full_page",
            "learn_shell_open",
            "learn_open_full_page",
            "chat_completion_stopped",
        }
        self._embed_dimension: Optional[int] = None
        self._collection_dimension: Optional[int] = None
        self._chroma_count: int = 0
        self._index_manifest: Optional[Dict[str, Any]] = None
        self._index_manifest_es: Optional[Dict[str, Any]] = None
        self._startup_rag_status: Dict[str, Any] = {
            "app_env": self.app_env,
            "startup_rag_ok": None,
            "startup_rag_error": None,
            "startup_rag_checked_at": None,
            "startup_rag_test_query": "What was the War of 1812?",
            "startup_rag_test_expected_terms": ["war", "1812"],
            "startup_rag_test_matches": [],
            "startup_reranker_ok": None,
            "startup_reranker_error": None,
            "embed_model_path": str(self.embed_model),
            "embed_model_exists": False,
            "chroma_persist_dir": str(self.chroma_persist_dir),
            "chroma_persist_exists": False,
            "chroma_collection": self.chroma_collection,
            "chroma_count": 0,
            "embed_dimension": None,
            "collection_dimension": None,
            "index_manifest": None,
            "index_manifest_path": None,
            "index_manifest_error": None,
            "rerank_model_path": str(self.rerank_model),
            "rerank_available": False,
            "require_rag_startup": True,
            "ready": False,
        }

    # -----------------------------
    # Small utility helpers
    # -----------------------------
    def now_iso(self) -> str:
        """Return the current UTC time in ISO format for DB rows and JSON files."""
        return datetime.now(timezone.utc).isoformat()

    def now_ts(self) -> int:
        return int(time.time())

    def uid(self) -> str:
        return str(uuid.uuid4())

    def nuser(self, value: str) -> str:
        return (value or "").strip().lower()

    def ip(self, req: Request) -> str:
        # Only honor X-Forwarded-For when TRUST_PROXY_HEADERS is explicitly enabled.
        # Otherwise an untrusted client could spoof their source IP and defeat
        # per-IP lockouts / signup rate limits.
        if self.trust_proxy_headers:
            xff = req.headers.get("x-forwarded-for", "").strip()
            if xff:
                return xff.split(",")[0].strip()
        if req.client:
            return str(req.client.host)
        return "unknown"

    def _same_site_host(self, req: Request) -> str:
        return str(req.headers.get("host") or req.url.netloc or "").strip().lower()

    def _origin_host(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
        except Exception:
            return ""
        if parsed.scheme.lower() not in ("http", "https"):
            return ""
        return str(parsed.netloc or "").strip().lower()

    def validate_same_origin_write(self, req: Request) -> None:
        """Reject browser-originated cross-site writes for cookie-authenticated routes."""
        expected_host = self._same_site_host(req)
        if not expected_host:
            raise HTTPException(403, "Invalid request origin")

        fetch_site = str(req.headers.get("sec-fetch-site") or "").strip().lower()
        if fetch_site in ("cross-site", "same-site"):
            raise HTTPException(403, "Cross-site write blocked")

        origin = str(req.headers.get("origin") or "").strip()
        if origin:
            if origin.lower() == "null" or self._origin_host(origin) != expected_host:
                raise HTTPException(403, "Invalid request origin")
            return

        referer = str(req.headers.get("referer") or "").strip()
        if referer and self._origin_host(referer) != expected_host:
            raise HTTPException(403, "Invalid request origin")

    def persist_and_raise(self, c: sqlite3.Connection, status_code: int, detail: str) -> None:
        """Commit enforcement-side effects before raising an HTTP error.

        Rate limits, lockouts, and security events are part of the intended state
        transition, not incidental work. Commit them before raising so the outer
        request transaction does not roll them back.
        """
        try:
            c.commit()
        except sqlite3.Error:
            logger.exception("Failed to commit enforcement state before raising %s", status_code)
        raise HTTPException(status_code, detail)

    def token_hash(self, token: str) -> str:
        return hashlib.sha256(f"{token}:{self.token_pepper}".encode("utf-8")).hexdigest()

    def parse_iso(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def is_future(self, value: Optional[str]) -> bool:
        dt = self.parse_iso(value)
        return bool(dt and dt > datetime.now(timezone.utc))

    def seconds_until(self, value: Optional[str]) -> int:
        dt = self.parse_iso(value)
        if not dt:
            return 0
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(delta))

    def sanitize_markdown(self, value: Any) -> str:
        return str(value or "").replace("\x00", "")

    def estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text or "") / 4))

    def user_root(self, user_id: str) -> Path:
        return (self.users_root / user_id).resolve()

    def safe_user_dir(self, user_id: str) -> Path:
        root = self.users_root.resolve()
        target = (root / user_id).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"Refusing to access path outside users root: {target}") from exc
        return target

    def remove_user_dir(self, user_id: str) -> bool:
        target = self.safe_user_dir(user_id)
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

    def path_size(self, path: Path) -> int:
        """Best-effort recursive size helper used before deletion/quarantine."""
        try:
            if path.is_file():
                return int(path.stat().st_size)
            if path.is_dir():
                return int(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()))
        except Exception:
            return 0
        return 0

    def cleanup_backup_marker_is_fresh(self) -> bool:
        """Return true when a recent verified-backup marker exists."""
        try:
            marker = self.cleanup_backup_marker_path
            if not marker.exists():
                return False
            # utf-8-sig strips a UTF-8 BOM if present. PowerShell 5.1's default
            # Set-Content -Encoding UTF8 emits one, which json.loads otherwise
            # rejects with "Unexpected UTF-8 BOM".
            raw = json.loads(marker.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict) or raw.get("verified") is not True:
                return False
            value = raw.get("verified_at") or raw.get("created_at")
            dt = self.parse_iso(str(value or ""))
            if dt is None:
                return False
            age = datetime.now(timezone.utc) - dt
            return age <= timedelta(hours=self.cleanup_backup_marker_max_hours)
        except Exception:
            return False

    def write_cleanup_manifest(self, manifest: Dict[str, Any]) -> Optional[str]:
        """Persist a cleanup manifest for audit/recovery visibility."""
        try:
            self.cleanup_manifest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            suffix = "dry-run" if manifest.get("dry_run") else "applied"
            path = self.cleanup_manifest_dir / f"cleanup-{stamp}-{suffix}-{self.uid()[:8]}.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            return str(path)
        except Exception:
            logger.warning("failed to write cleanup manifest", exc_info=True)
            return None

    def quarantine_orphan_user_dir(self, path: Path) -> Tuple[int, str]:
        """Move an orphaned user directory out of active storage instead of deleting it."""
        root = self.users_root.resolve()
        entry = path.resolve()
        try:
            entry.relative_to(root)
        except Exception as exc:
            raise RuntimeError(f"Refusing to quarantine path outside users root: {entry}") from exc
        size = self.path_size(entry)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_root = self.orphan_quarantine_root / stamp
        dest_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = dest_root / entry.name
        shutil.move(str(entry), str(dest))
        manifest = {
            "quarantined_at": self.now_iso(),
            "source": str(entry),
            "destination": str(dest),
            "size_bytes": size,
            "purge_after_days": self.orphan_quarantine_days,
        }
        (dest_root / f"{entry.name}.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return size, str(dest)

    def _validate_within_data_root(self, candidate: Path, env_var_name: str) -> None:
        """Refuse to use a configured path that escapes self.data_root.

        Run during __init__ so misconfiguration fails loudly at boot instead
        of writing manifests / quarantined user data outside the data tree.
        Resolves symlinks/reparse points to defeat link-based escapes.
        """
        try:
            root_resolved = self.data_root.resolve(strict=False)
        except Exception:
            root_resolved = self.data_root
        try:
            target = candidate.resolve(strict=False)
        except Exception:
            raise RuntimeError(
                f"{env_var_name}={candidate} could not be resolved; refusing to start."
            )
        # Avoid is_relative_to so this works on Py 3.9 too.
        try:
            target.relative_to(root_resolved)
        except ValueError:
            raise RuntimeError(
                f"{env_var_name}={candidate} resolves to {target} which is outside "
                f"APP_DATA_ROOT={root_resolved}. Refusing to start."
            )
        # If the candidate exists, also reject if any path component is a
        # symlink that points outside root_resolved.
        if candidate.exists():
            try:
                for parent in [candidate, *candidate.parents]:
                    if parent.is_symlink():
                        resolved = parent.resolve(strict=False)
                        try:
                            resolved.relative_to(root_resolved)
                        except ValueError:
                            raise RuntimeError(
                                f"{env_var_name}={candidate} traverses symlink {parent} → {resolved} "
                                f"which is outside APP_DATA_ROOT={root_resolved}."
                            )
                    if parent == root_resolved:
                        break
            except RuntimeError:
                raise
            except Exception:
                # OS error walking parents — be conservative, accept; the
                # resolve() containment check above already covered the path.
                pass

    def ensure_dirs(self) -> None:
        for p in (self.data_root, self.users_root, self.tmp_root, self.db_path.parent):
            p.mkdir(parents=True, exist_ok=True)

    def ensure_user_dirs(self, user_id: str) -> None:
        root = self.users_root / user_id
        for p in (root / "docs", root / "chats", root / "trash" / "docs", root / "trash" / "chats"):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)

    def is_development_mode(self) -> bool:
        return self.app_env in {"dev", "development", "local", "test"}

    def _rag_required_at_startup(self) -> bool:
        return not self.is_development_mode()

    def _update_rag_status(self, **updates: Any) -> None:
        self._startup_rag_status.update(updates)
        self._startup_rag_status["app_env"] = self.app_env
        self._startup_rag_status["require_rag_startup"] = self._rag_required_at_startup()
        self._startup_rag_status["ready"] = bool(self._startup_rag_status.get("startup_rag_ok"))

    def _index_manifest_path(self, persist_dir: Path) -> Path:
        return Path(persist_dir) / "index_manifest.json"

    # Schema versions we know how to read. Anything ≤ this is accepted; newer
    # manifests fail closed because we can't reason about fields we don't know.
    _MANIFEST_SCHEMA_MAX = 1
    # Required top-level keys for a manifest to be usable at runtime. Missing
    # keys means the builder didn't finish or wrote an older/incompatible
    # format — reject rather than silently drift.
    _MANIFEST_REQUIRED_KEYS = ("schema_version", "embedding_model", "chroma")

    def _validate_index_manifest(self, raw: Dict[str, Any]) -> Optional[str]:
        try:
            schema = int(raw.get("schema_version") or 0)
        except (TypeError, ValueError):
            return "manifest_schema_invalid"
        if schema <= 0:
            return "manifest_schema_missing"
        if schema > self._MANIFEST_SCHEMA_MAX:
            return f"manifest_schema_unsupported (schema_version={schema}, max_known={self._MANIFEST_SCHEMA_MAX})"
        for key in self._MANIFEST_REQUIRED_KEYS:
            if key not in raw:
                return f"manifest_missing_required_key:{key}"
        model = raw.get("embedding_model") if isinstance(raw.get("embedding_model"), dict) else None
        if not model or not (model.get("name") or model.get("path")):
            return "manifest_missing_embedding_model"
        chroma = raw.get("chroma") if isinstance(raw.get("chroma"), dict) else None
        if not chroma or not chroma.get("collection_name"):
            return "manifest_missing_chroma_collection"
        return None

    def _load_index_manifest(self, persist_dir: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        path = self._index_manifest_path(persist_dir)
        if not path.exists():
            return None, str(path), "manifest_missing"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, str(path), f"{type(exc).__name__}: {exc}"
        if not isinstance(raw, dict):
            return None, str(path), "manifest_not_object"
        schema_error = self._validate_index_manifest(raw)
        if schema_error:
            logger.error("index manifest schema validation failed for %s: %s", path, schema_error)
            return None, str(path), schema_error
        return raw, str(path), None

    def _manifest_summary(self, manifest: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(manifest, dict):
            return None
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        model = manifest.get("embedding_model") if isinstance(manifest.get("embedding_model"), dict) else {}
        chroma = manifest.get("chroma") if isinstance(manifest.get("chroma"), dict) else {}
        build = manifest.get("build") if isinstance(manifest.get("build"), dict) else {}
        return {
            "schema_version": manifest.get("schema_version"),
            "tool_version": manifest.get("tool_version"),
            "built_at": manifest.get("built_at"),
            "source_file": source.get("chunks_file_name") or source.get("chunks_file"),
            "source_sha256": source.get("chunks_file_sha256"),
            "embedding_model": model.get("name") or model.get("path"),
            "embedding_dimension": model.get("dimension"),
            "collection_name": chroma.get("collection_name"),
            "chunk_count": chroma.get("chunk_count") or build.get("embedded_chunks"),
            "skipped_chunks": build.get("skipped_chunks"),
        }

    def _validate_smoke_matches(self, matches: List[Dict[str, Any]], expected_terms: List[str], label: str) -> None:
        if not matches:
            raise RuntimeError(f"startup retrieval test ({label}) returned no matches")
        haystack = " ".join(
            " ".join(
                str(match.get(key) or "")
                for key in ("title", "section_title", "section_path", "preview", "source_document")
            )
            for match in matches
        ).lower()
        missing = [term for term in expected_terms if term.lower() not in haystack]
        if missing:
            raise RuntimeError(f"startup retrieval test ({label}) missing expected terms: {', '.join(missing)}")

    # -----------------------------
    # Retrieval and diagnostics state
    # -----------------------------
    def rag_status_snapshot(self) -> Dict[str, Any]:
        """Return the latest retrieval startup status for readiness and admin views."""
        snapshot = dict(self._startup_rag_status)
        snapshot["app_env"] = self.app_env
        snapshot["require_rag_startup"] = self._rag_required_at_startup()
        snapshot["ready"] = bool(snapshot.get("startup_rag_ok"))
        return snapshot

    def _base_retrieval_meta(self, retrieval_enabled: bool) -> Dict[str, Any]:
        rag_status = self.rag_status_snapshot()
        collection_dimension = rag_status.get("collection_dimension")
        embed_dimension = rag_status.get("embed_dimension")
        dimensions_match = collection_dimension in (None, embed_dimension)
        return {
            "retrieval_enabled": bool(retrieval_enabled),
            "retrieval_attempted": False,
            "retrieval_used": False,
            "retrieval_count": 0,
            "retrieval_ms": 0,
            "retrieval_timed_out": False,
            "retrieval_error": None,
            "retrieval_skipped_reason": None,
            "retrieval_context_chars": 0,
            "retrieved_chunks": [],
            "retrieval_candidates": [],
            "citations": [],
            "primary_citation": None,
            "retrieval_query": None,
            "retrieval_candidate_count": 0,
            "retrieval_candidate_k": int(self.retrieval_candidate_k),
            "retrieval_top_k": int(self.retrieval_top_k),
            "min_relevance_score": float(self.rerank_score_threshold),
            "retrieval_path_loaded": bool(rag_status.get("startup_rag_ok")),
            "collection_loaded": bool(rag_status.get("chroma_count")),
            "dimensions_match": bool(dimensions_match),
            "chunks_after_rerank": 0,
            "chunks_after_budget_trim": 0,
            "rag_fallback_triggered": False,
            "no_context_answer_mode": False,
            "retrieval_system_message": None,
            "final_context_estimated_tokens": 0,
            "rag_chunk_truncation_count": 0,
            "request_prep_ms": 0,
            "upstream_open_ms": None,
            "max_token_gap_ms": 0,
            "continuation_gap_ms": 0,
            "embed_model_path": rag_status.get("embed_model_path"),
            "embed_model_exists": rag_status.get("embed_model_exists"),
            "chroma_persist_dir": rag_status.get("chroma_persist_dir"),
            "chroma_collection": rag_status.get("chroma_collection"),
            "chroma_count": rag_status.get("chroma_count"),
            "index_manifest": self._clone_data(rag_status.get("index_manifest")),
            "index_manifest_path": rag_status.get("index_manifest_path"),
            "index_manifest_error": rag_status.get("index_manifest_error"),
            "embed_dimension": embed_dimension,
            "collection_dimension": collection_dimension,
            "startup_rag_ok": rag_status.get("startup_rag_ok"),
            "startup_rag_error": rag_status.get("startup_rag_error"),
            "startup_rag_checked_at": rag_status.get("startup_rag_checked_at"),
            "startup_rag_test_query": rag_status.get("startup_rag_test_query"),
            "startup_rag_test_expected_terms": rag_status.get("startup_rag_test_expected_terms"),
            "startup_rag_test_matches": rag_status.get("startup_rag_test_matches"),
            "startup_reranker_ok": rag_status.get("startup_reranker_ok"),
            "startup_reranker_error": rag_status.get("startup_reranker_error"),
            "rerank_model_path": rag_status.get("rerank_model_path"),
            "rerank_enabled": False,
            "rerank_ms": 0,
            "rerank_error": None,
            "rerank_available": bool(rag_status.get("rerank_available")),
            "rag_summary": None,
            "rag_index_language": None,
            "rag_collection_path": None,
        }
    def _int_or_none(self, value: Any, default: Optional[int] = None) -> Optional[int]:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        try:
            return int(float(text))
        except Exception:
            return default

    def _float_or_none(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        try:
            return float(text)
        except Exception:
            return default

    def _clone_data(self, value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False))
        except Exception:
            return value

    def _next_request_identity(self) -> Tuple[str, bool]:
        with self._request_counter_lock:
            cold_request = self._request_counter == 0
            self._request_counter += 1
        return self.uid(), cold_request

    def _current_process_rss_bytes(self) -> Optional[int]:
        status_path = Path("/proc/self/status")
        if status_path.exists():
            try:
                for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
            except Exception:
                pass
        return None

    def _current_mem_available_bytes(self) -> Optional[int]:
        meminfo_path = Path("/proc/meminfo")
        if meminfo_path.exists():
            try:
                for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
            except Exception:
                pass
        return None

    def _resource_snapshot(self) -> Dict[str, Any]:
        rss = self._current_process_rss_bytes()
        mem_available = self._current_mem_available_bytes()
        return {
            "captured_at": self.now_iso(),
            "process_rss_bytes": rss,
            "system_memory_available_bytes": mem_available,
            "gpu_attached": True if (self.llama_n_gpu_layers is not None and self.llama_n_gpu_layers > 0) else None,
            "gpu_memory_bytes": None,
            "gpu_utilization_pct": None,
            "cpu_utilization_pct": None,
            "unavailable_reason": None if (rss is not None or mem_available is not None) else "resource_snapshot_unavailable",
        }

    def _trace_diagnostics(self, summary: Dict[str, Any], request_start_t: float, stage: str, **extra: Any) -> None:
        entry = {
            "stage": stage,
            "t_ms": int((time.perf_counter() - request_start_t) * 1000),
            "at": self.now_iso(),
        }
        for key, value in extra.items():
            if value is not None:
                entry[key] = value
        summary.setdefault("stage_trace", []).append(entry)
        summary["current_stage"] = stage

    def _flatten_warning_messages(self, warnings: Any) -> List[str]:
        out: List[str] = []
        if not isinstance(warnings, list):
            return out
        for warning in warnings:
            if isinstance(warning, dict):
                text = warning.get("message") or warning.get("detail") or warning.get("warning") or warning.get("metric")
            else:
                text = warning
            text = str(text or "").strip()
            if text:
                out.append(text)
        return out

    def _language_directive(self, response_language: Optional[str] = None) -> str:
        """Build a language directive to append to the system prompt.

        The directive itself is written in the target language to keep the entire
        system prompt monolingual — mixed-language prompts cause Qwen to drift to
        Chinese mid-stream on Spanish queries.
        """
        if not response_language:
            return ""
        lang = response_language.strip().lower()
        if lang == "es":
            return "IMPORTANTE: Debes responder SIEMPRE en español. El idioma del estudiante es español. No uses inglés ni otros idiomas en tu respuesta bajo ninguna circunstancia."
        elif lang == "en":
            return "IMPORTANT: You MUST respond in English. The student's preferred language is English."
        return ""

    def _build_retrieval_system_message(self, context: str, response_language: Optional[str] = None, rag_index_language: Optional[str] = None) -> str:
        parts = []
        prompt_language = (response_language or "en").strip().lower()
        is_es = prompt_language == "es"
        base_system_prompt = self.base_system_prompt_es if is_es and self.base_system_prompt_es else self.base_system_prompt
        if base_system_prompt:
            parts.append(base_system_prompt)
        lang_directive = self._language_directive(response_language)
        if lang_directive:
            parts.append(lang_directive)
        retrieval_instruction = self.retrieval_instruction_es if is_es and self.retrieval_instruction_es else self.retrieval_instruction
        parts.append(retrieval_instruction)
        # Add cross-lingual note when the index language differs from the response language
        idx_lang = (rag_index_language or "en").strip().lower()
        if idx_lang == "es" and not is_es:
            parts.append("Note: The retrieved Wikipedia passages below are in Spanish. Read them to extract the relevant facts, then write your response in English.")
        elif idx_lang == "en" and is_es:
            parts.append("Nota: Los pasajes de Wikipedia recuperados están en inglés. Léelos para extraer los hechos relevantes y luego responde en español. NO copies inglés en tu respuesta — tradúcelo siempre.")
        context_heading = "Contexto de Wikipedia" if is_es else "Wikipedia context"
        parts.append(f"\n{context_heading}:\n{str(context or '').strip()}")
        if is_es:
            # Final anchor right before the user message — keeps the model from
            # drifting to Chinese at generation time, which Qwen 2.5 will do when
            # the prompt mixes languages or contains long English context.
            parts.append("Recordatorio final: tu respuesta debe estar 100% en español.")
        return "\n\n".join(parts).strip()

    def _build_prompt_preview(self, messages: List[Dict[str, Any]]) -> str:
        blocks: List[str] = []
        for message in messages:
            role = str(message.get("role", "") or "").strip().upper() or "UNKNOWN"
            content = str(message.get("content", "") or "").strip()
            blocks.append(f"[{role}]\n{content}")
        return "\n\n---\n\n".join(blocks).strip()

    def _summarize_token_context(self, final_messages: List[Dict[str, Any]], latest_user_text: str, retrieval_system_message: Optional[str]) -> Dict[str, Any]:
        final_prompt_tokens = sum(self.estimate_tokens(str(message.get("content", "") or "")) for message in final_messages)
        latest_user = str(latest_user_text or "")
        latest_user_seen = False
        system_prompt_tokens = 0
        history_tokens = 0
        user_prompt_tokens = 0
        rag_chunk_tokens = 0
        other_injected_tokens = 0
        retrieval_prefix = f"{self.retrieval_instruction}\n\nWiki retrieval snippets:\n"

        for message in final_messages:
            role = str(message.get("role", "") or "").strip().lower()
            content = str(message.get("content", "") or "")
            if role == "system":
                if retrieval_system_message and content == retrieval_system_message:
                    if content.startswith(retrieval_prefix):
                        chunk_text = content[len(retrieval_prefix):]
                        other_injected_tokens += self.estimate_tokens(self.retrieval_instruction)
                        other_injected_tokens += self.estimate_tokens("Wiki retrieval snippets:")
                        rag_chunk_tokens += self.estimate_tokens(chunk_text)
                    else:
                        other_injected_tokens += self.estimate_tokens(content)
                else:
                    system_prompt_tokens += self.estimate_tokens(content)
                continue
            if role == "user" and not latest_user_seen and content == latest_user:
                user_prompt_tokens += self.estimate_tokens(content)
                latest_user_seen = True
                continue
            history_tokens += self.estimate_tokens(content)

        remaining_headroom = max(0, self.llama_ctx_size - final_prompt_tokens - self.llama_n_predict)
        estimated_over_budget = (final_prompt_tokens + self.llama_n_predict) > self.llama_ctx_size
        return {
            "total_input_tokens": final_prompt_tokens,
            "final_prompt_tokens": final_prompt_tokens,
            "system_prompt_tokens": system_prompt_tokens,
            "conversation_history_tokens": history_tokens,
            "user_prompt_tokens": user_prompt_tokens,
            "rag_chunk_tokens": rag_chunk_tokens,
            "other_injected_tokens": other_injected_tokens,
            "model_max_context": self.llama_ctx_size,
            "max_output_tokens": self.llama_n_predict,
            "reserved_output_budget": self.llama_n_predict,
            "remaining_context_headroom": remaining_headroom,
            "estimated_over_budget": estimated_over_budget,
            "context_shift_configured": bool(self.llama_context_shift_enabled),
        }

    def _runtime_generation_settings(self) -> Dict[str, Any]:
        return {
            "max_output_tokens": self.llama_n_predict,
            "temperature": self.llama_temperature,
            "top_p": self.llama_top_p,
            "top_k": self.llama_top_k,
            "min_p": self.llama_min_p,
            "repeat_penalty": self.llama_repeat_penalty,
            "seed": self.llama_seed,
            "stop_sequences": self._clone_data(self.llama_stop_sequences),
            "grammar_mode": self.llama_grammar_mode,
            "context_window": self.llama_ctx_size,
            "thread_count": self.llama_threads,
            "thread_batch_count": self.llama_threads_batch,
            "batch_size": self.llama_batch_size,
            "ubatch_size": self.llama_ubatch_size,
            "gpu_layers_offloaded": self.llama_n_gpu_layers,
            "flash_attention": self.llama_flash_attn,
            "context_shift_enabled": bool(self.llama_context_shift_enabled),
            "context_shift_keep": self.llama_context_shift_keep,
            "rope_scaling": self.llama_rope_scaling,
            "yarn_scaling": self.llama_yarn_scaling,
        }

    def _runtime_info_snapshot(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "model_name": summary.get("model_name"),
            "model_file": self.llama_model_file,
            "model_path": self.llama_model_path,
            "quantization": self.llama_model_file,
            "backend_runtime": self.runtime_backend_name,
            "llama_base_url": self.llama_base_url,
            "host_name": self.runtime_host,
            "container_name": self.llama_container_name,
            "cpu_in_use": self.llama_threads,
            "gpu_in_use": True if (self.llama_n_gpu_layers is not None and self.llama_n_gpu_layers > 0) else None,
            "streamed": bool(summary.get("stream")),
            "model_load_success": summary.get("request_error") is None,
            "runtime_warning_text": summary.get("runtime_warning_text"),
            "stop_reason": summary.get("finish_reason"),
        }

    def _bound_diagnostics_value(self, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return {"truncated": True, "reason": "max_depth"}
        if isinstance(value, str):
            if len(value) > self.diagnostics_max_string_chars:
                return {
                    "truncated": True,
                    "original_chars": len(value),
                    "preview": value[: self.diagnostics_max_string_chars].rstrip(),
                }
            return value
        if isinstance(value, list):
            bounded = [self._bound_diagnostics_value(item, depth + 1) for item in value[: self.diagnostics_max_list_items]]
            if len(value) > self.diagnostics_max_list_items:
                bounded.append({"truncated": True, "omitted_items": len(value) - self.diagnostics_max_list_items})
            return bounded
        if isinstance(value, dict):
            return {str(key): self._bound_diagnostics_value(item, depth + 1) for key, item in value.items()}
        return value

    def _bound_diagnostics_payload(self, diagnostics: Dict[str, Any]) -> Dict[str, Any]:
        bounded = self._bound_diagnostics_value(diagnostics)
        if not isinstance(bounded, dict):
            return {"truncated": True, "payload": bounded}
        raw = json.dumps(bounded, ensure_ascii=False, default=str)
        if len(raw.encode("utf-8")) <= self.diagnostics_max_bytes:
            bounded.setdefault("limits", {})
            bounded["limits"].update({
                "truncated": False,
                "max_bytes": self.diagnostics_max_bytes,
                "actual_bytes": len(raw.encode("utf-8")),
            })
            return bounded
        compact = {
            "request_id": diagnostics.get("request_id"),
            "chat_id": diagnostics.get("chat_id"),
            "user_id": diagnostics.get("user_id"),
            "status_level": diagnostics.get("status_level"),
            "general": self._bound_diagnostics_value(diagnostics.get("general") or {}),
            "timing": self._bound_diagnostics_value(diagnostics.get("timing") or {}),
            "rag": {
                "overview": self._bound_diagnostics_value(((diagnostics.get("rag") or {}).get("overview") or {})),
                "chunks": {
                    "retrieval_query": (((diagnostics.get("rag") or {}).get("chunks") or {}).get("retrieval_query")),
                    "chunk_statistics": self._bound_diagnostics_value((((diagnostics.get("rag") or {}).get("chunks") or {}).get("chunk_statistics") or {})),
                },
            },
            "errors_warnings": self._bound_diagnostics_value(diagnostics.get("errors_warnings") or {}),
            "limits": {
                "truncated": True,
                "reason": "max_bytes",
                "max_bytes": self.diagnostics_max_bytes,
                "pre_truncate_bytes": len(raw.encode("utf-8")),
            },
        }
        return self._bound_diagnostics_value(compact)

    def build_user_retrieval_warnings(self, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return a small, non-admin-safe list of soft warnings about retrieval/rerank.

        These let the chat UI render hints like "results may be lower quality"
        when the rerank path falls through to a cosine-only ranking or the
        retrieval times out. They are intentionally coarse — internal error
        strings stay on the admin diagnostics channel.
        """
        out: List[Dict[str, Any]] = []
        rerank_error = summary.get("rerank_error")
        rerank_enabled = bool(summary.get("rerank_enabled"))
        if rerank_error and not rerank_enabled:
            err_str = str(rerank_error)
            kind = "rerank_unavailable"
            if "cross_lingual_fallback" in err_str:
                kind = "rerank_cross_lingual_fallback"
            elif "heuristic fallback" in err_str:
                kind = "rerank_heuristic_fallback"
            out.append({"kind": kind, "detail": "Results ranked without the cross-encoder reranker; quality may be lower."})
        elif rerank_error and rerank_enabled:
            # Soft case: cross-lingual fallback after the reranker ran but scored poorly.
            out.append({"kind": "rerank_cross_lingual_fallback", "detail": "Fell back to distance-based ranking for this query."})
        if summary.get("retrieval_timed_out"):
            out.append({"kind": "retrieval_timeout", "detail": "Retrieval took too long and was skipped for this turn."})
        retrieval_error = summary.get("retrieval_error")
        if retrieval_error and not summary.get("retrieval_timed_out") and retrieval_error not in ("no_relevant_chunks", "missing_user_query", "skipped_non_encyclopedic_query"):
            out.append({"kind": "retrieval_error", "detail": "Encyclopedia lookup failed; answer is from the model only."})
        trunc = int(summary.get("rag_chunk_truncation_count") or 0)
        if trunc > 0:
            out.append({"kind": "chunks_truncated", "detail": f"{trunc} retrieved passage(s) were truncated to fit the context budget.", "count": trunc})
        if summary.get("no_context_answer_mode") and not summary.get("retrieval_timed_out") and not retrieval_error:
            out.append({"kind": "no_context", "detail": "No relevant encyclopedia passages were found; answer is from the model only."})
        return out

    def build_admin_diagnostics(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        warnings = self._flatten_warning_messages(summary.get("warnings") or [])
        final_messages = self._clone_data(summary.get("final_conversation") or summary.get("base_messages") or [])
        base_messages = self._clone_data(summary.get("base_messages") or [])
        retrieval_system_message = summary.get("retrieval_system_message")
        token_context = self._summarize_token_context(final_messages, str(summary.get("raw_user_message") or ""), retrieval_system_message)
        runtime = self._runtime_info_snapshot(summary)
        requested_output_tokens = int(summary.get("completion_tokens") or summary.get("completion_tokens_estimate") or 0)
        request_error = summary.get("request_error")
        fallback_flags = [
            bool(summary.get("rag_fallback_triggered")),
            bool(summary.get("no_context_answer_mode")),
            bool(summary.get("rerank_error")),
        ]
        warning_flags = [flag for flag, enabled in (
            ("continuation_limit_hit", bool(summary.get("continuation_limit_hit"))),
            ("retrieval_timeout", bool(summary.get("retrieval_timed_out"))),
            ("retrieval_error", bool(summary.get("retrieval_error"))),
            ("rerank_fallback", bool(summary.get("rerank_error"))),
            ("rag_chunk_truncated", bool(summary.get("rag_chunk_truncation_count"))),
            ("estimated_context_pressure", bool(token_context.get("estimated_over_budget"))),
        ) if enabled]
        status_level = "error" if request_error else ("warn" if warnings or warning_flags else ("fallback" if any(fallback_flags) else "ok"))
        diagnostics = {
            "request_id": summary.get("request_id"),
            "chat_id": summary.get("chat_id"),
            "user_id": summary.get("user_id"),
            "user_role": summary.get("user_role"),
            "status_level": status_level,
            "general": {
                "request_id": summary.get("request_id"),
                "request_started_at": summary.get("request_started_at"),
                "request_finished_at": summary.get("request_finished_at"),
                "chat_id": summary.get("chat_id"),
                "user_id": summary.get("user_id"),
                "user_role": summary.get("user_role"),
                "model_name": summary.get("model_name"),
                "backend_runtime": self.runtime_backend_name,
                "cold_request": bool(summary.get("cold_request")),
                "warm_request": not bool(summary.get("cold_request")),
                "streamed": bool(summary.get("stream")),
                "rag_used": bool(summary.get("retrieval_used")),
                "rag_enabled": bool(summary.get("retrieval_enabled")),
                "total_generation_time_ms": summary.get("total_ms"),
                "time_to_first_token_ms": summary.get("ttft_ms"),
                "tokens_per_second": summary.get("tps") or summary.get("tps_estimate"),
                "input_tokens": token_context.get("total_input_tokens"),
                "max_context_tokens": token_context.get("model_max_context"),
                "output_tokens": requested_output_tokens,
                "max_output_tokens": self.llama_n_predict,
                "stop_reason": summary.get("finish_reason"),
                "context_shift_configured": token_context.get("context_shift_configured"),
                "truncation_occurred": bool(summary.get("rag_chunk_truncation_count")),
                "estimated_over_budget": bool(token_context.get("estimated_over_budget")),
                "has_warning": bool(warnings or warning_flags),
                "has_error": bool(request_error),
                "has_fallback": any(fallback_flags),
                "status_level": status_level,
            },
            "timing": {
                "model_load_time_ms": None,
                "prompt_construction_time_ms": summary.get("request_prep_ms"),
                "retrieval_time_ms": summary.get("retrieval_ms"),
                "rerank_time_ms": summary.get("rerank_ms"),
                "total_generation_time_ms": summary.get("generation_ms"),
                "total_request_time_ms": summary.get("total_ms"),
                "time_to_first_token_ms": summary.get("ttft_ms"),
                "time_to_first_streamed_chunk_ms": summary.get("first_stream_chunk_ms") or summary.get("ttft_ms"),
                "upstream_open_time_ms": summary.get("upstream_open_ms"),
                "last_token_at": summary.get("last_token_at"),
                "longest_pause_during_stream_ms": summary.get("max_token_gap_ms"),
                "continuation_gap_ms": summary.get("continuation_gap_ms"),
                "queue_wait_ms": None,
                "prompt_eval_time_ms": None,
                "decode_time_ms": summary.get("generation_ms"),
                "stage_trace": self._clone_data(summary.get("stage_trace") or []),
            },
            "token_context": token_context,
            "generation_settings": self._runtime_generation_settings(),
            "runtime": runtime,
            "prompt_construction": {
                "raw_user_message": summary.get("raw_user_message"),
                "normalized_user_message": summary.get("normalized_user_message") or summary.get("raw_user_message"),
                "conversation_history_included": self._clone_data(base_messages[:-1] if len(base_messages) > 1 else []),
                "system_prompts_included": [
                    self._clone_data(message)
                    for message in final_messages
                    if str(message.get("role", "") or "").strip().lower() == "system" and str(message.get("content", "") or "") != str(retrieval_system_message or "")
                ],
                "rag_instruction_block": retrieval_system_message,
                "final_messages": self._clone_data(final_messages),
                "upstream_request_body": self._clone_data(summary.get("upstream_request_body") or {}),
                "assembled_prompt_preview": self._build_prompt_preview(final_messages),
                "final_prompt_tokens": token_context.get("final_prompt_tokens"),
                "max_context_tokens": token_context.get("model_max_context"),
                "prompt_template_version": None,
            },
            "rag": {
                "overview": {
                    "rag_enabled": bool(summary.get("retrieval_enabled")),
                    "retrieval_attempted": bool(summary.get("retrieval_attempted")),
                    "retrieval_used": bool(summary.get("retrieval_used")),
                    "retrieval_skipped_reason": summary.get("retrieval_skipped_reason"),
                    "retrieval_path_loaded": bool(summary.get("retrieval_path_loaded")),
                    "vector_db_collection": summary.get("chroma_collection"),
                    "vector_db_path": summary.get("chroma_persist_dir"),
                    "collection_loaded": bool(summary.get("collection_loaded")),
                    "collection_document_count": summary.get("chroma_count"),
                    "index_manifest": self._clone_data(summary.get("index_manifest")),
                    "index_manifest_path": summary.get("index_manifest_path"),
                    "index_manifest_error": summary.get("index_manifest_error"),
                    "embedding_model_path": summary.get("embed_model_path"),
                    "embedding_dimension": summary.get("embed_dimension"),
                    "query_embedding_dimension": summary.get("embed_dimension"),
                    "dimensions_match": summary.get("dimensions_match"),
                    "retrieval_pipeline_time_ms": summary.get("retrieval_ms"),
                    "top_k_requested": summary.get("retrieval_top_k") or self.retrieval_top_k,
                    "candidate_count": summary.get("retrieval_candidate_count"),
                    "chunks_after_rerank": summary.get("chunks_after_rerank"),
                    "chunks_after_token_budget_trim": summary.get("chunks_after_budget_trim"),
                    "fallback_triggered": bool(summary.get("rag_fallback_triggered")),
                    "no_context_answer_mode": bool(summary.get("no_context_answer_mode")),
                    "retrieval_context_chars": summary.get("retrieval_context_chars"),
                    "retrieval_context_tokens": summary.get("final_context_estimated_tokens"),
                    "primary_citation": self._clone_data(summary.get("primary_citation")),
                },
                "chunks": {
                    "retrieval_query": summary.get("retrieval_query"),
                    "all_retrieved_chunks": self._clone_data(summary.get("retrieval_candidates") or []),
                    "final_injected_chunks": self._clone_data(summary.get("retrieved_chunks") or []),
                    "citations": self._clone_data(summary.get("citations") or []),
                    "final_prompt_with_chunk_injection": retrieval_system_message,
                    "final_prompt_tokens": token_context.get("final_prompt_tokens"),
                    "max_context_tokens": token_context.get("model_max_context"),
                    "chunk_statistics": {
                        "candidate_count": summary.get("retrieval_candidate_count"),
                        "selected_count": summary.get("retrieval_count"),
                        "truncated_chunk_count": summary.get("rag_chunk_truncation_count"),
                    },
                },
                "rerank": {
                    "reranking_working": bool(summary.get("rerank_enabled")),
                    "reranker_available": bool(summary.get("rerank_available")),
                    "reranker_model_path": summary.get("rerank_model_path"),
                    "reranker_device": self.rerank_device,
                    "reranking_time_ms": summary.get("rerank_ms"),
                    "initial_to_final_order": [
                        {
                            "chunk_id": f"{chunk.get('page_id')}:{chunk.get('chunk_index')}",
                            "original_rank": chunk.get("original_rank"),
                            "reranked_rank": chunk.get("reranked_rank"),
                            "relevance_score": chunk.get("relevance_score"),
                            "included": chunk.get("included"),
                            "dropped_reason": chunk.get("dropped_reason"),
                        }
                        for chunk in (summary.get("retrieval_candidates") or [])
                    ],
                    "reranker_failure": summary.get("rerank_error") is not None and not bool(summary.get("rerank_enabled")),
                    "fallback_behavior": summary.get("rerank_error"),
                },
            },
            "errors_warnings": {
                "stage_failed": bool(request_error),
                "failed_stage": summary.get("failed_stage"),
                "exception_message": request_error,
                "timeout_occurred": bool(summary.get("retrieval_timed_out")) or ("timeout" in str(request_error or "").lower()),
                "client_disconnect": False,
                "server_cancellation": False,
                "embedder_unavailable": not bool(summary.get("embed_model_exists")),
                "reranker_unavailable": bool(summary.get("rerank_error")) and not bool(summary.get("rerank_enabled")),
                "retrieval_unavailable": bool(summary.get("retrieval_enabled")) and not bool(summary.get("retrieval_path_loaded")),
                "fallback_mode_used": any(fallback_flags),
                "fallback_reason": summary.get("retrieval_error") or summary.get("rerank_error"),
                "warning_flags": warning_flags,
                "warnings": warnings,
            },
            "resources": {
                "request_start": self._clone_data(summary.get("resource_start") or {}),
                "request_end": self._clone_data(summary.get("resource_end") or {}),
                "ram_start_bytes": (summary.get("resource_start") or {}).get("process_rss_bytes") if isinstance(summary.get("resource_start"), dict) else None,
                "ram_end_bytes": (summary.get("resource_end") or {}).get("process_rss_bytes") if isinstance(summary.get("resource_end"), dict) else None,
                "ram_peak_bytes": None,
                "vram_start_bytes": None,
                "vram_end_bytes": None,
                "vram_peak_bytes": None,
                "cpu_prompt_eval_pct": None,
                "cpu_generation_pct": None,
                "gpu_prompt_eval_pct": None,
                "gpu_generation_pct": None,
                "disk_activity": None,
                "queue_delay_ms": None,
                "resource_unavailable_reason": ((summary.get("resource_start") or {}).get("unavailable_reason") if isinstance(summary.get("resource_start"), dict) else None),
            },
            "warnings": warnings,
        }
        return self._bound_diagnostics_payload(diagnostics)

    def build_admin_metrics(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        diagnostics = self.build_admin_diagnostics(summary)
        flat_keys = (
            "request_id", "request_started_at", "request_finished_at", "chat_id", "user_id", "user_role",
            "ttft_ms", "upstream_open_ms", "max_token_gap_ms", "continuation_gap_ms", "request_prep_ms",
            "total_ms", "generation_ms", "prompt_tokens", "completion_tokens", "total_tokens", "tps",
            "prompt_tokens_estimate", "completion_tokens_estimate", "total_tokens_estimate", "tps_estimate",
            "finish_reason", "continuation_count", "continuation_limit_hit", "retrieval_enabled",
            "retrieval_attempted", "retrieval_used", "retrieval_count", "retrieval_ms", "retrieval_timed_out",
            "retrieval_error", "retrieval_skipped_reason", "retrieval_context_chars", "retrieval_query",
            "retrieval_candidate_count", "retrieval_candidate_k", "retrieval_top_k", "min_relevance_score",
            "chunks_after_rerank", "chunks_after_budget_trim", "rerank_model_path", "rerank_enabled",
            "rerank_available", "rerank_ms", "rerank_error", "embed_model_path", "embed_model_exists",
            "chroma_persist_dir", "chroma_collection", "chroma_count", "embed_dimension", "collection_dimension",
            "index_manifest", "index_manifest_path", "index_manifest_error",
            "dimensions_match", "retrieval_path_loaded", "collection_loaded", "rag_index_language", "rag_collection_path", "rag_fallback_triggered",
            "no_context_answer_mode", "final_context_estimated_tokens", "rag_chunk_truncation_count",
            "startup_rag_ok", "startup_rag_error", "startup_rag_checked_at", "startup_rag_test_query",
            "startup_rag_test_expected_terms", "startup_rag_test_matches", "startup_reranker_ok", "startup_reranker_error", "retrieved_chunks",
            "citations", "primary_citation",
            "retrieval_candidates", "raw_user_message", "normalized_user_message", "request_error",
            "failed_stage", "stage_trace", "status_level"
        )
        flat = {key: summary.get(key) for key in flat_keys}
        flat["admin_diagnostics_available"] = True
        flat["admin_diagnostics_version"] = 2
        flat["general"] = diagnostics.get("general")
        return flat
    def _collection_dimension_from_metadata(self, metadata: Any) -> Optional[int]:
        if not isinstance(metadata, dict):
            return None
        for key in ("dimension", "embedding_dimension", "embed_dimension"):
            value = metadata.get(key)
            try:
                dim = int(value)
            except Exception:
                continue
            if dim > 0:
                return dim
        return None

    def _load_retriever_runtime(self) -> Dict[str, Any]:
        """Load the embedder, Chroma collection, and collection-dimension metadata."""
        embed_model_exists = Path(self.embed_model).exists()
        chroma_exists = self.chroma_persist_dir.exists()
        rerank_available = self._reranker is not None or Path(self.rerank_model).is_dir()
        if not embed_model_exists:
            raise FileNotFoundError(f"Path {self.embed_model} not found")
        if not chroma_exists:
            raise FileNotFoundError(f"Path {self.chroma_persist_dir} not found")
        try:
            import chromadb
            from chromadb.config import Settings
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            raise RuntimeError(f"retrieval dependencies unavailable: {type(exc).__name__}: {exc}") from exc

        with self._retriever_init_lock:
            if self._embedder is None:
                try:
                    self._embedder = SentenceTransformer(
                        self.embed_model,
                        device=self.retrieval_device,
                        local_files_only=True,
                    )
                except TypeError:
                    self._embedder = SentenceTransformer(
                        self.embed_model,
                        device=self.retrieval_device,
                    )
            if self._chroma_client is None:
                self._chroma_client = chromadb.PersistentClient(
                    path=str(self.chroma_persist_dir),
                    settings=Settings(anonymized_telemetry=False),
                )
            if self._wiki_collection is None:
                self._wiki_collection = self._chroma_client.get_collection(self.chroma_collection)

            embedder = self._embedder
            client = self._chroma_client
            collection = self._wiki_collection
            test_vec = embedder.encode(["startup_dim_check"], normalize_embeddings=True)
            if not len(test_vec) or not len(test_vec[0]):
                raise RuntimeError("retrieval embedder returned an empty vector")
            embed_dimension = int(len(test_vec[0]))
            collection_metadata = getattr(collection, "metadata", None) or {}
            collection_dimension = self._collection_dimension_from_metadata(collection_metadata)
            manifest, manifest_path, manifest_error = self._load_index_manifest(self.chroma_persist_dir)
            count = int(collection.count())
            if count <= 0:
                raise RuntimeError(f"collection {self.chroma_collection} is empty")
            if collection_dimension is not None and collection_dimension != embed_dimension:
                raise RuntimeError(
                    f"embedding dimension mismatch: embedder={embed_dimension} collection={collection_dimension}"
                )
            manifest_summary = self._manifest_summary(manifest)
            if manifest_summary:
                manifest_dim = manifest_summary.get("embedding_dimension")
                if manifest_dim is not None and int(manifest_dim) != embed_dimension:
                    raise RuntimeError(
                        f"manifest embedding dimension mismatch: manifest={manifest_dim} embedder={embed_dimension}"
                    )
                manifest_count = manifest_summary.get("chunk_count")
                if manifest_count is not None and int(manifest_count) != count:
                    raise RuntimeError(
                        f"manifest chunk count mismatch: manifest={manifest_count} collection={count}"
                    )
            self._embed_dimension = embed_dimension
            self._collection_dimension = collection_dimension
            self._chroma_count = count
            self._index_manifest = manifest_summary
            # Spanish collection is loaded lazily on first Spanish-user request (see _ensure_wiki_retriever_es)
            self._update_rag_status(
                embed_model_path=str(self.embed_model),
                embed_model_exists=embed_model_exists,
                chroma_persist_dir=str(self.chroma_persist_dir),
                chroma_persist_exists=chroma_exists,
                chroma_collection=self.chroma_collection,
                chroma_count=count,
                embed_dimension=embed_dimension,
                collection_dimension=collection_dimension,
                index_manifest=manifest_summary,
                index_manifest_path=manifest_path,
                index_manifest_error=manifest_error,
                rerank_model_path=str(self.rerank_model),
                rerank_available=rerank_available,
            )
            return {
                "embedder": embedder,
                "client": client,
                "collection": collection,
                "embed_dimension": embed_dimension,
                "collection_dimension": collection_dimension,
                "count": count,
                "rerank_available": rerank_available,
            }

    def _run_startup_retrieval_test(self, query: str, runtime: Dict[str, Any]) -> Dict[str, Any]:
        candidates = self.retrieve_wiki_chunks(query, limit=max(20, self.retrieval_candidate_k))
        ranked, rerank_enabled, rerank_ms, rerank_error = self.rerank_wiki_chunks(query, candidates)
        matches = self._serialize_debug_chunks(ranked[:3])
        return {
            "matches": matches,
            "candidate_count": len(candidates),
            "rerank_enabled": rerank_enabled,
            "rerank_ms": rerank_ms,
            "rerank_error": rerank_error,
        }

    def validate_startup_rag(self) -> None:
        """Run the startup retrieval smoke test required for production readiness."""
        checked_at = self.now_iso()
        smoke_query = "What was the War of 1812?"
        expected_terms = ["war", "1812"]
        try:
            runtime = self._load_retriever_runtime()
            rerank_error = self.ensure_reranker()
            if rerank_error is not None or self._reranker is None:
                raise RuntimeError(f"reranker unavailable: {rerank_error or 'failed_to_initialize'}")
            runtime["rerank_available"] = True
            smoke = self._run_startup_retrieval_test(smoke_query, runtime)
            matches = smoke["matches"]
            self._validate_smoke_matches(matches, expected_terms, "en")
            if not smoke["rerank_enabled"]:
                raise RuntimeError(f"startup rerank test did not enable reranker: {smoke['rerank_error'] or 'unknown'}")
            self._update_rag_status(
                startup_rag_ok=True,
                startup_rag_error=None,
                startup_rag_checked_at=checked_at,
                startup_rag_test_query=smoke_query,
                startup_rag_test_expected_terms=expected_terms,
                startup_rag_test_matches=matches,
                startup_reranker_ok=True,
                startup_reranker_error=None,
                rerank_model_path=str(self.rerank_model),
                rerank_available=True,
            )
            logger.info("embedding model found at %s", self.embed_model)
            logger.info("reranker model found at %s", self.rerank_model)
            logger.info("vector store loaded from %s", self.chroma_persist_dir)
            logger.info(
                "collection %s count=%s embed_dimension=%s collection_dimension=%s rerank_available=%s",
                self.chroma_collection,
                runtime["count"],
                runtime["embed_dimension"],
                runtime["collection_dimension"],
                runtime["rerank_available"],
            )
            logger.info(
                "startup rerank test enabled=%s rerank_ms=%s candidate_count=%s",
                smoke["rerank_enabled"],
                smoke["rerank_ms"],
                smoke["candidate_count"],
            )
            logger.info("startup retrieval test query=%r matches=%s", smoke_query, json.dumps(matches, ensure_ascii=False))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._update_rag_status(
                startup_rag_ok=False,
                startup_rag_error=error,
                startup_rag_checked_at=checked_at,
                startup_rag_test_query=smoke_query,
                startup_rag_test_expected_terms=expected_terms,
                startup_rag_test_matches=[],
                startup_reranker_ok=False,
                startup_reranker_error=error,
                embed_model_path=str(self.embed_model),
                embed_model_exists=Path(self.embed_model).exists(),
                chroma_persist_dir=str(self.chroma_persist_dir),
                chroma_persist_exists=self.chroma_persist_dir.exists(),
                chroma_collection=self.chroma_collection,
                chroma_count=int(self._chroma_count or 0),
                embed_dimension=self._embed_dimension,
                collection_dimension=self._collection_dimension,
                index_manifest=self._index_manifest,
                index_manifest_path=str(self._index_manifest_path(self.chroma_persist_dir)),
                index_manifest_error=None if self._index_manifest else "manifest_unavailable",
                rerank_model_path=str(self.rerank_model),
                rerank_available=bool(self._reranker is not None),
            )
            logger.error("startup RAG validation failed: %s", error)
            if self._rag_required_at_startup():
                raise RuntimeError(error) from exc

    def validate_startup_rag_es(self) -> None:
        """Spanish-first variant of the startup smoke test. Used when
        WARMUP_EN_AT_STARTUP=0 — validates the Spanish collection (already
        kicked off as eager-load) instead of the English one, then marks
        startup_rag_ok=True so the portal loading screen lifts. The English
        collection stays cold until the first English request triggers
        ensure_wiki_retriever()."""
        checked_at = self.now_iso()
        smoke_query = "¿Quién fue Simón Bolívar?"
        expected_terms = ["bolívar"]
        try:
            self._ensure_wiki_retriever_es()
            if self._wiki_collection_es is None:
                raise RuntimeError("Spanish ChromaDB not loaded (still warming or path missing)")
            spanish_count = int(self._wiki_collection_es.count())
            manifest, manifest_path, manifest_error = self._load_index_manifest(self.chroma_persist_dir_es)
            manifest_summary = self._manifest_summary(manifest)
            # Load embedder without touching the English chroma client (it
            # stays cold until the first English request).
            if self._embedder is None:
                from sentence_transformers import SentenceTransformer
                with self._retriever_init_lock:
                    if self._embedder is None:
                        try:
                            self._embedder = SentenceTransformer(
                                self.embed_model,
                                device=self.retrieval_device,
                                local_files_only=True,
                            )
                        except TypeError:
                            self._embedder = SentenceTransformer(
                                self.embed_model,
                                device=self.retrieval_device,
                            )
            try:
                test_vec = self._embedder.encode(["startup_dim_check"], normalize_embeddings=True)
            except Exception as exc:
                raise RuntimeError(f"retrieval embedder failed test encode: {exc}") from exc
            if not len(test_vec) or not len(test_vec[0]):
                raise RuntimeError("retrieval embedder returned an empty vector")
            embed_dimension = int(len(test_vec[0]))
            collection_metadata = getattr(self._wiki_collection_es, "metadata", None) or {}
            collection_dimension = self._collection_dimension_from_metadata(collection_metadata)
            if collection_dimension is not None and collection_dimension != embed_dimension:
                raise RuntimeError(
                    f"embedding dimension mismatch (es): embedder={embed_dimension} collection={collection_dimension}"
                )
            self._embed_dimension = embed_dimension
            self._collection_dimension = collection_dimension
            rerank_error = self.ensure_reranker()
            if rerank_error is not None or self._reranker is None:
                raise RuntimeError(f"reranker unavailable: {rerank_error or 'failed_to_initialize'}")
            candidates = self.retrieve_wiki_chunks(
                smoke_query,
                limit=max(20, self.retrieval_candidate_k),
                user_language="es",
            )
            ranked, rerank_enabled, rerank_ms, rerank_error = self.rerank_wiki_chunks(smoke_query, candidates)
            matches = self._serialize_debug_chunks(ranked[:3])
            self._validate_smoke_matches(matches, expected_terms, "es")
            if not rerank_enabled:
                raise RuntimeError(f"startup rerank test (es) did not enable reranker: {rerank_error or 'unknown'}")
            self._update_rag_status(
                startup_rag_ok=True,
                startup_rag_error=None,
                startup_rag_checked_at=checked_at,
                startup_rag_test_query=smoke_query,
                startup_rag_test_expected_terms=expected_terms,
                startup_rag_test_matches=matches,
                startup_reranker_ok=True,
                startup_reranker_error=None,
                embed_model_path=str(self.embed_model),
                embed_model_exists=Path(self.embed_model).exists(),
                embed_dimension=self._embed_dimension,
                collection_dimension=self._collection_dimension,
                chroma_persist_dir=str(self.chroma_persist_dir_es),
                chroma_persist_exists=self.chroma_persist_dir_es.exists(),
                chroma_collection=self.chroma_collection_es,
                chroma_count=spanish_count,
                index_manifest=manifest_summary,
                index_manifest_path=manifest_path,
                index_manifest_error=manifest_error,
                rerank_model_path=str(self.rerank_model),
                rerank_available=True,
            )
            logger.info(
                "Spanish startup retrieval test query=%r matches=%s",
                smoke_query, json.dumps(matches, ensure_ascii=False),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._update_rag_status(
                startup_rag_ok=False,
                startup_rag_error=error,
                startup_rag_checked_at=checked_at,
                startup_rag_test_query=smoke_query,
                startup_rag_test_expected_terms=expected_terms,
                startup_rag_test_matches=[],
                startup_reranker_ok=False,
                startup_reranker_error=error,
                embed_model_path=str(self.embed_model),
                embed_model_exists=Path(self.embed_model).exists(),
                chroma_persist_dir=str(self.chroma_persist_dir_es),
                chroma_persist_exists=self.chroma_persist_dir_es.exists(),
                chroma_collection=self.chroma_collection_es,
                chroma_count=0,
                embed_dimension=self._embed_dimension,
                collection_dimension=self._collection_dimension,
                index_manifest=self._index_manifest_es,
                index_manifest_path=str(self._index_manifest_path(self.chroma_persist_dir_es)),
                index_manifest_error=None if self._index_manifest_es else "manifest_unavailable",
                rerank_model_path=str(self.rerank_model),
                rerank_available=bool(self._reranker is not None),
            )
            logger.error("startup RAG validation (es) failed: %s", error)

    def run_warmup_queries(self) -> None:
        """Run diverse warm-up queries to keep the HNSW index and embedding model hot."""
        warmup_queries = [
            "What is photosynthesis?",
            "Quien fue Simon Bolivar?",
            "How does the water cycle work?",
            "What are the planets in the solar system?",
        ]
        for query in warmup_queries:
            try:
                self.prepare_wiki_context(query)
                logger.info("warmup query completed: %r", query)
            except Exception as exc:
                logger.warning("warmup query failed: %r -> %s", query, exc)

    def keep_warm_loop(self) -> None:
        """Periodically run a retrieval query to keep the HNSW index and models warm."""
        warmup_queries = [
            "What is gravity?",
            "Que es la democracia?",
            "How do volcanoes form?",
            "What is the Amazon rainforest?",
        ]
        idx = 0
        while True:
            time.sleep(180)
            try:
                query = warmup_queries[idx % len(warmup_queries)]
                self.prepare_wiki_context(query)
                logger.debug("keep-warm query completed: %r", query)
            except Exception as exc:
                logger.warning("keep-warm query failed: %s", exc)
            idx += 1

    def ensure_wiki_retriever(self) -> None:
        if self.rag_spanish_only:
            # Spanish-only mode: only the embedder is needed for query-time
            # encoding; the English Chroma client stays closed for the life
            # of the process. retrieve_wiki_chunks() routes everything to the
            # Spanish collection.
            self._ensure_embedder_only()
            return
        if self._embedder is not None and self._wiki_collection is not None:
            return
        self._load_retriever_runtime()

    def _ensure_embedder_only(self) -> None:
        """Load the bge-m3 embedder without touching the English Chroma
        client. Used in Spanish-only mode so we never pay the cost of opening
        the English index. Safe to call repeatedly (idempotent under lock)."""
        if self._embedder is not None:
            return
        embed_model_exists = Path(self.embed_model).exists()
        if not embed_model_exists:
            raise FileNotFoundError(f"Path {self.embed_model} not found")
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            raise RuntimeError(f"retrieval dependencies unavailable: {type(exc).__name__}: {exc}") from exc
        with self._retriever_init_lock:
            if self._embedder is not None:
                return
            try:
                self._embedder = SentenceTransformer(
                    self.embed_model,
                    device=self.retrieval_device,
                    local_files_only=True,
                )
            except TypeError:
                self._embedder = SentenceTransformer(
                    self.embed_model,
                    device=self.retrieval_device,
                )

    def _kick_off_wiki_retriever_es_load(self) -> None:
        """Start a background load of the Spanish ChromaDB if not already loaded
        or loading. Returns immediately so callers don't block on a multi-minute
        cold-load."""
        if self._wiki_collection_es is not None:
            return
        if not self.chroma_persist_dir_es.exists():
            return
        # Cheap, non-blocking check first
        if self._es_load_started:
            return
        with self._retriever_init_lock_es:
            if self._es_load_started or self._wiki_collection_es is not None:
                return
            self._es_load_started = True
        threading.Thread(
            target=self._ensure_wiki_retriever_es,
            daemon=True,
            name="app-storage-es-load",
        ).start()

    def _ensure_wiki_retriever_es(self) -> None:
        """Load the Spanish ChromaDB collection. Called from the background
        startup thread or from the kickoff helper — never inside a request path
        because the cold-load takes minutes on the 12 GB HNSW."""
        if self._wiki_collection_es is not None:
            return
        if not self.chroma_persist_dir_es.exists():
            logger.info("Spanish ChromaDB path does not exist, skipping: %s", self.chroma_persist_dir_es)
            return
        try:
            import chromadb
            from chromadb.config import Settings
            t0 = time.perf_counter()
            with self._retriever_init_lock_es:
                if self._wiki_collection_es is not None:
                    return  # another thread beat us here
                if self._chroma_client_es is None:
                    logger.info("Loading Spanish ChromaDB from %s (cold load can take 1–3 minutes for the 2.8M-chunk index)...", self.chroma_persist_dir_es)
                    self._chroma_client_es = chromadb.PersistentClient(
                        path=str(self.chroma_persist_dir_es),
                        settings=Settings(anonymized_telemetry=False),
                    )
                collection = self._chroma_client_es.get_collection(self.chroma_collection_es)
                count = int(collection.count())
                manifest, manifest_path, manifest_error = self._load_index_manifest(self.chroma_persist_dir_es)
                manifest_summary = self._manifest_summary(manifest)
                if manifest_summary:
                    manifest_count = manifest_summary.get("chunk_count")
                    if manifest_count is not None and int(manifest_count) != count:
                        raise RuntimeError(
                            f"Spanish manifest chunk count mismatch: manifest={manifest_count} collection={count}"
                        )
                self._index_manifest_es = manifest_summary
                self._wiki_collection_es = collection
                if self._startup_rag_status.get("chroma_persist_dir") == str(self.chroma_persist_dir_es):
                    self._update_rag_status(
                        chroma_count=count,
                        index_manifest=manifest_summary,
                        index_manifest_path=manifest_path,
                        index_manifest_error=manifest_error,
                    )
                logger.info("Spanish ChromaDB loaded: %s (%d chunks) in %.1fs", self.chroma_collection_es, count, time.perf_counter() - t0)
        except Exception as exc:
            self._chroma_client_es = None
            self._wiki_collection_es = None
            self._es_load_started = False  # allow retry on next request
            logger.warning("Spanish ChromaDB unavailable, falling back to English index: %s", exc)

    def ensure_reranker(self) -> Optional[str]:
        if self._reranker is not None:
            return None
        model_path = Path(self.rerank_model)
        if not model_path.exists():
            logger.warning("reranker model not found at %s", self.rerank_model)
            return f"FileNotFoundError: Path {self.rerank_model} not found"
        with self._reranker_init_lock:
            if self._reranker is not None:
                return None
            try:
                from sentence_transformers import CrossEncoder
            except Exception as exc:
                logger.warning("reranker dependencies unavailable: %s", exc)
                return f"RuntimeError: rerank dependencies unavailable: {type(exc).__name__}: {exc}"
            try:
                self._reranker = CrossEncoder(
                    self.rerank_model,
                    device=self.rerank_device,
                    local_files_only=True,
                )
            except TypeError:
                self._reranker = CrossEncoder(
                    self.rerank_model,
                    device=self.rerank_device,
                )
            except Exception as exc:
                logger.warning("reranker failed to load from %s: %s", self.rerank_model, exc)
                return f"{type(exc).__name__}: {exc}"
            logger.info("reranker loaded successfully from %s on device=%s", self.rerank_model, self.rerank_device)
        return None

    def _compact_text(self, value: Any, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if limit > 0 and len(text) > limit:
            return text[:limit].rstrip()
        return text

    def _request_base_url(self, req: Request) -> str:
        forwarded_proto = str(req.headers.get("x-forwarded-proto") or "").strip()
        forwarded_host = str(req.headers.get("x-forwarded-host") or req.headers.get("host") or "").strip()
        if forwarded_proto and forwarded_host:
            return f"{forwarded_proto}://{forwarded_host}".rstrip("/")
        return str(req.base_url).rstrip("/")

    def _kiwix_article_exists(self, language: str, page_title: str) -> bool:
        """Probe the appropriate Kiwix container to confirm an article is in the ZIM.

        Returns True if the link is safe to expose to the user, False otherwise.
        Failures (timeout, DNS, 5xx) fail-OPEN — we'd rather show a working link
        with the slim chance of one occasional 404 than suppress every citation
        when Kiwix is briefly slow.

        Kiwix URL scheme (kiwix-serve with --urlRootLocation=/wiki/{lang}):
            /wiki/{lang}/content/{book}/A/{slug}
        302 if the article exists (redirects to the canonical /{slug} URL),
        404 if not. Slug = title with spaces → '_', URL-escaped.
        """
        if not self.wiki_link_verify_enabled:
            return True
        title = (page_title or "").strip()
        if not title:
            return False
        lang = "es" if str(language or "").strip().lower() == "es" else "en"
        cache_key = (lang, title.lower())
        with self._wiki_exists_lock:
            cached = self._wiki_exists_cache.get(cache_key)
            if cached is not None:
                # touch for LRU
                self._wiki_exists_cache.move_to_end(cache_key)
                return cached
        base = self.kiwix_base_es if lang == "es" else self.kiwix_base_en
        book = self.kiwix_book_es if lang == "es" else self.kiwix_book_en
        if not base or not book:
            return True  # not configured — fail open
        slug = quote(title.replace(" ", "_"), safe="_-:.()")
        url = f"{base}/wiki/{lang}/content/{book}/A/{slug}"
        exists: Optional[bool] = None
        try:
            req = UrllibRequest(url, method="HEAD")
            # No-follow opener so a 302 is treated as "article exists" without
            # paying for the second hop. urllib follows redirects by default.
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def http_error_302(self, request, fp, code, msg, headers):
                    err = urllib.error.HTTPError(request.full_url, code, msg, headers, fp)
                    err.fp = fp
                    raise err
                http_error_301 = http_error_303 = http_error_307 = http_error_300 = http_error_302
            opener = urllib.request.build_opener(_NoRedirect)
            try:
                with opener.open(req, timeout=self.kiwix_probe_timeout) as resp:
                    exists = 200 <= resp.status < 400
            except urllib.error.HTTPError as e:
                # 302 == article exists (redirect to canonical content URL).
                # 404 == article not in this ZIM. Everything else: fail open.
                if 300 <= e.code < 400:
                    exists = True
                elif e.code == 404:
                    exists = False
                else:
                    exists = True
        except Exception:
            # Network error / DNS / timeout — fail open. Don't poison the cache.
            return True
        with self._wiki_exists_lock:
            self._wiki_exists_cache[cache_key] = bool(exists)
            self._wiki_exists_cache.move_to_end(cache_key)
            while len(self._wiki_exists_cache) > self.kiwix_probe_cache_max:
                self._wiki_exists_cache.popitem(last=False)
        return bool(exists)

    def _build_wiki_citation(self, title: Any, base_url: Optional[str], wiki_language: Optional[str] = None) -> Optional[Dict[str, str]]:
        page_title = self._compact_text(title, 240)
        if not page_title:
            return None
        root = str(base_url or "").rstrip("/")
        language = normalize_language_preference(wiki_language, default="en")
        encoded_title = quote(page_title, safe="")
        wiki_url = f"{root}/wiki/{language}/viewer#wiki/{encoded_title}" if root else f"/wiki/{language}/viewer#wiki/{encoded_title}"
        verified = self._kiwix_article_exists(language, page_title)
        # If the article isn't in the ZIM, blank wiki_url. The portal frontend
        # filters out citations with empty wiki_url, so dead links never reach
        # the student. Preserve page_title so the citation is still countable.
        return {
            "page_title": page_title,
            "wiki_url": wiki_url if verified else "",
            "label": f"Wikipedia: {page_title}",
        }

    def _attach_chunk_citation(self, chunk: Dict[str, Any], base_url: Optional[str]) -> Dict[str, Any]:
        entry = dict(chunk or {})
        citation = self._build_wiki_citation(
            entry.get("title") or entry.get("source_document"),
            base_url,
            wiki_language=entry.get("rag_index_language"),
        )
        if citation is None:
            return entry
        entry["page_title"] = citation["page_title"]
        entry["wiki_url"] = citation["wiki_url"]
        entry["wiki_label"] = citation["label"]
        entry["citation"] = self._clone_data(citation)
        return entry

    def _decorate_chunk_list_with_citations(self, chunks: Any, base_url: Optional[str]) -> List[Dict[str, Any]]:
        if not isinstance(chunks, list):
            return []
        return [self._attach_chunk_citation(chunk, base_url) for chunk in chunks if isinstance(chunk, dict)]

    def _citations_from_chunks(self, chunks: Any, base_url: Optional[str], limit: Optional[int] = None) -> List[Dict[str, str]]:
        citations: List[Dict[str, str]] = []
        seen_titles: set = set()
        if not isinstance(chunks, list):
            return citations
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            citation = self._build_wiki_citation(
                chunk.get("title") or chunk.get("source_document"),
                base_url,
                wiki_language=chunk.get("rag_index_language"),
            )
            if citation is None:
                continue
            title_key = citation["page_title"].strip().lower()
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            citations.append(citation)
            if limit is not None and len(citations) >= max(1, int(limit)):
                break
        return citations

    def _chunk_debug_entry(self, chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        meta = chunk.get("meta") or {}
        body = str(chunk.get("doc") or "").strip()
        if not body:
            return None
        return {
            "title": meta.get("title") or None,
            "section_title": meta.get("section_title") or None,
            "section_path": meta.get("section_path") or None,
            "page_id": meta.get("page_id"),
            "chunk_index": meta.get("chunk_index"),
            "distance": chunk.get("distance"),
            "relevance_score": chunk.get("relevance_score"),
            "token_estimate": chunk.get("token_estimate") or self.estimate_tokens(body),
            "original_rank": chunk.get("original_rank"),
            "reranked_rank": chunk.get("reranked_rank"),
            "included": bool(chunk.get("included")),
            "inclusion_decision": "included" if chunk.get("included") else "dropped",
            "dropped_reason": chunk.get("dropped_reason"),
            "duplicate_removed": bool(chunk.get("duplicate_removed")),
            "truncated": bool(chunk.get("truncated")),
            "prompt_block": chunk.get("prompt_block"),
            "prompt_block_tokens_estimate": chunk.get("prompt_block_tokens_estimate"),
            "source_document": meta.get("title") or None,
            "source_metadata": self._clone_data(meta),
            "preview": self._compact_text(body, self.retrieval_preview_chars),
            "rag_index_language": chunk.get("rag_index_language"),
        }

    def _serialize_debug_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        serialized: List[Dict[str, Any]] = []
        for chunk in chunks:
            entry = self._chunk_debug_entry(chunk)
            if entry is not None:
                serialized.append(entry)
        return serialized
    def _informative_terms(self, text: str) -> List[str]:
        stopwords = {
            # English
            "about", "after", "also", "an", "and", "are", "been", "being", "between", "both", "but",
            "can", "could", "did", "does", "for", "from", "had", "has", "have", "her", "here", "him",
            "his", "how", "into", "its", "just", "more", "most", "much", "not", "now", "off", "onto",
            "our", "out", "over", "she", "should", "some", "than", "that", "the", "their", "them",
            "then", "there", "these", "they", "this", "those", "through", "under", "very", "was", "were",
            "what", "when", "where", "which", "while", "who", "with", "would", "your",
            # Spanish
            "como", "con", "cual", "cuando", "del", "ella", "ellos", "entre", "era", "esa", "ese",
            "eso", "esta", "estas", "este", "esto", "estos", "fue", "han", "hay", "las", "les",
            "los", "mas", "muy", "nos", "otra", "otro", "para", "pero", "por", "que", "ser",
            "sin", "sobre", "son", "sus", "tiene", "toda", "todo", "una", "uno", "unos",
        }
        return [
            token
            for token in re.findall(r"[A-Za-z\u00C0-\u024F0-9']+", str(text or "").lower())
            if len(token) > 2 and token not in stopwords
        ]

    def _extract_topic_hint(self, messages: List[Dict[str, str]]) -> str:
        generic = {
            # English
            "president", "country", "economy", "government", "history", "leader", "leaders", "policy",
            "policies", "person", "people", "state", "states", "topic", "topics", "world",
            # Spanish
            "presidente", "pais", "economia", "gobierno", "historia", "lider", "lideres", "politica",
            "persona", "personas", "estado", "estados", "tema", "temas", "mundo",
        }
        for message in reversed(messages):
            content = self._compact_text(message.get("content", ""), 220)
            if not content:
                continue
            for pattern in (
                # English
                r"\b(?:who|what|where|when)\s+(?:is|was|were|are)\s+([^?.!,]+)",
                r"\babout\s+([^?.!,]+)",
                # Spanish
                r"\b(?:qui[eé]n|qu[eé]|d[oó]nde|cu[aá]ndo)\s+(?:es|fue|era|son|eran)\s+([^?.!,]+)",
                r"\bsobre\s+([^?.!,]+)",
                r"\bacerca\s+de\s+([^?.!,]+)",
                # Quoted terms
                r'"([^"]{3,80})"',
            ):
                match = re.search(pattern, content, flags=re.IGNORECASE)
                if not match:
                    continue
                candidate = self._compact_text(match.group(1), 80).strip(" .,:;!?\"'")
                words = [word.lower() for word in candidate.split() if word]
                if not candidate or (words and all(word in generic for word in words)):
                    continue
                return candidate
            for candidate in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4})\b", content):
                normalized = self._compact_text(candidate, 80).strip(" .,:;!?")
                words = [word.lower() for word in normalized.split() if word]
                if not normalized or (words and all(word in generic for word in words)):
                    continue
                return normalized
        return ""

    def _is_contextual_followup(self, text: str) -> bool:
        words = re.findall(r"[A-Za-z\u00C0-\u024F0-9']+", str(text or "").lower())
        if not words:
            return False
        referential_terms = {
            # English
            "he", "her", "hers", "him", "his", "it", "its", "itself", "she", "that", "their", "them",
            "they", "this", "those", "these", "then", "there", "what", "which", "who", "whom", "why", "how",
            # Spanish
            "el", "ella", "ellos", "ellas", "ese", "esa", "eso", "esos", "esas",
            "este", "esta", "esto", "estos", "estas", "aquel", "aquella",
            "quien", "cual", "donde", "cuando", "porque",
        }
        return any(word in referential_terms for word in words)

    def _should_skip_retrieval(self, text: str) -> bool:
        compact = self._compact_text(text, self.retrieval_query_max_chars)
        lowered = compact.lower()
        if not lowered:
            logger.debug("skip_retrieval: empty query")
            return True

        # Skip pure greetings (English and Spanish)
        _GREETING_PHRASES = {
            "hi", "hello", "hey", "hola", "buenos dias", "buenas tardes",
            "buenas noches", "gracias", "thank you", "thanks", "bye",
            "adios", "chao", "como estas", "how are you", "good morning",
            "good afternoon", "good evening", "ok", "okay", "yes", "no", "si",
            "por favor", "de nada", "hasta luego", "buen dia", "que tal",
        }
        stripped = re.sub(r"[^\w\s]", "", lowered).strip()
        if stripped in _GREETING_PHRASES:
            logger.debug("skip_retrieval: greeting detected")
            return True

        # Skip pure math expressions (e.g. "2+2", "5*3+1")
        math_stripped = re.sub(r"\s", "", lowered)
        if re.fullmatch(r"[\d+\-*/^().=<>%]+", math_stripped) and len(math_stripped) >= 2:
            logger.debug("skip_retrieval: pure math expression")
            return True

        # Skip "what is 2+2" style math queries (supports Spanish math words)
        math_query = re.sub(r"^(what\s+is|what[''\u2019]s|cuanto\s+es|cu[aá]nto\s+es|calcula|calculate|solve|resuelve|suma|multiplica)\s+", "", lowered).strip()
        # Convert Spanish math words to operators before checking
        math_query_norm = re.sub(r"\bpor\b", "*", math_query)
        math_query_norm = re.sub(r"\bmas\b|\bm[aá]s\b", "+", math_query_norm)
        math_query_norm = re.sub(r"\bmenos\b", "-", math_query_norm)
        math_query_norm = re.sub(r"\bentre\b|\bdividido\b", "/", math_query_norm)
        math_query_stripped = re.sub(r"\s", "", math_query_norm)
        if math_query != lowered and re.fullmatch(r"[\d+\-*/^().=<>%]+", math_query_stripped) and len(math_query_stripped) >= 2:
            logger.debug("skip_retrieval: math query")
            return True

        # Skip very short messages with no question indicators
        # But allow single topic words (>= 5 chars) like "mitochondria", "photosynthesis"
        words = set(re.findall(r"[A-Za-z0-9']+", lowered))
        _QUESTION_WORDS = {"what", "who", "where", "when", "why", "how", "explain",
                           "que", "quien", "donde", "cuando", "por", "como", "cual"}
        longest_word = max((len(w) for w in words), default=0)
        if len(words) < 3 and "?" not in compact and not (words & _QUESTION_WORDS) and longest_word < 5:
            logger.debug("skip_retrieval: short non-question")
            return True

        # Skip personal/subjective queries (existing logic)
        personal_words = {
            "i", "me", "my", "mine", "we", "us", "our", "ours",
            "yo", "mi", "mis", "nosotros", "nuestro", "nuestra",
        }
        subjective_phrases = (
            # English
            "what is my",
            "what's my",
            "what are my",
            "who am i",
            "should i",
            "can you help me",
            "for me",
            "favorite",
            "prefer",
            "preference",
            "my opinion",
            "my plan",
            "my homework",
            "my notes",
            "my current",
            # Spanish
            "cual es mi",
            "quien soy",
            "puedes ayudarme",
            "para mi",
            "mi opinion",
            "mi plan",
            "mi tarea",
            "mis notas",
            "mi actual",
        )
        is_personal = bool(words & personal_words) and any(phrase in lowered for phrase in subjective_phrases)
        if not is_personal:
            return False
        has_topic_nouns = bool(re.search(r"[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*", compact))
        if has_topic_nouns:
            logger.debug("skip_retrieval: personal query but topic nouns detected, proceeding with retrieval")
            return False
        logger.debug("skip_retrieval: personal/subjective query with no topic nouns, skipping")
        return True

    def build_retrieval_query(self, messages: List[Dict[str, str]]) -> str:
        """Build the wiki search query from the latest user turn plus nearby context."""
        user_messages = [
            self._compact_text(message.get("content", ""), self.retrieval_query_max_chars)
            for message in messages
            if str(message.get("role", "")).strip().lower() == "user"
        ]
        latest = next((message for message in reversed(user_messages) if message), "")
        if not latest:
            return ""
        if len([message for message in user_messages if message]) <= 1:
            # Expand very short queries (1-2 words) for better embedding signal
            words = latest.split()
            if 1 <= len(words) <= 2 and not self._should_skip_retrieval(latest):
                latest = f"Explain {latest}"
            return latest
        contextual = self._is_contextual_followup(latest)
        prior_user = ""
        for message in reversed(user_messages[:-1]):
            if message and message != latest:
                prior_user = self._compact_text(message, 160)
                break
        topic_hint = self._extract_topic_hint(messages[:-1] if len(messages) > 1 else messages)
        parts = [latest]
        if topic_hint and topic_hint.lower() not in latest.lower():
            parts.append(f"Topic: {topic_hint}")
        if contextual and prior_user:
            parts.append(f"Recent user context: {prior_user}")
        query = ""
        for part in parts:
            candidate = part if not query else f"{query}\n{part}"
            if len(candidate) > self.retrieval_query_max_chars:
                remaining = self.retrieval_query_max_chars - len(query) - (1 if query else 0)
                if remaining <= 0:
                    break
                part = part[:remaining].rstrip()
                candidate = part if not query else f"{query}\n{part}"
            query = candidate
        return query.strip()

    def retrieve_wiki_chunks(self, query: str, limit: Optional[int] = None, user_language: str = "en") -> List[Dict[str, Any]]:
        q = str(query or "").strip()
        if not q:
            return []
        self.ensure_wiki_retriever()
        assert self._embedder is not None
        if self.rag_spanish_only:
            # Spanish-only mode: ignore user_language and always query the
            # Spanish collection. The startup gate (LOAD_ES_INDEX_AT_STARTUP=1
            # + readiness_ok blocking on validate_startup_rag_es) means the
            # Spanish collection is already loaded by the time any chat
            # request reaches this code path. The defensive kickoff below
            # covers manual invocations from scripts that bypass the gate.
            if self._wiki_collection_es is None:
                logger.warning(
                    "spanish-only mode but Spanish ChromaDB not loaded; kicking off background load"
                )
                self._kick_off_wiki_retriever_es_load()
                wait_started = time.perf_counter()
                while self._wiki_collection_es is None and (time.perf_counter() - wait_started) < 60:
                    time.sleep(0.5)
                if self._wiki_collection_es is None:
                    raise RuntimeError(
                        "Spanish ChromaDB not loaded after 60s; cannot retrieve in spanish-only mode"
                    )
            active_collection = self._wiki_collection_es
            rag_index_language = "es"
        else:
            assert self._wiki_collection is not None
            # Spanish collection: only used when already loaded. The 12 GB HNSW
            # on a WSL2 9P-bind-mount serializes filesystem I/O with active
            # queries, so an in-request lazy-load stalls the entire chat. The
            # load is opt-in via LOAD_ES_INDEX_AT_STARTUP and runs in the
            # background-startup thread. When the Spanish index is not loaded,
            # fall back to the English index (bge-m3 is multilingual, so
            # cross-lingual retrieval works).
            use_es = (str(user_language or "en").strip().lower() == "es" and self._wiki_collection_es is not None)
            active_collection = self._wiki_collection_es if use_es else self._wiki_collection
            rag_index_language = "es" if use_es else "en"
        n_results = max(1, int(limit or self.retrieval_candidate_k))
        with self._retriever_run_lock:
            q_emb = self._embedder.encode([q], normalize_embeddings=True).tolist()
            res = active_collection.query(
                query_embeddings=q_emb,
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )

        chunks: List[Dict[str, Any]] = []
        docs = ((res.get("documents") or [[]])[0]) if isinstance(res, dict) else []
        metas = ((res.get("metadatas") or [[]])[0]) if isinstance(res, dict) else []
        dists = ((res.get("distances") or [[]])[0]) if isinstance(res, dict) else []
        for idx, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
            text = str(doc or "").strip()
            if not text:
                continue
            chunks.append(
                {
                    "doc": text,
                    "meta": meta or {},
                    "distance": float(dist) if dist is not None else None,
                    "original_rank": idx,
                    "reranked_rank": idx,
                    "included": False,
                    "duplicate_removed": False,
                    "truncated": False,
                    "dropped_reason": None,
                    "token_estimate": self.estimate_tokens(text),
                    "rag_index_language": rag_index_language,
                }
            )
        return chunks
    def _sigmoid_score(self, value: Any) -> float:
        try:
            score = float(value)
        except Exception:
            return 0.0
        if 0.0 <= score <= 1.0:
            return score
        clamped = max(-12.0, min(12.0, score))
        return 1.0 / (1.0 + math.exp(-clamped))

    _INJECTION_PATTERNS = re.compile(
        r"(?i)"
        r"(?:ignore\s+(?:all\s+)?previous\s+instructions)"
        r"|(?:you\s+are\s+now\s+)"
        r"|(?:system\s*prompt)"
        r"|(?:assistant\s+must)"
        r"|(?:forget\s+(?:all\s+)?(?:your|previous)\s+)"
        r"|(?:new\s+instructions?\s*:)"
        r"|(?:role\s*:\s*(?:system|assistant|user))"
        r"|(?:do\s+not\s+follow\s+)"
        r"|(?:disregard\s+(?:all\s+)?(?:previous|above|prior))"
        r"|(?:override\s+(?:your|all|previous))"
    )

    # Compiled separately so the fast-path probe can run before the per-line
    # scan. Most Wikipedia chunks contain none of these keyword stems, so we
    # can skip the regex scan entirely after a single substring check.
    _INJECTION_FAST_KEYWORDS = (
        "ignore", "system:", "assistant:", "user:", "role:",
        "do not follow", "disregard", "override",
    )

    def _sanitize_retrieved_chunk(self, text: str) -> Tuple[str, bool]:
        """Strip injection-like lines from retrieved chunk content."""
        if not text:
            return text, False
        # Fast-path: if no suspicious keyword appears in the body at all, the
        # full per-line regex scan is guaranteed to find nothing. This is the
        # common case for encyclopedia text and saves O(N_lines) regex calls.
        lowered = text.lower()
        if not any(kw in lowered for kw in self._INJECTION_FAST_KEYWORDS):
            return text.strip(), False
        flagged = False
        clean_lines = []
        for line in text.split("\n"):
            if self._INJECTION_PATTERNS.search(line):
                flagged = True
                continue
            clean_lines.append(line)
        return "\n".join(clean_lines).strip(), flagged

    _CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
    _WHITESPACE_RUN = re.compile(r"\s+")

    def _safe_chunk_title(self, value: Any, max_len: int = 200) -> str:
        """Sanitize a title-like field before injecting into the prompt heading.

        Strips control chars, collapses whitespace, drops injection markers,
        and caps length. Used for chunk title and section_title.
        """
        if not value:
            return ""
        text = self._CONTROL_CHARS.sub(" ", str(value))
        text = self._WHITESPACE_RUN.sub(" ", text).strip()
        if not text:
            return ""
        cleaned, _ = self._sanitize_retrieved_chunk(text)
        cleaned = self._WHITESPACE_RUN.sub(" ", cleaned).strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len].rstrip()
        return cleaned

    _SPANISH_INDICATORS = re.compile(
        r"\b(?:el|la|los|las|del|una|unos|unas|es|fue|son|para|por|con|como|pero|sobre|entre|desde|hasta|donde|cuando|porque|puede|tiene|hace|esta|este|estos|estas|ese|esos|esas|aquel)\b",
        re.IGNORECASE,
    )

    def _detect_query_language(self, text: str) -> str:
        """Return 'es' if text looks Spanish, 'en' otherwise.

        Cached on the instance via a small dict keyed by the 500-char prefix
        used for detection. Queries within a single chat are highly repetitive
        (the rerank pipeline calls this with every candidate's pair string)
        so a tiny cache eliminates a meaningful chunk of regex CPU.
        """
        sample = (text or "")[:500]
        cache = getattr(self, "_lang_detect_cache", None)
        if cache is None:
            cache = {}
            self._lang_detect_cache = cache  # type: ignore[attr-defined]
        cached = cache.get(sample)
        if cached is not None:
            return cached
        words = re.findall(r"\w+", sample, re.UNICODE)
        if not words:
            result = "en"
        else:
            matches = len(self._SPANISH_INDICATORS.findall(sample))
            result = "es" if matches / len(words) > 0.06 else "en"
        # Cap the cache; LRU isn't worth the import here, but unbounded growth
        # would matter on a long-running install. 4096 short strings is < 1 MB.
        if len(cache) >= 4096:
            cache.pop(next(iter(cache)))
        cache[sample] = result
        return result

    def _heuristic_rerank_score(self, query: str, chunk: Dict[str, Any], query_language: str = "en") -> float:
        query_terms = set(self._informative_terms(query))
        if not query_terms:
            return 0.0
        meta = chunk.get("meta") or {}
        title = self._compact_text(meta.get("title", ""), 120)
        body = self._compact_text(chunk.get("doc", ""), 600)
        title_terms = set(self._informative_terms(title))
        body_terms = set(self._informative_terms(body))
        overlap = len(query_terms & body_terms) / max(1, len(query_terms))
        title_overlap = len(query_terms & title_terms) / max(1, min(len(query_terms), 4))
        try:
            distance = float(chunk.get("distance")) if chunk.get("distance") is not None else 1.0
        except Exception:
            distance = 1.0
        distance_score = max(0.0, 1.0 - min(distance, 1.5) / 1.5)
        base = (0.5 * overlap) + (0.3 * title_overlap) + (0.2 * distance_score)
        chunk_lang = (meta.get("language") or "en").strip().lower()
        if chunk_lang == query_language:
            base += 0.05
        return round(min(1.0, base), 4)

    def rerank_wiki_chunks(self, query: str, chunks: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool, int, Optional[str]]:
        if not chunks:
            return [], False, 0, None
        ranked = [dict(chunk) for chunk in chunks]
        rerank_enabled = False
        rerank_error = self.ensure_reranker()
        query_language = self._detect_query_language(query)
        start_t = time.perf_counter()
        scores: Optional[List[float]] = None
        if rerank_error is None and self._reranker is not None:
            # Hot path — keep at DEBUG so 50-req/s load doesn't flood the log.
            # Failure paths below stay at WARNING/INFO.
            logger.debug("reranker active, scoring %d chunks (query_lang=%s)", len(ranked), query_language)
            pairs = []
            for chunk in ranked:
                meta = chunk.get("meta") or {}
                title = self._compact_text(meta.get("title", ""), 120)
                body = self._compact_text(chunk.get("doc", ""), 1200)
                pairs.append([query, f"{title}\n{body}".strip()])
            try:
                # Batch the cross-encoder forward pass so peak VRAM stays
                # bounded even when retrieval_candidate_k is bumped up. A
                # batch of 16 is conservative for bge-reranker on either CPU
                # or an 8 GB GPU; raise via RERANK_BATCH_SIZE when memory
                # headroom allows.
                rerank_batch = max(1, int(os.getenv("RERANK_BATCH_SIZE", "16")))
                raw_scores: List[float] = []
                for i in range(0, len(pairs), rerank_batch):
                    chunk_scores = self._reranker.predict(pairs[i:i + rerank_batch])
                    raw_scores.extend(list(chunk_scores))
                scores = [self._sigmoid_score(score) for score in raw_scores]
                rerank_enabled = True
            except Exception as exc:
                rerank_error = f"{type(exc).__name__}: {exc}"
                logger.warning("reranker predict failed: %s", rerank_error)
        else:
            logger.debug("reranker disabled: error=%s, loaded=%s", rerank_error, self._reranker is not None)
        if scores is None:
            rerank_error = f"{rerank_error or 'reranker_unavailable'} (heuristic fallback)"
            scores = [self._heuristic_rerank_score(query, chunk, query_language) for chunk in ranked]
        # Cross-lingual fallback: if the reranker scored everything very low but
        # the embedding distances are good, the reranker likely can't handle the
        # language pair.  Fall back to distance-based heuristic scores instead.
        if rerank_enabled and scores:
            max_rerank_score = max(scores)
            best_distance = min((float(c.get("distance") or 999) for c in ranked), default=999)
            if max_rerank_score < self.rerank_score_threshold and best_distance < 0.45:
                logger.info(
                    "cross-lingual fallback: reranker max_score=%.4f < threshold=%.2f "
                    "but best_distance=%.4f is strong; using heuristic scores",
                    max_rerank_score, self.rerank_score_threshold, best_distance,
                )
                scores = [self._heuristic_rerank_score(query, chunk, query_language) for chunk in ranked]
                rerank_error = f"cross_lingual_fallback (max_rerank={max_rerank_score:.4f})"

        for idx, (chunk, score) in enumerate(zip(ranked, scores), 1):
            meta = chunk.get("meta") or {}
            if str(meta.get("section_title") or "").strip():
                score = score + 0.03
            chunk_lang = (meta.get("language") or "en").strip().lower()
            if rerank_enabled and chunk_lang == query_language:
                score = score + 0.05
            chunk["relevance_score"] = round(min(1.0, float(score)), 4)
            chunk.setdefault("original_rank", idx)
        ranked.sort(
            key=lambda chunk: (
                -float(chunk.get("relevance_score") or 0.0),
                float(chunk.get("distance")) if chunk.get("distance") is not None else 999999.0,
                int(chunk.get("original_rank") or 999999),
            )
        )
        for idx, chunk in enumerate(ranked, 1):
            chunk["reranked_rank"] = idx
        rerank_ms = int((time.perf_counter() - start_t) * 1000)
        return ranked, rerank_enabled, rerank_ms, rerank_error
    def prepare_wiki_context(self, query: str, user_language: str = "en") -> Dict[str, Any]:
        candidates = self.retrieve_wiki_chunks(query, limit=self.retrieval_candidate_k, user_language=user_language)
        rag_index_language = candidates[0].get("rag_index_language", "en") if candidates else "en"
        rag_collection_path = str(self.chroma_persist_dir_es if rag_index_language == "es" else self.chroma_persist_dir)
        rag_manifest = self._index_manifest_es if rag_index_language == "es" else self._index_manifest
        rag_manifest_path = str(self._index_manifest_path(self.chroma_persist_dir_es if rag_index_language == "es" else self.chroma_persist_dir))
        ranked, rerank_enabled, rerank_ms, rerank_error = self.rerank_wiki_chunks(query, candidates)
        eligible: List[Dict[str, Any]] = []
        for chunk in ranked:
            chunk.setdefault("included", False)
            chunk.setdefault("duplicate_removed", False)
            chunk.setdefault("truncated", False)
            chunk.setdefault("dropped_reason", None)
            if float(chunk.get("relevance_score") or 0.0) < self.rerank_score_threshold:
                chunk["dropped_reason"] = "below_relevance_threshold"
                continue
            eligible.append(chunk)
        context, selected_chunks, context_stats = self.build_wiki_context_payload(eligible)
        for chunk in eligible:
            if not chunk.get("included") and not chunk.get("dropped_reason"):
                chunk["dropped_reason"] = "context_budget"
        selected_chunks = self._decorate_chunk_list_with_citations(selected_chunks, None)
        candidate_chunks = self._decorate_chunk_list_with_citations(self._serialize_debug_chunks(ranked), None)
        citations = self._citations_from_chunks(selected_chunks, None)
        return {
            "context": context,
            "selected_chunks": selected_chunks,
            "retrieval_candidates": candidate_chunks,
            "citations": citations,
            "primary_citation": citations[0] if citations else None,
            "candidate_count": len(candidate_chunks),
            "rerank_enabled": rerank_enabled,
            "rerank_ms": rerank_ms,
            "rerank_error": rerank_error,
            "chunks_after_rerank": len(eligible),
            "chunks_after_budget_trim": len(selected_chunks),
            "context_tokens_estimate": context_stats.get("context_tokens_estimate", 0),
            "chunk_truncation_count": context_stats.get("truncated_count", 0),
            "reason": None if selected_chunks else ("no_candidates" if not candidates else "no_relevant_chunks"),
            "rag_index_language": rag_index_language,
            "rag_collection_path": rag_collection_path,
            "index_manifest": self._clone_data(rag_manifest),
            "index_manifest_path": rag_manifest_path,
            "index_manifest_error": None if rag_manifest else "manifest_unavailable",
        }
    def build_wiki_context_payload(self, chunks: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        parts: List[str] = []
        selected: List[Dict[str, Any]] = []
        separator = "\n\n---\n\n"
        total = 0
        truncated_count = 0
        for i, chunk in enumerate(chunks, 1):
            meta = chunk.get("meta") or {}
            title = self._safe_chunk_title(meta.get("title"))
            section_title = self._safe_chunk_title(meta.get("section_title"))
            section_path = self._safe_chunk_title(meta.get("section_path"))
            body = str(chunk.get("doc") or "").strip()
            if not body:
                continue
            body, was_injection_flagged = self._sanitize_retrieved_chunk(body)
            if was_injection_flagged:
                logger.warning("injection pattern found in chunk: title=%s section=%s", title, section_title)
            if not body:
                continue
            anchor = section_title or section_path
            lang = str(meta.get("language") or "").strip()
            heading = f"[{i}] {title}" if title else f"[{i}]"
            if anchor:
                heading = f"{heading} :: {anchor}"
            if lang:
                heading = f"{heading} ({lang})"
            block = f"{heading}\n{body}"
            remaining = self.retrieval_max_context_chars - total
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = block[:remaining].rstrip()
                truncated_count += 1
                chunk["truncated"] = True
            if not block:
                break
            chunk["included"] = True
            parts.append(block)
            entry = self._chunk_debug_entry(chunk)
            if entry is not None:
                selected.append(entry)
            total += len(block)
            if len(selected) >= self.retrieval_top_k or total >= self.retrieval_max_context_chars:
                break
        context_stats = {
            "total_chars": total,
            "chunk_count": len(selected),
            "chunk_titles": [s.get("title") or "" for s in selected],
            "truncated": truncated_count > 0,
            "context_tokens_estimate": total // 4,
            "truncated_count": truncated_count,
        }
        return separator.join(parts), selected, context_stats

    def inject_wiki_context(self, base_messages: List[Dict[str, str]], context: str, missing_reason: Optional[str] = None, response_language: Optional[str] = None, rag_index_language: Optional[str] = None) -> List[Dict[str, str]]:
        if context.strip():
            content = self._build_retrieval_system_message(context, response_language=response_language, rag_index_language=rag_index_language)
        else:
            prompt_language = (response_language or "").strip().lower()
            base = self.base_system_prompt_es if prompt_language == "es" and self.base_system_prompt_es else self.base_system_prompt
            if not base:
                return [dict(message) for message in base_messages]
            lang_directive = self._language_directive(response_language)
            content = f"{base}\n\n{lang_directive}".strip() if lang_directive else base
        system_message = {"role": "system", "content": content}
        conversation: List[Dict[str, str]] = []
        inserted = False
        for message in base_messages:
            role = str(message.get("role", "") or "").strip().lower()
            if not inserted and role != "system":
                conversation.append(system_message)
                inserted = True
            conversation.append(dict(message))
        if not inserted:
            conversation.append(system_message)
        return conversation
    def safe_path(self, rel: str, user_id: Optional[str] = None) -> Path:
        if not rel or rel.startswith("/") or rel.startswith("\\"):
            raise HTTPException(400, "Invalid file path")
        base = self.data_root.resolve()
        p = (base / rel).resolve()
        try:
            p.relative_to(base)
        except Exception:
            raise HTTPException(400, "Invalid file path")
        if user_id is not None:
            try:
                p.relative_to(self.user_root(user_id))
            except Exception:
                raise HTTPException(403, "Cross-user file access denied")
        return p

    def write_bytes_atomic(self, path: Path, content: bytes) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent), prefix=path.name + ".tmp.") as f:
                tmp_path = Path(f.name)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp_path), str(path))
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
            return int(len(content))
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    def encrypt_blob(self, plain: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        cipher = self._aes.encrypt(nonce, plain, None)
        env = {
            "v": self.enc_version,
            "alg": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(cipher).decode("ascii"),
        }
        return json.dumps(env, separators=(",", ":")).encode("utf-8")

    def decrypt_blob(self, blob: bytes) -> bytes:
        try:
            env = json.loads(blob.decode("utf-8"))
        except Exception:
            logger.warning("decrypt_blob: envelope parse failed", exc_info=True)
            raise HTTPException(500, "Internal storage error")
        if not isinstance(env, dict) or env.get("v") != self.enc_version or env.get("alg") != "AES-256-GCM":
            logger.warning("decrypt_blob: unsupported envelope format v=%r alg=%r", env.get("v") if isinstance(env, dict) else "?", env.get("alg") if isinstance(env, dict) else "?")
            raise HTTPException(500, "Internal storage error")
        try:
            nonce = base64.b64decode(str(env.get("nonce", "")), validate=True)
            cipher = base64.b64decode(str(env.get("ciphertext", "")), validate=True)
            return self._aes.decrypt(nonce, cipher, None)
        except Exception:
            logger.warning("decrypt_blob: AES-GCM decryption failed", exc_info=True)
            raise HTTPException(500, "Internal storage error")

    def write_json_atomic(self, path: Path, payload: Dict[str, Any]) -> int:
        plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.write_bytes_atomic(path, self.encrypt_blob(plain))

    def read_json(self, path: Path) -> Dict[str, Any]:
        blob = path.read_bytes()
        plain = self.decrypt_blob(blob)
        try:
            obj = json.loads(plain.decode("utf-8"))
        except Exception:
            raise HTTPException(500, "Corrupted JSON file")
        if not isinstance(obj, dict):
            raise HTTPException(500, "Corrupted JSON file")
        return obj

    def remove_file(self, path: Path) -> int:
        try:
            size = path.stat().st_size
        except Exception:
            size = 0
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return int(size)

    # 5-second cache of disk usage. ensure_capacity and storage_warning both
    # call disk() on the hot path; disk_usage is a syscall that on Windows
    # serializes briefly with concurrent FS activity. A 5 s staleness window
    # is fine for a metric that changes by KBs per second.
    _DISK_CACHE_TTL_S = 5.0

    def disk(self) -> Dict[str, Any]:
        now = time.monotonic()
        cached = getattr(self, "_disk_cache", None)
        cached_ts = getattr(self, "_disk_cache_ts", 0.0)
        if cached is not None and (now - cached_ts) < self._DISK_CACHE_TTL_S:
            return cached
        usage = shutil.disk_usage(str(self.data_root))
        pct = (usage.used / max(usage.total, 1)) * 100.0
        snap = {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
            "used_percent": round(pct, 4),
        }
        self._disk_cache = snap  # type: ignore[attr-defined]
        self._disk_cache_ts = now  # type: ignore[attr-defined]
        return snap

    def pressure(self, snap: Dict[str, Any]) -> str:
        if snap["used_percent"] >= self.emer_pct or snap["free_bytes"] <= self.emer_free:
            return "emergency"
        if snap["used_percent"] >= self.clean_pct or snap["free_bytes"] <= self.clean_free:
            return "cleanup"
        if snap["used_percent"] >= self.warn_pct or snap["free_bytes"] <= self.warn_free:
            return "warning"
        return "normal"

    def storage_warning(self) -> Optional[Dict[str, Any]]:
        snap = self.disk()
        lv = self.pressure(snap)
        if lv == "normal":
            return None
        return {"level": lv, "used_percent": snap["used_percent"], "free_bytes": snap["free_bytes"]}

    def db(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=WAL")
        # synchronous=NORMAL is the WAL-recommended setting: it still flushes
        # the WAL at every commit (so a power loss can lose at most the last
        # committed transaction, never corrupt the DB) but skips the extra
        # fsync of the WAL header that FULL adds. Under contention this is
        # the difference between 20–100 ms of lock thrash per request and
        # near-zero overhead. WAL journaling provides the durability we care
        # about; FULL is over-cautious for a non-financial app.
        c.execute("PRAGMA synchronous=NORMAL")
        # tiny in-memory temp store, modest mmap window for hot-page reads —
        # both help cleanup queries and analytics rollups stop hitting disk.
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA mmap_size=134217728")  # 128 MB
        c.execute("PRAGMA cache_size=-65536")    # ≈ 64 MB page cache per conn
        return c

    @contextmanager
    def tx(self):
        c = self.db()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def table_columns(self, c: sqlite3.Connection, table: str) -> List[str]:
        return migration_table_columns(c, table)

    def ensure_column(self, c: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        migration_ensure_column(c, table, column, ddl)

    def migrate_chat_schema(self, c: sqlite3.Connection) -> None:
        c.execute(
            "CREATE TABLE IF NOT EXISTS chat_folders(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,name_norm TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"
        )
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_folders_user_name_norm ON chat_folders(user_id,name_norm)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_chat_folders_user_updated ON chat_folders(user_id,updated_at)")

        self.ensure_column(c, "chats", "is_saved", "is_saved INTEGER NOT NULL DEFAULT 0")
        self.ensure_column(c, "chats", "folder_id", "folder_id TEXT")
        self.ensure_column(c, "chats", "deleted_by_user", "deleted_by_user INTEGER NOT NULL DEFAULT 0")
        self.ensure_column(c, "chats", "is_guest_owned", "is_guest_owned INTEGER NOT NULL DEFAULT 0")

        c.execute("UPDATE chats SET is_saved=0 WHERE is_saved IS NULL")
        c.execute("UPDATE chats SET folder_id=NULL WHERE folder_id IS NOT NULL AND TRIM(folder_id)='' ")
        c.execute("UPDATE chats SET folder_id=NULL WHERE folder_id IS NOT NULL AND folder_id NOT IN (SELECT id FROM chat_folders)")

        c.execute("CREATE INDEX IF NOT EXISTS ix_chats_user_deleted_updated ON chats(user_id,is_deleted,updated_at)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_chats_user_saved_deleted ON chats(user_id,is_saved,is_deleted)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_chats_user_folder_deleted ON chats(user_id,folder_id,is_deleted)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_chats_deleted_deleted_at ON chats(is_deleted,deleted_at)")

    def clean_chat_folder_name(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:60]

    def ensure_saved_chat_capacity(self, c: sqlite3.Connection, user_id: str, exclude_chat_id: Optional[str] = None) -> None:
        query = "SELECT COUNT(*) c FROM chats WHERE user_id=? AND is_deleted=0 AND is_saved=1"
        args: List[Any] = [user_id]
        if exclude_chat_id:
            query += " AND id<>?"
            args.append(exclude_chat_id)
        count = int(c.execute(query, tuple(args)).fetchone()["c"])
        if count >= self.max_saved_chats:
            raise HTTPException(409, f"You can only keep {self.max_saved_chats} saved chats.")

    def get_chat_folder_row(self, c: sqlite3.Connection, user_id: str, folder_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM chat_folders WHERE id=? AND user_id=?", (folder_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Folder not found")
        return row

    def resolve_chat_folder_id(self, c: sqlite3.Connection, user_id: str, folder_id: Optional[str]) -> Optional[str]:
        value = str(folder_id or "").strip()
        if not value:
            return None
        return str(self.get_chat_folder_row(c, user_id, value)["id"])
    def init_db(self) -> None:
        self.ensure_dirs()
        try:
            check_conn = self.db()
            try:
                result = check_conn.execute("PRAGMA integrity_check(1)").fetchone()
                if result and str(result[0]).lower() != "ok":
                    logger.critical("SQLite integrity check FAILED: %s", result[0])
                    raise RuntimeError(f"Database integrity check failed: {result[0]}")
            finally:
                check_conn.close()
        except sqlite3.DatabaseError as exc:
            logger.critical("SQLite integrity check error: %s", exc)
            raise RuntimeError(f"Database corrupted or unreadable: {exc}") from exc
        with self.tx() as c:
            run_migrations(c, self.now_iso())
            c.execute("INSERT OR IGNORE INTO user_restrictions(user_id,updated_at) SELECT id, ? FROM users", (self.now_iso(),))

    def analytics_day_bucket(self, iso_value: Optional[str] = None) -> str:
        try:
            dt = datetime.fromisoformat(str(iso_value or self.now_iso()).replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()

    def _analytics_metadata_json(self, metadata: Optional[Dict[str, Any]]) -> str:
        safe: Dict[str, Any] = {}
        for key, value in (metadata or {}).items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                safe[str(key)] = value
            else:
                safe[str(key)] = self._clone_data(value)
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))

    def record_analytics_event(
        self,
        c: sqlite3.Connection,
        *,
        event_type: str,
        surface: str,
        user: Optional[sqlite3.Row] = None,
        created_at: Optional[str] = None,
        value: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> None:
        event_name = str(event_type or "").strip().lower()
        surface_name = str(surface or "unknown").strip().lower()
        created_at_value = created_at or self.now_iso()
        day_bucket = self.analytics_day_bucket(created_at_value)
        user_id = str(user["id"]) if user is not None and user["id"] is not None else None
        username = str(user["username"]) if user is not None and user["username"] is not None else None
        user_role = str(user["role"] or "") if user is not None else ""
        preferred_language = normalize_language_preference((user["preferred_language"] if user is not None else None), default="")
        amount = max(0, int(value or 0))
        if not event_name or amount <= 0:
            return
        metadata_json = self._analytics_metadata_json(metadata)
        c.execute(
            "INSERT INTO usage_events(event_type,surface,day_bucket,created_at,created_ts,user_id,username,user_role,preferred_language,session_id,value,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_name, surface_name, day_bucket, created_at_value, self.now_ts(), user_id, username, user_role, preferred_language, session_id, amount, metadata_json),
        )
        c.execute(
            """
            INSERT INTO analytics_daily_rollups(day_bucket,metric_key,surface,user_role,preferred_language,value)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(day_bucket,metric_key,surface,user_role,preferred_language)
            DO UPDATE SET value=value+excluded.value
            """,
            (day_bucket, event_name, surface_name, user_role, preferred_language, amount),
        )
        if user_id:
            c.execute(
                "INSERT OR IGNORE INTO analytics_daily_active_users(day_bucket,user_id,user_role,preferred_language) VALUES(?,?,?,?)",
                (day_bucket, user_id, user_role, preferred_language),
            )

    def analytics_range(self, date_from: Optional[str], date_to: Optional[str], default_days: int = 30) -> Tuple[str, str]:
        today = datetime.now(timezone.utc).date()
        try:
            end_day = datetime.fromisoformat(str(date_to)).date() if date_to else today
        except Exception:
            end_day = today
        try:
            start_day = datetime.fromisoformat(str(date_from)).date() if date_from else (end_day - timedelta(days=max(0, default_days - 1)))
        except Exception:
            start_day = end_day - timedelta(days=max(0, default_days - 1))
        if start_day > end_day:
            start_day, end_day = end_day, start_day
        return start_day.isoformat(), end_day.isoformat()

    def analytics_metric_rows(self, c: sqlite3.Connection, date_from: str, date_to: str) -> List[sqlite3.Row]:
        return c.execute(
            """
            SELECT day_bucket,metric_key,surface,user_role,preferred_language,value
            FROM analytics_daily_rollups
            WHERE day_bucket>=? AND day_bucket<=?
            ORDER BY day_bucket ASC, metric_key ASC, surface ASC
            """,
            (date_from, date_to),
        ).fetchall()

    def analytics_active_rows(self, c: sqlite3.Connection, date_from: str, date_to: str) -> List[sqlite3.Row]:
        return c.execute(
            """
            SELECT day_bucket,user_role,preferred_language,COUNT(*) AS active_users
            FROM analytics_daily_active_users
            WHERE day_bucket>=? AND day_bucket<=?
            GROUP BY day_bucket,user_role,preferred_language
            ORDER BY day_bucket ASC
            """,
            (date_from, date_to),
        ).fetchall()

    def analytics_payload(self, c: sqlite3.Connection, date_from: str, date_to: str) -> Dict[str, Any]:
        metric_rows = self.analytics_metric_rows(c, date_from, date_to)
        active_rows = self.analytics_active_rows(c, date_from, date_to)
        summary_totals: Dict[str, int] = {}
        by_surface: Dict[str, int] = {}
        by_role: Dict[str, int] = {}
        by_language: Dict[str, int] = {}
        days: Dict[str, Dict[str, Any]] = {}
        for row in metric_rows:
            day = str(row["day_bucket"])
            metric = str(row["metric_key"])
            surface = str(row["surface"] or "unknown")
            role = str(row["user_role"] or "unknown")
            language = str(row["preferred_language"] or "unknown")
            value = int(row["value"] or 0)
            bucket = days.setdefault(day, {"day": day})
            bucket[metric] = int(bucket.get(metric, 0)) + value
            summary_totals[metric] = summary_totals.get(metric, 0) + value
            by_surface[surface] = by_surface.get(surface, 0) + value
            by_role[role] = by_role.get(role, 0) + value
            by_language[language] = by_language.get(language, 0) + value
        active_total = 0
        for row in active_rows:
            day = str(row["day_bucket"])
            count = int(row["active_users"] or 0)
            role = str(row["user_role"] or "unknown")
            language = str(row["preferred_language"] or "unknown")
            bucket = days.setdefault(day, {"day": day})
            bucket["active_users"] = int(bucket.get("active_users", 0)) + count
            active_total += count
            by_role[f"active:{role}"] = by_role.get(f"active:{role}", 0) + count
            by_language[f"active:{language}"] = by_language.get(f"active:{language}", 0) + count
        summary_totals["active_users"] = active_total
        metrics_order = [
            "accounts_created",
            "logins_succeeded",
            "chat_opened",
            "chat_sessions_created",
            "chat_deleted",
            "chat_restored",
            "chat_completion_requested",
            "chat_completion_succeeded",
            "chat_completion_failed",
            "chat_completion_stopped",
            "chat_messages_sent",
            "chat_completion_tokens_emitted",
            "chat_citations_emitted",
            "documents_created",
            "documents_opened",
            "documents_updated",
            "documents_starred",
            "documents_unstarred",
            "documents_deleted",
            "documents_restored",
            "documents_trash_cleared",
            "folders_created",
            "folders_renamed",
            "folders_deleted",
            "portal_tool_open",
            "wiki_shell_open",
            "wiki_open_full_page",
            "learn_shell_open",
            "learn_open_full_page",
            "active_users",
        ]
        return {
            "date_from": date_from,
            "date_to": date_to,
            "generated_at": self.now_iso(),
            "metrics_version": self.analytics_export_version,
            "summary": {
                "totals": {key: int(summary_totals.get(key, 0)) for key in metrics_order if key in summary_totals or key == "active_users"},
                "by_surface": [{"surface": key, "value": int(value)} for key, value in sorted(by_surface.items())],
                "by_role": [{"role": key, "value": int(value)} for key, value in sorted(by_role.items())],
                "by_language": [{"language": key, "value": int(value)} for key, value in sorted(by_language.items())],
            },
            "timeseries": [days[key] for key in sorted(days.keys())],
        }

    def analytics_csv(self, payload: Dict[str, Any]) -> str:
        rows = list(payload.get("timeseries") or [])
        metric_keys = sorted({key for row in rows for key in row.keys() if key != "day"})
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["day", *metric_keys])
        for row in rows:
            writer.writerow([row.get("day", ""), *[row.get(key, 0) for key in metric_keys]])
        return out.getvalue()
    def log_event(self, c: sqlite3.Connection, *, user: Optional[sqlite3.Row] = None, ip_: str = "", endpoint: str = "", et: str, sev: str, detail: str, obs: Optional[float] = None, th: Optional[float] = None, act: str = "") -> None:
        c.execute(
            "INSERT INTO security_events(user_id,username,ip,endpoint,event_type,severity,detail,observed,threshold,action,created_at,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"] if user else None,
                user["username"] if user else None,
                ip_,
                endpoint,
                et,
                sev,
                detail,
                obs,
                th,
                act,
                self.now_iso(),
                self.now_ts(),
            ),
        )

    def ensure_restrictions_row(self, c: sqlite3.Connection, uid_: str) -> None:
        c.execute("INSERT OR IGNORE INTO user_restrictions(user_id,updated_at) VALUES(?,?)", (uid_, self.now_iso()))

    def get_restrictions(self, c: sqlite3.Connection, uid_: str) -> sqlite3.Row:
        self.ensure_restrictions_row(c, uid_)
        row = c.execute("SELECT * FROM user_restrictions WHERE user_id=?", (uid_,)).fetchone()
        if row is None:
            raise HTTPException(500, "Missing restrictions row")
        return row

    def is_manual_locked(self, c: sqlite3.Connection, user: sqlite3.Row) -> bool:
        if user["role"] == "admin":
            return False
        rr = self.get_restrictions(c, user["id"])
        if int(rr["manual_lock_permanent"] or 0) == 1:
            return True
        return self.is_future(rr["manual_locked_until"])

    def sync_legacy_lock_fields(self, c: sqlite3.Connection, uid_: str) -> None:
        rr = self.get_restrictions(c, uid_)
        if int(rr["manual_lock_permanent"] or 0) == 1:
            until = "9999-12-31T23:59:59+00:00"
            reason = rr["manual_lock_reason"] or "manual_permanent"
        elif self.is_future(rr["manual_locked_until"]):
            until = rr["manual_locked_until"]
            reason = rr["manual_lock_reason"]
        else:
            until = None
            reason = None
        c.execute("UPDATE users SET locked_until=?, lock_reason=? WHERE id=?", (until, reason, uid_))

    def ensure_manual_write_access(self, c: sqlite3.Connection, user: sqlite3.Row) -> None:
        if user["role"] == "admin":
            return
        rr = self.get_restrictions(c, user["id"])
        if int(rr["manual_lock_permanent"] or 0) == 1:
            raise HTTPException(423, "Account is locked by administrator")
        if self.is_future(rr["manual_locked_until"]):
            raise HTTPException(423, f"Account is locked by administrator for {self.seconds_until(rr['manual_locked_until'])} seconds")

    def ensure_docs_write_access(self, c: sqlite3.Connection, user: sqlite3.Row) -> None:
        self.ensure_manual_write_access(c, user)
        if user["role"] == "admin":
            return
        rr = self.get_restrictions(c, user["id"])
        if self.is_future(rr["docs_write_blocked_until"]):
            raise HTTPException(429, f"Document writes are temporarily blocked for {self.seconds_until(rr['docs_write_blocked_until'])} seconds")

    def ensure_ai_send_access(self, c: sqlite3.Connection, user: sqlite3.Row) -> None:
        self.ensure_manual_write_access(c, user)
        if user["role"] == "admin":
            return
        rr = self.get_restrictions(c, user["id"])
        if self.is_future(rr["ai_send_blocked_until"]):
            raise HTTPException(429, f"AI send is temporarily blocked for {self.seconds_until(rr['ai_send_blocked_until'])} seconds")
        if self.is_future(rr["ai_prompt_cooldown_until"]):
            raise HTTPException(429, f"Please wait {self.seconds_until(rr['ai_prompt_cooldown_until'])} seconds before sending another AI request")

    def count_security_events(self, c: sqlite3.Connection, uid_: str, et: str, sec: int) -> int:
        r = c.execute("SELECT COUNT(*) c FROM security_events WHERE user_id=? AND event_type=? AND created_ts>=?", (uid_, et, self.now_ts() - sec)).fetchone()
        return int(r["c"] if r else 0)

    def apply_docs_penalty(self, c: sqlite3.Connection, user: sqlite3.Row, ip_: str, endpoint: str) -> None:
        if user["role"] == "admin":
            return
        hits = self.count_security_events(c, user["id"], "docs_limit_block", self.docs_offense_window)
        if hits < self.docs_offense_hits:
            return
        until = (datetime.now(timezone.utc) + timedelta(seconds=self.docs_write_block_seconds)).isoformat()
        c.execute("UPDATE user_restrictions SET docs_write_blocked_until=?, docs_block_reason=?, updated_at=? WHERE user_id=?", (until, "docs_rate_abuse", self.now_iso(), user["id"]))
        self.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="docs_write_block", sev="error", detail="Docs write blocked due to repeated limits", obs=float(hits), th=float(self.docs_offense_hits), act="docs_block")

    def apply_ai_penalty(self, c: sqlite3.Connection, user: sqlite3.Row, ip_: str, endpoint: str) -> None:
        if user["role"] == "admin":
            return
        hits = self.count_security_events(c, user["id"], "ai_limit_block", self.ai_offense_window)
        cool_until = (datetime.now(timezone.utc) + timedelta(seconds=self.ai_prompt_cooldown_seconds)).isoformat()
        c.execute("UPDATE user_restrictions SET ai_prompt_cooldown_until=?, updated_at=? WHERE user_id=?", (cool_until, self.now_iso(), user["id"]))
        self.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="ai_cooldown", sev="warning", detail="AI prompt cooldown applied", obs=float(self.ai_prompt_cooldown_seconds), th=float(self.ai_prompt_cooldown_seconds), act="cooldown")
        if hits < self.ai_block_hits:
            return
        send_until = (datetime.now(timezone.utc) + timedelta(seconds=self.ai_send_block_seconds)).isoformat()
        c.execute("UPDATE user_restrictions SET ai_send_blocked_until=?, updated_at=? WHERE user_id=?", (send_until, self.now_iso(), user["id"]))
        self.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="ai_send_block", sev="error", detail="AI send blocked due to repeated limits", obs=float(hits), th=float(self.ai_block_hits), act="ai_block")

    def arm_ai_cooldown_if_needed(self, c: sqlite3.Connection, user: sqlite3.Row) -> None:
        if user["role"] == "admin":
            return
        hits = self.count_security_events(c, user["id"], "ai_limit_block", self.ai_offense_window)
        if hits < 1:
            return
        cool_until = (datetime.now(timezone.utc) + timedelta(seconds=self.ai_prompt_cooldown_seconds)).isoformat()
        c.execute("UPDATE user_restrictions SET ai_prompt_cooldown_until=?, updated_at=? WHERE user_id=?", (cool_until, self.now_iso(), user["id"]))

    # ── Rate-limit window semantics ──────────────────────────────────────
    # These counters implement a SLIDING (non-tumbling) window: each query
    # asks "how many events of type X did user U record in the last `sec`
    # seconds *from now*". That means the limit refills continuously as old
    # events age out — there is no fixed minute boundary at :00. The benefit
    # is no thundering-herd at boundaries; the cost is that a busy user can't
    # see a "next-reset" clock because there isn't one. If a strict bucket is
    # ever required (e.g. for a contractual quota), replace with a token
    # bucket persisted per user (capacity = th, refill = th/sec).
    def rate_count(self, c: sqlite3.Connection, uid_: str, typ: str, sec: int = 60) -> int:
        r = c.execute("SELECT COUNT(*) c FROM rate_events WHERE user_id=? AND event_type=? AND created_ts>=?", (uid_, typ, self.now_ts() - sec)).fetchone()
        return int(r["c"] if r else 0)

    def rate_bytes(self, c: sqlite3.Connection, uid_: str, typ: str, sec: int = 60) -> int:
        r = c.execute("SELECT COALESCE(SUM(bytes),0) b FROM rate_events WHERE user_id=? AND event_type=? AND created_ts>=?", (uid_, typ, self.now_ts() - sec)).fetchone()
        return int(r["b"] if r else 0)

    def rate_add(self, c: sqlite3.Connection, uid_: str, typ: str, b: int = 0) -> None:
        c.execute("INSERT INTO rate_events(user_id,event_type,bytes,created_ts) VALUES(?,?,?,?)", (uid_, typ, int(b), self.now_ts()))

    def ip_rate_count(self, c: sqlite3.Connection, key: str, typ: str, sec: int = 60) -> int:
        r = c.execute("SELECT COUNT(*) c FROM ip_rate_events WHERE key=? AND event_type=? AND created_ts>=?", (key, typ, self.now_ts() - sec)).fetchone()
        return int(r["c"] if r else 0)

    def ip_rate_add(self, c: sqlite3.Connection, key: str, typ: str) -> None:
        c.execute("INSERT INTO ip_rate_events(key,event_type,created_ts) VALUES(?,?,?)", (key, typ, self.now_ts()))

    def check_limit(self, c: sqlite3.Connection, user: sqlite3.Row, ip_: str, endpoint: str, metric: str, obs: float, th: float, warnings: List[Dict[str, Any]], scope: str = "") -> None:
        scope = (scope or "").strip().lower()
        warn_type = f"{scope}_limit_warning" if scope else "limit_warning"
        block_type = f"{scope}_limit_block" if scope else "limit_block"
        if obs >= th * 0.8:
            warnings.append({"metric": metric, "observed": obs, "threshold": th, "warning": "near_limit"})
            self.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et=warn_type, sev="warning", detail=f"Near {metric}", obs=obs, th=th, act="warn")
        if obs > th:
            self.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et=block_type, sev="error", detail=f"Blocked {metric}", obs=obs, th=th, act="blocked")
            if scope == "docs":
                self.apply_docs_penalty(c, user, ip_, endpoint)
            elif scope == "ai":
                self.apply_ai_penalty(c, user, ip_, endpoint)
            self.persist_and_raise(c, 429, f"Rate/storage limit exceeded: {metric}")

    def recalc_storage(self, c: sqlite3.Connection, uid_: str) -> None:
        d = c.execute("SELECT COALESCE(SUM(size_bytes),0) s FROM documents WHERE user_id=?", (uid_,)).fetchone()["s"]
        h = c.execute("SELECT COALESCE(SUM(size_bytes),0) s FROM chats WHERE user_id=?", (uid_,)).fetchone()["s"]
        c.execute("UPDATE users SET storage_bytes_used=? WHERE id=?", (int(d or 0) + int(h or 0), uid_))

    def req_user(self, c: sqlite3.Connection, req: Request, admin: bool = False, write: bool = False) -> sqlite3.Row:
        """Resolve the current session cookie into a validated user row."""
        if write:
            self.validate_same_origin_write(req)
        tok = req.cookies.get(self.cookie)
        if not tok:
            raise HTTPException(401, "Not authenticated")
        r = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND u.is_deleted=0 LIMIT 1",
            (self.token_hash(tok), self.now_iso()),
        ).fetchone()
        if r is None:
            raise HTTPException(401, "Session expired")
        if admin and r["role"] != "admin":
            raise HTTPException(403, "Admin required")
        self.ensure_restrictions_row(c, r["id"])
        if write:
            self.ensure_manual_write_access(c, r)
        c.execute("UPDATE users SET last_active_at=? WHERE id=?", (self.now_iso(), r["id"]))
        c.execute("UPDATE sessions SET last_accessed_at=? WHERE token_hash=?", (self.now_iso(), self.token_hash(tok)))
        return c.execute("SELECT * FROM users WHERE id=?", (r["id"],)).fetchone() or r

    def preferred_wiki_language_for_request(self, req: Request) -> str:
        """Resolve the user's preferred wiki language, defaulting to English."""
        try:
            with self.tx() as c:
                user = self.req_user(c, req)
                return normalize_language_preference(user["preferred_language"], default="en")
        except HTTPException as exc:
            if int(exc.status_code) == 401:
                return "en"
            raise

    def build_wiki_redirect_target(self, req: Request, wiki_path: str = "") -> str:
        """Translate legacy /wiki routes into concrete language-prefixed Kiwix routes."""
        language = self.preferred_wiki_language_for_request(req)
        normalized_path = str(wiki_path or "").lstrip("/")
        target = f"/wiki/{language}/"
        if normalized_path:
            first_segment = normalized_path.split("/", 1)[0].strip().lower()
            if first_segment in ("en", "es"):
                raise HTTPException(404, "Not found")
            target = f"/wiki/{language}/{normalized_path}"
        query = str(req.url.query or "").strip()
        return f"{target}?{query}" if query else target

    def mk_resp(self, data: Dict[str, Any], token: Optional[str] = None, clear: bool = False) -> JSONResponse:
        r = JSONResponse(data)
        if clear:
            r.delete_cookie(self.cookie, path="/")
        elif token is not None:
            r.set_cookie(
                self.cookie,
                token,
                httponly=True,
                samesite=self.cookie_samesite,
                secure=self.cookie_secure,
                max_age=self.session_days * 86400,
                path="/",
            )
        return r

    def doc_rel(self, uid_: str, did: str) -> str:
        return f"users/{uid_}/docs/{did}.json"

    def chat_rel(self, uid_: str, cid: str) -> str:
        return f"users/{uid_}/chats/{cid}.json"

    def trash_rel(self, uid_: str, kind: str, iid: str) -> str:
        return f"users/{uid_}/trash/{kind}/{iid}-{self.uid()}.json"

    def needs_cleanup(self, required: int = 0) -> bool:
        snap = self.disk()
        return self.pressure(snap) in ("cleanup", "emergency") or (required > 0 and snap["free_bytes"] < required + self.clean_free)

    def ensure_capacity(self, required: int, reason: str, c: Optional[sqlite3.Connection] = None) -> None:
        if required <= 0:
            return
        if self.needs_cleanup(required):
            try:
                self.run_cleanup(reason, required, c=c)
            except RuntimeError as exc:
                # run_cleanup can refuse to run (e.g. CLEANUP_REQUIRE_BACKUP_MARKER=1
                # in production and no fresh backup marker). Failing closed here
                # bricks every write path because needs_cleanup() trips at 85%
                # host-disk usage. Fall through to the actual capacity check
                # below — if there is real space, the write proceeds; if not,
                # the 507 path returns a clean error.
                logger.warning("ensure_capacity: cleanup unavailable, continuing without reclaim: %s", exc)
        snap = self.disk()
        if snap["free_bytes"] < required + self.emer_free:
            raise HTTPException(507, "Insufficient storage")
        if self.pressure(snap) == "emergency":
            raise HTTPException(507, "Storage emergency threshold reached")

    def hard_del_doc(self, c: sqlite3.Connection, row: sqlite3.Row) -> Tuple[int, int]:
        b = self.remove_file(self.safe_path(row["file_path"], row["user_id"]))
        c.execute("DELETE FROM documents WHERE id=?", (row["id"],))
        return b, 1

    def hard_del_chat(self, c: sqlite3.Connection, row: sqlite3.Row) -> Tuple[int, int]:
        b = self.remove_file(self.safe_path(row["file_path"], row["user_id"]))
        c.execute("DELETE FROM chats WHERE id=?", (row["id"],))
        return b, 1

    def run_cleanup(self, reason: str = "manual", required: int = 0, c: Optional[sqlite3.Connection] = None, dry_run: bool = False) -> Dict[str, Any]:
        if not self._cleanup_lock.acquire(blocking=False):
            return {"running": True}
        reclaimed, deleted = 0, 0
        manifest: Dict[str, Any] = {
            "started_at": self.now_iso(),
            "reason": reason,
            "required_bytes": int(required),
            "dry_run": bool(dry_run),
            "backup_marker_required": bool(self.cleanup_require_backup_marker),
            "backup_marker_path": str(self.cleanup_backup_marker_path),
            "backup_marker_fresh": self.cleanup_backup_marker_is_fresh(),
            "candidates": {},
            "quarantined_orphans": [],
            "errors": [],
        }

        def cleanup_pass(conn: sqlite3.Connection) -> None:
            nonlocal reclaimed, deleted
            now = datetime.now(timezone.utc)
            tcut = (now - timedelta(days=self.trash_retention_days)).isoformat()
            trash_docs = conn.execute("SELECT * FROM documents WHERE is_deleted=1 AND deleted_at IS NOT NULL AND deleted_at<=? ORDER BY deleted_at ASC", (tcut,)).fetchall()
            manifest["candidates"]["expired_trash_docs"] = len(trash_docs)
            for r in trash_docs:
                if dry_run:
                    reclaimed += self.path_size(self.safe_path(r["file_path"], r["user_id"])); deleted += 1
                else:
                    b, n = self.hard_del_doc(conn, r); reclaimed += b; deleted += n
            trash_chats = conn.execute("SELECT * FROM chats WHERE is_deleted=1 AND deleted_at IS NOT NULL AND deleted_at<=? ORDER BY deleted_at ASC", (tcut,)).fetchall()
            manifest["candidates"]["expired_trash_chats"] = len(trash_chats)
            for r in trash_chats:
                if dry_run:
                    reclaimed += self.path_size(self.safe_path(r["file_path"], r["user_id"])); deleted += 1
                else:
                    b, n = self.hard_del_chat(conn, r); reclaimed += b; deleted += n
            gcut = (now - timedelta(days=self.guest_retention_days)).isoformat()
            logout_cut = (now - timedelta(minutes=self.guest_logout_delete_minutes)).isoformat()
            guests = conn.execute(
                "SELECT * FROM users WHERE role='guest' AND is_deleted=0 AND (COALESCE(last_active_at,created_at)<=? OR (guest_logout_at IS NOT NULL AND guest_logout_at<=?))",
                (gcut, logout_cut),
            ).fetchall()
            manifest["candidates"]["expired_guest_users"] = len(guests)
            for g in guests:
                for r in conn.execute("SELECT * FROM documents WHERE user_id=?", (g["id"],)).fetchall():
                    if dry_run:
                        reclaimed += self.path_size(self.safe_path(r["file_path"], r["user_id"])); deleted += 1
                    else:
                        b, n = self.hard_del_doc(conn, r); reclaimed += b; deleted += n
                for r in conn.execute("SELECT * FROM chats WHERE user_id=?", (g["id"],)).fetchall():
                    if dry_run:
                        reclaimed += self.path_size(self.safe_path(r["file_path"], r["user_id"])); deleted += 1
                    else:
                        b, n = self.hard_del_chat(conn, r); reclaimed += b; deleted += n
                if not dry_run:
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (g["id"],)); conn.execute("DELETE FROM users WHERE id=?", (g["id"],))

            def still() -> bool:
                s = self.disk(); return self.pressure(s) in ("cleanup", "emergency") or (required > 0 and s["free_bytes"] < required + self.clean_free)

            if still():
                ccut = (now - timedelta(days=self.chat_retention_days)).isoformat()
                old_chats = conn.execute("SELECT * FROM chats WHERE is_deleted=0 AND is_saved=0 AND COALESCE(last_accessed_at,updated_at,created_at)<=? ORDER BY COALESCE(last_accessed_at,updated_at,created_at) ASC", (ccut,)).fetchall()
                manifest["candidates"]["old_unsaved_chats"] = len(old_chats)
                for r in old_chats:
                    if not still(): break
                    if dry_run:
                        reclaimed += self.path_size(self.safe_path(r["file_path"], r["user_id"])); deleted += 1
                    else:
                        b, n = self.hard_del_chat(conn, r); reclaimed += b; deleted += n
            if still():
                dcut = (now - timedelta(days=self.doc_retention_days)).isoformat()
                old_docs = conn.execute("SELECT * FROM documents WHERE is_deleted=0 AND is_starred=0 AND COALESCE(last_accessed_at,updated_at,created_at)<=? ORDER BY COALESCE(last_accessed_at,updated_at,created_at) ASC", (dcut,)).fetchall()
                manifest["candidates"]["old_unstarred_docs"] = len(old_docs)
                for r in old_docs:
                    if not still(): break
                    if dry_run:
                        reclaimed += self.path_size(self.safe_path(r["file_path"], r["user_id"])); deleted += 1
                    else:
                        b, n = self.hard_del_doc(conn, r); reclaimed += b; deleted += n
            # --- TTL cleanup for unbounded tables ---
            rate_cut_ts = int((now - timedelta(days=self.rate_events_retention_days)).timestamp())
            manifest["candidates"]["old_rate_events"] = int(conn.execute("SELECT COUNT(*) c FROM rate_events WHERE created_ts < ?", (rate_cut_ts,)).fetchone()["c"])
            manifest["candidates"]["old_ip_rate_events"] = int(conn.execute("SELECT COUNT(*) c FROM ip_rate_events WHERE created_ts < ?", (rate_cut_ts,)).fetchone()["c"])
            sec_cut = (now - timedelta(days=self.security_events_retention_days)).isoformat()
            manifest["candidates"]["old_security_events"] = int(conn.execute("SELECT COUNT(*) c FROM security_events WHERE created_at < ?", (sec_cut,)).fetchone()["c"])
            usage_cut_ts = int((now - timedelta(days=self.usage_events_retention_days)).timestamp())
            manifest["candidates"]["old_usage_events"] = int(conn.execute("SELECT COUNT(*) c FROM usage_events WHERE created_ts < ?", (usage_cut_ts,)).fetchone()["c"])
            cleanup_cut = (now - timedelta(days=self.cleanup_events_retention_days)).isoformat()
            manifest["candidates"]["old_cleanup_events"] = int(conn.execute("SELECT COUNT(*) c FROM cleanup_events WHERE created_at < ?", (cleanup_cut,)).fetchone()["c"])
            login_cut_ts = int((now - timedelta(days=self.login_attempts_retention_days)).timestamp())
            manifest["candidates"]["old_login_attempts"] = int(conn.execute("SELECT COUNT(*) c FROM login_attempts WHERE last_attempt_ts < ?", (login_cut_ts,)).fetchone()["c"])
            session_cut = (now - timedelta(days=self.session_retention_days)).isoformat()
            manifest["candidates"]["old_sessions"] = int(conn.execute("SELECT COUNT(*) c FROM sessions WHERE expires_at < ? AND (revoked_at IS NOT NULL OR expires_at < ?)", (session_cut, session_cut)).fetchone()["c"])
            if not dry_run:
                conn.execute("DELETE FROM rate_events WHERE created_ts < ?", (rate_cut_ts,))
                conn.execute("DELETE FROM ip_rate_events WHERE created_ts < ?", (rate_cut_ts,))
                conn.execute("DELETE FROM security_events WHERE created_at < ?", (sec_cut,))
                conn.execute("DELETE FROM usage_events WHERE created_ts < ?", (usage_cut_ts,))
                conn.execute("DELETE FROM cleanup_events WHERE created_at < ?", (cleanup_cut,))
                conn.execute("DELETE FROM login_attempts WHERE last_attempt_ts < ?", (login_cut_ts,))
                conn.execute("DELETE FROM sessions WHERE expires_at < ? AND (revoked_at IS NOT NULL OR expires_at < ?)", (session_cut, session_cut))

            # --- Orphan user directory cleanup ---
            try:
                known_uids = {str(r["id"]) for r in conn.execute("SELECT id FROM users").fetchall()}
                if self.users_root.exists():
                    for entry in self.users_root.iterdir():
                        if entry.is_dir() and entry.name not in known_uids:
                            try:
                                size = self.path_size(entry)
                                if dry_run:
                                    reclaimed += size; deleted += 1
                                else:
                                    _, dest = self.quarantine_orphan_user_dir(entry)
                                    manifest["quarantined_orphans"].append({"source": str(entry), "destination": dest, "size_bytes": size})
                                    deleted += 1
                            except Exception as exc:
                                manifest["errors"].append({"stage": "orphan_quarantine", "path": str(entry), "error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                logger.warning("orphan user directory scan failed", exc_info=True)

            if not dry_run:
                for u in conn.execute("SELECT id FROM users").fetchall(): self.recalc_storage(conn, u["id"])
            snap_inner = self.disk()
            if not dry_run:
                conn.execute("INSERT INTO cleanup_events(reason,level,bytes_reclaimed,items_deleted,used_percent,free_bytes,details,created_at) VALUES(?,?,?,?,?,?,?,?)", (reason, self.pressure(snap_inner), int(reclaimed), int(deleted), float(snap_inner["used_percent"]), int(snap_inner["free_bytes"]), json.dumps({"manifest_path": manifest.get("manifest_path"), "candidates": manifest.get("candidates")}, ensure_ascii=False), self.now_iso()))

        try:
            if self.cleanup_require_backup_marker and not dry_run and not manifest["backup_marker_fresh"]:
                manifest["blocked"] = True
                manifest["finished_at"] = self.now_iso()
                manifest["manifest_path"] = self.write_cleanup_manifest(manifest)
                raise RuntimeError(
                    f"Cleanup requires a verified backup marker newer than {self.cleanup_backup_marker_max_hours}h at {self.cleanup_backup_marker_path}"
                )
            if self.tmp_root.exists():
                for p in sorted([x for x in self.tmp_root.rglob("*") if x.is_file()], key=lambda q: q.stat().st_mtime):
                    if dry_run:
                        reclaimed += self.path_size(p)
                    else:
                        reclaimed += self.remove_file(p)
                    deleted += 1
            if c is not None:
                cleanup_pass(c)
            else:
                with self.tx() as conn:
                    cleanup_pass(conn)
            # Bound the WAL file: PASSIVE never blocks readers/writers.
            if not dry_run:
                try:
                    wc = self.db()
                    try:
                        wc.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    finally:
                        wc.close()
                except Exception:
                    logger.warning("wal_checkpoint failed", exc_info=True)
            # Periodic VACUUM to reclaim disk space from deleted rows
            if not dry_run and self.now_ts() - self._last_vacuum_ts >= self.vacuum_interval_hours * 3600:
                try:
                    vc = self.db()
                    try:
                        vc.execute("VACUUM")
                    finally:
                        vc.close()
                    self._last_vacuum_ts = self.now_ts()
                except Exception:
                    logger.warning("VACUUM failed", exc_info=True)
            snap = self.disk()
            manifest["finished_at"] = self.now_iso()
            manifest["estimated_reclaimed_bytes" if dry_run else "reclaimed_bytes"] = int(reclaimed)
            manifest["candidate_items" if dry_run else "deleted_items"] = int(deleted)
            manifest["disk_after"] = {"used_percent": snap["used_percent"], "free_bytes": snap["free_bytes"], "level": self.pressure(snap)}
            manifest["manifest_path"] = self.write_cleanup_manifest(manifest)
            return {"running": False, "dry_run": bool(dry_run), "reclaimed": int(reclaimed), "deleted_items": int(deleted), "used_percent": snap["used_percent"], "free_bytes": snap["free_bytes"], "level": self.pressure(snap), "manifest_path": manifest.get("manifest_path")}
        finally:
            self._cleanup_lock.release()

    def cleanup_loop(self):
        while True:
            try: self.run_cleanup("background")
            except Exception: logger.exception("background cleanup loop error")
            time.sleep(max(60, self.cleanup_loop_seconds))

    def seed_admin(self) -> None:
        with self.tx() as c:
            r = c.execute("SELECT * FROM users WHERE username_norm=?", (self.nuser(self.admin_username),)).fetchone()
            if r is not None:
                promoted = False
                if r["role"] != "admin":
                    c.execute("UPDATE users SET role='admin' WHERE id=?", (r["id"],))
                    promoted = True
                self.ensure_restrictions_row(c, r["id"])
                if promoted:
                    self._record_security_event(
                        c,
                        et="admin_promoted",
                        sev="warning",
                        detail=f"existing user {self.admin_username!r} promoted to admin during seed_admin",
                        username=self.admin_username,
                    )
                return
            uid_ = self.uid()
            self.ensure_user_dirs(uid_)
            c.execute(
                "INSERT INTO users(id,username,username_norm,password_hash,role,created_at,storage_bytes_used,preferred_language,preferred_theme) VALUES(?,?,?,?, 'admin', ?,0,?,?)",
                (uid_, self.admin_username, self.nuser(self.admin_username), self._ph.hash(self.admin_password), self.now_iso(), "en", "light"),
            )
            self.ensure_restrictions_row(c, uid_)
            self._record_security_event(
                c,
                et="admin_seeded",
                sev="warning",
                detail=f"initial admin user {self.admin_username!r} created",
                username=self.admin_username,
            )

    def _record_security_event(
        self,
        c: sqlite3.Connection,
        *,
        et: str,
        sev: str,
        detail: str,
        username: str = "",
    ) -> None:
        """Best-effort write to security_events without depending on a Request.

        Used by paths (seed_admin, etc.) that don't have an HTTP request scope.
        """
        try:
            now_iso = self.now_iso()
            now_ts = int(time.time())
            c.execute(
                "INSERT INTO security_events(user_id,username,ip,endpoint,event_type,severity,detail,observed,threshold,action,created_at,created_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (None, username or None, None, None, et, sev, detail, None, None, None, now_iso, now_ts),
            )
        except Exception:
            logger.exception("Failed to record security event %s", et)

    def doc_limits(self, c: sqlite3.Connection, user: sqlite3.Row, req: Request, bytes_write: int, create: bool) -> List[Dict[str, Any]]:
        warnings: List[Dict[str, Any]] = []
        ip_, ep = self.ip(req), req.url.path
        if bytes_write > self.doc_max:
            self.log_event(c, user=user, ip_=ip_, endpoint=ep, et="docs_limit_block", sev="error", detail="Doc exceeds max size", obs=float(bytes_write), th=float(self.doc_max), act="blocked")
            self.apply_docs_penalty(c, user, ip_, ep)
            self.persist_and_raise(c, 429, "Document size exceeds limit")
        if create:
            doc_count = int(c.execute(
                "SELECT COUNT(*) c FROM documents WHERE user_id=? AND is_deleted=0", (user["id"],)
            ).fetchone()["c"])
            if doc_count >= self.max_docs:
                lang = str(user["preferred_language"] or "en").lower()
                msg = (f"Has alcanzado el límite de {self.max_docs} documentos"
                       if lang == "es" else
                       f"You have reached the maximum of {self.max_docs} documents")
                self.log_event(c, user=user, ip_=ip_, endpoint=ep, et="docs_limit_block", sev="error",
                    detail=f"Max docs limit reached: {doc_count}/{self.max_docs}",
                    obs=float(doc_count), th=float(self.max_docs), act="blocked")
                self.apply_docs_penalty(c, user, ip_, ep)
                self.persist_and_raise(c, 429, msg)
            self.check_limit(c, user, ip_, ep, "docs_per_minute", float(self.rate_count(c, user["id"], "doc_create") + 1), float(self.doc_create_per_min), warnings, scope="docs")
        self.check_limit(c, user, ip_, ep, "doc_write_bytes_per_minute", float(self.rate_bytes(c, user["id"], "doc_write") + bytes_write), float(self.doc_write_bpm), warnings, scope="docs")
        return warnings

    def chat_limits(self, c: sqlite3.Connection, user: sqlite3.Row, req: Request, bytes_write: int, create: bool, scope: str = "chat") -> List[Dict[str, Any]]:
        warnings: List[Dict[str, Any]] = []
        ip_, ep = self.ip(req), req.url.path
        if bytes_write > self.chat_max:
            et = "ai_limit_block" if scope == "ai" else "chat_limit_block"
            self.log_event(c, user=user, ip_=ip_, endpoint=ep, et=et, sev="error", detail="Chat exceeds max size", obs=float(bytes_write), th=float(self.chat_max), act="blocked")
            if scope == "ai":
                self.apply_ai_penalty(c, user, ip_, ep)
            self.persist_and_raise(c, 429, "Chat size exceeds limit")
        if create:
            ac = int(c.execute("SELECT COUNT(*) c FROM chats WHERE user_id=? AND is_deleted=0", (user["id"],)).fetchone()["c"])
            if ac >= self.max_chats:
                raise HTTPException(429, "Maximum active chat sessions reached")
            self.check_limit(c, user, ip_, ep, "chat_creations_per_minute", float(self.rate_count(c, user["id"], "chat_create") + 1), float(self.chat_create_per_min), warnings, scope=scope)
        self.check_limit(c, user, ip_, ep, "chat_write_bytes_per_minute", float(self.rate_bytes(c, user["id"], "chat_write") + bytes_write), float(self.chat_write_bpm), warnings, scope=scope)
        return warnings

    # ------------------------------------------------------------------
    # Resource-limit helpers
    # ------------------------------------------------------------------

    def _count_security_events(self, c: sqlite3.Connection, user_id: str, event_type: str, window_sec: int) -> int:
        cutoff = self.now_ts() - window_sec
        return int(c.execute(
            "SELECT COUNT(*) c FROM security_events WHERE user_id=? AND event_type=? AND created_ts>=?",
            (user_id, event_type, cutoff)
        ).fetchone()["c"])

    def _check_flag_escalation(self, c: sqlite3.Connection, user: sqlite3.Row, ip_: str, endpoint: str,
                                event_type: str, window_sec: int, threshold: int, penalty_type: str) -> None:
        if user["role"] == "admin":
            return
        count = self._count_security_events(c, user["id"], event_type, window_sec)
        if count >= threshold:
            if penalty_type == "ai":
                self.apply_ai_penalty(c, user, ip_, endpoint)
            elif penalty_type == "docs":
                self.apply_docs_penalty(c, user, ip_, endpoint)
            self.log_event(c, user=user, ip_=ip_, endpoint=endpoint,
                et="auto_escalation", sev="block",
                detail=f"Auto-escalation triggered: {event_type} count={count} in window={window_sec}s",
                obs=float(count), th=float(threshold), act="auto_cooldown")

    def check_concurrent_generations(self, c: sqlite3.Connection, user: sqlite3.Row,
                                      ip_: str, endpoint: str, request_id: str) -> None:
        if user["role"] == "admin":
            return
        stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        c.execute("DELETE FROM active_generations WHERE started_at < ?", (stale,))
        count = int(c.execute(
            "SELECT COUNT(*) c FROM active_generations WHERE user_id=?", (user["id"],)
        ).fetchone()["c"])
        if count >= self.max_concurrent_generations:
            lang = str(user["preferred_language"] or "en").lower()
            msg = ("Por favor espera hasta que tus otros chats terminen de generar"
                   if lang == "es" else
                   "Please wait until your other chats finish generating")
            self.log_event(c, user=user, ip_=ip_, endpoint=endpoint,
                et="chat_concurrent_blocked", sev="warn",
                detail=f"Concurrent generation limit reached: count={count}",
                obs=float(count), th=float(self.max_concurrent_generations), act="blocked")
            self._check_flag_escalation(c, user, ip_, endpoint,
                "chat_concurrent_blocked", self.ai_offense_window, self.ai_block_hits, "ai")
            self.persist_and_raise(c, 429, msg)
        c.execute(
            "INSERT OR IGNORE INTO active_generations(request_id, user_id, started_at) VALUES(?,?,?)",
            (request_id, user["id"], self.now_iso())
        )

    def remove_active_generation(self, request_id: str) -> None:
        try:
            with self.tx() as c:
                c.execute("DELETE FROM active_generations WHERE request_id=?", (request_id,))
        except Exception:
            logger.warning("Failed to remove active generation %s", request_id, exc_info=True)

    def check_prompt_length(self, c: sqlite3.Connection, user: sqlite3.Row,
                             ip_: str, endpoint: str, user_msg: str) -> None:
        if user["role"] == "admin":
            return
        stripped_len = len(user_msg.replace(" ", "").replace("\n", "").replace("\t", ""))
        lang = str(user["preferred_language"] or "en").lower()
        if stripped_len > 7000:
            msg = (f"El mensaje es demasiado largo ({stripped_len} / 7000 caracteres sin espacios)"
                   if lang == "es" else
                   f"Message is too long ({stripped_len} / 7000 characters excluding spaces)")
            self.log_event(c, user=user, ip_=ip_, endpoint=endpoint,
                et="chat_prompt_too_long", sev="warn",
                detail=f"Prompt too long: {stripped_len} chars (excl whitespace)",
                obs=float(stripped_len), th=7000.0, act="blocked")
            self._check_flag_escalation(c, user, ip_, endpoint,
                "chat_prompt_too_long", self.ai_offense_window, self.ai_block_hits, "ai")
            self.persist_and_raise(c, 400, msg)
        if stripped_len > self.heavy_prompt_chars:
            self.rate_add(c, user["id"], "chat_heavy_prompt", 0)
            self.log_event(c, user=user, ip_=ip_, endpoint=endpoint,
                et="chat_heavy_usage", sev="warn",
                detail=f"Heavy prompt: {stripped_len} chars (excl whitespace)",
                obs=float(stripped_len), th=float(self.heavy_prompt_chars), act="rate_logged")
            heavy_count = float(self.rate_count(c, user["id"], "chat_heavy_prompt", self.ai_offense_window))
            self.check_limit(c, user, ip_, endpoint, "chat_heavy_prompts_per_window",
                heavy_count, float(self.heavy_prompt_hits), [], scope="ai")

    def check_ai_request_rate(self, c: sqlite3.Connection, user: sqlite3.Row, ip_: str, endpoint: str) -> None:
        if user["role"] == "admin":
            return
        lang = str(user["preferred_language"] or "en").lower()
        msg = "Demasiadas solicitudes de IA; por favor espera un momento" if lang == "es" else "Too many AI requests; please wait a moment"
        per_min = self.rate_count(c, user["id"], "ai_request", 60)
        per_hour = self.rate_count(c, user["id"], "ai_request", 3600)
        ip_key = ip_ or "unknown"
        ip_per_min = self.ip_rate_count(c, ip_key, "ai_request", 60)
        observed = per_min
        threshold = self.ai_requests_per_min
        metric = "ai_requests_per_min"
        if per_hour >= self.ai_requests_per_hour:
            observed = per_hour
            threshold = self.ai_requests_per_hour
            metric = "ai_requests_per_hour"
        if ip_per_min >= self.ai_ip_requests_per_min:
            observed = ip_per_min
            threshold = self.ai_ip_requests_per_min
            metric = "ai_ip_requests_per_min"
        if observed >= threshold:
            self.log_event(
                c,
                user=user,
                ip_=ip_,
                endpoint=endpoint,
                et="ai_request_rate_blocked",
                sev="warning",
                detail=f"{metric} exceeded",
                obs=float(observed),
                th=float(threshold),
                act="rate_block",
            )
            self.persist_and_raise(c, 429, msg)
        self.rate_add(c, user["id"], "ai_request", 0)
        self.ip_rate_add(c, ip_key, "ai_request")


# -----------------------------
# FastAPI route mounting
# -----------------------------
def mount_app_storage(app, llama_base_url: str):
    """Create the runtime, initialize storage, and mount all /v1/app/* routes."""
    rt = StorageRuntime(llama_base_url)
    rt.ensure_dirs()
    model_path = Path(rt.llama_model_path)
    if not model_path.exists():
        logger.warning(
            "Model file not found at %s. The llama container will fail to start. "
            "Check LLAMA_MODEL_FILE env var and /models volume mount.",
            model_path,
        )
    rt.init_db()
    rt.seed_admin()
    def _background_startup():
        # Retry RAG validation indefinitely so transient failures (slow disk,
        # missing-then-restored index, model files still being copied) self-heal
        # without requiring a container restart. Without this, a single failure
        # at boot leaves startup_rag_ok=False forever and the portal loading
        # screen never lifts.
        retry_seconds = 60
        # Spanish-first mode: eager-load Spanish before doing any English work.
        # Used when WARMUP_EN_AT_STARTUP=0 — the English collection is left
        # cold and will load lazily on the first English request.
        if rt.load_es_index_at_startup:
            try:
                rt._kick_off_wiki_retriever_es_load()
            except Exception:
                logger.exception("failed to schedule Spanish ChromaDB background load")
        if not rt.warmup_en_at_startup:
            # Wait for the Spanish collection to come online, then validate
            # against it. Sets startup_rag_ok=True so the portal loading
            # screen lifts even though English never warmed.
            attempt = 0
            while True:
                attempt += 1
                try:
                    rt.validate_startup_rag_es()
                except Exception:
                    logger.warning("startup RAG (es) validation exception", exc_info=True)
                if rt._startup_rag_status.get("startup_rag_ok"):
                    logger.info("startup RAG validation (es) succeeded on attempt %d", attempt)
                    break
                logger.warning(
                    "startup RAG validation (es) attempt %d failed; retrying in %ds",
                    attempt, retry_seconds,
                )
                time.sleep(retry_seconds)
            try:
                rt.run_warmup_queries()
            except Exception:
                logger.exception("warmup queries failed after Spanish RAG became ready")
            return
        attempt = 0
        while True:
            attempt += 1
            try:
                rt.validate_startup_rag()
            except Exception:
                logger.warning("startup RAG validation exception", exc_info=True)  # status recorded via _update_rag_status
            if rt._startup_rag_status.get("startup_rag_ok"):
                logger.info("startup RAG validation succeeded on attempt %d", attempt)
                break
            logger.warning(
                "startup RAG validation attempt %d failed; retrying in %ds",
                attempt, retry_seconds,
            )
            time.sleep(retry_seconds)
        try:
            rt.run_warmup_queries()
        except Exception:
            logger.exception("warmup queries failed after RAG became ready")

    threading.Thread(target=_background_startup, daemon=True, name="app-storage-init").start()
    threading.Thread(target=rt.cleanup_loop, daemon=True, name="app-storage-cleanup").start()
    threading.Thread(target=rt.keep_warm_loop, daemon=True, name="app-storage-keep-warm").start()

    @app.middleware("http")
    async def same_origin_write_guard(req: Request, call_next):
        method = str(req.method or "").upper()
        if method in ("POST", "PUT", "PATCH", "DELETE") and req.url.path.startswith("/v1/") and req.cookies.get(rt.cookie):
            try:
                rt.validate_same_origin_write(req)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(req)

    @app.get("/wiki")
    @app.get("/wiki/")
    def wiki_root_redirect(req: Request):
        return RedirectResponse(url=rt.build_wiki_redirect_target(req), status_code=307)

    @app.get("/wiki/{wiki_path:path}")
    def wiki_language_redirect(wiki_path: str, req: Request):
        return RedirectResponse(url=rt.build_wiki_redirect_target(req, wiki_path), status_code=307)

    @app.post("/v1/app/auth/signup")
    def app_signup(p: SignupPayload, req: Request):
        """Create a new account, user directories, and an initial restrictions row.

        Enforces a per-IP hourly signup cap and an admin-controlled kill switch so a
        single abusive client cannot flood the user table. All outcomes are logged to
        security_events for audit visibility.
        """
        username = (p.username or "").strip(); pw = p.password or ""; role = (p.role or "user").strip().lower()
        preferred_language = normalize_language_preference(p.preferred_language, default="en")
        preferred_theme = normalize_theme_preference(p.preferred_theme, default="light")
        ip_ = rt.ip(req)
        endpoint = req.url.path
        if not rt.allow_public_signup:
            with rt.tx() as c:
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="signup_disabled", sev="warning", detail=f"Signup blocked (public signup disabled) for username={username[:50]!r}", act="blocked")
            raise HTTPException(403, "Public signup is disabled")
        if len(username) < 3: raise HTTPException(400, "Username must be at least 3 characters")
        if len(username) > 50: raise HTTPException(400, "Username must be at most 50 characters")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,50}", username):
            raise HTTPException(400, "Username may only contain letters, digits, and the characters . _ @ -")
        min_password_len = rt.guest_password_min_length if role == "guest" else rt.user_password_min_length
        if len(pw) < min_password_len: raise HTTPException(400, f"Password must be at least {min_password_len} characters")
        if len(pw) > 128: raise HTTPException(400, "Password must be at most 128 characters")
        if role not in ("user", "guest"): role = "user"
        # Per-IP signup rate limit. Stored in the FK-free ip_rate_events table
        # since there is no user row to anchor a synthetic ip:<addr> key against.
        rate_key = ip_ or "unknown"
        with rt.tx() as c:
            recent = rt.ip_rate_count(c, rate_key, "signup", sec=3600)
            if recent >= rt.signup_max_per_hour_per_ip:
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="signup_rate_block", sev="warning", detail=f"Signup blocked: {recent} attempts in last hour from ip={ip_}", obs=float(recent), th=float(rt.signup_max_per_hour_per_ip), act="rate_block")
                rt.persist_and_raise(c, 429, "Too many signups from this network; try later")
            if c.execute("SELECT 1 FROM users WHERE username_norm=?", (rt.nuser(username),)).fetchone() is not None:
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="signup_conflict", sev="info", detail=f"Signup rejected: username {username!r} already exists", act="blocked")
                rt.ip_rate_add(c, rate_key, "signup")
                rt.persist_and_raise(c, 409, "Username already exists")
            uid_ = rt.uid(); rt.ensure_user_dirs(uid_)
            c.execute(
                "INSERT INTO users(id,username,username_norm,password_hash,role,created_at,storage_bytes_used,preferred_language,preferred_theme) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid_, username, rt.nuser(username), rt._ph.hash(pw), role, rt.now_iso(), 0, preferred_language, preferred_theme),
            )
            rt.ensure_restrictions_row(c, uid_)
            created_user = c.execute("SELECT * FROM users WHERE id=?", (uid_,)).fetchone()
            rt.record_analytics_event(c, event_type="accounts_created", surface="auth", user=created_user)
            rt.ip_rate_add(c, rate_key, "signup")
            rt.log_event(c, user=created_user, ip_=ip_, endpoint=endpoint, et="account_created", sev="info", detail=f"Account created: {username} role={role}", act="signup")
        return {"ok": True, "user": {"id": uid_, "username": username, "role": role, "preferred_language": preferred_language, "preferred_theme": preferred_theme}}

    @app.post("/v1/app/auth/login")
    def app_login(p: LoginPayload, req: Request):
        """Validate credentials, enforce lockouts, and issue a session cookie."""
        username = (p.username or "").strip(); pw = p.password or ""
        if not username or not pw: raise HTTPException(400, "Missing username or password")
        if len(username) > 50: raise HTTPException(400, "Username must be at most 50 characters")
        if len(pw) > 128: raise HTTPException(400, "Password must be at most 128 characters")
        nn, ip_ = rt.nuser(username), rt.ip(req)
        endpoint = req.url.path
        with rt.tx() as c:
            a = c.execute("SELECT * FROM login_attempts WHERE username_norm=? AND ip=?", (nn, ip_)).fetchone()
            if a is not None and a["lockout_until_ts"] and int(a["lockout_until_ts"]) > rt.now_ts():
                rt.log_event(c, user=None, ip_=ip_, endpoint=endpoint, et="auth_login_blocked", sev="warning", detail=f"Login blocked while locked out: username={username!r}", obs=float(a["fail_count"] or 0), th=float(rt.login_fail_limit), act="locked_out")
                rt.persist_and_raise(c, 429, "Too many login attempts; try later")
            user = c.execute("SELECT * FROM users WHERE username_norm=? AND is_deleted=0", (nn,)).fetchone()
            valid = False
            if user is not None:
                try: rt._ph.verify(user["password_hash"], pw); valid = True
                except VerifyMismatchError: valid = False
            if not valid:
                if a is None or rt.now_ts() - int(a["first_attempt_ts"]) > rt.login_window:
                    fc = 1
                    c.execute("INSERT OR REPLACE INTO login_attempts(username_norm,ip,fail_count,first_attempt_ts,last_attempt_ts,lockout_until_ts) VALUES(?,?,1,?,?,NULL)", (nn, ip_, rt.now_ts(), rt.now_ts()))
                    lock = None
                else:
                    fc = int(a["fail_count"]) + 1
                    lock = rt.now_ts() + rt.login_window if fc >= rt.login_fail_limit else None
                    c.execute("UPDATE login_attempts SET fail_count=?, last_attempt_ts=?, lockout_until_ts=? WHERE username_norm=? AND ip=?", (fc, rt.now_ts(), lock, nn, ip_))
                # Log the failure with attempt count but never the submitted password.
                rt.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="auth_login_failure", sev="warning", detail=f"Failed login for username={username!r}", obs=float(fc), th=float(rt.login_fail_limit), act="auth_fail")
                if lock is not None:
                    rt.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="auth_login_lockout", sev="error", detail=f"Lockout engaged for username={username!r} ip={ip_}", obs=float(fc), th=float(rt.login_fail_limit), act="lockout")
                detail = "Too many login attempts; try later" if a is not None and int(a["fail_count"]) + 1 >= rt.login_fail_limit else "Invalid username or password"
                status_code = 429 if detail.startswith("Too many login attempts") else 401
                rt.persist_and_raise(c, status_code, detail)
            c.execute("DELETE FROM login_attempts WHERE username_norm=? AND ip=?", (nn, ip_))
            rt.ensure_restrictions_row(c, user["id"])
            if rt.is_manual_locked(c, user) and user["role"] != "admin":
                rt.log_event(c, user=user, ip_=ip_, endpoint=endpoint, et="auth_login_blocked", sev="warning", detail=f"Login blocked: account manually locked ({user['username']})", act="manual_locked")
                rt.persist_and_raise(c, 423, "Account is locked by administrator")
            raw = secrets.token_urlsafe(32)
            c.execute("INSERT INTO sessions(id,user_id,token_hash,created_at,expires_at,last_accessed_at,ip,user_agent,revoked_at) VALUES(?,?,?,?,?,?,?,?,NULL)", (rt.uid(), user["id"], rt.token_hash(raw), rt.now_iso(), (datetime.now(timezone.utc)+timedelta(days=rt.session_days)).isoformat(), rt.now_iso(), ip_, req.headers.get("user-agent", "")))
            c.execute("UPDATE users SET last_login_at=?, last_active_at=? WHERE id=?", (rt.now_iso(), rt.now_iso(), user["id"]))
            refreshed_user = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone() or user
            rt.record_analytics_event(c, event_type="logins_succeeded", surface="auth", user=refreshed_user)
            rt.log_event(c, user=refreshed_user, ip_=ip_, endpoint=endpoint, et="auth_login_success", sev="info", detail=f"Login success: {user['username']} role={user['role']}", act="auth_success")
            return rt.mk_resp({"ok": True, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}, token=raw)

    @app.post("/v1/app/auth/logout")
    def app_logout(req: Request):
        tok = req.cookies.get(rt.cookie)
        if tok:
            with rt.tx() as c:
                row = c.execute(
                    "SELECT u.id AS id, u.username AS username, u.role AS role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? LIMIT 1",
                    (rt.token_hash(tok),),
                ).fetchone()
                c.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (rt.now_iso(), rt.token_hash(tok)))
                if row is not None:
                    rt.log_event(c, user=row, ip_=rt.ip(req), endpoint=req.url.path, et="auth_logout", sev="info", detail=f"Logout: {row['username']}", act="auth_logout")
                    if row["role"] == "guest":
                        c.execute("UPDATE users SET guest_logout_at=? WHERE id=?", (rt.now_iso(), row["id"]))
        return rt.mk_resp({"ok": True}, clear=True)

    @app.get("/v1/app/auth/me")
    def app_me(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            rr = rt.get_restrictions(c, u["id"])
            lock_until = "permanent" if int(rr["manual_lock_permanent"] or 0) == 1 else rr["manual_locked_until"]
            return {
                "ok": True,
                "user": {
                    "id": u["id"],
                    "username": u["username"],
                    "role": u["role"],
                    "created_at": u["created_at"],
                    "last_login_at": u["last_login_at"],
                    "last_active_at": u["last_active_at"],
                    "storage_bytes_used": int(u["storage_bytes_used"]),
                    "preferred_language": normalize_language_preference(u["preferred_language"], default="en"),
                    "preferred_theme": normalize_theme_preference(u["preferred_theme"], default="light"),
                    "locked_until": lock_until,
                    "docs_write_blocked_until": rr["docs_write_blocked_until"],
                    "ai_prompt_cooldown_until": rr["ai_prompt_cooldown_until"],
                    "ai_send_blocked_until": rr["ai_send_blocked_until"],
                },
            }

    @app.post("/v1/app/auth/preferences")
    def app_update_preferences(p: PreferencePayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            preferred_language = normalize_language_preference(p.preferred_language, default=normalize_language_preference(u["preferred_language"], default="en"))
            preferred_theme = normalize_theme_preference(p.preferred_theme, default=normalize_theme_preference(u["preferred_theme"], default="light"))
            c.execute("UPDATE users SET preferred_language=?, preferred_theme=? WHERE id=?", (preferred_language, preferred_theme, u["id"]))
            return {"ok": True, "user": {"id": u["id"], "username": u["username"], "role": u["role"], "preferred_language": preferred_language, "preferred_theme": preferred_theme}}

    @app.get("/v1/app/admin/users")
    def app_admin_users(req: Request):
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            flag_cutoff = rt.now_ts() - 86400
            rows = c.execute(
                """
SELECT u.id,u.username,u.role,u.created_at,u.last_login_at,u.last_active_at,u.storage_bytes_used,
       CASE WHEN COALESCE(r.manual_lock_permanent,0)=1 THEN 'permanent' ELSE r.manual_locked_until END AS locked_until,
       r.manual_lock_reason AS lock_reason,
       r.docs_write_blocked_until,r.ai_prompt_cooldown_until,r.ai_send_blocked_until,
       COALESCE(r.manual_lock_permanent,0) AS manual_lock_permanent,
       COALESCE((SELECT COUNT(*) FROM security_events se
                 WHERE se.user_id=u.id AND se.severity IN ('warn','block')
                 AND se.created_ts >= ?), 0) AS recent_flag_count
FROM users u
LEFT JOIN user_restrictions r ON r.user_id=u.id
WHERE u.is_deleted=0
ORDER BY u.username COLLATE NOCASE
                """,
                (flag_cutoff,)
            ).fetchall()
            return {"ok": True, "users": [dict(r) for r in rows]}

    @app.post("/v1/app/admin/users/{uid_}/reset-password")
    def app_admin_reset(uid_: str, p: ResetPasswordPayload, req: Request):
        if len(p.password or "") < rt.admin_password_min_length: raise HTTPException(400, f"Password must be at least {rt.admin_password_min_length} characters")
        if len(p.password or "") > 128: raise HTTPException(400, "Password must be at most 128 characters")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None: raise HTTPException(404, "User not found")
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (rt._ph.hash(p.password), uid_))
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_password_reset", sev="info", detail=f"Password reset for {u['username']}", act="password_reset")
        return {"ok": True}

    @app.post("/v1/app/admin/users/{uid_}/role")
    def app_admin_role(uid_: str, p: RolePayload, req: Request):
        role = (p.role or "").strip().lower()
        if role not in ("admin", "user", "guest"): raise HTTPException(400, "Invalid role")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            target = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if target is None: raise HTTPException(404, "User not found")
            old_role = target["role"]
            if old_role == role:
                return {"ok": True, "role": role, "unchanged": True}
            reason = str(p.reason or "").strip()
            if role == "admin" and old_role != "admin":
                if p.confirm != "PROMOTE_ADMIN" or len(reason) < 8:
                    raise HTTPException(400, "Promoting a user to admin requires confirm='PROMOTE_ADMIN' and a reason")
            if old_role == "admin" and role != "admin":
                admin_count = int(c.execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND is_deleted=0").fetchone()["c"])
                if admin_count <= 1:
                    raise HTTPException(409, "Cannot demote the last admin account")
                if len(reason) < 8:
                    raise HTTPException(400, "Demoting an admin requires a reason")
            c.execute("UPDATE users SET role=? WHERE id=?", (role, uid_))
            rt.ensure_restrictions_row(c, uid_)
            sev = "warning" if role == "admin" or old_role == "admin" else "info"
            reason_suffix = f" reason={reason[:120]!r}" if reason else ""
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_role_change", sev=sev, detail=f"{admin['username']} changed role of {target['username']}: {old_role} -> {role}.{reason_suffix}", act="role_change")
        return {"ok": True, "role": role}

    @app.post("/v1/app/admin/users/{uid_}/lock")
    def app_admin_lock(uid_: str, p: LockPayload, req: Request):
        reason = (p.reason or "").strip()
        if not reason:
            raise HTTPException(400, "Lock reason is required")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None: raise HTTPException(404, "User not found")
            permanent = bool(p.permanent)
            until = None
            if not permanent:
                minutes = max(1, min(int(p.duration_minutes or 30), 525600))
                until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            rt.ensure_restrictions_row(c, uid_)
            c.execute(
                "UPDATE user_restrictions SET manual_locked_until=?, manual_lock_reason=?, manual_locked_by=?, manual_lock_permanent=?, updated_at=? WHERE user_id=?",
                (until, reason, admin["username"], 1 if permanent else 0, rt.now_iso(), uid_),
            )
            rt.sync_legacy_lock_fields(c, uid_)
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_lock", sev="warning", detail=f"Locked {u['username']} ({reason})", act="lock")
        return {"ok": True, "locked_until": (until or "permanent"), "permanent": permanent}

    @app.post("/v1/app/admin/users/{uid_}/unlock")
    def app_admin_unlock(uid_: str, p: UnlockPayload, req: Request):
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None: raise HTTPException(404, "User not found")
            rt.ensure_restrictions_row(c, uid_)
            c.execute("UPDATE user_restrictions SET manual_locked_until=NULL, manual_lock_reason=NULL, manual_locked_by=NULL, manual_lock_permanent=0, updated_at=? WHERE user_id=?", (rt.now_iso(), uid_))
            rt.sync_legacy_lock_fields(c, uid_)
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_unlock", sev="info", detail=f"Unlocked {u['username']} ({p.reason or 'manual'})", act="unlock")
        return {"ok": True}

    @app.post("/v1/app/admin/users/{uid_}/delete")
    def app_admin_delete_user(uid_: str, req: Request):
        user_dir_removed = False
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            u = c.execute("SELECT * FROM users WHERE id=? AND is_deleted=0", (uid_,)).fetchone()
            if u is None:
                raise HTTPException(404, "User not found")
            if u["id"] == admin["id"]:
                raise HTTPException(409, "You cannot delete your own account")
            if u["role"] == "admin":
                raise HTTPException(403, "Admin accounts cannot be deleted")
            username_norm = str(u["username_norm"] or "")
            c.execute("DELETE FROM login_attempts WHERE username_norm=?", (username_norm,))
            c.execute("DELETE FROM security_events WHERE user_id=?", (uid_,))
            c.execute("DELETE FROM rate_events WHERE user_id=?", (uid_,))
            c.execute("DELETE FROM users WHERE id=?", (uid_,))
            rt.log_event(c, user=admin, ip_=rt.ip(req), endpoint=req.url.path, et="admin_delete_user", sev="warning", detail=f"Deleted {u['username']}", act="delete_user")
        try:
            user_dir_removed = rt.remove_user_dir(uid_)
        except Exception:
            logger.exception("Failed to remove user directory for deleted user %s", uid_)
        return {"ok": True, "user_dir_removed": user_dir_removed}

    @app.get("/v1/app/admin/storage-insights")
    def app_admin_storage_insights(req: Request, top_users: int = 20, top_files: int = 40):
        top_users = max(1, min(int(top_users), 200))
        top_files = max(1, min(int(top_files), 200))
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            users = c.execute(
                "SELECT id,username,role,storage_bytes_used,last_active_at,last_login_at,created_at FROM users WHERE is_deleted=0 ORDER BY storage_bytes_used DESC LIMIT ?",
                (top_users,),
            ).fetchall()
            docs = c.execute(
                """
SELECT d.id,d.user_id,u.username,d.title,d.type,d.size_bytes,d.created_at,d.updated_at,d.last_accessed_at,
       d.is_deleted,d.is_starred,d.deleted_at
FROM documents d JOIN users u ON u.id=d.user_id
ORDER BY d.size_bytes DESC, COALESCE(d.last_accessed_at,d.updated_at,d.created_at) ASC
LIMIT ?
                """,
                (top_files,),
            ).fetchall()
            chats = c.execute(
                """
SELECT h.id,h.user_id,u.username,h.title,h.size_bytes,h.token_count_estimate,h.created_at,h.updated_at,h.last_accessed_at,
       h.is_deleted,h.deleted_at
FROM chats h JOIN users u ON u.id=h.user_id
ORDER BY h.size_bytes DESC, COALESCE(h.last_accessed_at,h.updated_at,h.created_at) ASC
LIMIT ?
                """,
                (top_files,),
            ).fetchall()
            return {"ok": True, "top_users": [dict(r) for r in users], "largest_documents": [dict(r) for r in docs], "largest_chats": [dict(r) for r in chats]}

    @app.post("/v1/app/admin/storage-cleanup")
    def app_admin_storage_cleanup(p: CleanupPayload, req: Request):
        reason = str(p.reason or "admin").strip()[:80] or "admin"
        required = max(0, int(p.required_bytes or 0))
        dry_run = bool(True if p.dry_run is None else p.dry_run)
        with rt.tx() as c:
            rt.req_user(c, req, admin=True, write=not dry_run)
        try:
            result = rt.run_cleanup(reason=f"admin:{reason}", required=required, dry_run=dry_run)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True, "cleanup": result}

    @app.get("/v1/app/admin/security-events")
    def app_admin_events(req: Request, limit: int = 200):
        limit = max(1, min(int(limit), 1000))
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            rows = c.execute("SELECT * FROM security_events ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
            return {"ok": True, "events": [dict(r) for r in rows]}

    @app.get("/v1/app/admin/analytics/summary")
    def app_admin_analytics_summary(req: Request, date_from: Optional[str] = None, date_to: Optional[str] = None):
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            start_day, end_day = rt.analytics_range(date_from, date_to)
            payload = rt.analytics_payload(c, start_day, end_day)
            return {
                "ok": True,
                "date_from": payload["date_from"],
                "date_to": payload["date_to"],
                "generated_at": payload["generated_at"],
                "metrics_version": payload["metrics_version"],
                "summary": payload["summary"],
            }

    @app.get("/v1/app/admin/analytics/timeseries")
    def app_admin_analytics_timeseries(req: Request, date_from: Optional[str] = None, date_to: Optional[str] = None):
        with rt.tx() as c:
            rt.req_user(c, req, admin=True)
            start_day, end_day = rt.analytics_range(date_from, date_to)
            payload = rt.analytics_payload(c, start_day, end_day)
            return {
                "ok": True,
                "date_from": payload["date_from"],
                "date_to": payload["date_to"],
                "generated_at": payload["generated_at"],
                "metrics_version": payload["metrics_version"],
                "timeseries": payload["timeseries"],
            }

    @app.get("/v1/app/admin/analytics/export")
    def app_admin_analytics_export(
        req: Request,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        export_format: str = "json",
    ):
        fmt = str(export_format or "json").strip().lower()
        if fmt not in ("json", "csv"):
            raise HTTPException(400, "Invalid export_format")
        with rt.tx() as c:
            admin = rt.req_user(c, req, admin=True, write=True)
            start_day, end_day = rt.analytics_range(date_from, date_to)
            payload = rt.analytics_payload(c, start_day, end_day)
            c.execute(
                """
                INSERT INTO analytics_exports(
                    id,format,date_from,date_to,created_at,created_by_user_id,status,file_path,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    rt.uid(),
                    fmt,
                    start_day,
                    end_day,
                    rt.now_iso(),
                    admin["id"],
                    "generated",
                    None,
                    rt._analytics_metadata_json({
                        "metrics_version": payload.get("metrics_version"),
                        "row_count": len(payload.get("timeseries") or []),
                    }),
                ),
            )
            filename = f"aibox-analytics-{start_day}-to-{end_day}.{fmt}"
            headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
            if fmt == "csv":
                return Response(rt.analytics_csv(payload), media_type="text/csv; charset=utf-8", headers=headers)
            return JSONResponse(payload, headers=headers)

    @app.post("/v1/app/analytics/events")
    def app_analytics_event_ingest(p: AnalyticsEventPayload, req: Request):
        event_name = str(p.event_name or "").strip().lower()
        surface = str(p.surface or "").strip().lower()
        allowed_surfaces = {"chat", "docs", "wiki", "learn", "portal", "auth", "admin"}
        with rt.tx() as c:
            u = rt.req_user(c, req)
            if event_name not in rt.analytics_frontend_events:
                rt.log_event(c, user=u, ip_=rt.ip(req), endpoint=req.url.path, et="analytics_event_rejected", sev="warn", detail=f"Invalid analytics event_name: {event_name}", act="analytics_rejected")
                raise HTTPException(400, "Invalid event_name")
            if surface not in allowed_surfaces:
                rt.log_event(c, user=u, ip_=rt.ip(req), endpoint=req.url.path, et="analytics_event_rejected", sev="warn", detail=f"Invalid analytics surface: {surface}", act="analytics_rejected")
                raise HTTPException(400, "Invalid surface")
            rt.record_analytics_event(
                c,
                event_type=event_name,
                surface=surface,
                user=u,
                metadata=rt._clone_data(p.metadata or {}),
            )
        return {"ok": True}
    def write_payload(c: sqlite3.Connection, user: sqlite3.Row, req: Request, path: Path, obj: Dict[str, Any], op: str) -> int:
        try:
            return rt.write_json_atomic(path, obj)
        except Exception as e:
            rt.log_event(
                c,
                user=user,
                ip_=rt.ip(req),
                endpoint=req.url.path,
                et="file_write_failed",
                sev="error",
                detail=f"{op}: {type(e).__name__}: {e}",
                act="write_failed",
            )
            raise HTTPException(500, "Failed to persist content")

    def enforce_content_access(user: sqlite3.Row, owner_id: str) -> None:
        if user["role"] == "admin" and user["id"] != owner_id:
            raise HTTPException(403, "Admin cannot access other users' content bodies")

    def get_doc(c: sqlite3.Connection, u: sqlite3.Row, did: str, include_deleted: bool = False) -> sqlite3.Row:
        q = "SELECT * FROM documents WHERE id=?" + ("" if u["role"] == "admin" else " AND user_id=?")
        args = (did,) if u["role"] == "admin" else (did, u["id"])
        r = c.execute(q, args).fetchone()
        if r is None:
            raise HTTPException(404, "Document not found")
        enforce_content_access(u, r["user_id"])
        if not include_deleted and int(r["is_deleted"]) == 1:
            raise HTTPException(400, "Document is deleted")
        return r

    def get_chat(c: sqlite3.Connection, u: sqlite3.Row, cid: str, include_deleted: bool = False) -> sqlite3.Row:
        q = "SELECT * FROM chats WHERE id=?" + ("" if u["role"] == "admin" else " AND user_id=?")
        args = (cid,) if u["role"] == "admin" else (cid, u["id"])
        r = c.execute(q, args).fetchone()
        if r is None:
            raise HTTPException(404, "Chat not found")
        enforce_content_access(u, r["user_id"])
        if not include_deleted and int(r["is_deleted"]) == 1:
            raise HTTPException(400, "Chat is deleted")
        return r

    def load_chat_json(row: sqlite3.Row) -> Dict[str, Any]:
        p = rt.safe_path(row["file_path"], row["user_id"])
        if not p.exists():
            return {"id": row["id"], "title": row["title"], "messages": [], "created_at": row["created_at"], "updated_at": row["updated_at"], "version": 1}
        return rt.read_json(p)

    def append_chat(
        c: sqlite3.Connection,
        row: sqlite3.Row,
        role: str,
        text: str,
        user: sqlite3.Row,
        req: Request,
        reason: str,
        scope: str = "chat",
        citations: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[int, int, List[Dict[str, Any]]]:
        o = load_chat_json(row)
        msgs = o.get("messages") if isinstance(o.get("messages"), list) else []
        message = {"role": role, "content": str(text or ""), "created_at": rt.now_iso()}
        if citations:
            message["citations"] = rt._clone_data(citations)
        msgs.append(message)
        o["messages"] = msgs
        o["updated_at"] = rt.now_iso()
        o["version"] = int(o.get("version", 0)) + 1
        o["title"] = row["title"]
        size_est = len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
        warns = rt.chat_limits(c, user, req, size_est, create=False, scope=scope)
        rt.ensure_capacity(size_est, reason, c)
        b = write_payload(c, user, req, rt.safe_path(row["file_path"], row["user_id"]), o, reason)
        t = sum(rt.estimate_tokens(str(m.get("content", ""))) for m in msgs)
        return b, t, warns

    def normalize_messages(msgs_in: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if not isinstance(msgs_in, list):
            return out
        for m in msgs_in:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).strip().lower()
            if role == "ai":
                role = "assistant"
            if role not in ("user", "assistant"):
                continue
            out.append({"role": role, "content": str(m.get("content", "") or "")})
        return out

    async def build_model_conversation(
        base_messages: List[Dict[str, str]],
        retrieval_enabled: bool,
        summary: Optional[Dict[str, Any]] = None,
        request_start_t: Optional[float] = None,
        response_language: Optional[str] = None,
        user_language: str = "en",
        request_base_url: Optional[str] = None,
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        conversation = [dict(m) for m in base_messages]
        retrieval_meta = rt._base_retrieval_meta(retrieval_enabled)
        latest_user = next((str(m.get("content", "") or "") for m in reversed(conversation) if str(m.get("role", "")).strip().lower() == "user"), "")
        query = rt.build_retrieval_query(conversation).strip() if retrieval_enabled else ""
        retrieval_meta["retrieval_query"] = query or None
        retrieval_meta["retrieval_attempted"] = bool(retrieval_enabled)
        if summary is not None:
            summary["raw_user_message"] = latest_user
            summary.setdefault("normalized_user_message", latest_user.strip())
        if summary is not None and request_start_t is not None:
            rt._trace_diagnostics(summary, request_start_t, "conversation_history_loaded", message_count=len(conversation))
        if not retrieval_enabled:
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            return conversation, retrieval_meta
        if summary is not None and request_start_t is not None:
            rt._trace_diagnostics(summary, request_start_t, "rag_query_built", query_length=len(query or ""))
        if rt._should_skip_retrieval(latest_user):
            retrieval_meta["retrieval_skipped_reason"] = "skipped_non_encyclopedic_query"
            retrieval_meta["retrieval_error"] = "skipped_non_encyclopedic_query"
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            return conversation, retrieval_meta
        logger.debug("retrieval query len=%d", len(query))
        if not query:
            retrieval_meta["retrieval_skipped_reason"] = "missing_user_query"
            retrieval_meta["retrieval_error"] = "missing_user_query"
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            return conversation, retrieval_meta
        start_t = time.perf_counter()
        if summary is not None and request_start_t is not None:
            rt._trace_diagnostics(summary, request_start_t, "retrieval_started")
        try:
            work = asyncio.to_thread(rt.prepare_wiki_context, query, user_language)
            if rt.retrieval_timeout_seconds > 0:
                payload = await asyncio.wait_for(work, timeout=rt.retrieval_timeout_seconds)
            else:
                payload = await work
            context = str(payload.get("context", "") or "")
            selected_chunks = rt._decorate_chunk_list_with_citations(payload.get("selected_chunks") or [], request_base_url)
            candidate_chunks = rt._decorate_chunk_list_with_citations(payload.get("retrieval_candidates") or [], request_base_url)
            citations = rt._citations_from_chunks(selected_chunks, request_base_url)
            retrieval_meta["retrieval_ms"] = int((time.perf_counter() - start_t) * 1000)
            retrieval_meta["retrieval_candidate_count"] = int(payload.get("candidate_count") or 0)
            retrieval_meta["retrieval_count"] = len(selected_chunks)
            retrieval_meta["retrieval_context_chars"] = len(context)
            retrieval_meta["retrieval_used"] = bool(context)
            retrieval_meta["retrieved_chunks"] = selected_chunks
            retrieval_meta["retrieval_candidates"] = candidate_chunks
            retrieval_meta["citations"] = citations
            retrieval_meta["primary_citation"] = citations[0] if citations else None
            retrieval_meta["rerank_model_path"] = rt.rerank_model
            retrieval_meta["rerank_enabled"] = bool(payload.get("rerank_enabled"))
            retrieval_meta["rerank_ms"] = int(payload.get("rerank_ms") or 0)
            retrieval_meta["rerank_error"] = payload.get("rerank_error")
            retrieval_meta["chunks_after_rerank"] = int(payload.get("chunks_after_rerank") or 0)
            retrieval_meta["chunks_after_budget_trim"] = int(payload.get("chunks_after_budget_trim") or 0)
            retrieval_meta["final_context_estimated_tokens"] = int(payload.get("context_tokens_estimate") or 0)
            retrieval_meta["rag_chunk_truncation_count"] = int(payload.get("chunk_truncation_count") or 0)
            retrieval_meta["rag_index_language"] = payload.get("rag_index_language", "en")
            retrieval_meta["rag_collection_path"] = payload.get("rag_collection_path")
            retrieval_meta["index_manifest"] = payload.get("index_manifest")
            retrieval_meta["index_manifest_path"] = payload.get("index_manifest_path")
            retrieval_meta["index_manifest_error"] = payload.get("index_manifest_error")
            if summary is not None and request_start_t is not None:
                rt._trace_diagnostics(summary, request_start_t, "retrieval_completed", candidate_count=retrieval_meta["retrieval_candidate_count"], selected_count=retrieval_meta["retrieval_count"])
                rt._trace_diagnostics(summary, request_start_t, "rerank_completed", rerank_ms=retrieval_meta["rerank_ms"], rerank_enabled=retrieval_meta["rerank_enabled"])
            _rag_idx_lang = retrieval_meta.get("rag_index_language", "en")
            if context:
                retrieval_message = rt._build_retrieval_system_message(context, response_language=response_language, rag_index_language=_rag_idx_lang)
                retrieval_meta["retrieval_system_message"] = retrieval_message
                conversation = rt.inject_wiki_context(conversation, context, response_language=response_language, rag_index_language=_rag_idx_lang)
            else:
                retrieval_meta["retrieval_error"] = str(payload.get("reason") or "no_relevant_chunks")
                retrieval_meta["retrieval_skipped_reason"] = retrieval_meta["retrieval_error"]
                retrieval_meta["rag_fallback_triggered"] = True
                retrieval_meta["no_context_answer_mode"] = True
                conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
        except asyncio.TimeoutError:
            retrieval_meta["retrieval_ms"] = int((time.perf_counter() - start_t) * 1000)
            retrieval_meta["retrieval_timed_out"] = True
            retrieval_meta["retrieval_error"] = "retrieval_timeout"
            retrieval_meta["rag_fallback_triggered"] = True
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
        except Exception as exc:
            retrieval_meta["retrieval_ms"] = int((time.perf_counter() - start_t) * 1000)
            retrieval_meta["retrieval_error"] = f"{type(exc).__name__}: {exc}"
            retrieval_meta["rag_fallback_triggered"] = True
            retrieval_meta["no_context_answer_mode"] = True
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
        if retrieval_meta.get("retrieval_used"):
            chunk_titles = list(dict.fromkeys(
                str(c.get("title") or "Unknown") for c in retrieval_meta.get("retrieved_chunks", [])
            ))
            lang_label = "ES index" if retrieval_meta.get("rag_index_language") == "es" else "EN index"
            retrieval_meta["rag_summary"] = f"Retrieved {retrieval_meta.get('retrieval_count', 0)} chunks from: {', '.join(chunk_titles)} [{lang_label}]"
        elif retrieval_meta.get("retrieval_skipped_reason"):
            retrieval_meta["rag_summary"] = f"Retrieval skipped: {retrieval_meta['retrieval_skipped_reason']}"
        elif retrieval_meta.get("retrieval_error"):
            retrieval_meta["rag_summary"] = f"Retrieval failed: {retrieval_meta['retrieval_error']}"
        else:
            retrieval_meta["rag_summary"] = "Retrieval not attempted"
        return conversation, retrieval_meta

    async def stream_model_text(
        model: str,
        base_messages: List[Dict[str, str]],
        summary: Dict[str, Any],
        retrieval_enabled: bool,
        response_language: Optional[str] = None,
        user_language: str = "en",
        request_base_url: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        request_start_t = float(summary.get("_request_start_perf") or time.perf_counter())
        summary.setdefault("stage_trace", [])
        summary.setdefault("request_started_at", rt.now_iso())
        summary["model_name"] = model
        summary["stream"] = True
        summary.setdefault("base_messages", rt._clone_data(base_messages))
        summary.setdefault("resource_start", rt._resource_snapshot())
        if retrieval_enabled:
            conversation, retrieval_meta = await build_model_conversation(
                base_messages,
                retrieval_enabled,
                summary,
                request_start_t,
                response_language=response_language,
                user_language=user_language,
                request_base_url=request_base_url,
            )
        else:
            conversation = [dict(m) for m in base_messages]
            conversation = rt.inject_wiki_context(conversation, "", response_language=response_language)
            retrieval_meta = rt._base_retrieval_meta(False)
            rt._trace_diagnostics(summary, request_start_t, "conversation_history_loaded", message_count=len(conversation))
        retrieval_meta["request_prep_ms"] = int((time.perf_counter() - request_start_t) * 1000)
        if not conversation:
            raise HTTPException(400, "Missing messages")
        summary["final_conversation"] = rt._clone_data(conversation)
        summary["retrieval_system_message"] = retrieval_meta.get("retrieval_system_message")
        summary.update(retrieval_meta)
        rt._trace_diagnostics(summary, request_start_t, "final_prompt_assembled", message_count=len(conversation))

        model_start_t = time.perf_counter()
        ttft_ms: Optional[int] = None
        upstream_open_ms: Optional[int] = None
        max_token_gap_ms = 0
        continuation_gap_ms = 0
        last_token_t: Optional[float] = None
        last_token_at: Optional[str] = None
        pending_continuation_started_at: Optional[float] = None
        continuation_count = 0
        final_finish = "stop"
        limit_hit = False
        text_out = ""
        prompt_tokens_est = sum(rt.estimate_tokens(str(m.get("content", ""))) for m in conversation)
        usage: Dict[str, Any] = {}
        first_stream_chunk_ms: Optional[int] = None
        # Granular timeouts: a fast dial + an unhurried read window. The old
        # flat 180 s applied to every phase, which let a stuck connect block
        # for three minutes. read=180 s preserves the existing tolerance for
        # very long completions; connect=5 s and pool=10 s surface upstream
        # outages immediately instead of hanging the request.
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=10.0)) as client:
            initial_request_logged = False
            while True:
                req_body = {"model": model, "messages": conversation, "stream": True}
                if not initial_request_logged:
                    summary["upstream_request_body"] = rt._clone_data(req_body)
                    initial_request_logged = True
                segment = ""
                finish_reason = "stop"
                open_start_t = time.perf_counter()
                rt._trace_diagnostics(summary, request_start_t, "generation_started")
                async with client.stream("POST", f"{rt.llama_base_url}/v1/chat/completions", json=req_body) as resp:
                    if upstream_open_ms is None:
                        upstream_open_ms = int((time.perf_counter() - open_start_t) * 1000)
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="ignore")
                        raise HTTPException(resp.status_code, body or "Model request failed")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        choice = ((obj.get("choices") or [{}])[0]) if isinstance(obj, dict) else {}
                        delta = choice.get("delta") or {}
                        part = str(delta.get("content", "") or "")
                        if part:
                            now_t = time.perf_counter()
                            if ttft_ms is None:
                                ttft_ms = int((now_t - model_start_t) * 1000)
                                first_stream_chunk_ms = ttft_ms
                                rt._trace_diagnostics(summary, request_start_t, "first_token_emitted")
                            if pending_continuation_started_at is not None:
                                continuation_gap_ms = max(
                                    continuation_gap_ms,
                                    int((now_t - pending_continuation_started_at) * 1000),
                                )
                                pending_continuation_started_at = None
                            if last_token_t is not None:
                                max_token_gap_ms = max(max_token_gap_ms, int((now_t - last_token_t) * 1000))
                            last_token_t = now_t
                            last_token_at = rt.now_iso()
                            segment += part
                            text_out += part
                            yield part
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = str(fr)
                        if isinstance(obj.get("usage"), dict):
                            usage = obj.get("usage")
                final_finish = finish_reason or final_finish
                conversation.append({"role": "assistant", "content": segment})
                truncated = final_finish in ("length", "max_tokens")
                if truncated and continuation_count < rt.chat_continuation_limit and segment.strip():
                    continuation_count += 1
                    conversation.append({"role": "user", "content": rt.chat_continue_prompt})
                    pending_continuation_started_at = time.perf_counter()
                    continue
                limit_hit = bool(truncated and continuation_count >= rt.chat_continuation_limit)
                break
        total_ms = int((time.perf_counter() - request_start_t) * 1000)
        generation_ms = max(1, total_ms - int(retrieval_meta.get("request_prep_ms") or 0) - (ttft_ms or 0))
        completion_tokens_est = rt.estimate_tokens(text_out)
        tps = round(completion_tokens_est / max(generation_ms / 1000.0, 0.001), 4)
        summary.update(
            {
                "text": text_out,
                "finish_reason": final_finish,
                "continuation_count": continuation_count,
                "continuation_limit_hit": limit_hit,
                "ttft_ms": ttft_ms,
                "first_stream_chunk_ms": first_stream_chunk_ms,
                "upstream_open_ms": int(upstream_open_ms or 0),
                "max_token_gap_ms": max_token_gap_ms,
                "continuation_gap_ms": continuation_gap_ms,
                "last_token_at": last_token_at,
                "total_ms": total_ms,
                "generation_ms": generation_ms,
                "prompt_tokens": int(usage.get("prompt_tokens") or prompt_tokens_est),
                "completion_tokens": int(usage.get("completion_tokens") or completion_tokens_est),
                "total_tokens": int(usage.get("total_tokens") or (prompt_tokens_est + completion_tokens_est)),
                "tps": tps,
                "prompt_tokens_estimate": int(usage.get("prompt_tokens") or prompt_tokens_est),
                "completion_tokens_estimate": int(usage.get("completion_tokens") or completion_tokens_est),
                "total_tokens_estimate": int(usage.get("total_tokens") or (prompt_tokens_est + completion_tokens_est)),
                "tps_estimate": tps,
                "request_finished_at": rt.now_iso(),
                "resource_end": rt._resource_snapshot(),
                "runtime_warning_text": None,
            }
        )
        summary.update(retrieval_meta)
        rt._trace_diagnostics(summary, request_start_t, "generation_completed", total_ms=total_ms)

    @app.get("/v1/app/docs")
    def docs_list(req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            where = "user_id=?"
            args: Tuple[Any, ...] = (u["id"],)
            if not include_deleted: where += " AND is_deleted=0"
            rows = c.execute(f"SELECT id,user_id,title,type,created_at,updated_at,last_accessed_at,size_bytes,is_starred,is_deleted,deleted_at FROM documents WHERE {where} ORDER BY COALESCE(last_accessed_at,updated_at,created_at) DESC", args).fetchall()
            return {"ok": True, "documents": [dict(r) for r in rows], "warning": rt.storage_warning()}

    @app.post("/v1/app/docs")
    def docs_create(p: CreateDocPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u); rt.ensure_user_dirs(u["id"])
            did = rt.uid(); title = (p.title or "Untitled Document").strip() or "Untitled Document"; typ = (p.type or "markdown").strip().lower() or "markdown"; typ = typ if typ in ("markdown", "json") else "markdown"
            obj = {"id": did, "title": title, "content_markdown": rt.sanitize_markdown(p.content_markdown), "created_at": rt.now_iso(), "updated_at": rt.now_iso(), "version": 1}
            size_est = len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            warns = rt.doc_limits(c, u, req, size_est, create=True)
            rt.ensure_capacity(size_est, "doc_create", c)
            fp = rt.doc_rel(u["id"], did); b = write_payload(c, u, req, rt.safe_path(fp, u["id"]), obj, "doc_create")
            c.execute("INSERT INTO documents(id,user_id,title,type,created_at,updated_at,last_accessed_at,size_bytes,file_path,is_starred,is_deleted,deleted_at,deleted_by_user,is_guest_owned) VALUES(?,?,?,?,?,?,?,?,?,0,0,NULL,0,?)", (did, u["id"], title, typ, rt.now_iso(), rt.now_iso(), rt.now_iso(), int(b), fp, 1 if u["role"] == "guest" else 0))
            rt.rate_add(c, u["id"], "doc_create", 0); rt.rate_add(c, u["id"], "doc_write", int(b)); rt.recalc_storage(c, u["id"])
            rt.record_analytics_event(c, event_type="documents_created", surface="docs", user=u, metadata={"document_id": did, "type": typ, "size_bytes": int(b)})
            sw = rt.storage_warning()
            if sw: warns.append(sw)
            return {"ok": True, "document": {"id": did, "title": title, "type": typ, "updated_at": rt.now_iso(), "size_bytes": int(b), "is_starred": False}, "warnings": warns}

    @app.get("/v1/app/docs/{did}")
    def docs_get(did: str, req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            r = get_doc(c, u, did, include_deleted=include_deleted)
            pth = rt.safe_path(r["file_path"], r["user_id"])
            body = rt.read_json(pth) if pth.exists() else {}
            c.execute("UPDATE documents SET last_accessed_at=? WHERE id=?", (rt.now_iso(), did))
            if int(r["is_deleted"] or 0) == 0:
                rt.record_analytics_event(c, event_type="documents_opened", surface="docs", user=u, metadata={"document_id": did, "type": r["type"]})
            return {"ok": True, "document": {"id": r["id"], "title": r["title"], "type": r["type"], "content_markdown": body.get("content_markdown", ""), "created_at": r["created_at"], "updated_at": r["updated_at"], "is_starred": bool(r["is_starred"]), "is_deleted": bool(r["is_deleted"])}}

    @app.patch("/v1/app/docs/{did}")
    def docs_update(did: str, p: UpdateDocPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did)
            path = rt.safe_path(r["file_path"], r["user_id"])
            cur = rt.read_json(path) if path.exists() else {"id": r["id"], "title": r["title"], "content_markdown": "", "created_at": r["created_at"], "updated_at": r["updated_at"], "version": 1}
            if p.title is not None: cur["title"] = p.title.strip() or "Untitled Document"
            if p.content_markdown is not None: cur["content_markdown"] = rt.sanitize_markdown(p.content_markdown)
            cur["updated_at"] = rt.now_iso(); cur["version"] = int(cur.get("version", 0)) + 1
            size_est = len(json.dumps(cur, ensure_ascii=False).encode("utf-8"))
            warns = rt.doc_limits(c, u, req, size_est, create=False)
            rt.ensure_capacity(size_est, "doc_update", c)
            b = write_payload(c, u, req, path, cur, "doc_update")
            next_type = (p.type or r["type"] or "markdown").strip().lower(); next_type = next_type if next_type in ("markdown", "json") else "markdown"
            c.execute("UPDATE documents SET title=?, type=?, updated_at=?, last_accessed_at=?, size_bytes=? WHERE id=?", (cur["title"], next_type, rt.now_iso(), rt.now_iso(), int(b), did))
            rt.rate_add(c, u["id"], "doc_write", int(b)); rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="documents_updated", surface="docs", user=u, metadata={"document_id": did, "type": next_type, "size_bytes": int(b)})
            sw = rt.storage_warning()
            if sw: warns.append(sw)
            return {"ok": True, "document": {"id": did, "title": cur["title"], "updated_at": rt.now_iso(), "size_bytes": int(b)}, "warnings": warns}

    @app.post("/v1/app/docs/{did}/star")
    def docs_star(did: str, p: StarDocPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did)
            c.execute("UPDATE documents SET is_starred=?, updated_at=? WHERE id=?", (1 if p.starred else 0, rt.now_iso(), r["id"]))
            rt.record_analytics_event(c, event_type="documents_starred" if p.starred else "documents_unstarred", surface="docs", user=u, metadata={"document_id": did})
        return {"ok": True, "starred": bool(p.starred)}

    @app.post("/v1/app/docs/trash/clear")
    def docs_trash_clear(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            rows = c.execute("SELECT * FROM documents WHERE user_id=? AND is_deleted=1 ORDER BY deleted_at ASC, updated_at ASC", (u["id"],)).fetchall()
            reclaimed = 0
            deleted_count = 0
            for row in rows:
                b, n = rt.hard_del_doc(c, row)
                reclaimed += int(b)
                deleted_count += int(n)
            rt.recalc_storage(c, u["id"])
            if deleted_count:
                rt.record_analytics_event(c, event_type="documents_trash_cleared", surface="docs", user=u, value=deleted_count, metadata={"deleted_count": deleted_count, "reclaimed_bytes": reclaimed})
        return {"ok": True, "deleted_count": deleted_count, "reclaimed_bytes": reclaimed}

    @app.delete("/v1/app/docs/{did}")
    def docs_delete(did: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did)
            if int(r["is_starred"]) == 1:
                raise HTTPException(409, "Starred documents must be unstarred before deletion.")
            src = rt.safe_path(r["file_path"], r["user_id"]); tr = rt.trash_rel(r["user_id"], "docs", r["id"]); dst = rt.safe_path(tr, r["user_id"])
            if src.exists(): dst.parent.mkdir(parents=True, exist_ok=True); os.replace(str(src), str(dst))
            c.execute("UPDATE documents SET file_path=?, is_deleted=1, deleted_at=?, deleted_by_user=1, updated_at=? WHERE id=?", (tr, rt.now_iso(), rt.now_iso(), did)); rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="documents_deleted", surface="docs", user=u, metadata={"document_id": did, "type": r["type"]})
        return {"ok": True}

    @app.post("/v1/app/docs/{did}/restore")
    def docs_restore(did: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True); rt.ensure_docs_write_access(c, u)
            r = get_doc(c, u, did, include_deleted=True)
            if int(r["is_deleted"]) == 0: return {"ok": True}
            src = rt.safe_path(r["file_path"], r["user_id"]); lv = rt.doc_rel(r["user_id"], r["id"]); dst = rt.safe_path(lv, r["user_id"])
            if src.exists(): dst.parent.mkdir(parents=True, exist_ok=True); os.replace(str(src), str(dst))
            c.execute("UPDATE documents SET file_path=?, is_deleted=0, deleted_at=NULL, deleted_by_user=0, updated_at=? WHERE id=?", (lv, rt.now_iso(), did)); rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="documents_restored", surface="docs", user=u, metadata={"document_id": did, "type": r["type"]})
        return {"ok": True}

    @app.post("/v1/app/docs/report-paste-abuse")
    def docs_report_paste_abuse(p: PasteAbusePayload, req: Request):
        valid_types = {"paste_cooldown", "paste_duplicate", "paste_too_long"}
        if (p.abuse_type or "").strip() not in valid_types:
            raise HTTPException(400, "Invalid abuse_type")
        et_map = {
            "paste_cooldown": "docs_paste_cooldown_blocked",
            "paste_duplicate": "docs_paste_duplicate_blocked",
            "paste_too_long": "docs_paste_too_long",
        }
        et = et_map[p.abuse_type.strip()]
        with rt.tx() as c:
            u = rt.req_user(c, req)
            rt.log_event(c, user=u, ip_=rt.ip(req), endpoint=req.url.path,
                et=et, sev="warn",
                detail=p.detail or f"Paste abuse: {p.abuse_type}",
                act="client_reported")
            rt._check_flag_escalation(c, u, rt.ip(req), req.url.path, et,
                rt.docs_offense_window, rt.docs_offense_hits, "docs")
        return {"ok": True}

    def serialize_chat_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_accessed_at": row["last_accessed_at"],
            "token_count_estimate": row["token_count_estimate"],
            "size_bytes": row["size_bytes"],
            "is_deleted": bool(row["is_deleted"]),
            "deleted_at": row["deleted_at"],
            "is_saved": bool(row["is_saved"]),
            "folder_id": row["folder_id"],
        }

    def serialize_chat_folder_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @app.get("/v1/app/chat-folders")
    def chat_folders_list(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            rows = c.execute("SELECT * FROM chat_folders WHERE user_id=? ORDER BY LOWER(name) ASC, created_at ASC", (u["id"],)).fetchall()
            return {"ok": True, "folders": [serialize_chat_folder_row(r) for r in rows]}

    @app.post("/v1/app/chat-folders")
    def chat_folders_create(p: CreateChatFolderPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            name = rt.clean_chat_folder_name(p.name)
            if not name:
                raise HTTPException(400, "Folder name is required")
            name_norm = rt.nuser(name)
            existing = c.execute("SELECT id FROM chat_folders WHERE user_id=? AND name_norm=?", (u["id"], name_norm)).fetchone()
            if existing is not None:
                raise HTTPException(409, "Folder name already exists")
            fid = rt.uid()
            now = rt.now_iso()
            c.execute(
                "INSERT INTO chat_folders(id,user_id,name,name_norm,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (fid, u["id"], name, name_norm, now, now),
            )
            row = c.execute("SELECT * FROM chat_folders WHERE id=?", (fid,)).fetchone()
            rt.record_analytics_event(c, event_type="folders_created", surface="chat", user=u, metadata={"folder_id": fid})
            return {"ok": True, "folder": serialize_chat_folder_row(row)}

    @app.patch("/v1/app/chat-folders/{fid}")
    def chat_folders_update(fid: str, p: UpdateChatFolderPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            folder = rt.get_chat_folder_row(c, u["id"], fid)
            name = rt.clean_chat_folder_name(p.name)
            if not name:
                raise HTTPException(400, "Folder name is required")
            name_norm = rt.nuser(name)
            existing = c.execute("SELECT id FROM chat_folders WHERE user_id=? AND name_norm=? AND id<>?", (u["id"], name_norm, fid)).fetchone()
            if existing is not None:
                raise HTTPException(409, "Folder name already exists")
            c.execute("UPDATE chat_folders SET name=?, name_norm=?, updated_at=? WHERE id=?", (name, name_norm, rt.now_iso(), folder["id"]))
            row = c.execute("SELECT * FROM chat_folders WHERE id=?", (fid,)).fetchone()
            rt.record_analytics_event(c, event_type="folders_renamed", surface="chat", user=u, metadata={"folder_id": fid})
            return {"ok": True, "folder": serialize_chat_folder_row(row)}

    @app.delete("/v1/app/chat-folders/{fid}")
    def chat_folders_delete(fid: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            folder = rt.get_chat_folder_row(c, u["id"], fid)
            now = rt.now_iso()
            c.execute("UPDATE chats SET folder_id=NULL, updated_at=? WHERE user_id=? AND folder_id=?", (now, u["id"], folder["id"]))
            c.execute("DELETE FROM chat_folders WHERE id=?", (folder["id"],))
            rt.record_analytics_event(c, event_type="folders_deleted", surface="chat", user=u, metadata={"folder_id": fid})
        return {"ok": True}

    @app.get("/v1/app/chats")
    def chats_list(req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            where = "user_id=?"
            args: Tuple[Any, ...] = (u["id"],)
            if not include_deleted:
                where += " AND is_deleted=0"
            rows = c.execute(
                f"SELECT id,user_id,title,created_at,updated_at,last_accessed_at,token_count_estimate,size_bytes,is_deleted,deleted_at,is_saved,folder_id FROM chats WHERE {where} ORDER BY COALESCE(last_accessed_at,updated_at,created_at) DESC",
                args,
            ).fetchall()
            return {"ok": True, "chats": [serialize_chat_row(r) for r in rows], "warning": rt.storage_warning()}

    @app.get("/v1/app/chats/{cid}")
    def chats_get(cid: str, req: Request, include_deleted: bool = False):
        with rt.tx() as c:
            u = rt.req_user(c, req)
            r = get_chat(c, u, cid, include_deleted=include_deleted)
            o = load_chat_json(r)
            if int(r["is_deleted"] or 0) == 0:
                c.execute("UPDATE chats SET last_accessed_at=? WHERE id=?", (rt.now_iso(), cid))
                rt.record_analytics_event(c, event_type="chat_opened", surface="chat", user=u, metadata={"chat_id": cid})
            return {
                "ok": True,
                "chat": {
                    "id": r["id"],
                    "title": r["title"],
                    "messages": o.get("messages", []),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "is_deleted": bool(r["is_deleted"]),
                    "is_saved": bool(r["is_saved"]),
                    "folder_id": r["folder_id"],
                },
            }

    @app.post("/v1/app/chats")
    def chats_create(p: CreateChatPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            rt.ensure_user_dirs(u["id"])
            cid = rt.uid()
            title = (p.title or "New Chat").strip() or "New Chat"
            now = rt.now_iso()
            o = {"id": cid, "title": title, "messages": [], "created_at": now, "updated_at": now, "version": 1}
            size_est = len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
            warns = rt.chat_limits(c, u, req, size_est, create=True, scope="chat")
            rt.ensure_capacity(size_est, "chat_create", c)
            fp = rt.chat_rel(u["id"], cid)
            b = write_payload(c, u, req, rt.safe_path(fp, u["id"]), o, "chat_create")
            c.execute(
                "INSERT INTO chats(id,user_id,title,created_at,updated_at,last_accessed_at,token_count_estimate,file_path,size_bytes,is_deleted,deleted_at,deleted_by_user,is_guest_owned,is_saved,folder_id) VALUES(?,?,?,?,?,?,0,?,?,0,NULL,0,?,0,NULL)",
                (cid, u["id"], title, now, now, now, fp, int(b), 1 if u["role"] == "guest" else 0),
            )
            rt.rate_add(c, u["id"], "chat_create", 0)
            rt.rate_add(c, u["id"], "chat_write", int(b))
            rt.recalc_storage(c, u["id"])
            rt.record_analytics_event(c, event_type="chat_sessions_created", surface="chat", user=u, metadata={"chat_id": cid})
            sw = rt.storage_warning()
            if sw:
                warns.append(sw)
            row = c.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
            return {"ok": True, "chat": serialize_chat_row(row), "warnings": warns}

    @app.patch("/v1/app/chats/{cid}")
    def chats_update(cid: str, p: UpdateChatPayload, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            r = get_chat(c, u, cid, include_deleted=True)
            fields_set = set(getattr(p, "model_fields_set", getattr(p, "__fields_set__", set())))
            updates: List[str] = []
            args: List[Any] = []
            if "title" in fields_set:
                updates.append("title=?")
                args.append((p.title or "").strip() or "Untitled Chat")
            if "is_saved" in fields_set:
                next_saved = bool(p.is_saved)
                if next_saved and not bool(r["is_saved"]):
                    rt.ensure_saved_chat_capacity(c, u["id"], exclude_chat_id=r["id"])
                updates.append("is_saved=?")
                args.append(1 if next_saved else 0)
            if "folder_id" in fields_set:
                updates.append("folder_id=?")
                args.append(rt.resolve_chat_folder_id(c, u["id"], p.folder_id))
            if updates:
                updates.append("updated_at=?")
                args.append(rt.now_iso())
                args.append(r["id"])
                c.execute(f"UPDATE chats SET {', '.join(updates)} WHERE id=?", tuple(args))
            row = c.execute("SELECT * FROM chats WHERE id=?", (r["id"],)).fetchone()
            return {"ok": True, "chat": serialize_chat_row(row)}

    @app.delete("/v1/app/chats/{cid}")
    def chats_delete(cid: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            r = get_chat(c, u, cid)
            src = rt.safe_path(r["file_path"], r["user_id"])
            tr = rt.trash_rel(r["user_id"], "chats", r["id"])
            dst = rt.safe_path(tr, r["user_id"])
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(src), str(dst))
            c.execute("UPDATE chats SET file_path=?, is_deleted=1, deleted_at=?, deleted_by_user=1, updated_at=? WHERE id=?", (tr, rt.now_iso(), rt.now_iso(), cid))
            rt.recalc_storage(c, r["user_id"])
            rt.record_analytics_event(c, event_type="chat_deleted", surface="chat", user=u, metadata={"chat_id": cid})
        return {"ok": True}

    @app.post("/v1/app/chats/{cid}/restore")
    def chats_restore(cid: str, req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            r = get_chat(c, u, cid, include_deleted=True)
            if int(r["is_deleted"]) == 0:
                row = c.execute("SELECT * FROM chats WHERE id=?", (r["id"],)).fetchone()
                return {"ok": True, "chat": serialize_chat_row(row)}
            folder_id = None
            if r["folder_id"] is not None:
                folder = c.execute("SELECT id FROM chat_folders WHERE user_id=? AND id=?", (r["user_id"], r["folder_id"])).fetchone()
                folder_id = folder["id"] if folder is not None else None
            if bool(r["is_saved"]):
                rt.ensure_saved_chat_capacity(c, u["id"])
            src = rt.safe_path(r["file_path"], r["user_id"])
            lv = rt.chat_rel(r["user_id"], r["id"])
            dst = rt.safe_path(lv, r["user_id"])
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(src), str(dst))
            now = rt.now_iso()
            c.execute(
                "UPDATE chats SET file_path=?, is_deleted=0, deleted_at=NULL, deleted_by_user=0, folder_id=?, updated_at=?, last_accessed_at=? WHERE id=?",
                (lv, folder_id, now, now, cid),
            )
            rt.recalc_storage(c, r["user_id"])
            row = c.execute("SELECT * FROM chats WHERE id=?", (r["id"],)).fetchone()
            rt.record_analytics_event(c, event_type="chat_restored", surface="chat", user=u, metadata={"chat_id": cid})
            return {"ok": True, "chat": serialize_chat_row(row)}

    @app.post("/v1/app/chats/trash/clear")
    def chats_trash_clear(req: Request):
        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            rows = c.execute("SELECT * FROM chats WHERE user_id=? AND is_deleted=1 ORDER BY deleted_at ASC, updated_at ASC", (u["id"],)).fetchall()
            reclaimed = 0
            deleted_count = 0
            for row in rows:
                b, n = rt.hard_del_chat(c, row)
                reclaimed += int(b)
                deleted_count += int(n)
            rt.recalc_storage(c, u["id"])
            if deleted_count:
                rt.record_analytics_event(c, event_type="chat_deleted", surface="chat", user=u, value=deleted_count, metadata={"deleted_count": deleted_count, "reclaimed_bytes": reclaimed})
        return {"ok": True, "deleted_count": deleted_count, "reclaimed_bytes": reclaimed}

    @app.post("/v1/app/chat/completions")
    async def app_chat_completions(req: Request):
        """Handle the main signed-in chat route backed by llama.cpp and optional wiki RAG."""
        request_id, cold_request = rt._next_request_identity()
        request_start_perf = time.perf_counter()
        request_started_at = rt.now_iso()
        try:
            payload = await req.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        if not isinstance(payload, dict): raise HTTPException(400, "Invalid payload")
        msgs = payload.get("messages")
        model = str(payload.get("model") or "").strip()
        if not model:
            raise HTTPException(400, "Missing model")
        norm_messages = normalize_messages(msgs)
        if not norm_messages:
            raise HTTPException(400, "Missing messages")
        user_msg = ""
        for m in reversed(norm_messages):
            if m.get("role") == "user": user_msg = str(m.get("content", "") or ""); break
        if not user_msg.strip(): raise HTTPException(400, "No user message found")

        stream = bool(payload.get("stream", False))
        retrieval_enabled = _coerce_bool(payload.get("retrieval_enabled"), default=rt.retrieval_enabled_default)
        chat_id = payload.get("chat_id")
        title = str(payload.get("title") or user_msg[:48] or "New Chat")[:120]
        warns: List[Dict[str, Any]] = []
        user_role = "user"
        preferred_language = "en"
        response_language = "en"
        analytics_user: Optional[Dict[str, Any]] = None
        summary_base: Dict[str, Any] = {
            "request_id": request_id,
            "request_started_at": request_started_at,
            "request_finished_at": None,
            "_request_start_perf": request_start_perf,
            "stage_trace": [],
            "model_name": model,
            "stream": stream,
            "retrieval_enabled": retrieval_enabled,
            "cold_request": cold_request,
            "raw_user_message": user_msg,
            "normalized_user_message": user_msg.strip(),
            "request_base_url": rt._request_base_url(req),
            "base_messages": rt._clone_data(norm_messages),
            "resource_start": rt._resource_snapshot(),
            "warnings": warns,
        }
        rt._trace_diagnostics(summary_base, request_start_perf, "request_received")
        rt._trace_diagnostics(summary_base, request_start_perf, "normalized_messages_prepared", message_count=len(norm_messages))

        with rt.tx() as c:
            u = rt.req_user(c, req, write=True)
            user_role = str(u["role"] or "user")
            analytics_user = {
                "id": u["id"],
                "username": u["username"],
                "role": u["role"],
                "preferred_language": u["preferred_language"],
            }
            try:
                preferred_language = str(u["preferred_language"] or "en").strip().lower()
            except (KeyError, IndexError):
                preferred_language = "en"
            # Detect query language; fall back to user's preferred language
            detected_lang = rt._detect_query_language(user_msg)
            response_language = detected_lang if detected_lang in ("en", "es") else preferred_language
            # For ambiguous queries (short, no clear language signal), use preference
            lang_words = set(user_msg.lower().split())
            _SPANISH_SIGNAL = {"que", "quien", "como", "donde", "cuando", "por", "cual", "es", "la", "el", "los", "las", "del", "una", "en"}
            _ENGLISH_SIGNAL = {"what", "who", "how", "where", "when", "which", "the", "is", "are", "was", "does", "did"}
            has_spanish = bool(lang_words & _SPANISH_SIGNAL)
            has_english = bool(lang_words & _ENGLISH_SIGNAL)
            if not has_spanish and not has_english:
                response_language = preferred_language
            summary_base["user_role"] = user_role
            summary_base["user_id"] = str(u["id"])
            summary_base["response_language"] = response_language
            rt.ensure_ai_send_access(c, u)
            rt.check_ai_request_rate(c, u, rt.ip(req), req.url.path)
            rt.check_prompt_length(c, u, rt.ip(req), req.url.path, user_msg)
            rt.check_concurrent_generations(c, u, rt.ip(req), req.url.path, request_id)
            rt.arm_ai_cooldown_if_needed(c, u)
            if chat_id:
                ch = get_chat(c, u, str(chat_id))
            else:
                rt.ensure_user_dirs(u["id"]); cid = rt.uid(); fp = rt.chat_rel(u["id"], cid)
                o = {"id": cid, "title": title, "messages": [], "created_at": rt.now_iso(), "updated_at": rt.now_iso(), "version": 1}
                init_size = len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
                warns.extend(rt.chat_limits(c, u, req, init_size, create=True, scope="ai"))
                rt.ensure_capacity(init_size, "chat_create", c)
                b = write_payload(c, u, req, rt.safe_path(fp, u["id"]), o, "chat_create")
                c.execute("INSERT INTO chats(id,user_id,title,created_at,updated_at,last_accessed_at,token_count_estimate,file_path,size_bytes,is_deleted,deleted_at,deleted_by_user,is_guest_owned,is_saved,folder_id) VALUES(?,?,?,?,?,?,0,?,?,0,NULL,0,?,0,NULL)", (cid, u["id"], title, rt.now_iso(), rt.now_iso(), rt.now_iso(), fp, int(b), 1 if u["role"] == "guest" else 0))
                rt.rate_add(c, u["id"], "chat_create", 0); rt.rate_add(c, u["id"], "chat_write", int(b)); chat_id = cid
                ch = c.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
                rt.record_analytics_event(c, event_type="chat_sessions_created", surface="chat", user=u, metadata={"chat_id": cid, "source": "completion"})
            b1, tk1, ww = append_chat(c, ch, "user", user_msg, u, req, "chat_append_user", scope="ai")
            warns.extend(ww)
            c.execute("UPDATE chats SET updated_at=?, last_accessed_at=?, size_bytes=?, token_count_estimate=? WHERE id=?", (rt.now_iso(), rt.now_iso(), int(b1), int(tk1), ch["id"]))
            rt.rate_add(c, u["id"], "chat_write", int(b1)); rt.recalc_storage(c, ch["user_id"])
            rt.record_analytics_event(c, event_type="chat_completion_requested", surface="chat", user=u, metadata={"chat_id": str(chat_id), "model": model, "retrieval_enabled": retrieval_enabled, "response_language": response_language})
            rt.record_analytics_event(c, event_type="chat_messages_sent", surface="chat", user=u, metadata={"chat_id": str(chat_id), "message_chars": len(user_msg), "response_language": response_language})
        summary_base["chat_id"] = chat_id
        rt._trace_diagnostics(summary_base, request_start_perf, "conversation_history_loaded", chat_id=chat_id)

        async def persist_ai(ai_text: str, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
            add_warnings: List[Dict[str, Any]] = []
            if not ai_text:
                return add_warnings
            with rt.tx() as c:
                u2 = rt.req_user(c, req, write=True)
                ch2 = get_chat(c, u2, str(chat_id))
                b2, tk2, ww2 = append_chat(
                    c,
                    ch2,
                    "assistant",
                    ai_text,
                    u2,
                    req,
                    "chat_append_ai",
                    scope="ai",
                    citations=summary.get("citations"),
                )
                add_warnings.extend(ww2)
                c.execute("UPDATE chats SET updated_at=?, last_accessed_at=?, size_bytes=?, token_count_estimate=? WHERE id=?", (rt.now_iso(), rt.now_iso(), int(b2), int(tk2), ch2["id"]))
                rt.rate_add(c, u2["id"], "chat_write", int(b2)); rt.recalc_storage(c, ch2["user_id"])
                citations = summary.get("citations") or []
                rt.record_analytics_event(
                    c,
                    event_type="chat_completion_succeeded",
                    surface="chat",
                    user=u2,
                    metadata={
                        "chat_id": str(chat_id),
                        "model": model,
                        "response_language": response_language,
                        "completion_tokens": int(summary.get("completion_tokens") or 0),
                        "citation_count": len(citations),
                    },
                )
                if int(summary.get("completion_tokens") or 0) > 0:
                    rt.record_analytics_event(
                        c,
                        event_type="chat_completion_tokens_emitted",
                        surface="chat",
                        user=u2,
                        value=int(summary.get("completion_tokens") or 0),
                        metadata={"chat_id": str(chat_id), "model": model},
                    )
                if citations:
                    rt.record_analytics_event(
                        c,
                        event_type="chat_citations_emitted",
                        surface="chat",
                        user=u2,
                        value=len(citations),
                        metadata={"chat_id": str(chat_id), "model": model},
                    )
            rt._trace_diagnostics(summary, request_start_perf, "response_persisted_completed")
            return add_warnings

        if stream:
            async def sse() -> AsyncGenerator[str, None]:
                summary: Dict[str, Any] = dict(summary_base)
                yield f"event: meta\ndata: {json.dumps({'chat_id': chat_id, 'request_id': request_id}, ensure_ascii=False)}\n\n"
                try:
                    async for part in stream_model_text(
                        model,
                        norm_messages,
                        summary,
                        retrieval_enabled,
                        response_language=response_language,
                        user_language=preferred_language,
                        request_base_url=summary.get("request_base_url"),
                    ):
                        yield f"event: delta\ndata: {json.dumps({'delta': part}, ensure_ascii=False)}\n\n"
                except asyncio.CancelledError:
                    summary["request_finished_at"] = rt.now_iso()
                    if analytics_user is not None:
                        with rt.tx() as analytics_conn:
                            rt.record_analytics_event(
                                analytics_conn,
                                event_type="chat_completion_stopped",
                                surface="chat",
                                user=analytics_user,
                                metadata={"chat_id": str(chat_id), "model": model, "source": "disconnect"},
                            )
                    rt.remove_active_generation(request_id)
                    return
                except HTTPException as e:
                    summary["failed_stage"] = summary.get("current_stage") or "generation"
                    summary["request_error"] = str(e.detail)
                    summary["request_finished_at"] = rt.now_iso()
                    summary["warnings"] = list(warns)
                    if analytics_user is not None:
                        with rt.tx() as analytics_conn:
                            rt.record_analytics_event(
                                analytics_conn,
                                event_type="chat_completion_failed",
                                surface="chat",
                                user=analytics_user,
                                metadata={"chat_id": str(chat_id), "model": model, "detail": str(e.detail), "status_code": int(e.status_code)},
                            )
                    if user_role == "admin":
                        metrics = rt.build_admin_metrics(summary)
                        diagnostics = rt.build_admin_diagnostics(summary)
                        yield f"event: diagnostics\ndata: {json.dumps({'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: error\ndata: {json.dumps({'detail': e.detail, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: done\ndata: {json.dumps({'chat_id': chat_id, 'request_id': request_id, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                    else:
                        yield f"event: error\ndata: {json.dumps({'detail': e.detail}, ensure_ascii=False)}\n\n"
                        yield "event: done\ndata: {}\n\n"
                    rt.remove_active_generation(request_id)
                    return
                except Exception as e:
                    detail = f'llama proxy error: {type(e).__name__}: {e}'
                    logger.error("chat completion unexpected error request_id=%s: %s", request_id, detail, exc_info=True)
                    summary["failed_stage"] = summary.get("current_stage") or "generation"
                    summary["request_error"] = detail
                    summary["request_finished_at"] = rt.now_iso()
                    summary["warnings"] = list(warns)
                    if analytics_user is not None:
                        with rt.tx() as analytics_conn:
                            rt.record_analytics_event(
                                analytics_conn,
                                event_type="chat_completion_failed",
                                surface="chat",
                                user=analytics_user,
                                metadata={"chat_id": str(chat_id), "model": model, "detail": detail},
                            )
                    if user_role == "admin":
                        metrics = rt.build_admin_metrics(summary)
                        diagnostics = rt.build_admin_diagnostics(summary)
                        yield f"event: diagnostics\ndata: {json.dumps({'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: error\ndata: {json.dumps({'detail': detail, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                        yield f"event: done\ndata: {json.dumps({'chat_id': chat_id, 'request_id': request_id, 'metrics': metrics, 'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                    else:
                        yield f"event: error\ndata: {json.dumps({'detail': 'AI service temporarily unavailable'}, ensure_ascii=False)}\n\n"
                        yield "event: done\ndata: {}\n\n"
                    rt.remove_active_generation(request_id)
                    return
                warns_local = list(warns)
                warns_local.extend(await persist_ai(str(summary.get("text", "")), summary))
                sw = rt.storage_warning()
                if sw: warns_local.append(sw)
                summary["warnings"] = warns_local
                summary["request_finished_at"] = summary.get("request_finished_at") or rt.now_iso()
                retrieval_warnings = rt.build_user_retrieval_warnings(summary)
                if retrieval_warnings:
                    yield f"event: retrieval_warning\ndata: {json.dumps({'warnings': retrieval_warnings}, ensure_ascii=False)}\n\n"
                metrics = None
                diagnostics = None
                if user_role == "admin":
                    metrics = rt.build_admin_metrics(summary)
                    diagnostics = rt.build_admin_diagnostics(summary)
                    yield f"event: diagnostics\ndata: {json.dumps({'diagnostics': diagnostics}, ensure_ascii=False)}\n\n"
                done_payload = {
                    "chat_id": chat_id,
                    "request_id": request_id,
                    "citations": summary.get("citations") or [],
                    "warnings": warns_local,
                    "retrieval_warnings": retrieval_warnings,
                    "finish_reason": summary.get("finish_reason"),
                    "continuation_count": summary.get("continuation_count", 0),
                    "continuation_limit_hit": summary.get("continuation_limit_hit", False),
                }
                if metrics is not None:
                    done_payload["metrics"] = metrics
                if diagnostics is not None:
                    done_payload["diagnostics"] = diagnostics
                yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
                rt.remove_active_generation(request_id)
            return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        summary: Dict[str, Any] = dict(summary_base)
        ai_parts: List[str] = []
        try:
            try:
                async for p in stream_model_text(
                    model,
                    norm_messages,
                    summary,
                    retrieval_enabled,
                    response_language=response_language,
                    user_language=preferred_language,
                    request_base_url=summary.get("request_base_url"),
                ):
                    ai_parts.append(p)
            except HTTPException as e:
                summary["failed_stage"] = summary.get("current_stage") or "generation"
                summary["request_error"] = str(e.detail)
                summary["request_finished_at"] = rt.now_iso()
                summary["warnings"] = list(warns)
                if analytics_user is not None:
                    with rt.tx() as analytics_conn:
                        rt.record_analytics_event(
                            analytics_conn,
                            event_type="chat_completion_failed",
                            surface="chat",
                            user=analytics_user,
                            metadata={"chat_id": str(chat_id), "model": model, "detail": str(e.detail), "status_code": int(e.status_code)},
                        )
                if user_role == "admin":
                    return JSONResponse({
                        "detail": e.detail,
                        "chat_id": chat_id,
                        "admin_metrics": rt.build_admin_metrics(summary),
                        "admin_diagnostics": rt.build_admin_diagnostics(summary),
                    }, status_code=e.status_code)
                raise
            except Exception as e:
                detail = f"llama proxy error: {type(e).__name__}: {e}"
                logger.error("chat completion unexpected error request_id=%s: %s", request_id, detail, exc_info=True)
                summary["failed_stage"] = summary.get("current_stage") or "generation"
                summary["request_error"] = detail
                summary["request_finished_at"] = rt.now_iso()
                summary["warnings"] = list(warns)
                if analytics_user is not None:
                    with rt.tx() as analytics_conn:
                        rt.record_analytics_event(
                            analytics_conn,
                            event_type="chat_completion_failed",
                            surface="chat",
                            user=analytics_user,
                            metadata={"chat_id": str(chat_id), "model": model, "detail": detail},
                        )
                if user_role == "admin":
                    return JSONResponse({
                        "detail": detail,
                        "chat_id": chat_id,
                        "admin_metrics": rt.build_admin_metrics(summary),
                        "admin_diagnostics": rt.build_admin_diagnostics(summary),
                    }, status_code=502)
                raise HTTPException(502, "AI service temporarily unavailable")
            ai_text = "".join(ai_parts)

            warns.extend(await persist_ai(ai_text, summary))
            sw = rt.storage_warning()
            if sw: warns.append(sw)
            summary["warnings"] = warns
            summary["request_finished_at"] = summary.get("request_finished_at") or rt.now_iso()
            citations = summary.get("citations") or []

            retrieval_warnings = rt.build_user_retrieval_warnings(summary)
            out: Dict[str, Any] = {
                "id": rt.uid(),
                "object": "chat.completion",
                "chat_id": chat_id,
                "request_id": request_id,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ai_text, "citations": citations}, "finish_reason": summary.get("finish_reason", "stop")}],
                "citations": citations,
                "warnings": warns,
                "retrieval_warnings": retrieval_warnings,
            }
            if user_role == "admin":
                out["admin_metrics"] = rt.build_admin_metrics(summary)
                out["admin_diagnostics"] = rt.build_admin_diagnostics(summary)
            return JSONResponse(out)
        finally:
            rt.remove_active_generation(request_id)

    return rt
