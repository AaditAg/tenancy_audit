# push_pdf_articles_to_firestore.py
# Minimal: init Firebase from serviceAccountKeypee.json, extract each PDF, split into articles,
# and upload EACH ARTICLE as its own document in Firestore.

import os
import re
from datetime import datetime

# ---------- Firestore Admin ----------
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- PDF text extraction (pdfminer -> pypdf fallback) ----------
_USE_PDFMINER = True
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except Exception:
    _USE_PDFMINER = False

try:
    import pypdf
    _HAS_PYPDF = True
except Exception:
    _HAS_PYPDF = False

SA_PATH = "serviceAccountKeypee.json"  # must exist in this folder
PDF_DIR = "."                          # scan this folder for *.pdf
COLLECTION = "pdf_articles"            # each article becomes ONE doc in this collection

# --- Extraction helpers ---
def extract_pdf_text(path: str):
    """Return (text, backend). Try pdfminer first, then pypdf."""
    if _USE_PDFMINER:
        try:
            txt = pdfminer_extract_text(path) or ""
            if txt.strip():
                return txt, "pdfminer"
        except Exception:
            pass
    if _HAS_PYPDF:
        try:
            with open(path, "rb") as f:
                reader = pypdf.PdfReader(f)
                pages = [pg.extract_text() or "" for pg in reader.pages]
            return "\n".join(pages), "pypdf"
        except Exception:
            pass
    return "", "none"

# --- Very light cleanup ---
def _clean(s: str) -> str:
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

# --- Simple title + article chunking ---
# We try to detect a "title header" (e.g., "Law …", "Decree …", etc.) and split blocks by "Article (x)" or "Article x".
TITLE_RX = re.compile(r"^(?:Law|Decree|Executive Council Resolution)[^\n]+$", re.M)
ARTICLE_RX = re.compile(r"(Article\s*(?:\(\d+\)|\d+))", re.I)

def detect_titles_positions(text: str):
    """Return list of (start_index, title_line). If none, fallback to a generic title."""
    hits = [(m.start(), m.group(0).strip()) for m in TITLE_RX.finditer(text)]
    return hits or [(0, "Document")]

def split_block_into_articles(block_text: str):
    """
    Split a block into [(article_heading, body_text), ...].
    If no 'Article' headings are found, return a single 'General' article.
    """
    parts = ARTICLE_RX.split(block_text)
    out = []
    # structure: [pre, head1, body1, head2, body2, ...]
    for i in range(1, len(parts), 2):
        head = parts[i].strip()
        body = _clean(parts[i + 1]) if i + 1 < len(parts) else ""
        if body:
            out.append((head, body))
    if not out and block_text.strip():
        out.append(("General", _clean(block_text)))
    return out

def chunk_articles(full_text: str):
    """
    Return a list of dicts:
      {title, article, text}
    Each dict is one article.
    """
    items = []
    titles = sorted(detect_titles_positions(full_text), key=lambda x: x[0])
    titles.append((len(full_text) + 1, "END"))
    for i in range(len(titles) - 1):
        start, title = titles[i]
        end, _ = titles[i + 1]
        block = full_text[start:end]
        for art, body in split_block_into_articles(block):
            items.append({
                "title": title,
                "article": art,
                "text": body
            })
    if not items and full_text.strip():
        items.append({"title": "Document", "article": "General", "text": _clean(full_text)})
    return items

def main():
    # 1) Firestore init (simple)
    if not os.path.exists(SA_PATH):
        raise FileNotFoundError(f"'{SA_PATH}' not found. Put your service account JSON here with that exact name.")
    if not firebase_admin._apps:
        cred = credentials.Certificate(SA_PATH)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✓ Firestore ready")

    # 2) Find PDFs
    pdfs = [f for f in os.listdir(PDF_DIR) if f.lower().endswith(".pdf")]
    if not pdfs:
        print("No PDFs found in this folder. Drop some .pdf files here and rerun.")
        return

    # 3) Process each PDF; write EACH ARTICLE as a separate doc
    for fname in sorted(pdfs):
        path = os.path.join(PDF_DIR, fname)
        text, backend = extract_pdf_text(path)
        cleaned = _clean(text)
        articles = chunk_articles(cleaned)
        print(f"{fname}: extracted {len(cleaned)} chars via {backend}; found {len(articles)} article(s)")

        for idx, art in enumerate(articles, start=1):
            doc = {
                "filename": fname,
                "title": art["title"],
                "article": art["article"],
                "text": art["text"],
                "length": len(art["text"]),
                "backend": backend,
                "article_index": idx,
                "created_at": firestore.SERVER_TIMESTAMP,
                "local_time": datetime.now().isoformat(),
            }
            ref = db.collection(COLLECTION).document()  # auto-ID
            ref.set(doc)

        print(f"✓ Uploaded {len(articles)} documents for {fname} into '{COLLECTION}'")

if __name__ == "__main__":
    main()
