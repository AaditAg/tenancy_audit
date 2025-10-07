#!/usr/bin/env python3
"""
seed_regulations.py  — simple & robust Firestore seeder

- Uses Firebase Admin SDK with ./serviceAccountKeypee.json (no JWT flows)
- Extracts text from a legislation PDF (default: en-legislation.pdf)
- Chunks into Title + Article pieces and writes to Firestore
- Safe, paged collection clear (no giant reads; no hangs)
- Progress logs you can actually trust

Usage:
  python seed_regulations.py --clear
  python seed_regulations.py --pdf en-legislation.pdf --collection regulations
  python seed_regulations.py --batch 150 --timeout 25
"""

from __future__ import annotations
import os, re, sys, argparse, time
from typing import List, Dict, Tuple

# Quiet down gRPC/TF/absl spam
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

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
    return (txt, "pypdf") if txt else ("", "none")

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
    # parts pattern: [pre, head1, body1, head2, body2, ...]
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
                "text": body[:5000],  # keep docs small/light
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

# ---------------- Firestore (Admin SDK) ----------------
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

def clear_collection_paged(
    db: firestore.Client,
    collection: str,
    page_size: int = 100,
    timeout: float = 20.0,
    sleep_s: float = 0.1,
) -> int:
    """
    Safe, paged deletion — orders by doc name, fetches a small page,
    deletes it, then continues from the last doc. Won’t hang or load everything.
    """
    coll = db.collection(collection)
    deleted_total = 0
    last_doc = None
    page_num = 0

    while True:
        q = coll.order_by("__name__").limit(page_size)
        if last_doc is not None:
            q = q.start_after({u"__name__": last_doc.id})
        docs = list(q.stream(timeout=timeout))
        if not docs:
            break

        page_num += 1
        batch = db.batch()
        for d in docs:
            batch.delete(d.reference)
        batch.commit()
        deleted_total += len(docs)
        last_doc = docs[-1]
        print(f"   Deleted {deleted_total} docs (page {page_num}, size {len(docs)})")
        time.sleep(sleep_s)  # be gentle

    return deleted_total

def upload_docs_batched(
    db: firestore.Client,
    collection: str,
    docs: List[Dict],
    batch_size: int = 200,
) -> int:
    coll = db.collection(collection)
    total = len(docs)
    uploaded = 0
    for i in range(0, total, batch_size):
        chunk = docs[i:i+batch_size]
        batch = db.batch()
        for d in chunk:
            ref = coll.document()
            batch.set(ref, d)
        batch.commit()
        uploaded += len(chunk)
        print(f"   Uploaded {uploaded}/{total}")
    return uploaded

# ---------------- CLI ----------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Seed Firestore with Dubai tenancy regulations.")
    ap.add_argument("--pdf", default="en-legislation.pdf", help="Path to legislation PDF")
    ap.add_argument("--collection", default="regulations", help="Firestore collection name")
    ap.add_argument("--clear", action="store_true", help="Clear the collection before upload")
    ap.add_argument("--batch", type=int, default=200, help="Batch size for writes/deletes")
    ap.add_argument("--timeout", type=float, default=20.0, help="Per-call timeout (seconds)")
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
    print(f"   Prepared {len(docs)} documents")

    print("→ Initializing Firestore…")
    db = init_firestore()
    print("   Firestore ready ✓")

    if args.clear:
        print(f"→ Clearing collection: /{args.collection}")
        deleted = clear_collection_paged(
            db, args.collection, page_size=args.batch, timeout=args.timeout
        )
        print(f"   Deleted: {deleted}")

    print(f"→ Uploading to /{args.collection} …")
    count = upload_docs_batched(db, args.collection, docs, batch_size=args.batch)
    print(f"✓ Uploaded {count} documents")
    print("Done.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user")
        sys.exit(130)
