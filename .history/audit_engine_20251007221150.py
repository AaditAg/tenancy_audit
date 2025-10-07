# audit_engine.py
# ---------------------------------------------------------------------
# Audit engine with:
# - PDF extraction (pdfminer -> PyPDF fallback, optional OCR)
# - Ejari field parsing
# - Simple rule checks (fast)
# - LLM cross-check (Gemini) against Firestore /pdf_articles
# - Firestore "ledger" writer (append-only style)
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

KEYWORDS_FOR_LLM = re.compile(
    r"\b(evict|eviction|terminate|termination|increase|rent|deposit|maintenance|penalty|notice|refund|withhold|arbitrary|discretion)\b",
    re.I
)

def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def to_date(value: str | date | None) -> date:
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
    m2 = INT_RE.search(text.replace(",", "")) if text else None
    if m2:
        return int(m2.group(0))
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
    llm_reason: Optional[str] = None
    matched_regs: Optional[List[str]] = None  # titles/articles we used


@dataclass
class AuditResult:
    verdict: str  # "pass" | "fail"
    failed_count: int
    clause_findings: List[ClauseFinding]
    notes: List[str]
    contract_text: str
    timestamp: str


# =============================== PDF parsing ==============================
TERMS_ANCHOR = re.compile(r"(?:Terms?\s*&\s*Conditions?|^Terms\s*:\s*$)", re.I)

def parse_ejari_text(text: str) -> EjariFields:
    lines = clean_lines(text)
    fields = EjariFields()

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

    for ln in lines:
        if "Contract Period" in ln or ("From" in ln and "To" in ln):
            ds = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if len(ds) >= 1:
                fields.start_date = to_date(ds[0])
            if len(ds) >= 2:
                fields.end_date = to_date(ds[1])

    for ln in lines:
        if re.search(r"Renewal|Renewal Date|End Date", ln, re.I):
            m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if m:
                fields.renewal_date = to_date(m.group(0))
        if "notice" in ln.lower():
            m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", ln)
            if m:
                fields.notice_sent_date = to_date(m.group(0))

    if not fields.renewal_date and fields.end_date:
        fields.renewal_date = fields.end_date

    return fields


def parse_pdf_smart(pdf_bytes: bytes) -> Dict[str, Any]:
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


# ============================= Rule checks ================================
ILLEGAL_PATTERNS: List[Tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?i)evict.*at any time.*without notice"),
     "Eviction without statutory notice is not allowed.", "fail"),
    (re.compile(r"(?i)landlord.*may evict.*for any reason"),
     "Eviction must meet lawful grounds.", "fail"),
    (re.compile(r"(?i)rent.*(?:increase|adjust).*(?:absolute|sole).*discretion"),
     "Rent increases cannot be at landlord’s sole/absolute discretion.", "fail"),
    (re.compile(r"(?i)\bno\s+refunds\b"),
     "Total refund prohibition is often unfair unless narrowly scoped.", "warn"),
]

def scan_clauses_fast(contract_text: str) -> List[ClauseFinding]:
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


# ========================== Firestore & Gemini ============================
_firebase_ready = False
_firestore = None

def firebase_init_from_mapping(cfg: Dict[str, Any]) -> None:
    global _firebase_ready, _firestore
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import credentials, firestore  # type: ignore
        if not firebase_admin._apps:
            cred = credentials.Certificate(cfg) if isinstance(cfg, dict) else credentials.Certificate(json.loads(cfg))  # type: ignore
            firebase_admin.initialize_app(cred)
        _firestore = firestore.client()
        _firebase_ready = True
    except Exception as e:
        _firebase_ready = False
        raise RuntimeError(f"Firebase init error: {e}")

def firebase_init_from_file(path: str = "serviceAccountKeypee.json") -> None:
    with open(path, "r") as f:
        cfg = json.load(f)
    firebase_init_from_mapping(cfg)

def firebase_available() -> bool:
    return _firebase_ready and (_firestore is not None)

def load_all_regulations(collection: str = "pdf_articles", max_chars_per_doc: int = 8000) -> List[Dict[str, str]]:
    """
    Pull every article doc: [{'title','article','text'}...]
    """
    if not firebase_available():
        return []
    from google.cloud.firestore_v1 import Client  # type: ignore
    db: Client = _firestore  # type: ignore
    out: List[Dict[str, str]] = []
    for doc in db.collection(collection).stream():
        d = doc.to_dict() or {}
        txt = (d.get("text") or "")[:max_chars_per_doc]
        out.append({
            "title": str(d.get("title") or ""),
            "article": str(d.get("article") or ""),
            "text": txt
        })
    return out

def _score_article(clause: str, art: Dict[str, str]) -> float:
    # tiny lexical scorer: overlap of keywords (length>=4)
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", clause)}
    regw = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", art.get("text",""))}
    if not words or not regw: 
        return 0.0
    return len(words & regw) / (len(words) ** 0.5)

def llm_cross_check(
    gemini_api_key: str,
    clause: str,
    regs: List[Dict[str, str]],
    top_k: int = 8,
) -> Tuple[str, str, List[str]]:
    """
    Ask Gemini to validate this clause vs the most relevant regulations.
    Returns: (verdict: pass|warn|fail, reason, matched_titles)
    """
    import google.generativeai as genai  # type: ignore

    # select top_k regs
    sorted_regs = sorted(regs, key=lambda r: _score_article(clause, r), reverse=True)[:top_k]
    context_blocks = []
    titles = []
    for r in sorted_regs:
        t = (r["title"] + " — " + r["article"]).strip(" —")
        titles.append(t)
        context_blocks.append(f"### {t}\n{r['text'][:1500]}")
    context = "\n\n".join(context_blocks) if context_blocks else "No regulations loaded."

    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")  # fast & cheap

    system_prompt = (
        "You are a compliance checker for Dubai tenancy contracts. "
        "You MUST base your answer strictly on the provided regulations context. "
        "Output JSON like: {\"verdict\":\"pass|warn|fail\",\"reason\":\"...\"}. "
        "If the clause is illegal (e.g., arbitrary eviction, rent increase outside RERA rules, etc.) mark 'fail'. "
        "If unclear, mark 'warn' and explain."
    )
    user_prompt = f"""
[CLAUSE]
{clause}

[REGULATIONS CONTEXT]
{context}
"""
    try:
        resp = model.generate_content([system_prompt, user_prompt])
        txt = (resp.text or "").strip()
        # Try to extract JSON
        m = re.search(r'\{.*\}', txt, re.S)
        if m:
            js = json.loads(m.group(0))
            v = str(js.get("verdict","warn")).lower().strip()
            if v not in ("pass","warn","fail"):
                v = "warn"
            reason = str(js.get("reason","")).strip() or txt[:4000]
        else:
            # fallback: heuristic
            low = txt.lower()
            if "illegal" in low or "contrary" in low or "not permitted" in low:
                v = "fail"
            elif "unclear" in low or "depends" in low:
                v = "warn"
            else:
                v = "pass"
            reason = txt[:4000]
        return v, reason, titles
    except Exception as e:
        # If LLM fails, do not block: warn
        return "warn", f"LLM check error: {e}", titles


# =============================== Run audit ================================
def run_audit(
    contract_text: str,
    ejari: EjariFields,                     # kept for future use, not used in verdict math now
    gemini_api_key: Optional[str] = None,   # if provided, run LLM checks
    regs_collection: str = "pdf_articles",
) -> AuditResult:
    """
    Final verdict rule:
      - Count only 'fail' clauses after LLM cross-check (if enabled).
      - If failed_count == 0 → PASS; else FAIL.
    """
    notes: List[str] = []
    # Step 1: fast rule scan (labels)
    findings = scan_clauses_fast(contract_text)

    # Step 2: optionally load all regulations & call Gemini on clauses that look relevant
    if gemini_api_key:
        if not firebase_available():
            notes.append("Firestore not initialized: cannot load regulations.")
        else:
            regs = load_all_regulations(collection=regs_collection)
            if not regs:
                notes.append("No regulations found in Firestore collection.")
            else:
                for f in findings:
                    # Only LLM-check clauses that have meaningful text or matched keywords
                    if len(f.text) < 8:
                        continue
                    if f.verdict == "fail" or KEYWORDS_FOR_LLM.search(f.text):
                        verdict, reason, titles = llm_cross_check(gemini_api_key, f.text, regs, top_k=8)
                        # Merge with fast verdict (LLM has the final say):
                        f.verdict = verdict
                        f.llm_reason = reason
                        f.matched_regs = titles

    # Step 3: final verdict = only on fails
    failed_count = sum(1 for f in findings if f.verdict == "fail")
    verdict = "pass" if failed_count == 0 else "fail"

    return AuditResult(
        verdict=verdict,
        failed_count=failed_count,
        clause_findings=findings,
        notes=notes,
        contract_text=contract_text,
        timestamp=now_iso(),
    )


# =========================== Firestore ledger =============================
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def write_ledger(
    tenant: str,
    landlord: str,
    ejari: EjariFields,
    audit: AuditResult,
    pdf_bytes: Optional[bytes] = None,
    collection_root: str = "agreements",
) -> str:
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
        "audit": {
            "verdict": audit.verdict,
            "failed_count": audit.failed_count,
            "clause_findings": [asdict(c) for c in audit.clause_findings],
            "notes": audit.notes,
        },
        "contract_text_hash": _sha256_hex((audit.contract_text or "").encode("utf-8")),
        "pdf_sha256": _sha256_hex(pdf_bytes) if pdf_bytes else None,
        "version": 2,
    }

    agreements = db.collection(collection_root)
    agreements.document(agreement_id).set({"created_at": audit.timestamp}, merge=True)
    ledger_ref = agreements.document(agreement_id).collection("ledger").document()
    ledger_ref.set(doc)

    return agreement_id
