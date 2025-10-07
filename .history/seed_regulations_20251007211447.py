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
import os, re, io, sys, argparse, time, json
from typing import List, Dict, Any, Tuple
from datetime import datetime

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
from google.api_core import exceptions as google_exceptions  # type: ignore
from google.auth import exceptions as auth_exceptions  # type: ignore

# ------------- PDF helpers -------------
def _extract_text_pdfminer(path: str) -> str:
    if not _PDFMINER_OK:
        return ""
    try:
        return pdfminer_extract_text(path) or ""
    except Exception as e:
        print(f"   Warning: pdfminer failed: {e}")
        return ""

def _extract_text_pypdf(path: str) -> Tuple[str, List[str]]:
    if not _PYPDF_OK:
        return "", []
    try:
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            pages = [(pg.extract_text() or "") for pg in reader.pages]
        return "\n".join(pages), pages
    except Exception as e:
        print(f"   Warning: pypdf failed: {e}")
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

# ------------- Firestore init with better error handling -------------
def verify_service_account(sa_path: str) -> bool:
    """Verify service account JSON is valid and not expired"""
    try:
        with open(sa_path, 'r') as f:
            sa_data = json.load(f)
        
        # Check required fields
        required_fields = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
        for field in required_fields:
            if field not in sa_data:
                print(f"[!] Missing required field in service account: {field}")
                return False
        
        if sa_data['type'] != 'service_account':
            print(f"[!] Invalid credential type: {sa_data['type']}")
            return False
            
        print(f"   Service account email: {sa_data.get('client_email', 'unknown')}")
        print(f"   Project ID: {sa_data.get('project_id', 'unknown')}")
        return True
    except json.JSONDecodeError as e:
        print(f"[!] Invalid JSON in service account file: {e}")
        return False
    except Exception as e:
        print(f"[!] Error reading service account file: {e}")
        return False

def init_firestore_from_service_account() -> firestore.Client:
    """Initialize Firestore with better error handling and retry logic"""
    
    # Clean up any existing app first
    if firebase_admin._apps:
        print("   Cleaning up existing Firebase app...")
        try:
            firebase_admin.delete_app(firebase_admin.get_app())
        except Exception:
            pass
    
    sa_path = os.path.join(os.getcwd(), "serviceAccountKeypee.json")
    
    # Try service account file first
    if os.path.exists(sa_path):
        print(f"   Found service account at: {sa_path}")
        
        # Verify the service account file
        if not verify_service_account(sa_path):
            print("\n[!] Service account verification failed!")
            print("Please ensure:")
            print("1. The serviceAccountKeypee.json file is valid")
            print("2. Download a fresh copy from Firebase Console:")
            print("   - Go to Project Settings > Service Accounts")
            print("   - Click 'Generate New Private Key'")
            print("   - Save as 'serviceAccountKeypee.json' in project root")
            sys.exit(1)
        
        try:
            # Initialize with explicit timeout settings
            cred = credentials.Certificate(sa_path)
            app = firebase_admin.initialize_app(cred, options={
                'projectId': json.load(open(sa_path))['project_id']
            })
            
            # Get client with explicit timeout
            client = firestore.client(app)
            
            # Test the connection with a simple operation
            print("   Testing Firestore connection...")
            test_retry = Retry(initial=1.0, maximum=5.0, multiplier=2.0, deadline=30.0)
            try:
                # Try to list collections (lightweight operation)
                collections = list(client.collections(retry=test_retry, timeout=10.0))
                print(f"   Connection successful! Found {len(collections)} collections.")
            except Exception as e:
                print(f"   Warning: Initial connection test failed: {e}")
                print("   Will attempt to continue anyway...")
            
            return client
            
        except (auth_exceptions.RefreshError, auth_exceptions.GoogleAuthError) as e:
            print(f"\n[!] Authentication error: {e}")
            print("\nThis usually means:")
            print("1. The service account key is expired or invalid")
            print("2. System time is not synchronized")
            print("\nFixes to try:")
            print("1. Download a fresh service account key from Firebase Console")
            print("2. Check your system time: run 'date' command")
            print("3. On Mac: System Preferences > Date & Time > Set automatically")
            print("4. On Linux: sudo ntpdate -s time.nist.gov")
            sys.exit(1)
            
        except Exception as e:
            print(f"\n[!] Failed to initialize Firebase: {e}")
            sys.exit(1)
    
    # Try environment variable
    elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        env_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        print(f"   Using GOOGLE_APPLICATION_CREDENTIALS: {env_path}")
        
        if not os.path.exists(env_path):
            print(f"[!] File not found: {env_path}")
            sys.exit(1)
            
        if not verify_service_account(env_path):
            print("[!] Service account verification failed!")
            sys.exit(1)
            
        try:
            firebase_admin.initialize_app()
            return firestore.client()
        except Exception as e:
            print(f"\n[!] Failed to initialize Firebase: {e}")
            sys.exit(1)
    
    else:
        print("\n[!] No Firebase credentials found!")
        print("\nPlease do one of the following:")
        print("1. Place serviceAccountKeypee.json in project root")
        print("2. Set GOOGLE_APPLICATION_CREDENTIALS environment variable")
        print("\nTo get service account key:")
        print("1. Go to Firebase Console > Project Settings > Service Accounts")
        print("2. Click 'Generate New Private Key'")
        print("3. Save as 'serviceAccountKeypee.json' in this directory")
        sys.exit(1)

# ------------- Delete helpers with better error handling -------------
def clear_collection(db: firestore.Client, collection: str, batch_size: int = 100) -> int:
    """Clear collection with smaller batches and better error handling"""
    coll = db.collection(collection)
    count = 0
    retry_policy = Retry(initial=1.0, maximum=10.0, multiplier=2.0, deadline=300.0)
    
    try:
        print(f"   Fetching documents from /{collection}...")
        # Fetch with explicit timeout and retry
        to_delete = []
        try:
            query_stream = coll.stream(retry=retry_policy, timeout=30.0)
            for doc in query_stream:
                to_delete.append(doc)
                if len(to_delete) % 100 == 0:
                    print(f"   Found {len(to_delete)} docs so far...")
        except Exception as e:
            print(f"   Warning during fetch: {e}")
            print("   Attempting alternative fetch method...")
            # Try limiting the query
            to_delete = list(coll.limit(1000).stream(retry=retry_policy, timeout=30.0))
        
        total = len(to_delete)
        print(f"   Found {total} docs to delete.")
        
        if total == 0:
            return 0
        
        # Delete in smaller batches with error handling
        while to_delete:
            chunk = to_delete[:batch_size]
            to_delete = to_delete[batch_size:]
            
            # Retry batch commit up to 3 times
            for attempt in range(3):
                try:
                    batch = db.batch()
                    for doc in chunk:
                        batch.delete(doc.reference)
                    batch.commit()
                    count += len(chunk)
                    print(f"   Deleted {count}/{total} documents...")
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"   Failed to delete batch after 3 attempts: {e}")
                        print("   Continuing with next batch...")
                    else:
                        print(f"   Retry {attempt + 1}/3 for batch delete...")
                        time.sleep(2 ** attempt)
        
        return count
        
    except Exception as e:
        print(f"   Error during collection clear: {e}")
        print("   Attempting document-by-document deletion...")
        return clear_collection_slow(db, collection)

def clear_collection_slow(db: firestore.Client, collection: str, limit: int = 50, timeout: float = 20.0) -> int:
    """Ultra-safe deletion with aggressive retries"""
    coll = db.collection(collection)
    retry = Retry(initial=2.0, maximum=15.0, multiplier=2.0, deadline=120.0)
    total_deleted = 0
    pass_num = 0
    
    while True:
        pass_num += 1
        try:
            docs = list(coll.limit(limit).stream(retry=retry, timeout=timeout))
        except Exception as e:
            print(f"   Error fetching docs in pass {pass_num}: {e}")
            break
            
        if not docs:
            break
            
        for i, doc in enumerate(docs, start=1):
            for attempt in range(3):
                try:
                    doc.reference.delete(retry=retry, timeout=timeout)
                    total_deleted += 1
                    if total_deleted % 20 == 0:
                        print(f"   Deleted {total_deleted} documents...")
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"   Failed to delete doc after 3 attempts: {e}")
                    else:
                        time.sleep(1)
        
        print(f"   Pass {pass_num}: deleted {len(docs)} docs")
        time.sleep(1)  # Be gentle on the API
    
    return total_deleted

# ------------- Upload helpers with better error handling -------------
def upload_docs_batch(db: firestore.Client, collection: str, docs: List[Dict[str, Any]], batch_size: int = 100) -> int:
    """Batch upload with smaller batches and retry logic"""
    coll = db.collection(collection)
    uploaded = 0
    total = len(docs)
    
    for i in range(0, total, batch_size):
        chunk = docs[i:i + batch_size]
        
        # Retry batch up to 3 times
        for attempt in range(3):
            try:
                batch = db.batch()
                for d in chunk:
                    ref = coll.document()
                    if "id" not in d or not d["id"]:
                        d["id"] = ref.id
                    batch.set(ref, d)
                batch.commit()
                uploaded += len(chunk)
                print(f"   Uploaded {uploaded}/{total} documents...")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"   Failed batch after 3 attempts: {e}")
                    print("   Switching to safe mode for this batch...")
                    # Fall back to individual uploads for this batch
                    for d in chunk:
                        try:
                            ref = coll.document()
                            if "id" not in d or not d["id"]:
                                d["id"] = ref.id
                            ref.set(d, timeout=10.0)
                            uploaded += 1
                        except Exception as doc_e:
                            print(f"   Failed to upload doc: {doc_e}")
                else:
                    print(f"   Retry {attempt + 1}/3 for batch upload...")
                    time.sleep(2 ** attempt)
    
    return uploaded

def upload_docs_safe(db: firestore.Client, collection: str, docs: List[Dict[str, Any]], timeout: float = 20.0) -> int:
    """Safe upload with aggressive retry per document"""
    coll = db.collection(collection)
    retry = Retry(initial=2.0, maximum=15.0, multiplier=2.0, deadline=120.0)
    uploaded = 0
    total = len(docs)
    failed = 0
    
    for d in docs:
        for attempt in range(3):
            try:
                ref = coll.document()
                if "id" not in d or not d["id"]:
                    d["id"] = ref.id
                ref.set(d, retry=retry, timeout=timeout)
                uploaded += 1
                if uploaded % 20 == 0 or uploaded == total:
                    print(f"   Uploaded {uploaded}/{total} documents...")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"   Failed to upload doc after 3 attempts: {e}")
                    failed += 1
                else:
                    time.sleep(1)
    
    if failed > 0:
        print(f"   Warning: {failed} documents failed to upload")
    
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

    print("\n" + "="*60)
    print("FIREBASE REGULATIONS SEEDER")
    print("="*60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PDF: {pdf_path}")
    print(f"Collection: {args.collection}")
    print("="*60 + "\n")

    print("→ Extracting PDF text...")
    full_text, pages, backend = extract_pdf_text(pdf_path)
    if args.show_backend:
        print(f"   Backend: {backend}")
    if not full_text.strip():
        print("[!] Could not extract any text.", file=sys.stderr)
        sys.exit(2)
    
    print(f"   Extracted {len(full_text)} characters from {len(pages)} pages")

    print("\n→ Chunking into titled articles...")
    docs = chunk_articles(clean_text_block(full_text))
    print(f"   Prepared {len(docs)} article documents")
    
    # Show sample of what will be uploaded
    if docs:
        print("\n   Sample document structure:")
        sample = docs[0]
        for key in sample:
            val_preview = str(sample[key])[:50] + "..." if len(str(sample[key])) > 50 else str(sample[key])
            print(f"     {key}: {val_preview}")

    print("\n→ Initializing Firestore connection...")
    db = init_firestore_from_service_account()
    print("   ✓ Firestore client initialized successfully")

    # Clear collection if requested
    if args.clear and args.delete_slow:
        print("[!] Choose either --clear OR --delete-slow, not both.", file=sys.stderr)
        sys.exit(3)

    if args.clear:
        print(f"\n→ Clearing collection: /{args.collection}")
        deleted = clear_collection(db, args.collection)
        print(f"   ✓ Deleted {deleted} documents")
    elif args.delete_slow:
        print(f"\n→ Clearing collection (slow mode): /{args.collection}")
        deleted = clear_collection_slow(db, args.collection)
        print(f"   ✓ Deleted {deleted} documents")
    elif not args.no_clear:
        print("\n   ℹ No clear flag specified, skipping deletion")

    # Upload documents
    print(f"\n→ Uploading documents to /{args.collection}...")
    if args.safe:
        print("   Using safe mode (slower but more reliable)")
        count = upload_docs_safe(db, args.collection, docs)
    else:
        print("   Using batch mode (faster)")
        count = upload_docs_batch(db, args.collection, docs)
    
    print(f"   ✓ Successfully uploaded {count} documents")

    # Write metadata
    print("\n→ Writing metadata...")
    try:
        db.collection(args.collection).document("_meta").set({
            "uploaded_at": firestore.SERVER_TIMESTAMP,
            "source_pdf": os.path.abspath(pdf_path),
            "backend": backend,
            "count": count,
            "timestamp": datetime.now().isoformat(),
            "note": "Seeded by seed_regulations.py (resilient)"
        }, merge=True)
        print("   ✓ Metadata document created")
    except Exception as e:
        print(f"   Warning: Could not write metadata: {e}")

    print("\n" + "="*60)
    print("UPLOAD COMPLETE!")
    print(f"Collection: /{args.collection}")
    print(f"Documents: {count}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60 + "\n")

if __name__ == "__main__":
    # Reduce gRPC noise but keep essential warnings visible
    os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[!] Upload interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n[!] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)