#!/usr/bin/env python3
"""
seed_regulations.py (simple)

- Uses Firebase Admin SDK with ./serviceAccountKeypee.json
- Extracts text from en-legislation.pdf (pdfminer -> PyPDF fallback)
- Chunks into title + Article parts and writes to Firestore
- Optional --clear to wipe collection first

Usage:
  python seed_regulations.py --clear
  python seed_regulations.py --pdf en-legislation.pdf --collection regulations
"""

from __future__ import annotations
import os, re, sys, argparse
from typing import List, Dict, Tuple

# ---------------- PDF extraction ----------------
_PDFMINER_OK = False
_PYPDF_OK = False
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
    _PDFMINER_OK = True
except Exception:
    _PDFMINER_OK = False
try:
    import pypdf
    _PYPDF_OK = True
except Exception:
    _PYPDF_OK = False

def _extract_pdfminer(path: str) -> str:
    if not _PDFMINER_OK:
        return ""
    try:
        return pdfminer_extract_text(path) or ""
    except Exception:
        return ""

def _extract_pypdf(path: str) -> str:
    if not _PYPDF_OK:
        return ""
    try:
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            pages = [pg.extract_text() or "" for pg in reader.pages]
        return "\n".join(pages)
    except Exception:
        return ""

def extract_pdf_text(path: str) -> Tuple[str, str]:
    """Return (text, backend)."""
    if _PDFMINER_OK:
        txt = _extract_pdfminer(path)
        if txt.strip():
            return txt, "pdfminer"
    txt = _extract_pypdf(path)
    return txt, "pypdf" if txt else "none"

# ---------------- chunking ----------------
TITLE_RX = re.compile(r"^(?:Law|Decree|Executive Council Resolution)[^\n]+$", re.M)
ARTICLE_RX = re.compile(r"(Article\s*(?:\(\d+\)|\d+))", re.I)

def _norm_ws(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s.strip())

def _clean_block(s: str) -> str:
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _detect_titles(text: str) -> List[Tuple[int, str]]:
    hits = [(m.start(), _norm_ws(m.group(0))) for m in TITLE_RX.finditer(text)]
    return hits or [(0, "Legislation Compilation")]

def _split_articles(block_text: str) -> List[Tuple[str, str]]:
    parts = ARTICLE_RX.split(block_text)
    out: List[Tuple[str, str]] = []
    # parts: [pre, head1, body1, head2, body2, ...]
    for i in range(1, len(parts), 2):
        head = _norm_ws(parts[i])
        body = _clean_block(parts[i + 1]) if i + 1 < len(parts) else ""
        if body:
            out.append((head, body))
    if not out and block_text.strip():
        out.append(("General", _clean_block(block_text)))
    return out

def chunk_articles(full_text: str) -> List[Dict]:
    items: List[Dict] = []
    titles = sorted(_detect_titles(full_text), key=lambda x: x[0])
    titles.append((len(full_text) + 1, "END"))
    for i in range(len(titles) - 1):
        start, title = titles[i]
        end, _ = titles[i + 1]
        block = full_text[start:end]
        for art, body in _split_articles(block):
            items.append({
                "title": title,
                "article": art,
                "text": body[:5000],
                "source": "Dubai Real Estate Legislation (English compilation)"
            })
    if not items and full_text.strip():
        items.append({
            "title": "Compilation",
            "article": "All",
            "text": full_text[:5000],
            "source": "Dubai Real Estate Legislation (English compilation)"
        })
    return items

# ---------------- Firestore ----------------
import firebase_admin
from firebase_admin import credentials, firestore

def init_firestore() -> firestore.Client:
    sa_path = os.path.join(os.getcwd(), "serviceAccountKeypee.json")
    if not os.path.exists(sa_path):
        print("[!] serviceAccountKeypee.json not found in project root.", file=sys.stderr)
        sys.exit(1)
    if not firebase_admin._apps:
        cred = credentials.Certificate(sa_path)
        firebase_admin.initialize_app(cred)
    return firestore.client()

def clear_collection(db: firestore.Client, collection: str, batch_size: int = 300) -> int:
    coll = db.collection(collection)
    docs = list(coll.stream())
    total = len(docs)
    if total == 0:
        print("   No documents to delete.")
        return 0
    print(f"   Deleting {total} docs…")
    deleted = 0
    for i in range(0, total, batch_size):
        chunk = docs[i : i + batch_size]
        batch = db.batch()
        for d in chunk:
            batch.delete(d.reference)
        batch.commit()
        deleted += len(chunk)
        print(f"     {deleted}/{total}")
    return deleted

def upload_docs(db: firestore.Client, collection: str, docs: List[Dict]) -> int:
    coll = db.collection(collection)
    total = len(docs)
    print(f"   Uploading {total} docs…")
    count = 0
    # simple batches of 300
    for i in range(0, total, 300):
        batch = db.batch()
        chunk = docs[i : i + 300]
        for d in chunk:
            ref = coll.document()
            batch.set(ref, d)
        batch.commit()
        count += len(chunk)
        print(f"     {count}/{total}")
    return count

# ---------------- CLI ----------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Seed Firestore with Dubai tenancy regulations.")
    ap.add_argument("--pdf", default="en-legislation.pdf", help="Path to the legislation PDF")
    ap.add_argument("--collection", default="regulations", help="Firestore collection name")
    ap.add_argument("--clear", action="store_true", help="Clear the collection before upload")
    args = ap.parse_args()

    if not os.path.exists(args.pdf):
        print(f"[!] PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    print("→ Extracting PDF text…")
    text, backend = extract_pdf_text(args.pdf)
    if not text.strip():
        print("[!] Could not extract any text from PDF.", file=sys.stderr)
        sys.exit(2)
    print(f"   Backend: {backend}")
    print(f"   Characters: {len(text)}")

    print("→ Chunking into titled articles…")
    docs = chunk_articles(_clean_block(text))
    print(f"   Prepared {len(docs)} docs")

    print("→ Initializing Firestore…")
    db = init_firestore()
    print("   Firestore ready ✓")

    if args.clear:
        print(f"→ Clearing collection: /{args.collection}")
        deleted = clear_collection(db, args.collection)
        print(f"   Deleted: {deleted}")

    print(f"→ Uploading to /{args.collection}")
    count = upload_docs(db, args.collection, docs)
    print(f"✓ Uploaded {count} docs")
    print("Done.")

if __name__ == "__main__":
    main()