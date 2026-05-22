"""Quality suite checks (10.x): retrieval gold set, reranker, generation, language, OOD."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from aibox.checks.harness.base import Check, CheckResult, register


# 10.1 — Retrieval gold-set recall -------------------------------------------

CHROMA_LANG_PATHS = {
    "en": ("chroma_db", "chroma_db_en"),
    "es": ("chroma_db_es",),
}
DEFAULT_COLLECTION = os.environ.get("AIBOX_CHECK_COLLECTION", "simplewiki_chunks")


@register(
    suite="quality", id="10.1", name="retrieval_gold_set",
    status="real",
    description="Runs the seeded gold queries against each chroma_db_* and reports "
                "recall@k. Seed set is small (~10 per language); expand in fixtures/.",
    requires=("module:chromadb", "module:sentence_transformers"),
)
class RetrievalGold(Check):
    K_VALUES = (1, 5, 10)

    def run(self, ctx) -> CheckResult:
        import chromadb
        from sentence_transformers import SentenceTransformer
        local_path = ctx.repo_root / "aibox" / "models" / "bge-m3"
        try:
            model = SentenceTransformer(str(local_path) if local_path.exists() else "BAAI/bge-m3")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(outcome="skipped", summary=f"embedding model unavailable: {exc}")

        any_found = False
        for lang, dirs in CHROMA_LANG_PATHS.items():
            db_path = next(
                (ctx.repo_root / "aibox" / d for d in dirs if (ctx.repo_root / "aibox" / d).exists()),
                None,
            )
            if not db_path:
                db_path = next((ctx.repo_root / d for d in dirs if (ctx.repo_root / d).exists()), None)
            if not db_path:
                continue
            any_found = True
            queries_fp = Path(__file__).resolve().parents[1] / "fixtures" / f"queries_{lang}.jsonl"
            queries = [json.loads(l) for l in queries_fp.read_text(encoding="utf-8").splitlines() if l.strip()]
            scored = [q for q in queries if q.get("expected_topic")]
            if not scored:
                continue
            try:
                client = chromadb.PersistentClient(path=str(db_path))
                col_names = [c.name for c in client.list_collections()]
                col = client.get_collection(DEFAULT_COLLECTION) if DEFAULT_COLLECTION in col_names else (client.list_collections() or [None])[0]
            except Exception as exc:  # noqa: BLE001
                ctx.metric("chroma_error", str(exc), lang=lang)
                continue
            if not col:
                ctx.metric("no_collection", True, lang=lang)
                continue
            hits = {k: 0 for k in self.K_VALUES}
            for q in scored:
                emb = model.encode([q["query"]]).tolist()[0]
                try:
                    res = col.query(query_embeddings=[emb], n_results=max(self.K_VALUES))
                except Exception as exc:  # noqa: BLE001
                    ctx.metric("query_error", str(exc), lang=lang)
                    continue
                docs = (res.get("documents") or [[]])[0]
                metadatas = (res.get("metadatas") or [[]])[0]
                topic = (q["expected_topic"] or "").lower()
                for k in self.K_VALUES:
                    window = docs[:k] + [str(m) for m in metadatas[:k]]
                    if any(topic in (w or "").lower() for w in window):
                        hits[k] += 1
            for k in self.K_VALUES:
                recall = hits[k] / len(scored) if scored else 0
                ctx.metric(f"recall_at_{k}", recall, unit="ratio", lang=lang, n=len(scored))
        if not any_found:
            return CheckResult(outcome="skipped", summary="no chroma_db_* directories found")
        return CheckResult(outcome="ok", summary="recall metrics emitted per language")


# 10.2 — Reranker contribution (stub) ----------------------------------------

@register(
    suite="quality", id="10.2", name="reranker_contribution",
    status="stub",
    description="Run 10.1 with reranker on vs off; the recall delta is the reranker's value. "
                "Stub: requires hooks into ai-control's reranker config.",
)
class RerankerContribution(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="needs a reranker-off code path in the check OR an ai-control flag",
        )


# 10.3 — Generation quality (stub) -------------------------------------------

@register(
    suite="quality", id="10.3", name="generation_quality",
    status="stub",
    description="Run a fixed prompt set and score answers by rubric (factual, relevant, "
                "language-matched, refuses when appropriate). Offline-only.",
)
class GenerationQuality(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="needs a rubric (JSON criteria) and either a local judge model "
                    "or a deterministic heuristic; sampled human review per release",
        )


# 10.4 — Language match ------------------------------------------------------

# Tiny heuristic detector — good enough as a smoke test; replace with fasttext-lid later.
SPANISH_HINTS = re.compile(r"[áéíóúñ¿¡]|(\bde la\b)|(\bes un\b)|(\bcuál\b)|(\bquién\b)", re.IGNORECASE)
ENGLISH_HINTS = re.compile(r"\b(the|is|of|and|what|who|when)\b", re.IGNORECASE)


def _detect_language(text: str) -> str:
    es = len(SPANISH_HINTS.findall(text))
    en = len(ENGLISH_HINTS.findall(text))
    if es > en and es > 0:
        return "es"
    if en > 0:
        return "en"
    return "unknown"


@register(
    suite="quality", id="10.4", name="language_match",
    status="real",
    description="Detects language of each fixture query and reports any mismatch with its "
                "filename. Adequate as a tripwire; swap in fasttext-lid for production.",
)
class LanguageMatch(Check):
    def run(self, ctx) -> CheckResult:
        mismatches = 0
        total = 0
        for lang in ("en", "es"):
            fp = Path(__file__).resolve().parents[1] / "fixtures" / f"queries_{lang}.jsonl"
            if not fp.exists():
                continue
            for line in fp.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                q = json.loads(line)
                detected = _detect_language(q["query"])
                total += 1
                if detected != lang:
                    mismatches += 1
                    ctx.metric("mismatch", q["id"], expected=lang, detected=detected)
        ctx.metric("queries_scanned", total)
        ctx.metric("mismatches", mismatches)
        outcome = "fail" if mismatches > total * 0.05 else "ok"
        return CheckResult(
            outcome=outcome,
            summary=f"{total} queries; {mismatches} language mismatches",
        )


# 10.5 — OOD / hallucination canary (stub) -----------------------------------

@register(
    suite="quality", id="10.5", name="ood_hallucination_canary",
    status="stub",
    description="Ask known-OOD questions; flag any high-confidence fabricated answer. "
                "Stub: needs the chat probe path (4.1's chat mode) plus an OOD-answer detector.",
)
class OodCanary(Check):
    def run(self, ctx) -> CheckResult:
        return CheckResult(
            outcome="stub",
            summary="extends 4.1 with the OOD subset of the fixture set + a detector "
                    "(refusal phrases vs confident assertions)",
        )
