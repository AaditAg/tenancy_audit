#!/usr/bin/env python3
"""
seed_regulations.py

Reads the Dubai tenancy legislation PDF, chunks it into "title + article" docs,
and uploads them to Firestore in the /regulations collection.

Defaults (override with CLI flags):
  - PDF path: ./en-legislation.pdf
  - Service account: ./serviceAccountKeypee.json (preferred), else GOOGLE_APPLICATION_CREDENTIALS
  - Collection: regulations
  - Clears collection first if --clear is passed.

Install:
  pip install pdfminer.six firebase-admin pypdf

Examples:
  python seed_regulations.py --pdf en-legislation.pdf --clear
  python seed_regulations.py --pdf /abs/path/file.pdf --collection regulations_v2
"""

from __future__ import annotations

import os
import re
import io
import sys
import json
import argparse
from typing import List, Dict, Any, Tuple, Iterable

# --- PDF extraction backends ---
_PDFMINER_OK = False
_PYPDF_OK = False

try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text  # type: ignore
    _PDFMINER_OK = True
except Exception:
    _PDFMINER_OK = False

try:
    import pypdf  # type: ignore
    _PYPDF_OK = True
except Exception:
    _PYPDF_OK = False

# --- Firestore ---
import firebase_admin
from firebase_admin import credentials, firestore


# ---------------------------- PDF helpers --------------------------------
def _extract_text_pdfminer(path: str) -> str:
    if not _PDFMINER_OK:
        return ""
    try:
        return pdfminer_extract_text(path) or ""
    except Exception:
        return ""


def _extract_text_pypdf(path: str) -> Tuple[str, List[str]]:
    """
    Returns (full_text, per_page_texts) using pypdf (sometimes better for per-page).
    """
    if not _PYPDF_OK:
        return "", []
    try:
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            pages = []
            for pg in reader.pages:
                pages.append(pg.extract_text() or "")
        return "\n".join(pages), pages
    except Exception:
        return "", []


def extract_pdf_text(path: str) -> Tuple[str, List[str], str]:
    """
    Try pdfminer first (better layout generally). If empty, fallback to pypdf.
    Returns: (full_text, per_page_texts, backend_used)
    """
    if _PDFMINER_OK:
        txt = _extract_text_pdfminer(path)
        if txt.strip():
            # we can still try to get page splits via pypdf (optional)
            _, pages = _extract_text_pypdf(path)
            return txt, pages, "pdfminer"
    # fallback
    full, pages = _extract_text_pypdf(path)
    return full, pages, "pypdf"


# ---------------------------- Text processing -----------------------------
def normalize_ws(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()


def clean_text_block(s: str) -> str:
    # Normalize whitespace & collapse long runs of blank lines
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


TITLE_RX = re.compile(r"^(?:Law|Decree|Executive Council Resolution)[^\n]+$", re.M)
# Articles often appear as "Article (1)" or "Article 1"
ARTICLE_HEAD_RX = re.compile(r"(Article\s*(?:\(\d+\)|\d+))", re.I)


def detect_titles_positions(text: str) -> List[Tuple[int, str]]:
    """
    Find top-level titles like "Decree No. (43) of 2013 ..." lines.
    Returns list of (start_index, title_line) sorted by position.
    """
    titles = [(m.start(), normalize_ws(m.group(0))) for m in TITLE_RX.finditer(text)]
    # If no titles found, treat the whole doc as one block with a generic title
    if not titles:
        return [(0, "Legislation Compilation")]
    return titles


def split_block_into_articles(block_text: str) -> List[Tuple[str, str]]:
    """
    Given a block that belongs to one 'title', split into (article_heading, article_body).
    Returns a list of tuples.
    """
    parts = ARTICLE_HEAD_RX.split(block_text)
    # parts = ["prefix", "Article (1)", "content...", "Article (2)", "content...", ...]
    out: List[Tuple[str, str]] = []
    for i in range(1, len(parts), 2):
        head = normalize_ws(parts[i])
        body = clean_text_block(parts[i + 1]) if i + 1 < len(parts) else ""
        if body:
            out.append((head, body))
    # If nothing, push entire block as one “article”
    if not out and block_text.strip():
        out.append(("General", clean_text_block(block_text)))
    return out


def chunk_articles(full_text: str) -> List[Dict[str, Any]]:
    """
    Chunk the entire PDF text into a list of dicts:
      { id, title, article, text, source }
    """
    items: List[Dict[str, Any]] = []
    titles = detect_titles_positions(full_text)
    titles_sorted = sorted(titles, key=lambda t: t[0])
    # Append a sentinel end
    titles_sorted.append((len(full_text) + 1, "END"))

    for i in range(len(titles_sorted) - 1):
        start, title = titles_sorted[i]
        end, _ = titles_sorted[i + 1]
        block = full_text[start:end]
        articles = split_block_into_articles(block)
        for head, body in articles:
            # Cap body to protect Firestore limits (1MB doc size; we stay modest)
            items.append({
                "id": f"{title}::{head}",
                "title": title,
                "article": head,
                "text": body[:5000],
                "source": "Dubai Real Estate Legislation (English compilation)"
            })

    if not items and full_text.strip():
        items.append({
            "id": "FULL_TEXT",
            "title": "Compilation",
            "article": "All",
            "text": full_text[:5000],
            "source": "Dubai Real Estate Legislation (English compilation)"
        })

    return items


# ---------------------------- Firestore init ------------------------------
def init_firestore_from_service_account() -> firestore.Client:
    """
    Preferred: ./serviceAccountKeypee.json in CWD.
    Fallback: GOOGLE_APPLICATION_CREDENTIALS env var (path).
    """
    if not firebase_admin._apps:
        sa_path = os.path.join(os.getcwd(), "serviceAccountKeypee.json")
        if os.path.exists(sa_path):
            cred = credentials.Certificate(sa_path)
            firebase_admin.initialize_app(cred)
        elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            firebase_admin.initialize_app()
        else:
            raise RuntimeError(
                "No Firebase credentials. Place serviceAccountKeypee.json in project root "
                "or set GOOGLE_APPLICATION_CREDENTIALS to a valid path."
            )
    return firestore.client()


# ------------------------------ Upload logic ------------------------------
def clear_collection(db: firestore.Client, collection: str) -> None:
    """
    Danger: deletes all docs in the collection. Use for dev resets.
    """
    coll = db.collection(collection)
    batch = db.batch()
    count = 0
    for doc in coll.stream():
        batch.delete(doc.reference)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    if count % 400 != 0:
        batch.commit()


def upload_docs(db: firestore.Client, collection: str, docs: List[Dict[str, Any]], batch_size: int = 400) -> int:
    """
    Upload in batches. If a doc already exists, we just create new auto-ids.
    If you want deterministic IDs, you could hash title+article.
    """
    coll = db.collection(collection)
    batch = db.batch()
    uploaded = 0
    for d in docs:
        ref = coll.document()
        # ensure an id field is present for matching (not Firestore doc id)
        if "id" not in d or not d["id"]:
            d["id"] = f"{ref.id}"
        batch.set(ref, d)
        uploaded += 1
        if uploaded % batch_size == 0:
            batch.commit()
            batch = db.batch()
    if uploaded % batch_size != 0:
        batch.commit()
    return uploaded


# ------------------------------ CLI / Main --------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Seed regulations (articles) into Firestore.")
    parser.add_argument("--pdf", default="en-legislation.pdf", help="Path to the legislation PDF (default: en-legislation.pdf)")
    parser.add_argument("--collection", default="regulations", help="Firestore collection name (default: regulations)")
    parser.add_argument("--clear", action="store_true", help="Clear the collection before uploading")
    parser.add_argument("--show-backend", action="store_true", help="Print which PDF backend was used")
    args = parser.parse_args()

    pdf_path = args.pdf
    if not os.path.exists(pdf_path):
        print(f"[!] PDF not found at: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print("→ Extracting PDF text…")
    full_text, pages, backend = extract_pdf_text(pdf_path)
    if args.show_backend:
        print(f"   Backend used: {backend}")
    if not full_text.strip():
        print("[!] Could not extract any text from the PDF.", file=sys.stderr)
        sys.exit(2)

    print("→ Chunking into titled articles…")
    docs = chunk_articles(clean_text_block(full_text))
    print(f"   Prepared {len(docs)} article docs.")

    print("→ Initializing Firestore…")
    db = init_firestore_from_service_account()

    if args.clear:
        print(f"→ Clearing collection: {args.collection}")
        clear_collection(db, args.collection)

    print(f"→ Uploading to /{args.collection} …")
    count = upload_docs(db, args.collection, docs, batch_size=400)
    print(f"✓ Uploaded {count} docs to /{args.collection}")

    # Optional: small index doc to record metadata/time
    meta_ref = db.collection(args.collection).document("_meta")
    meta_ref.set({
        "uploaded_at": firestore.SERVER_TIMESTAMP,
        "source_pdf": os.path.abspath(pdf_path),
        "backend": backend,
        "count": count,
        "note": "Seeded by seed_regulations.py",
    }, merge=True)
    print("✓ Wrote _meta document.")

if __name__ == "__main__":
    main()