#!/usr/bin/env python3
"""
seed_regulations.py  (resilient version)

Seeds Firestore collection /regulations from a local PDF (default: en-legislation.pdf).
- Prefers ./serviceAccountKeypee.json
- Progress logging for deletes/uploads
- Optional: safe (non-batch) writes with timeout, slow deletes with timeout
- Timeouts & retries to avoid silent hangs

Usage examples:
  python seed_regulations.py --no-clear --safe --show-backend
  python seed_regulations.py --clear
  python seed_regulations.py --delete-slow
  python seed_regulations.py --pdf en-legislation.pdf --collection regulations_v2
"""

from __future__ import annotations
import os, re, io, sys, argparse, time
from typing import List, Dict, Any, Tuple

# ---- PDF extraction backends ----
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

# ---- Firestore ----
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.retry import Retry  # type: ignore

# ------------- PDF helpers -------------
def _extract_text_pdfminer(path: str) -> str:
    if not _PDFMINER_OK:
        return ""
    try:
        return pdfminer_extract_text(path) or ""
    except Exception:
        return ""

def _extract_text_pypdf(path: str) -> Tuple[str, List[str]]:
    if not _PYPDF_OK:
        return "", []
    try:
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            pages = [(pg.extract_text() or "") for pg in reader.pages]
        return "\n".join(pages), pages
    except Exception:
        return "", []

def extract_pdf_text(path: str) -> Tuple[str, List[str], str]:
    if _PDFMINER_OK:
        txt = _extract_text_pdfminer(path)
        if txt.strip():
            _, pages = _extract_text_pypdf(path)
            return txt, pages, "pdfminer"
    full, pages = _extract_text_pypdf(path)
    return full, pages, "pypdf"

# ------------- Text chunking -------------
def normalize_ws(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()

def clean_text_block(s: str) -> str:
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

TITLE_RX = re.compile(r"^(?:Law|Decree|Executive Council Resolution)[^\n]+$", re.M)
ARTICLE_HEAD_RX = re.compile(r"(Article\s*(?:\(\d+\)|\d+))", re.I)

def detect_titles_positions(text: str) -> List[Tuple[int, str]]:
    titles = [(m.start(), normalize_ws(m.group(0))) for m in TITLE_RX.finditer(text)]
    return titles or [(0, "Legislation Compilation")]

def split_block_into_articles(block_text: str) -> List[Tuple[str, str]]:
    parts = ARTICLE_HEAD_RX.split(block_text)
    out: List[Tuple[str, str]] = []
    for i in range(1, len(parts), 2):
        head = normalize_ws(parts[i])
        body = clean_text_block(parts[i + 1]) if i + 1 < len(parts) else ""
        if body:
            out.append((head, body))
    if not out and block_text.strip():
        out.append(("General", clean_text_block(block_text)))
    return out

def chunk_articles(full_text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    titles = sorted(detect_titles_positions(full_text), key=lambda t: t[0])
    titles.append((len(full_text) + 1, "END"))
    for i in range(len(titles) - 1):
        start, title = titles[i]
        end, _ = titles[i + 1]
        block = full_text[start:end]
        for head, body in split_block_into_articles(block):
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

# ------------- Firestore init -------------
def init_firestore_from_service_account() -> firestore.Client:
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

# ------------- Delete helpers -------------
def clear_collection_batch(db: firestore.Client, collection: str, batch_size: int = 400, timeout: float = 30.0) -> int:
    coll = db.collection(collection)
    count = 0
    to_delete = list(coll.stream())
    total = len(to_delete)
    print(f"   Found {total} docs to delete.")
    while to_delete:
        chunk = to_delete[:batch_size]
        to_delete = to_delete[batch_size:]
        batch = db.batch()
        for doc in chunk:
            batch.delete(doc.reference)
        # batch.commit has no exposed timeout; but it's fast in practice. If it stalls,
        # recommend using --delete-slow which calls .delete(timeout=..)
        batch.commit()
        count += len(chunk)
        print(f"   Deleted {count}/{total} …")
    return count

def clear_collection_slow(db: firestore.Client, collection: str, limit: int = 200, timeout: float = 20.0) -> int:
    """
    Safer but slower: deletes docs individually with timeout+retry, showing progress.
    """
    coll = db.collection(collection)
    retry = Retry(initial=1.0, maximum=10.0, multiplier=2.0, deadline=60.0)
    total_deleted = 0
    pass_num = 0
    while True:
        pass_num += 1
        docs = list(coll.limit(limit).stream())
        if not docs:
            break
        for i, doc in enumerate(docs, start=1):
            doc.reference.delete(retry=retry, timeout=timeout)
            total_deleted += 1
            if total_deleted % 50 == 0:
                print(f"   Deleted {total_deleted} …")
        print(f"   Pass {pass_num}: deleted {len(docs)}")
        time.sleep(0.5)
    return total_deleted

# ------------- Upload helpers -------------
def upload_docs_batch(db: firestore.Client, collection: str, docs: List[Dict[str, Any]], batch_size: int = 300) -> int:
    """
    Fast path: batch writes (no per-op timeout exposed).
    """
    coll = db.collection(collection)
    uploaded = 0
    total = len(docs)
    for i in range(0, total, batch_size):
        batch = db.batch()
        chunk = docs[i:i + batch_size]
        for d in chunk:
            ref = coll.document()  # auto-id
            if "id" not in d or not d["id"]:
                d["id"] = ref.id
            batch.set(ref, d)
        batch.commit()
        uploaded += len(chunk)
        print(f"   Uploaded {uploaded}/{total} …")
    return uploaded

def upload_docs_safe(db: firestore.Client, collection: str, docs: List[Dict[str, Any]], timeout: float = 20.0) -> int:
    """
    Safe path: per-doc set with timeout & retry, with progress.
    """
    coll = db.collection(collection)
    retry = Retry(initial=1.0, maximum=10.0, multiplier=2.0, deadline=60.0)
    uploaded = 0
    total = len(docs)
    for d in docs:
        ref = coll.document()
        if "id" not in d or not d["id"]:
            d["id"] = ref.id
        ref.set(d, retry=retry, timeout=timeout)
        uploaded += 1
        if uploaded % 50 == 0 or uploaded == total:
            print(f"   Uploaded {uploaded}/{total} …")
    return uploaded

# ------------- CLI / Main -------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Seed regulations into Firestore with progress & timeouts.")
    ap.add_argument("--pdf", default="en-legislation.pdf", help="Path to legislation PDF (default: en-legislation.pdf)")
    ap.add_argument("--collection", default="regulations", help="Firestore collection (default: regulations)")
    ap.add_argument("--clear", action="store_true", help="Clear the collection first (fast batch)")
    ap.add_argument("--delete-slow", action="store_true", help="Clear collection via slow per-doc deletes")
    ap.add_argument("--no-clear", action="store_true", help="Do not clear before upload")
    ap.add_argument("--safe", action="store_true", help="Use safe per-doc writes with timeout/retry")
    ap.add_argument("--show-backend", action="store_true", help="Print PDF backend used")
    args = ap.parse_args()

    pdf_path = args.pdf
    if not os.path.exists(pdf_path):
        print(f"[!] PDF not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print("→ Extracting PDF text…")
    full_text, pages, backend = extract_pdf_text(pdf_path)
    if args.show_backend:
        print(f"   Backend: {backend}")
    if not full_text.strip():
        print("[!] Could not extract any text.", file=sys.stderr)
        sys.exit(2)

    print("→ Chunking into titled articles…")
    docs = chunk_articles(clean_text_block(full_text))
    print(f"   Prepared {len(docs)} article docs.")

    print("→ Initializing Firestore…")
    db = init_firestore_from_service_account()

    # Clear options
    if args.clear and args.delete_slow:
        print("[!] Choose either --clear OR --delete-slow, not both.", file=sys.stderr)
        sys.exit(3)

    if args.clear:
        print(f"→ Clearing collection fast: {args.collection}")
        deleted = clear_collection_batch(db, args.collection)
        print(f"✓ Deleted {deleted} docs.")
    elif args.delete_slow:
        print(f"→ Clearing collection slow: {args.collection}")
        deleted = clear_collection_slow(db, args.collection)
        print(f"✓ Deleted {deleted} docs.")
    elif not args.no_clear:
        # default: ask once
        print(f"   (No --clear/--delete-slow/--no-clear provided; defaulting to NO CLEAR)")

    # Upload
    print(f"→ Uploading to /{args.collection} …")
    if args.safe:
        count = upload_docs_safe(db, args.collection, docs)
    else:
        count = upload_docs_batch(db, args.collection, docs)
    print(f"✓ Uploaded {count} docs.")

    # Meta
    db.collection(args.collection).document("_meta").set({
        "uploaded_at": firestore.SERVER_TIMESTAMP,
        "source_pdf": os.path.abspath(pdf_path),
        "backend": backend,
        "count": count,
        "note": "Seeded by seed_regulations.py (resilient)"
    }, merge=True)
    print("✓ Wrote _meta document.")

if __name__ == "__main__":
    # Reduce gRPC noise but keep essential warnings visible
    os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
    main()
