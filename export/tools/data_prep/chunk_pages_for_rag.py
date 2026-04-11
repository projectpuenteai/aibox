"""Split cleaned wiki pages into retrieval-sized chunks for the Chroma index.

This script sits between extract_pages_from_dump.py and tools/index/build_chroma_index.py.
It reads page-level JSONL, breaks long articles into section-aware chunks, and writes
chunk metadata that the indexer will embed and store.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from itertools import repeat
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import CHUNKS_FILE, PAGES_FILE

DEFAULT_CHUNK_WORDS = int(os.getenv("CHUNK_WORDS", "900"))
DEFAULT_CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "5600"))
DEFAULT_OVERLAP_WORDS = int(os.getenv("CHUNK_OVERLAP_WORDS", "140"))
DEFAULT_MIN_CHUNK_WORDS = int(os.getenv("MIN_CHUNK_WORDS", "120"))
DEFAULT_MAX_CPU_PERCENT = 90
DEFAULT_WORKERS = max(1, int(int(os.getenv("CHUNK_WORKERS", os.cpu_count() or 1)) * DEFAULT_MAX_CPU_PERCENT / 100))
DEFAULT_MAP_CHUNKSIZE = int(os.getenv("CHUNK_MAP_CHUNKSIZE", "32"))

def count_lines(filepath: str) -> int:
    """Fast line count without loading entire file into memory."""
    count = 0
    with open(filepath, "rb") as f:
        while True:
            buf = f.raw.read(1024 * 1024)
            if not buf:
                break
            count += buf.count(b"\n")
    return count


def format_eta(seconds: float) -> str:
    if seconds <= 0 or not math.isfinite(seconds):
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def set_low_priority():
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                BELOW_NORMAL_PRIORITY_CLASS,
            )
        else:
            os.nice(10)
    except Exception:
        pass


_word_re = re.compile(r"\w+", re.UNICODE)
_inline_ws_re = re.compile(r"[ \t]+")
_blank_line_re = re.compile(r"\n{3,}")

_SPANISH_INDICATORS = re.compile(
    r"\b(?:el|la|los|las|del|una|unos|unas|es|fue|son|para|por|con|como|pero|sobre|entre|desde|hasta|donde|cuando|porque|puede|tiene|hace|esta|este|estos|estas|ese|esos|esas|aquel)\b",
    re.IGNORECASE,
)


def detect_language(text):
    """Return 'es' if text looks Spanish, 'en' otherwise."""
    sample = (text or "")[:2000]
    matches = len(_SPANISH_INDICATORS.findall(sample))
    words = len(_word_re.findall(sample))
    if words > 0 and matches / words > 0.06:
        return "es"
    return "en"


def chunk_words(words, chunk_size, overlap):
    """Yield overlapping word windows for paragraph-sized chunk splitting."""
    step = max(1, chunk_size - overlap)
    for start in range(0, len(words), step):
        end = start + chunk_size
        yield words[start:end]
        if end >= len(words):
            break


def count_words(text):
    return len(_word_re.findall(text or ""))


def normalize_text(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_inline_ws_re.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _blank_line_re.sub("\n\n", text)
    return text.strip()


def split_paragraphs(text):
    normalized = normalize_text(text)
    if not normalized:
        return []
    return [part.strip() for part in normalized.split("\n\n") if part.strip()]


def looks_like_heading(paragraph):
    text = str(paragraph or "").strip()
    if not text or not text.endswith(":"):
        return False
    if len(text) > 100:
        return False
    words = _word_re.findall(text[:-1])
    if not words or len(words) > 10:
        return False
    return not any(mark in text[:-1] for mark in ".!?")


def join_paragraphs(paragraphs):
    return "\n\n".join(part.strip() for part in paragraphs if str(part or "").strip()).strip()


def build_chunk_record(section_title, text):
    normalized_section = (section_title or "").strip() or None
    body = str(text or "").strip()
    return {
        "section_title": normalized_section,
        "section_path": normalized_section,
        "text": body,
        "word_count": count_words(body),
        "char_count": len(body),
    }


def merge_chunk_records(left, right):
    return build_chunk_record(left.get("section_title"), join_paragraphs([left["text"], right["text"]]))


def split_large_text_by_chars(section_title, text, chunk_chars_limit, overlap_chars, min_chunk_words):
    pieces = []
    normalized = normalize_text(text)
    if not normalized:
        return pieces

    start = 0
    text_len = len(normalized)
    min_boundary = max(1, int(chunk_chars_limit * 0.55))
    trim_chars = " \n\t,]}"

    while start < text_len:
        end = min(text_len, start + chunk_chars_limit)
        if end < text_len:
            search_start = start + min_boundary
            boundary = -1
            for needle in ("\n\n", "\n", ",", " "):
                boundary = max(boundary, normalized.rfind(needle, search_start, end))
            if boundary > start:
                if normalized[boundary:boundary + 2] == "\n\n":
                    end = boundary
                else:
                    end = boundary + 1
        body_text = normalized[start:end].strip(trim_chars)
        if body_text:
            record = build_chunk_record(section_title, body_text)
            if pieces and record["word_count"] < min_chunk_words and pieces[-1]["char_count"] + record["char_count"] <= chunk_chars_limit:
                pieces[-1] = merge_chunk_records(pieces[-1], record)
            else:
                pieces.append(record)
        if end >= text_len:
            break
        next_start = max(start + 1, end - overlap_chars)
        if next_start <= start:
            next_start = end
        start = next_start
    return pieces


def split_large_paragraph(section_title, paragraph, chunk_words_limit, chunk_chars_limit, overlap_words, min_chunk_words):
    words = paragraph.split()
    if len(paragraph) > chunk_chars_limit and len(words) < max(8, min_chunk_words // 4):
        return split_large_text_by_chars(
            section_title,
            paragraph,
            chunk_chars_limit=chunk_chars_limit,
            overlap_chars=max(120, chunk_chars_limit // 10),
            min_chunk_words=min_chunk_words,
        )

    pieces = []
    if len(words) < 2:
        return split_large_text_by_chars(
            section_title,
            paragraph,
            chunk_chars_limit=chunk_chars_limit,
            overlap_chars=max(120, chunk_chars_limit // 10),
            min_chunk_words=min_chunk_words,
        )

    for piece_words in chunk_words(words, chunk_words_limit, overlap_words):
        if len(piece_words) < min_chunk_words and pieces:
            combined = f"{pieces[-1]['text']}\n\n{' '.join(piece_words)}".strip()
            pieces[-1] = build_chunk_record(section_title, combined)
            continue
        body_text = " ".join(piece_words).strip()
        if not body_text:
            continue
        pieces.append(build_chunk_record(section_title, body_text))
    return pieces


def chunk_section(section_title, paragraphs, chunk_words_limit, chunk_chars_limit, overlap_words, min_chunk_words):
    """Pack one logical section into chunks that fit the retrieval budget."""
    raw_chunks = []
    buffer = []
    buffer_words = 0
    buffer_chars = 0

    def flush_buffer():
        nonlocal buffer, buffer_words, buffer_chars
        if not buffer:
            return
        text = join_paragraphs(buffer)
        if text:
            raw_chunks.append(build_chunk_record(section_title, text))
        buffer = []
        buffer_words = 0
        buffer_chars = 0

    for paragraph in paragraphs:
        paragraph_words = count_words(paragraph)
        paragraph_chars = len(paragraph)
        if paragraph_words <= 0 and paragraph_chars <= 0:
            continue
        if paragraph_words > chunk_words_limit or paragraph_chars > chunk_chars_limit:
            flush_buffer()
            raw_chunks.extend(
                split_large_paragraph(
                    section_title,
                    paragraph,
                    chunk_words_limit,
                    chunk_chars_limit,
                    overlap_words,
                    min_chunk_words,
                )
            )
            continue
        should_flush = False
        if buffer and buffer_words >= min_chunk_words and buffer_words + paragraph_words > chunk_words_limit:
            should_flush = True
        if buffer and buffer_chars >= max(400, chunk_chars_limit // 3) and buffer_chars + paragraph_chars > chunk_chars_limit:
            should_flush = True
        if should_flush:
            flush_buffer()
        buffer.append(paragraph)
        buffer_words += paragraph_words
        buffer_chars += paragraph_chars
    flush_buffer()

    merged = []
    for chunk in raw_chunks:
        if not merged:
            merged.append(chunk)
            continue
        if (
            chunk["word_count"] < min_chunk_words
            and chunk["char_count"] < chunk_chars_limit
            and merged[-1]["char_count"] + chunk["char_count"] <= chunk_chars_limit
            and chunk.get("section_path") == merged[-1].get("section_path")
        ):
            merged[-1] = merge_chunk_records(merged[-1], chunk)
            continue
        merged.append(chunk)
    return merged


def build_page_chunks(text, chunk_words_limit, chunk_chars_limit, overlap_words, min_chunk_words):
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return []

    sections = []
    current_section = None
    current_paragraphs = []
    for paragraph in paragraphs:
        if looks_like_heading(paragraph):
            if current_paragraphs:
                sections.append((current_section, current_paragraphs))
                current_paragraphs = []
            current_section = paragraph.rstrip(":").strip() or None
            continue
        current_paragraphs.append(paragraph)
    if current_paragraphs:
        sections.append((current_section, current_paragraphs))

    if not sections:
        return chunk_section(None, paragraphs, chunk_words_limit, chunk_chars_limit, overlap_words, min_chunk_words)

    chunks = []
    for section_title, section_paragraphs in sections:
        chunks.extend(
            chunk_section(
                section_title,
                section_paragraphs,
                chunk_words_limit,
                chunk_chars_limit,
                overlap_words,
                min_chunk_words,
            )
        )
    return chunks


def is_structured_noise_page(title, text):
    normalized_title = str(title or '').strip().lower()
    normalized_text = normalize_text(text)
    if not normalized_text:
        return False
    whitespace_tokens = len(normalized_text.split())
    if normalized_title.startswith('map data/') or normalized_title.startswith('attached kml/'):
        return True
    if whitespace_tokens > 64 or len(normalized_text) < 12000:
        return False
    if normalized_text.startswith('{"type":"FeatureCollection"'):
        return True
    return '"coordinates":' in normalized_text[:4000]


def process_page(line, config):
    """Convert one page JSONL line into zero or more chunk JSON strings."""
    obj = json.loads(line)

    page_id = obj.get("page_id")
    title = obj.get("title", "")
    text = obj.get("text", "") or ""
    if is_structured_noise_page(title, text):
        return [], 0, 0
    page_chunks = build_page_chunks(
        text,
        chunk_words_limit=int(config["chunk_words"]),
        chunk_chars_limit=int(config["chunk_chars"]),
        overlap_words=int(config["overlap_words"]),
        min_chunk_words=int(config["min_chunk_words"]),
    )
    if not page_chunks:
        return [], 0, 0

    records = []
    short_pages = 0
    for chunk_index, chunk in enumerate(page_chunks):
        chunk_text = chunk.get("text", "")
        record = {
            "chunk_id": f"{page_id}:{chunk_index}",
            "page_id": page_id,
            "title": title,
            "chunk_index": chunk_index,
            "section_title": chunk.get("section_title"),
            "section_path": chunk.get("section_path"),
            "word_count": int(chunk.get("word_count") or 0),
            "char_count": int(chunk.get("char_count") or 0),
            "text": chunk_text,
            "language": detect_language(chunk_text),
        }
        records.append(json.dumps(record, ensure_ascii=False) + "\n")
    if len(records) == 1 and int(page_chunks[0].get("word_count") or 0) < int(config["min_chunk_words"]):
        short_pages = 1
    return records, len(records), short_pages


def main(
    input_file,
    output_file,
    chunk_words_limit,
    chunk_chars_limit,
    overlap_words,
    min_chunk_words,
    workers,
    map_chunksize,
):
    set_low_priority()

    in_path = Path(input_file)
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[init] counting pages in {in_path} ...")
    total_pages = count_lines(str(in_path))
    print(f"[init] {total_pages:,} pages to process")

    pages_seen = 0
    chunks_written = 0
    short_pages = 0
    config = {
        "chunk_words": max(1, int(chunk_words_limit)),
        "chunk_chars": max(256, int(chunk_chars_limit)),
        "overlap_words": max(0, int(overlap_words)),
        "min_chunk_words": max(1, int(min_chunk_words)),
    }

    print(
        "[config] "
        f"workers={workers} map_chunksize={map_chunksize} "
        f"chunk_words={config['chunk_words']} chunk_chars={config['chunk_chars']} "
        f"overlap_words={config['overlap_words']} min_chunk_words={config['min_chunk_words']}"
    )

    t0 = time.time()
    last_report = t0

    with open(in_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for records, written, short in executor.map(process_page, fin, repeat(config), chunksize=map_chunksize):
                pages_seen += 1

                if records:
                    fout.writelines(records)

                chunks_written += written
                short_pages += short

                now = time.time()
                if now - last_report >= 2:
                    elapsed = now - t0
                    rate = pages_seen / max(1e-9, elapsed)
                    remaining = max(0, total_pages - pages_seen)
                    eta_sec = remaining / rate if rate > 0 else 0
                    pct = (pages_seen / total_pages * 100) if total_pages > 0 else 0
                    print(
                        f"[progress] {pages_seen:,} / {total_pages:,} ({pct:.1f}%) | "
                        f"chunks: {chunks_written:,} | "
                        f"{rate:,.0f} pages/sec | ETA: {format_eta(eta_sec)}"
                    )
                    last_report = now

    dt = time.time() - t0
    print(f"[done] pages processed: {pages_seen:,}")
    print(f"[done] chunks written: {chunks_written:,}")
    print(f"[done] short-page fallbacks: {short_pages:,}")
    print(f"[done] workers used: {workers}")
    print(f"[done] time: {dt:,.1f} sec | avg rate: {pages_seen / max(1e-9, dt):,.0f} pages/sec")
    print(f"[done] output file: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chunk cleaned page JSONL into retrieval chunk JSONL.")
    parser.add_argument("--input-file", default=PAGES_FILE)
    parser.add_argument("--output-file", default=CHUNKS_FILE)
    parser.add_argument("--chunk-words", type=int, default=DEFAULT_CHUNK_WORDS)
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--overlap-words", type=int, default=DEFAULT_OVERLAP_WORDS)
    parser.add_argument("--min-chunk-words", type=int, default=DEFAULT_MIN_CHUNK_WORDS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--map-chunksize", type=int, default=DEFAULT_MAP_CHUNKSIZE)
    args = parser.parse_args()
    main(
        args.input_file,
        args.output_file,
        args.chunk_words,
        args.chunk_chars,
        args.overlap_words,
        args.min_chunk_words,
        max(1, args.workers),
        max(1, args.map_chunksize),
    )




