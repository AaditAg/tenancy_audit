"""
Usage:
  1) Put en-legislation.pdf in the project root (or pass a path).
  2) Ensure Firestore is initialized via GOOGLE_APPLICATION_CREDENTIALS
     or edit the 'firebase_init_from_file' call below.
  3) Run:  python seed_regulations.py
"""

import os, re, json, io
from typing import List, Dict, Any
from pdfminer.high_level import extract_text as pdfminer_extract_text  # pip install pdfminer.six
import firebase_admin
from firebase_admin import credentials, firestore

PDF_PATH = os.environ.get("LEGISLATION_PDF", "en-legislation.pdf")
SOURCE = "Dubai Real Estate Legislation (English compilation)"
COLL = "regulations"

def init_firestore():
    # Option A: env var GOOGLE_APPLICATION_CREDENTIALS
    if not firebase_admin._apps:
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            firebase_admin.initialize_app()
        else:
            # Option B: hardcode path if needed:
            # cred = credentials.Certificate("serviceAccount.json")
            # firebase_admin.initialize_app(cred)
            raise RuntimeError("Set GOOGLE_APPLICATION_CREDENTIALS or edit seed_regulations.py to load credentials.")
    return firestore.client()

def chunk_articles(text: str) -> List[Dict[str, Any]]:
    # Split by "Article (N)" headings; keep context title if found earlier.
    # Also capture “Decree No. (43) of 2013 …” style titles.
    pieces: List[Dict[str, Any]] = []

    # Try to capture “Law/Decree Title” lines as blocks
    # Keep last seen title and apply to following articles until next title.
    title_rx = re.compile(r"^(?:Law|Decree|Executive Council Resolution)[^\n]+$", re.M)
    article_rx = re.compile(r"(Article\s*\(\d+\))", re.I)

    # Pre-find all titles with their positions
    titles = [(m.start(), m.group(0).strip()) for m in title_rx.finditer(text)]
    titles.append((len(text)+1, ""))  # sentinel

    for idx in range(len(titles)-1):
        start, title = titles[idx]
        end, _ = titles[idx+1]
        block = text[start:end]
        # Split this block into articles
        parts = article_rx.split(block)
        # parts like ["prefix", "Article (1)", "content...", "Article (2)", "content..."]
        for i in range(1, len(parts), 2):
            art_head = parts[i].strip()
            art_text = parts[i+1].strip() if i+1 < len(parts) else ""
            if not art_text:
                continue
            art_num = re.findall(r"\d+", art_head)
            pieces.append({
                "id": f"{title}::{art_head}",
                "title": title,
                "article": art_head,
                "text": art_text[:5000],  # cap
                "source": SOURCE,
            })
    # If nothing matched (fallback), push entire text as one doc
    if not pieces:
        pieces.append({"id": "FULL_TEXT", "title": "Compilation", "article": "All", "text": text[:5000], "source": SOURCE})
    return pieces

def main():
    if not os.path.exists(PDF_PATH):
        raise FileNotFoundError(f"PDF not found at {PDF_PATH}")
    print("Extracting text…")
    txt = pdfminer_extract_text(PDF_PATH)
    if not txt.strip():
        raise RuntimeError("Could not extract text from the legislation PDF.")

    print("Chunking into articles…")
    docs = chunk_articles(txt)
    print(f"Prepared {len(docs)} docs to upload.")

    db = init_firestore()
    batch = db.batch()
    coll = db.collection(COLL)

    # Clear old regs (optional)
    # NOTE: Only do this in dev — comment out if you need append-only
    for old in coll.stream():
        batch.delete(old.reference)
    batch.commit()

    # Upload new
    count = 0
    for d in docs:
        ref = coll.document()
        d['id'] = ref.id if not d.get('id') else d['id']
        batch.set(ref, d)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    print(f"Uploaded {count} regulation entries to /{COLL}")

if __name__ == "__main__":
    main()
