# audit_engine.py
# ---------------------------------------------------------------------
# Core audit engine for Dubai tenancy contracts (Ejari-like parsing).
# - Robust PDF extraction (pdfminer -> PyPDF fallback; optional OCR)
# - RERA rent-cap slabs (Decree 43/2013)
# - Rule checks (Law 26/2007, Law 33/2008, Decree 43/2013)
# - Append-only style Firestore ledger (Admin SDK)
# - Small helpers used by app.py (to_date, parsers, etc.)
# ---------------------------------------------------------------------

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

# ============================ PDF extraction =============================

# Prefer pdfminer for better layout/text; fallback to PyPDF (no system deps)
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
        out: List[str] = []
        for page in reader.pages:
            out.append(page.extract_text() or "")
        return "\n".join(out)

    _pypdf_ok = True
except Exception:
    _pypdf_ok = False


def _extract_text_any(pdf_bytes: bytes) -> str:
    """Try pdfminer first; fallback to PyPDF; return empty string if both fail."""
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


def _ocr_pdf_to_text(pdf_bytes: bytes) -> str:
    """
    Optional OCR fallback (works only when tesseract + poppler are installed).
    Streamlit Cloud doesn't provide these system packages.
    """
    try:
        from pdf2image import convert_from_bytes  # type: ignore
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        images = convert_from_bytes(pdf_bytes)
        texts: List[str] = []
        for im in images:
            if not isinstance(im, Image.Image):
                im = im.convert("RGB")
            texts.append(pytesseract.image_to_string(im))
        return "\n".join(texts)
    except Exception:
        return ""


# =============================== Utilities ===============================

AED_RE = re.compile(r"(?i)\bAED\s*([0-9][\d,\.]*)")
INT_RE = re.compile(r"\b\d+\b")
PCT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
EJARI_CONTACT_RE = re.compile(r"(?i)\b(?:Ejari|EJARI)\s*(?:Helpline|Contact|Phone)?[:\s]*([+0-9\s-]{6,})")


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def to_date(value: str | date | None) -> date:
    """
    Best-effort date parser that always returns a date.
    Used throughout app.py to keep Streamlit widgets happy.
    """
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
    # fallback: first integer in the line
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


# ============================ Data structures =============================

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
    issues: List[str]                 # blocking issues only
    rera_max_increase_pct: float
    proposed_increase_pct: float
    clause_findings: List[ClauseFinding]
    text_findings: List[str]          # notes/warnings (non-blocking)
    ejari: EjariFields
    notes: List[str]
    contract_text: str
    timestamp: str


# =============================== PDF parsing ==============================

# (Used if you want to split header vs "Terms & Conditions")
TERMS_ANCHOR = re.compile(r"(?:Terms?\s*&\s*Conditions?|^Terms\s*:\s*$)", re.I)


def parse_ejari_text(text: str) -> EjariFields:
    """
    Forgiving parser that maps common Ejari/tenancy PDF lines to fields.
    Works for text-based PDFs and the provided sample templates.
    """
    lines = clean_lines(text)
    fields = EjariFields()

    # pass 1: obvious labels and numbers
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
            ll = ln.lower()
            if "villa" in ll:
                fields.property_type = "villa"
            elif "townhouse" in ll:
                fields.property_type = "townhouse"
            else:
                fields.property_type = "apartment"
        if "Location" in ln or ln.lower().startswith("area:"):
            parts = re.split(r"[:\-–]", ln, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                fields.community = parts[1].strip()
        if "Ejari" in ln and ("Contact" in ln or "Helpline" in ln or re.search(r"\+?\d", ln)):
            m = EJARI_CONTACT_RE.search(ln)
            if m:
                fields.ejari_contact = re.sub(r"\s+", " ", m.group(1)).strip()

    # pass 2: contract period
    for ln in lines:
        if "Contract Period" in ln or ("From" in ln and "To" in ln):
            ds = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if len(ds) >= 1:
                fields.start_date = to_date(ds[0])
            if len(ds) >= 2:
                fields.end_date = to_date(ds[1])

    # pass 3: renewal / notice hints
    for ln in lines:
        if re.search(r"Renewal|Renewal Date|End Date", ln, re.I):
            m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if m:
                fields.renewal_date = to_date(m.group(0))
        if "notice" in ln.lower():
            m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if m:
                fields.notice_sent_date = to_date(m.group(0))

    # proposed rent if the PDF mentions it
    for ln in lines[:40]:
        if "Proposed" in ln and "Rent" in ln:
            fields.proposed_new_rent_aed = parse_aed(ln, fields.proposed_new_rent_aed)

    if not fields.renewal_date and fields.end_date:
        fields.renewal_date = fields.end_date

    return fields


def parse_pdf_smart(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Extract text with pdfminer/PyPDF; if too little text, try OCR (if available).
    Returns: {"text": str, "ejari": EjariFields, "ocr_used": bool, "notes": [str]}
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

    ocr_used = False
    if len(text.strip()) < 120:
        ocr = _ocr_pdf_to_text(pdf_bytes)
        if ocr and len(ocr.strip()) > len(text.strip()):
            text = ocr
            ocr_used = True
            notes.append("OCR fallback used (image PDF).")
        else:
            notes.append("OCR not available or produced too little text.")

    ejari = parse_ejari_text(text)
    return {"text": text, "ejari": ejari, "ocr_used": ocr_used, "notes": notes}


# =========================== RERA helper logic ============================

def compute_proposed_increase_pct(current_aed: int, proposed_aed: int) -> float:
    if current_aed <= 0:
        return 0.0
    return round(((proposed_aed - current_aed) / float(current_aed)) * 100.0, 2)


def estimate_gap_vs_index(current_aed: int, rera_index_aed: Optional[int]) -> float:
    """
    Rough gap % = how far current is below the index (positive means below index).
    If no index is provided, return 0 to keep audit conservative.
    """
    if not rera_index_aed or rera_index_aed <= 0 or current_aed <= 0:
        return 0.0
    if current_aed >= rera_index_aed:
        return 0.0
    diff = rera_index_aed - current_aed
    return round((diff / rera_index_aed) * 100.0, 2)


def rera_slabs_max_increase(current_vs_index_gap_pct: float) -> float:
    """
    Implements Decree 43/2013 slabs (publicly summarized thresholds):
      - If current rent is up to 10% below index → 0%
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


# ============================= Rule checks ================================

# Clear illegal/iffy patterns (case-insensitive)
ILLEGAL_PATTERNS: List[Tuple[re.Pattern[str], str, str]] = [
    # Eviction without statutory notice; arbitrary eviction
    (re.compile(r"(?i)evict.*at any time.*without notice"),
     "Eviction without statutory notice is not allowed (Law 33/2008).", "fail"),
    (re.compile(r"(?i)landlord.*may evict.*for any reason"),
     "Eviction must meet lawful grounds under Dubai tenancy laws.", "fail"),

    # Absolute/sole discretion rent increases
    (re.compile(r"(?i)rent.*(?:increase|adjust).*(?:landlord.?s|landlord’s|landlords).*(?:absolute|sole).*discretion"),
     "Rent increases cannot be at landlord's sole/absolute discretion; must comply with Decree 43/2013.", "fail"),

    # Blanket no-refund (often unfair/unenforceable unless specific)
    (re.compile(r"(?i)\bno\s+refunds\b"),
     "Total refund prohibition is often unfair unless narrowly scoped.", "warn"),

    # Vague penalty loading on tenant
    (re.compile(r"(?i)penalt(?:y|ies).*(tenant)"),
     "Penalty clauses must be specific and reasonable, not blanket.", "warn"),
]

NOTICE_MIN_DAYS = 90  # common RERA practice around renewal notifications


def scan_clauses(contract_text: str) -> List[ClauseFinding]:
    """Run rule-based scans line by line."""
    lines = clean_lines(contract_text)
    findings: List[ClauseFinding] = []
    for i, ln in enumerate(lines, start=1):
        verdict = "pass"
        issues = ""
        low = ln.lower()
        for rx, msg, sev in ILLEGAL_PATTERNS:
            if rx.search(low):
                verdict = sev
                issues = msg
                break
        findings.append(ClauseFinding(clause_no=i, text=ln, verdict=verdict, issues=issues))
    return findings


def check_notice_window(renewal: Optional[date], notice_sent: Optional[date]) -> Tuple[str, str]:
    """Return ('pass'|'fail'|'warn', message) for the 90-day notice rule-of-thumb."""
    if not renewal or not notice_sent:
        return "warn", "Missing renewal or notice date; cannot verify the 90-day notice window."
    days = (renewal - notice_sent).days
    if days < NOTICE_MIN_DAYS:
        return "fail", f"Notice appears to be {days} days (< {NOTICE_MIN_DAYS})."
    return "pass", f"Notice sent {days} days before renewal."


# =============================== Run audit ================================

def run_audit(
    contract_text: str,
    ejari: EjariFields,
    rera_index_aed: Optional[int] = None,
) -> AuditResult:
    """
    Evaluate the contract text and Ejari fields for compliance signals.
    Returns an AuditResult where:
      - verdict == 'fail' only when blocking problems exist
      - warnings are surfaced in text_findings, not in issues
    """
    clause_findings = scan_clauses(contract_text)

    # Rent math
    proposed_pct = compute_proposed_increase_pct(
        ejari.current_annual_rent_aed,
        ejari.proposed_new_rent_aed or ejari.current_annual_rent_aed,
    )
    gap_pct = estimate_gap_vs_index(ejari.current_annual_rent_aed, rera_index_aed)
    max_allowed_pct = rera_slabs_max_increase(gap_pct)

    issues: List[str] = []     # blocking
    warnings: List[str] = []   # non-blocking
    text_findings: List[str] = []

    # Proposed increase vs allowed
    if proposed_pct > max_allowed_pct + 1e-6:
        issues.append(
            f"Proposed increase {proposed_pct:.1f}% exceeds max allowed {max_allowed_pct:.1f}% (Decree 43/2013 slabs)."
        )

    # 90-day notice
    nv, nmsg = check_notice_window(ejari.renewal_date, ejari.notice_sent_date)
    if nv == "fail":
        issues.append(nmsg)
    else:
        warnings.append(nmsg)
    text_findings.append(nmsg)

    # Clause failures
    any_fail = any(cf.verdict == "fail" for cf in clause_findings)
    if any_fail:
        issues.append("One or more clauses are non-compliant (see table).")

    verdict = "fail" if (any_fail or nv == "fail" or any("exceeds" in i for i in issues)) else "pass"

    return AuditResult(
        verdict=verdict,
        issues=issues,
        rera_max_increase_pct=max_allowed_pct,
        proposed_increase_pct=proposed_pct,
        clause_findings=clause_findings,
        text_findings=text_findings + warnings,  # show warnings, but they don't force FAIL
        ejari=ejari,
        notes=[],
        contract_text=contract_text,
        timestamp=now_iso(),
    )


# =========================== Firestore (Admin) ============================

_firebase_ready = False
_firestore = None  # set by init


def firebase_init_from_mapping(cfg: Dict[str, Any]) -> None:
    """
    Initialize Firebase Admin from a mapping (perfect for st.secrets['firebase']).
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
    Append an immutable-style audit record to Firestore:
      /agreements/{agreement_id}/ledger/{auto_id}
    agreement_id is a deterministic hash of (contract_text + landlord + tenant).
    """
    if not firebase_available():
        raise RuntimeError("Firestore not initialized")

    from google.cloud.firestore_v1 import Client  # type: ignore

    db: Client = _firestore  # type: ignore

    seed = (
        (audit.contract_text or "").encode("utf-8")
        + (landlord or "").encode("utf-8")
        + (tenant or "").encode("utf-8")
    )
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

    agreements = db.collection("agreements")
    agreements.document(agreement_id).set({"created_at": audit.timestamp}, merge=True)
    ledger_ref = agreements.document(agreement_id).collection("ledger").document()
    ledger_ref.set(doc)

    return agreement_id
