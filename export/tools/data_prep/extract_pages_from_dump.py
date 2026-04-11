"""Turn a raw Wikipedia XML dump into a cleaned JSONL file for later indexing.

This is the first step in the local RAG pipeline. It reads the compressed
Wikipedia dump, removes markup and sections that do not help retrieval, and
writes one JSON record per page to the path configured in
`tools/config/index_settings.py`.
"""

import argparse
import bz2
import json
import re
import sys
from pathlib import Path

import mwxml

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.config.index_settings import PAGES_FILE

INPUT_DUMP = "simplewiki-latest-pages-articles.xml.bz2"

FILE_RE = re.compile(r"\[\[\s*(File|Image)\s*:[^\]]+\]\]", re.IGNORECASE)
CATEGORY_RE = re.compile(r"\[\[\s*Category\s*:[^\]]+\]\]", re.IGNORECASE)
TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
REF_RE = re.compile(r"<ref[^>]*>.*?</ref>", re.IGNORECASE | re.DOTALL)
REF_SELF_RE = re.compile(r"<ref[^/>]*/\s*>", re.IGNORECASE)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
EXTERNAL_LINK_RE = re.compile(r"\[https?://[^\s\]]+(?:\s+[^\]]+)?\]")
BOLD_ITALIC_RE = re.compile(r"'{2,5}")
HEADING_RE = re.compile(r"^==+\s*(.*?)\s*==+\s*$", re.MULTILINE)

DROP_SECTION_CONTAINS = (
    "events",
    "trivia",
    "see also",
    "references",
    "external links",
    "further reading",
    "notes",
    "list",
)


def clean_for_rag(text: str) -> str:
    """Remove noisy wiki markup so later chunking and indexing work on plain text.

    The output of this function is consumed by `chunk_pages_for_rag.py`, so the
    goal is predictable retrieval-friendly text rather than perfect wiki rendering.
    """
    text = HTML_COMMENT_RE.sub("", text)
    text = REF_RE.sub("", text)
    text = REF_SELF_RE.sub("", text)

    text = FILE_RE.sub("", text)
    text = CATEGORY_RE.sub("", text)
    text = EXTERNAL_LINK_RE.sub("", text)
    text = TEMPLATE_RE.sub("", text)

    text = BOLD_ITALIC_RE.sub("", text)

    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)

    matches = list(HEADING_RE.finditer(text))
    if matches:
        kept = [text[: matches[0].start()]]
        for i, m in enumerate(matches):
            title = m.group(1).strip().lower()
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            if any(k in title for k in DROP_SECTION_CONTAINS):
                continue
            kept.append(text[start:end])
        text = "".join(kept)

    text = re.sub(r"^==+\s*(.*?)\s*==+\s*$", r"\n\n\1:\n", text, flags=re.MULTILINE)
    text = re.sub(r"\[\[|\]\]", "", text)
    text = re.sub(r"^\*\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def main(input_dump: str, output_file: str):
    """Stream the compressed dump, clean each article, and write JSONL records.

    Each output line becomes an input row for the next pipeline stage, which means
    this file feeds directly into the chunking step and ultimately the Chroma index.
    """
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pages_seen = 0
    pages_written = 0

    with bz2.open(input_dump, "rb") as f, open(out_path, "w", encoding="utf-8") as out:
        dump = mwxml.Dump.from_file(f)

        for page in dump.pages:
            pages_seen += 1

            if page.redirect:
                continue

            latest = None
            for rev in page:
                latest = rev

            if latest is None or not latest.text:
                continue

            cleaned = clean_for_rag(latest.text)

            if len(cleaned) < 50:
                continue

            record = {
                "page_id": page.id,
                "title": page.title,
                "text": cleaned,
            }

            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            pages_written += 1

            if pages_seen % 1000 == 0:
                print(f"[status] pages seen: {pages_seen:,} | pages written: {pages_written:,}")

    print(f"[done] total pages seen: {pages_seen:,}")
    print(f"[done] total pages written: {pages_written:,}")
    print(f"[done] output file: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract cleaned pages JSONL from a Wikipedia XML dump.")
    parser.add_argument("--input-dump", default=INPUT_DUMP)
    parser.add_argument("--output-file", default=PAGES_FILE)
    args = parser.parse_args()
    main(args.input_dump, args.output_file)
