# audit_engine.py
# ----------------------------------------------------------------------
# Core audit engine for Dubai tenancy contracts (Ejari-style parsing).
# Robust PDF extraction, rule checks, RERA index helper, Firestore log.
# ----------------------------------------------------------------------

from __future__ import annotations

import io
import os
import re
import json
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple, Any

from dateutil.parser import parse as dtparse

# ----------------------------- Optional Gemini -----------------------------
_gemini_ok = False
try:
    import google.generativeai as genai  # type: ignore
    _gemini_ok = True
except Exception:
    _gemini_ok = False

# ----------------------------- PDF Extraction -----------------------------
# Prefer pdfminer for layout; fallback to PyPDF (no system deps).
_pdfminer_ok = False
_pypdf_ok = False

try:
    from pdfminer.high_level import extract_text as _pdfminer_extract_text  # type: ignore
    _pdfminer_ok = True
except Exception:
    _pdfminer_ok = False

try:
    import pypdf  # type: ignore

    def _pypdf_extract_text(pdf_bytes: bytes) -> str:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        out = []
        for page in reader.pages:
            out.append(page.extract_text() or "")
        return "\n".join(out)

    _pypdf_ok = True
except Exception:
    _pypdf_ok = False


def _extract_text_any(pdf_bytes: bytes) -> str:
    """Try pdfminer first, then PyPDF. Return empty string if both fail."""
    if _pdfminer_ok:
        try:
            return _pdfminer_extract_text(io.BytesIO(pdf_bytes))
        except Exception:
            pass
    if _pypdf_ok:
        try:
            return _pypdf_extract_text(pdf_bytes)
        except Exception:
            pass
    return ""


# Optional OCR (works locally; not on Streamlit Cloud)
def _ocr_pdf_to_text(pdf_bytes: bytes) -> str:
    """Attempt OCR (requires poppler + tesseract). Return '' if unavailable."""
    try:
        from pdf2image import convert_from_bytes  # type: ignore
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        images = convert_from_bytes(pdf_bytes)
        texts = []
        for im in images:
            if not isinstance(im, Image.Image):
                im = im.convert("RGB")
            texts.append(pytesseract.image_to_string(im))
        return "\n".join(texts)
    except Exception:
        return ""


# ----------------------------- Utilities ---------------------------------
AED_RE = re.compile(r"(?i)\bAED\s*([0-9][\d,\.]*)")
INT_RE = re.compile(r"\b\d+\b")
PCT_RE = re.compile(r"([-+]?\d+(\.\d+)?)\s*%")

EJARI_CONTACT_RE = re.compile(r"(?i)\b(?:Ejari|EJARI)\s*(?:Helpline|Contact|Phone)?[:\s]*([+0-9\s-]{6,})")


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def to_date(value: str | date | None) -> date:
    """Best-effort parser that always returns a date (defaults to a fixed future date if parsing fails)."""
    if value is None:
        return date(2025, 12, 1)
    if isinstance(value, date):
        return value
    try:
        return dtparse(str(value)).date()
    except Exception:
        return date(2025, 12, 1)


def parse_aed(text: str | None, default: int = 0) -> int:
    if not text:
        return default
    m = AED_RE.search(text)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            return int(float(raw))
        except Exception:
            return default
    # fall back to first integer present
    m2 = INT_RE.search(text.replace(",", "")) if text else None
    if m2:
        return int(m2.group(0))
    return default


def parse_pct(text: str | None, default: float = 0.0) -> float:
    if not text:
        return default
    m = PCT_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return default
    return default


def clean_lines(block: str) -> List[str]:
    return [ln.strip() for ln in (block or "").splitlines() if ln.strip()]


def read_articles_texts_from_csv(obj: Any) -> List[str]:
    """Read a CSV and return a list of text snippets to check clauses against.

    Heuristic: choose the object dtype column with the largest total length.
    Accepts a file path, file-like object, or bytes.
    """
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return []

    try:
        df = pd.read_csv(obj)
    except Exception:
        try:
            import io as _io
            if isinstance(obj, (bytes, bytearray)):
                df = pd.read_csv(_io.BytesIO(obj))
            else:
                return []
        except Exception:
            return []

    text_cols: List[str] = [c for c in df.columns if df[c].dtype == object]
    if not text_cols:
        # fallback: stringify all columns per row
        return [" ".join(str(v) for v in row if str(v).strip()) for row in df.astype(str).itertuples(index=False, name=None)]

    # choose the column with highest cumulative length
    best_col = max(text_cols, key=lambda c: df[c].astype(str).str.len().sum())
    texts = [str(x) for x in df[best_col].dropna().astype(str) if str(x).strip()]
    return texts


def _rank_articles_by_overlap(clause: str, articles: List[str], top_k: int = 50) -> List[Tuple[int, str]]:
    """Return list of (global_index, article_text) ranked by token overlap with clause."""
    if not clause or not articles:
        return []
    clause_tokens = set(re.findall(r"[a-zA-Z']{3,}", clause.lower()))
    scored: List[Tuple[int, float]] = []
    for idx, art in enumerate(articles):
        toks = set(re.findall(r"[a-zA-Z']{3,}", str(art).lower()))
        if not toks:
            continue
        overlap = len(clause_tokens & toks) / float(len(clause_tokens) + 1e-9)
        if overlap > 0:
            scored.append((idx, overlap))
    scored.sort(key=lambda x: x[1], reverse=True)
    selected = [(i, articles[i]) for i, _ in scored[:top_k]]
    return selected


def _gemini_check_clause_against_articles(
    clause: str,
    articles: List[str],
    api_key: str,
    model_name: str = "gemini-1.5-flash",
    batch_size: int = 20,
    start_index: int = 0,
) -> Tuple[str, str, List[int]]:
    """Return (verdict, reason, refs) where verdict is 'pass' or 'fail'.

    refs contains 0-based indices into the full `articles` list that the model
    indicates as relevant violations. We enumerate items to make extraction easy.
    """
    if not (_gemini_ok and api_key and articles):
        return "pass", "AI check skipped (missing API, library, or articles).", []

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
    except Exception as e:
        return "pass", f"AI init failed: {e}", []

    system_prompt = (
        "You are a compliance checker for Dubai tenancy clauses. Given a clause and a set of reference legal/guidance texts, "
        "decide if the clause appears non-compliant or problematic. Respond with a single line: 'FAIL - <reason>' if any reference suggests it is disallowed or risky; otherwise 'PASS - OK'."
    )

    # Preselect relevant articles to reduce noise
    ranked = _rank_articles_by_overlap(clause, articles, top_k=200)
    if not ranked:
        ranked = list(enumerate(articles))

    for i in range(0, len(ranked), batch_size):
        chunk = ranked[i : i + batch_size]
        numbered = []
        for (gidx, a) in chunk:
            idx = start_index + gidx
            numbered.append(f"[{idx}] {str(a).strip()[:2000]}")
        joined = "\n" + "\n".join(numbered)
        prompt = (
            f"{system_prompt}\n\nClause:\n{clause}\n\nReference texts (indexed):{joined}\n\n"
            "Return strict JSON only with keys: verdict ('FAIL'|'PASS'), reason (string), refs (array of integers referencing the indices above)."
        )
        try:
            resp = model.generate_content(prompt)
            txt = (getattr(resp, "text", None) or "").strip()
        except Exception as e:
            return "pass", f"AI request error: {e}", []

        # Parse JSON strictly; fallback to heuristic
        verdict = "PASS"
        reason = ""
        refs: List[int] = []
        try:
            data = json.loads(txt)
            verdict = str(data.get("verdict", "PASS")).upper()
            reason = str(data.get("reason", "")).strip() or "Flagged by AI layer"
            refs = [int(x) for x in data.get("refs", []) if isinstance(x, int)]
        except Exception:
            # Heuristic fallback
            head = txt.splitlines()[0].strip().upper() if txt else ""
            if head.startswith("FAIL") or head == "FAIL":
                verdict = "FAIL"
                reason = txt
                m = re.findall(r"\[(\d+)\]", txt)
                refs = [int(x) for x in m]

        if verdict == "FAIL":
            return "fail", reason, refs

    return "pass", "No conflicts detected by AI layer", []


# ----------------------------- Data Models --------------------------------
@dataclass
class EjariFields:
    city: str = "Dubai"
    community: str = ""
    property_type: str = "apartment"
    bedrooms: int = 1
    security_deposit_aed: int = 0
    current_annual_rent_aed: int = 0
    proposed_new_rent_aed: int = 0
    furnishing: str = "unfurnished"
    renewal_date: Optional[date] = None
    notice_sent_date: Optional[date] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    ejari_contact: Optional[str] = None


@dataclass
class ClauseFinding:
    clause_no: int
    text: str
    verdict: str  # "pass" | "warn" | "fail"
    issues: str = ""


@dataclass
class AuditResult:
    verdict: str  # "pass" | "fail"
    issues: List[str]
    rera_max_increase_pct: float
    proposed_increase_pct: float
    clause_findings: List[ClauseFinding]
    text_findings: List[str]
    ejari: EjariFields
    notes: List[str]
    contract_text: str
    timestamp: str


# ----------------------------- PDF Parsing --------------------------------
EJARI_KEYS = {
    "Property Usage": "property_usage",
    "Owner Name": "owner_name",
    "Landlord Name": "landlord_name",
    "Tenant Name": "tenant_name",
    "Location": "community",
    "Property Type": "property_type",
    "Contract Period": "period",
    "Annual Rent": "current_annual_rent_aed",
    "Security Deposit Amount": "security_deposit_aed",
    "Bedrooms": "bedrooms",
}

TERMS_ANCHOR = re.compile(r"(?:Terms?\s*&\s*Conditions?|^Terms\s*:\s*$)", re.IGNORECASE)


def parse_ejari_text(text: str) -> EjariFields:
    """
    Very forgiving parser that tries to map common Ejari/tenancy PDF lines to fields.
    Works for the sample PDFs and many text-based templates.
    """
    lines = clean_lines(text)
    fields = EjariFields()

    # 1) Shallow scans for obvious labels
    for ln in lines:
        if "Annual Rent" in ln:
            fields.current_annual_rent_aed = parse_aed(ln, fields.current_annual_rent_aed)
        if "Security Deposit" in ln:
            fields.security_deposit_aed = parse_aed(ln, fields.security_deposit_aed)
        if "Bedrooms" in ln or "BR" in ln:
            m = INT_RE.search(ln)
            if m:
                fields.bedrooms = int(m.group(0))
        if "Property Type" in ln:
            if "villa" in ln.lower():
                fields.property_type = "villa"
            elif "townhouse" in ln.lower():
                fields.property_type = "townhouse"
            else:
                fields.property_type = "apartment"
        if "Location" in ln or "Area:" in ln:
            # take everything after colon
            parts = re.split(r"[:\-–]", ln, maxsplit=1)
            if len(parts) == 2 and len(parts[1].strip()) > 1:
                fields.community = parts[1].strip()
        if "Ejari" in ln and ("Contact" in ln or "Helpline" in ln or re.search(r"\+?\d", ln)):
            m = EJARI_CONTACT_RE.search(ln)
            if m:
                fields.ejari_contact = re.sub(r"\s+", " ", m.group(1)).strip()

    # 2) Dates: try to infer period lines
    for ln in lines:
        if "Contract Period" in ln or "From" in ln and "To" in ln:
            # extract two dates
            ds = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if len(ds) >= 1:
                fields.start_date = to_date(ds[0])
            if len(ds) >= 2:
                fields.end_date = to_date(ds[1])

    # 3) RERA-ish clauses sometimes include renewal or notice hints
    for ln in lines:
        if re.search(r"Renewal|Renewal Date|End Date", ln, re.I):
            m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if m:
                fields.renewal_date = to_date(m.group(0))
        if "notice" in ln.lower() and re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln):
            fields.notice_sent_date = to_date(re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln).group(0))

    # 4) Proposed new rent (if present in free text)
    for ln in lines[:40]:  # header region is enough
        if "Proposed" in ln and "Rent" in ln:
            fields.proposed_new_rent_aed = parse_aed(ln, fields.proposed_new_rent_aed)

    # defaults
    if not fields.renewal_date and fields.end_date:
        fields.renewal_date = fields.end_date

    return fields


def parse_pdf_smart(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Extract text from PDF using pdfminer or PyPDF; OCR fallback when available.
    Also attempts to extract Ejari-like fields for form prefill.
    """
    notes: List[str] = []
    text = ""
    try:
        text = _extract_text_any(pdf_bytes)
        if not text.strip():
            notes.append("No extractable text (may be a scanned PDF).")
    except Exception as e:
        notes.append(f"PDF text extraction error: {e}")
        text = ""

    # OCR fallback
    ocr_used = False
    if len(text.strip()) < 120:
        ocr = _ocr_pdf_to_text(pdf_bytes)
        if ocr and len(ocr.strip()) > len(text.strip()):
            text = ocr
            ocr_used = True
            notes.append("OCR fallback used (image PDF).")
        else:
            notes.append("OCR not available or yielded too little text.")

    ejari = parse_ejari_text(text)
    return {"text": text, "ejari": ejari, "ocr_used": ocr_used, "notes": notes}


# ----------------------------- RERA Helpers -------------------------------
def compute_proposed_increase_pct(current_aed: int, proposed_aed: int) -> float:
    if current_aed <= 0:
        return 0.0
    return round(((proposed_aed - current_aed) / float(current_aed)) * 100.0, 2)


def rera_slabs_max_increase(current_vs_index_gap_pct: float) -> float:
    """
    Implements Decree 43/2013 slabs (commonly summarized):
      - If current rent is up to 10% below market index → 0%
      - >10% to 20% below → up to 5%
      - >20% to 30% below → up to 10%
      - >30% to 40% below → up to 15%
      - >40% below → up to 20%
    """
    gap = max(0.0, current_vs_index_gap_pct)
    if gap <= 10:
        return 0.0
    if gap <= 20:
        return 5.0
    if gap <= 30:
        return 10.0
    if gap <= 40:
        return 15.0
    return 20.0


def estimate_gap_vs_index(current_aed: int, rera_index_aed: Optional[int]) -> float:
    """
    Rough gap % = how far current is below index (positive means below index).
    If no index is provided, return 0 to keep audit conservative.
    """
    if not rera_index_aed or rera_index_aed <= 0 or current_aed <= 0:
        return 0.0
    if current_aed >= rera_index_aed:
        return 0.0
    diff = rera_index_aed - current_aed
    return round((diff / rera_index_aed) * 100.0, 2)


# ----------------------------- Clause Rules --------------------------------
ILLEGAL_PATTERNS = [
    # Unlawful eviction / absolute discretion
    (re.compile(r"evict.*at any time.*without notice", re.IGNORECASE), "Eviction without statutory notice is not allowed (Law 33/2008).", "fail"),
    (re.compile(r"evict.*without\s+notice", re.IGNORECASE), "Eviction without statutory notice is not allowed (Law 33/2008).", "fail"),
    (re.compile(r"(?i)landlord.*may evict.*for any reason"), "Eviction must meet lawful grounds under Dubai tenancy laws.", "fail"),
    # Absolute/sole discretion on rent increases (various phrasings and word orders)
    (re.compile(r"rent.*(increase|adjust).*(landlord(?:'?s)?|landlords?).*(absolute|sole).*(discretion)(?:s)?", re.IGNORECASE),
     "Rent increases cannot be at landlord's sole/absolute discretion; must comply with Decree 43/2013.", "fail"),
    (re.compile(r"(landlord(?:'?s)?|landlords?).*(increase|adjust).*rent.*(absolute|sole).*(discretion)(?:s)?", re.IGNORECASE),
     "Rent increases cannot be at landlord's sole/absolute discretion; must comply with Decree 43/2013.", "fail"),
    # No refunds / blanket waivers (often unfair)
    (re.compile(r"(?i)no\s+refunds"), "Total refund prohibition is typically unfair/unlawful unless specific circumstances.", "warn"),
    # Tenant pays penalties vaguely specified
    (re.compile(r"(?i)penalt(y|ies).*(tenant)"), "Penalty clauses must be reasonable, transparent, and specific.", "warn"),
]

NOTICE_MIN_DAYS = 90  # 90-day notice before renewal for rent changes (practice reflected in RERA comms)


def scan_clauses(contract_text: str) -> List[ClauseFinding]:
    """Run rule-based scans over the free text for clearly illegal/iffy clauses."""
    lines = [ln.strip() for ln in clean_lines(contract_text)]
    findings: List[ClauseFinding] = []
    cnum = 1
    for ln in lines:
        verdict = "pass"
        issues = ""
        lowered = ln.lower()
        for rx, msg, sev in ILLEGAL_PATTERNS:
            if rx.search(lowered):
                verdict = sev
                issues = msg
                break
        findings.append(ClauseFinding(clause_no=cnum, text=ln, verdict=verdict, issues=issues))
        cnum += 1
    return findings


def check_notice_window(renewal: Optional[date], notice_sent: Optional[date]) -> Tuple[str, str]:
    """Return ('pass'|'fail'|'warn', message) for the 90-day notice rule of thumb."""
    if not renewal or not notice_sent:
        return "warn", "Missing renewal or notice date; cannot verify 90-day notice."
    days = (renewal - notice_sent).days
    if days < NOTICE_MIN_DAYS:
        return "fail", f"Notice before renewal appears to be {days} days (< {NOTICE_MIN_DAYS})."
    return "pass", f"Notice sent {days} days before renewal."


# ----------------------------- Main Audit ----------------------------------
def run_audit(
    contract_text: str,
    ejari: EjariFields,
    rera_index_aed: Optional[int] = None,
    use_ai: bool = False,
    ai_api_key: Optional[str] = None,
    ai_articles_csv_path: Optional[str] = None,
    ai_articles_memory: Optional[List[str]] = None,
) -> AuditResult:
    """
    Evaluate the contract text and Ejari fields for compliance signals.
    """
    # Clause scans (regex layer)
    clause_findings = scan_clauses(contract_text)

    # Rent math
    proposed_pct = compute_proposed_increase_pct(ejari.current_annual_rent_aed, ejari.proposed_new_rent_aed or ejari.current_annual_rent_aed)
    gap_pct = estimate_gap_vs_index(ejari.current_annual_rent_aed, rera_index_aed)
    max_allowed_pct = rera_slabs_max_increase(gap_pct)

    issues: List[str] = []
    text_findings: List[str] = []

    # Proposed increase vs allowed
    if proposed_pct > max_allowed_pct + 1e-6:
        issues.append(
            f"Proposed increase {proposed_pct:.1f}% exceeds max allowed {max_allowed_pct:.1f}% (Decree 43/2013 slabs)."
        )

    # Notice window
    nv, nmsg = check_notice_window(ejari.renewal_date, ejari.notice_sent_date)
    if nv != "pass":
        issues.append(nmsg)
    text_findings.append(nmsg)

    # Optional AI layer: check each clause against provided articles
    if use_ai and (ai_api_key or os.environ.get("GEMINI_API_KEY")):
        api_key = ai_api_key or os.environ.get("GEMINI_API_KEY", "")
        articles: List[str] = []
        if ai_articles_memory:
            articles = list(ai_articles_memory)
        elif ai_articles_csv_path and os.path.exists(ai_articles_csv_path):
            articles = read_articles_texts_from_csv(ai_articles_csv_path)

        ai_any_fail = False
        ai_refs_by_clause: Dict[int, List[int]] = {}
        for idx, cf in enumerate(clause_findings):
            verdict, reason, refs = _gemini_check_clause_against_articles(cf.text, articles, api_key, start_index=0)
            if verdict == "fail":
                ai_any_fail = True
                if refs:
                    ai_refs_by_clause[idx] = refs
                # annotate this clause if regex didn't already fail
                if cf.verdict == "pass":
                    clause_findings[idx] = ClauseFinding(
                        clause_no=cf.clause_no,
                        text=cf.text,
                        verdict="fail",
                        issues=(cf.issues + "; " if cf.issues else "") + f"AI: {reason}" + (f" | Refs: {refs}" if refs else ""),
                    )
                else:
                    # append AI reason
                    clause_findings[idx].issues = (clause_findings[idx].issues + "; " if clause_findings[idx].issues else "") + f"AI: {reason}" + (f" | Refs: {refs}" if refs else "")
        if ai_any_fail:
            issues.append("AI layer flagged one or more clauses as non-compliant.")

    # Aggregate clause findings → any "fail" makes overall fail
    any_fail = any(cf.verdict == "fail" for cf in clause_findings)
    if any_fail:
        issues.append("One or more clauses are non-compliant (see table).")

    # Verdict strictly based on clause failures per requirement
    verdict = "fail" if any_fail else "pass"

    return AuditResult(
        verdict=verdict,
        issues=issues,
        rera_max_increase_pct=max_allowed_pct,
        proposed_increase_pct=proposed_pct,
        clause_findings=clause_findings,
        text_findings=text_findings,
        ejari=ejari,
        notes=[],
        contract_text=contract_text,
        timestamp=now_iso(),
    )


# ----------------------------- Firestore (Admin) ---------------------------
_firebase_ready = False
_firestore = None  # lazy


def firebase_init_from_mapping(cfg: Dict[str, Any]) -> None:
    """
    Initialize Firebase Admin from a dict (Streamlit `st.secrets["firebase"]` is perfect).
    Safe to call multiple times.
    """
    global _firebase_ready, _firestore
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import credentials, firestore  # type: ignore

        if not firebase_admin._apps:
            cred = credentials.Certificate(cfg)  # type: ignore
            firebase_admin.initialize_app(cred)
        _firestore = firestore.client()
        _firebase_ready = True
    except Exception as e:
        _firebase_ready = False
        raise RuntimeError(f"Firebase init error: {e}")


def firebase_init_from_json_string(sa_json: str) -> None:
    firebase_init_from_mapping(json.loads(sa_json))


def firebase_init_from_file(path: str) -> None:
    with open(path, "r") as f:
        data = json.load(f)
    firebase_init_from_mapping(data)


def firebase_init_from_bytes(b: bytes) -> None:
    firebase_init_from_mapping(json.loads(b.decode("utf-8")))


def firebase_available() -> bool:
    return _firebase_ready and (_firestore is not None)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_ledger(
    tenant: str,
    landlord: str,
    ejari: EjariFields,
    audit: AuditResult,
    pdf_bytes: Optional[bytes] = None,
    rera_index_aed: Optional[int] = None,
) -> str:
    """
    Append an immutable-style audit record into Firestore:
      /agreements/{agreement_id}/ledger/{auto_id}
    agreement_id is deterministic: SHA256(contract_text + landlord + tenant).
    """
    if not firebase_available():
        raise RuntimeError("Firestore not initialized")

    from google.cloud.firestore_v1 import Client  # type: ignore

    db: Client = _firestore  # type: ignore

    # Deterministic agreement id:
    seed = (audit.contract_text or "").encode("utf-8") + (landlord or "").encode("utf-8") + (tenant or "").encode("utf-8")
    agreement_id = _sha256_hex(seed)[:32]

    doc = {
        "timestamp": audit.timestamp,
        "tenant": tenant,
        "landlord": landlord,
        "ejari": asdict(ejari),
        "rera_index_aed": rera_index_aed,
        "audit": {
            "verdict": audit.verdict,
            "issues": audit.issues,
            "proposed_increase_pct": audit.proposed_increase_pct,
            "rera_max_increase_pct": audit.rera_max_increase_pct,
            "text_findings": audit.text_findings,
            "clause_findings": [asdict(c) for c in audit.clause_findings],
        },
        "contract_text_hash": _sha256_hex((audit.contract_text or "").encode("utf-8")),
        "pdf_sha256": _sha256_hex(pdf_bytes) if pdf_bytes else None,
        "version": 1,
    }

    # Write:
    agreements = db.collection("agreements")
    agreements.document(agreement_id).set({"created_at": audit.timestamp}, merge=True)
    ledger_ref = agreements.document(agreement_id).collection("ledger").document()
    ledger_ref.set(doc)

    return agreement_id