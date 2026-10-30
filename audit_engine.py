# audit_engine.py
# ----------------------------------------------------------------------
# Core audit engine for UAE tenancy contracts (Ejari-style parsing).
#
# Pipeline:  PDF/text -> field extraction -> clause segmentation
#            -> deterministic rules (rules.py, grounded in legal_kb.py)
#            -> numeric checks (rent cap, notice, deposit, dates)
#            -> optional open-model review (llm_client.py) with retrieval
#            -> score + verdict + Firestore ledger.
# ----------------------------------------------------------------------

from __future__ import annotations

import io
import json
import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from dateutil.parser import parse as dtparse

import legal_kb
import rules as rules_mod
from textnorm import detect_language, has_arabic, normalize_ar, to_ascii_digits
from llm_client import LLMClient, LLMError, extract_json

# ----------------------------- PDF Extraction -----------------------------
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
        return "\n".join((page.extract_text() or "") for page in reader.pages)

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


_TESSDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")
OCR_LANGS = "eng+ara+hin+mal+tam+urd"


def ocr_available() -> bool:
    try:
        import pytesseract  # type: ignore
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_config() -> str:
    return f'--tessdata-dir "{_TESSDATA}"' if os.path.isdir(_TESSDATA) else ""


def _ocr_langs() -> str:
    """Use every language pack that is actually installed (a missing pack makes tesseract fail)."""
    if not os.path.isdir(_TESSDATA):
        return "eng"
    have = {f[:-12] for f in os.listdir(_TESSDATA) if f.endswith(".traineddata")}
    langs = [l for l in OCR_LANGS.split("+") if l in have]
    return "+".join(langs) or "eng"


def ocr_images_to_text(images: List[Any]) -> str:
    """OCR a list of PIL images. Returns '' if tesseract/pytesseract is unavailable."""
    try:
        import pytesseract  # type: ignore
        return "\n".join(pytesseract.image_to_string(im, lang=_ocr_langs(), config=_ocr_config()) for im in images)
    except Exception:
        return ""


def ocr_image_bytes(image_bytes_list: List[bytes]) -> str:
    """OCR photos of a contract (phone camera / gallery). Handles phone rotation and big images."""
    try:
        from PIL import Image, ImageOps  # type: ignore
        imgs = []
        for b in image_bytes_list:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(b))).convert("L")
            if max(im.size) > 3000:
                im.thumbnail((3000, 3000))
            imgs.append(im)
        return ocr_images_to_text(imgs)
    except Exception:
        return ""


def _ocr_pdf_to_text(pdf_bytes: bytes) -> str:
    """OCR a scanned PDF (needs poppler + tesseract). Return '' if unavailable."""
    try:
        from pdf2image import convert_from_bytes  # type: ignore
        return ocr_images_to_text(convert_from_bytes(pdf_bytes, dpi=200))
    except Exception:
        return ""


# ----------------------------- Utilities ---------------------------------
AED_RE = re.compile(r"(?i)(?:\bAED|\bDhs?\.?|\bد\.إ)\s*([0-9][\d,]*(?:\.\d+)?)|([0-9][\d,]*(?:\.\d+)?)\s*(?:AED|Dhs?\b)")
INT_RE = re.compile(r"\b\d+\b")
EJARI_CONTACT_RE = re.compile(r"(?i)\b(?:Ejari)\s*(?:Helpline|Contact|Phone)?[:\s]*(\+?[0-9][0-9\s-]{5,})")
DATE_PATTERNS = [
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}",
    r"\d{1,2}[-/.]\d{1,2}[-/.]\d{4}",
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}",
]
DATE_RE = re.compile("|".join(f"(?:{p})" for p in DATE_PATTERNS), re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def to_date(value: Any) -> Optional[date]:
    """Best-effort parser. Returns None (never an invented date) when it can't parse."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        # ISO first; otherwise day-first, which is the norm in UAE contracts
        if re.match(r"\d{4}[-/.]\d", s):
            return dtparse(s).date()
        return dtparse(s, dayfirst=True).date()
    except Exception:
        return None


def parse_aed(text: Optional[str], default: int = 0) -> int:
    if not text:
        return default
    m = AED_RE.search(text)
    if m:
        raw = (m.group(1) or m.group(2)).replace(",", "")
        try:
            return int(float(raw))
        except Exception:
            return default
    nums = [int(n.replace(",", "")) for n in re.findall(r"\b\d[\d,]{2,}\b", text)]
    return max(nums) if nums else default


def clean_lines(block: str) -> List[str]:
    return [ln.strip() for ln in (block or "").splitlines() if ln.strip()]


def fmt_aed(n: Optional[float]) -> str:
    return "-" if n is None else f"AED {int(round(n)):,}"


# ----------------------------- Data Models --------------------------------
@dataclass
class EjariFields:
    jurisdiction: str = "dubai"            # key of legal_kb.JURISDICTIONS
    city: str = "Dubai"
    community: str = ""
    property_type: str = "apartment"
    bedrooms: int = 1
    tenant_name: str = ""
    landlord_name: str = ""
    ejari_number: str = ""
    security_deposit_aed: int = 0
    current_annual_rent_aed: int = 0
    proposed_new_rent_aed: int = 0
    furnishing: str = "unfurnished"
    usage: str = "residential"             # residential | commercial
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    renewal_date: Optional[date] = None
    notice_sent_date: Optional[date] = None
    ejari_contact: Optional[str] = None
    contract_language: str = "en"


@dataclass
class ClauseFinding:
    clause_no: int
    text: str
    verdict: str = "pass"                  # pass | warn | fail
    issues: str = ""                       # one-line summary (kept for compat / export)
    category: str = ""
    title: str = ""
    explanation: str = ""
    suggestion: str = ""
    laws: List[str] = field(default_factory=list)   # legal_kb ids (or ART-n for CSV refs)
    source: str = "rule"                   # rule | ai | rule+ai
    rule_ids: List[str] = field(default_factory=list)   # which rules fired (for translated titles)


@dataclass
class Check:
    id: str
    name: str
    status: str                            # pass | warn | fail | unknown
    message: str
    laws: List[str] = field(default_factory=list)
    args: Dict[str, str] = field(default_factory=dict)   # values for translated message templates


@dataclass
class AuditResult:
    verdict: str                           # pass | review | fail
    score: int
    issues: List[str]
    rera_max_increase_pct: Optional[float]
    proposed_increase_pct: float
    clause_findings: List[ClauseFinding]
    checks: List[Check]
    missing_items: List[str]
    ejari: EjariFields
    jurisdiction: str
    ai_used: bool
    ai_model: str
    ai_summary: str
    ai_error: str
    notes: List[str]
    contract_text: str
    timestamp: str
    kb_reviewed: str = legal_kb.LAST_REVIEWED

    @property
    def text_findings(self) -> List[str]:   # backwards compat with the old UI/ledger
        return [c.message for c in self.checks]


# ----------------------------- PDF Parsing --------------------------------
def _first_date_in(line: str) -> Optional[date]:
    m = DATE_RE.search(line)
    return to_date(m.group(0)) if m else None


def _after_label(line: str) -> str:
    parts = re.split(r"[:\-–]", line, maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


_EMIRATE_WORDS = [
    ("dubai", r"dubai|ejari|rera|دبي|ايجاري"),
    ("abu_dhabi", r"abu\s*dhabi|tawtheeq|ابوظبي|ابو\s*ظبي|توثيق"),
    ("sharjah", r"sharjah|الشارقة"),
    ("ajman", r"ajman|عجمان"),
    ("ras_al_khaimah", r"ras\s*al\s*khaimah|\brak\b|راس\s*الخيمة"),
    ("fujairah", r"fujairah|الفجيرة"),
    ("umm_al_quwain", r"umm\s*al\s*quwain|\buaq\b|ام\s*القيوين"),
]
# label alternatives: English | Arabic (matched against normalised text, so alef/hamza variants are unified)
_L = {
    "rent": r"annual\s+rent|annual\s+rental|الايجار\s+السنوي|قيمه?\s+الايجار|بدل\s+الايجار|اجمالي\s+الايجار",
    "deposit": r"security\s+deposit|التامين|مبلغ\s+التامين",
    "beds": r"\bbedrooms?\b|\bbr\b|\bbhk\b|عدد\s+الغرف|غرف\s+النوم",
    "ptype": r"property\s+type|نوع\s+العقار",
    "loc": r"\blocation\b|\barea\b|\bcommunity\b|الموقع|المنطقه|المنطقة|المجتمع",
    "tenant": r"tenant\s*name|lessee\s*name|اسم\s+المستاجر|المستاجر\s*:",
    "landlord": r"landlord\s*name|owner\s*name|lessor\s*name|اسم\s+المالك|اسم\s+المؤجر|المؤجر\s*:|المالك\s*:",
    "ejari_no": r"ejari|ايجاري|رقم\s+العقد|توثيق",
    "period": r"contract\s*period|lease\s*period|tenancy\s*period|\bfrom\b.*\bto\b|فتره\s+العقد|مده\s+العقد|مده\s+الايجار|من\s+.*\s+الي\s",
    "start": r"start\s*date|commencement|تاريخ\s+البدء|بدايه\s+العقد|تاريخ\s+بداية",
    "end": r"end\s*date|expiry|expiration|تاريخ\s+الانتهاء|نهايه\s+العقد|تاريخ\s+نهاي",
    "proposed": r"proposed|المقترح|الجديد",
}


def parse_ejari_text(text: str) -> EjariFields:
    """Forgiving parser mapping Ejari/tenancy contract lines (English or Arabic) to fields."""
    text = to_ascii_digits(text or "")
    lines = clean_lines(text)
    f = EjariFields()
    norm_all = normalize_ar(text).lower()
    f.contract_language = detect_language(text)

    for jur, rx in _EMIRATE_WORDS[1:]:           # specific emirates first; Dubai/Ejari is the default
        if re.search(rx, norm_all):
            f.jurisdiction = jur
            break
    else:
        f.jurisdiction = "dubai"
    f.city = legal_kb.JURISDICTIONS[f.jurisdiction]

    if re.search(r"\bcommercial\b|\boffice\b|\bshop\b|\bretail\b|تجاري|مكتب|محل", norm_all) and not re.search(r"\bresidential\b|سكني", norm_all):
        f.usage = "commercial"

    def val(ln: str) -> str:
        parts = re.split(r"[:：]", ln, maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else ""

    for ln in lines:
        low = (ln.lower() + " " + normalize_ar(ln).lower())
        if re.search(_L["rent"], low):
            f.current_annual_rent_aed = parse_aed(ln, f.current_annual_rent_aed)
        if re.search(_L["deposit"], low):
            f.security_deposit_aed = parse_aed(ln, f.security_deposit_aed)
        if re.search(_L["beds"], low):
            m = INT_RE.search(ln)
            if m and int(m.group(0)) <= 15:
                f.bedrooms = int(m.group(0))
        if re.search(_L["ptype"], low):
            f.property_type = "villa" if re.search(r"villa|فيلا", low) else "townhouse" if re.search(r"townhouse|تاون", low) else "apartment"
        if re.search(_L["loc"], low) and not f.community and len(val(ln)) > 1:
            f.community = val(ln)
        if re.search(_L["tenant"], low) and not f.tenant_name:
            f.tenant_name = val(ln)
        if re.search(_L["landlord"], low) and not f.landlord_name:
            f.landlord_name = val(ln)
        if re.search(_L["ejari_no"], low) and not f.ejari_number:
            m = re.search(r"\b(\d{8,14})\b", ln)
            if m:
                f.ejari_number = m.group(1)
        if "ejari" in low and ("contact" in low or "helpline" in low):
            m = EJARI_CONTACT_RE.search(ln)
            if m:
                f.ejari_contact = re.sub(r"\s+", " ", m.group(1)).strip()
        if re.search(_L["proposed"], low) and re.search(r"rent|الايجار", low):
            f.proposed_new_rent_aed = parse_aed(ln, f.proposed_new_rent_aed)

    for i, ln in enumerate(lines):
        low = (ln.lower() + " " + normalize_ar(ln).lower())
        if re.search(_L["period"], low):
            window = " ".join(lines[i:i + 2])
            ds = [d for d in (to_date(m.group(0)) for m in DATE_RE.finditer(window)) if d]
            if len(ds) >= 2 and not f.start_date:
                f.start_date, f.end_date = ds[0], ds[1]
        if re.search(_L["start"], low) and not f.start_date:
            f.start_date = _first_date_in(ln) or f.start_date
        if re.search(_L["end"], low) and not f.end_date:
            f.end_date = _first_date_in(ln) or f.end_date
        if re.search(r"notice|اشعار|انذار", low) and re.search(r"sent|served|dated|issued|ارسال|تاريخ", low):
            d = _first_date_in(ln)
            if d:
                f.notice_sent_date = d

    f.renewal_date = f.end_date
    return f


def parse_pdf_smart(pdf_bytes: bytes) -> Dict[str, Any]:
    """Extract text (pdfminer -> PyPDF -> OCR) and Ejari-style fields."""
    notes: List[str] = []
    text = ""
    try:
        text = _extract_text_any(pdf_bytes)
        if not text.strip():
            notes.append("No extractable text (may be a scanned PDF).")
    except Exception as e:
        notes.append(f"PDF text extraction error: {e}")

    ocr_used = False
    if len(text.strip()) < 120:
        ocr = _ocr_pdf_to_text(pdf_bytes)
        if ocr and len(ocr.strip()) > len(text.strip()):
            text, ocr_used = ocr, True
            notes.append("OCR fallback used (scanned PDF).")
        else:
            notes.append("OCR not available or yielded too little text.")

    return {"text": text, "ejari": parse_ejari_text(text), "ocr_used": ocr_used, "notes": notes}


# ----------------------------- Clause segmentation -------------------------
_CLAUSE_START = re.compile(r"^\s*(?:\(?\d{1,2}(?:\.\d{1,2})*[\).:\-]|article\s+\d+|clause\s+\d+|المادة\s+\S+|البند\s+\S+|[a-z]\)|[•\-–*])\s*", re.I)


_KV_LINE = re.compile(r"^[A-Za-z\u0600-\u06FF][A-Za-z\u0600-\u06FF ./&()'-]{1,40}[:：]\s*\S.{0,80}$")


def segment_clauses(text: str, min_len: int = 25, max_len: int = 1500) -> List[str]:
    """Group lines into clauses: a numbered/bulleted line starts a new clause; other lines continue it."""
    clauses: List[str] = []
    cur: List[str] = []

    def flush() -> None:
        s = re.sub(r"\s+", " ", " ".join(cur)).strip()
        if len(s) >= min_len:
            clauses.append(s[:max_len])
        cur.clear()

    for ln in clean_lines(to_ascii_digits(text)):
        if _KV_LINE.match(ln) and not _CLAUSE_START.match(ln) and (not cur or _KV_LINE.match(cur[-1])):
            cur.clear()          # form field such as "Annual Rent: AED 110,000" - not a clause
            continue
        if re.match(r"^(?:terms\s*(?:&|and)\s*conditions?|الشروط\s+والاحكام|الشروط\s+والأحكام|شروط\s+العقد)\s*:?$", ln, re.I):
            cur.clear()
            continue
        if _CLAUSE_START.match(ln) and cur:
            flush()
        cur.append(ln)
        if re.search(r"[.;]$", ln) and len(" ".join(cur)) > 400:
            flush()
    flush()
    return clauses


# ----------------------------- Rent / date helpers -------------------------
def compute_proposed_increase_pct(current_aed: int, proposed_aed: int) -> float:
    if current_aed <= 0:
        return 0.0
    return round(((proposed_aed - current_aed) / float(current_aed)) * 100.0, 2)


def rera_slabs_max_increase(gap_pct: float) -> float:
    """Decree 43/2013: max renewal increase given how far (%) current rent is below the index."""
    gap = max(0.0, gap_pct)
    if gap <= 10:
        return 0.0
    if gap <= 20:
        return 5.0
    if gap <= 30:
        return 10.0
    if gap <= 40:
        return 15.0
    return 20.0


def estimate_gap_vs_index(current_aed: int, index_aed: Optional[int]) -> Optional[float]:
    """% by which current rent is below the index; None when it cannot be computed."""
    if not index_aed or index_aed <= 0 or current_aed <= 0:
        return None
    if current_aed >= index_aed:
        return 0.0
    return round((index_aed - current_aed) / index_aed * 100.0, 2)


NOTICE_MIN_DAYS = 90


def check_notice_window(renewal: Optional[date], notice_sent: Optional[date], min_days: int = NOTICE_MIN_DAYS) -> Tuple[str, str]:
    if not renewal or not notice_sent:
        return "unknown", f"Notice date or renewal date not provided - the {min_days}-day rent-change notice could not be verified."
    days = (renewal - notice_sent).days
    if days < min_days:
        return "fail", f"Notice was sent {days} days before renewal; at least {min_days} days' written notice is required for a rent change."
    return "pass", f"Notice was sent {days} days before renewal (minimum {min_days})."


# ----------------------------- Numeric / structural checks -----------------
def _emirate_checks(e: EjariFields, proposed_pct: float) -> List[Check]:
    """Checks for emirates other than Dubai. Their rules are *reported* by secondary sources, so these only
    ever warn (never fail) and always say 'verify'."""
    prof = legal_kb.PROFILES.get(e.jurisdiction, {})
    name = legal_kb.JURISDICTIONS.get(e.jurisdiction, e.jurisdiction)
    laws = list(prof.get("law_ids", []))
    out: List[Check] = []

    if proposed_pct > 0:
        if prof.get("freeze"):
            out.append(Check("RENT-CAP", "Rent increase", "warn",
                             f"A {proposed_pct:.1f}% increase is proposed, but a rent-increase suspension in {name} was reported in "
                             "June 2026. If it is still in force, no increase is allowed. Confirm with the authority (ADREC).", laws))
        else:
            msgs = []
            start = e.start_date
            yrs = prof.get("protect_years")
            if yrs and start and (date.today() - start).days < 365 * yrs:
                msgs.append(f"rents are reported to be protected from increases for the first {yrs} years of a lease")
            cap = prof.get("cap_pct")
            if cap and proposed_pct > cap:
                msgs.append(f"the reported maximum increase is about {cap:.0f}%")
            if msgs:
                out.append(Check("RENT-CAP", "Rent increase", "warn",
                                 f"A {proposed_pct:.1f}% increase is proposed; in {name}, " + " and ".join(msgs) +
                                 ". These rules come from secondary sources - verify with the municipality.", laws))
            else:
                out.append(Check("RENT-CAP", "Rent increase", "unknown",
                                 f"A {proposed_pct:.1f}% increase is proposed. No firm cap for {name} is built into this tool - "
                                 "check the current local rule.", laws))
        nd = prof.get("notice")
        if nd:
            st, msg = check_notice_window(e.renewal_date, e.notice_sent_date, nd)
            out.append(Check("NOTICE", "Rent-change notice", "warn" if st == "fail" else st,
                             msg + " (reported period - verify)", laws))
    else:
        out.append(Check("RENT-CAP", "Rent increase", "pass", "No rent increase is proposed.", laws))

    reg = prof.get("reg", "registration")
    if e.ejari_number:
        out.append(Check("REGISTRATION", "Contract registration", "pass", f"Registration number present ({e.ejari_number}).", laws))
    else:
        out.append(Check("REGISTRATION", "Contract registration", "warn",
                         f"No registration number found. Tenancy contracts in {name} should be registered ({reg}); "
                         "unregistered leases may not be recognised by authorities.", laws))
    return out


def run_numeric_checks(e: EjariFields, rera_index_aed: Optional[int]) -> Tuple[List[Check], float, Optional[float]]:
    checks: List[Check] = []
    proposed = e.proposed_new_rent_aed or e.current_annual_rent_aed
    proposed_pct = compute_proposed_increase_pct(e.current_annual_rent_aed, proposed)
    max_pct: Optional[float] = None

    # --- Rent increase vs Decree 43/2013 (Dubai only)
    if e.jurisdiction == "dubai":
        if proposed_pct <= 0:
            checks.append(Check("RENT-CAP", "Rent increase", "pass",
                                "No rent increase is proposed.", ["DXB-43-2013"]))
        else:
            gap = estimate_gap_vs_index(e.current_annual_rent_aed, rera_index_aed)
            if gap is None:
                checks.append(Check("RENT-CAP", "Rent increase", "unknown",
                                    f"A {proposed_pct:.1f}% increase is proposed but no rental-index figure was supplied, "
                                    "so the Decree 43/2013 cap cannot be calculated. Enter the Dubai Smart Rental Index "
                                    "average for this unit.", ["DXB-43-2013", "DXB-RENTAL-INDEX"]))
            else:
                max_pct = rera_slabs_max_increase(gap)
                if proposed_pct > max_pct + 1e-6:
                    checks.append(Check("RENT-CAP", "Rent increase", "fail",
                                        f"Proposed increase {proposed_pct:.1f}% exceeds the {max_pct:.0f}% maximum "
                                        f"(current rent is {gap:.1f}% below the index of {fmt_aed(rera_index_aed)}).",
                                        ["DXB-43-2013"]))
                else:
                    checks.append(Check("RENT-CAP", "Rent increase", "pass",
                                        f"Proposed increase {proposed_pct:.1f}% is within the {max_pct:.0f}% maximum "
                                        f"(current rent is {gap:.1f}% below the index).", ["DXB-43-2013"]))

        if proposed_pct > 0:
            nv, nmsg = check_notice_window(e.renewal_date, e.notice_sent_date)
            checks.append(Check("NOTICE-90", "Rent-change notice", nv, nmsg, ["DXB-33-2008"]))
    else:
        checks.extend(_emirate_checks(e, proposed_pct))

    # --- Security deposit (market norm, not a statutory cap)
    if e.current_annual_rent_aed > 0 and e.security_deposit_aed > 0:
        pct = e.security_deposit_aed / e.current_annual_rent_aed * 100
        norm = 10 if e.furnishing != "unfurnished" else 5
        if e.usage == "commercial":
            checks.append(Check("DEPOSIT", "Security deposit", "pass",
                                f"Deposit is {pct:.1f}% of annual rent (commercial deposits are negotiated).", []))
        elif pct > norm * 2:
            checks.append(Check("DEPOSIT", "Security deposit", "warn",
                                f"Deposit is {pct:.1f}% of annual rent - well above the market norm of ~{norm}% "
                                f"for {e.furnishing} units. No statutory cap exists, but this is worth negotiating.",
                                ["DXB-26-2007"]))
        elif pct > norm:
            checks.append(Check("DEPOSIT", "Security deposit", "warn",
                                f"Deposit is {pct:.1f}% of annual rent, above the ~{norm}% market norm for "
                                f"{e.furnishing} units (customary, not a legal cap).", ["DXB-26-2007"]))
        else:
            checks.append(Check("DEPOSIT", "Security deposit", "pass",
                                f"Deposit is {pct:.1f}% of annual rent (market norm ~{norm}%).", []))

    # --- Contract period sanity
    if e.start_date and e.end_date:
        days = (e.end_date - e.start_date).days
        if days <= 0:
            checks.append(Check("TERM", "Contract period", "fail", "End date is not after the start date.", []))
        elif days > 365 * 10 + 5 and e.jurisdiction == "dubai":
            checks.append(Check("TERM", "Contract period", "warn",
                                f"Term is {days / 365:.1f} years. Long leases (over 10 years) generally need Dubai Land "
                                "Department registration rather than Ejari alone.", ["DXB-7-2006"]))
        else:
            checks.append(Check("TERM", "Contract period", "pass", f"Term is {days} days ({days / 365:.1f} years).", []))
        if date.today() > e.end_date:
            checks.append(Check("EXPIRED", "Contract status", "warn",
                                f"The contract ended on {e.end_date:%d %b %Y}; if the parties kept occupying, it renews "
                                "on the same terms unless properly notified.", ["DXB-33-2008"]))

    # --- Ejari / registration
    if e.jurisdiction == "dubai":
        if e.ejari_number:
            checks.append(Check("EJARI", "Ejari registration", "pass", f"Ejari contract number present ({e.ejari_number}).", ["DXB-26-2007"]))
        else:
            checks.append(Check("EJARI", "Ejari registration", "warn",
                                "No Ejari number found. A Dubai tenancy must be registered through Ejari.", ["DXB-26-2007", "DXB-33-2008"]))
    days_notice = (e.renewal_date - e.notice_sent_date).days if (e.renewal_date and e.notice_sent_date) else None
    for c in checks:
        c.args = {"proposed": f"{proposed_pct:.1f}", "max": "" if max_pct is None else f"{max_pct:.0f}",
                  "days": "" if days_notice is None else str(days_notice)}
    return checks, proposed_pct, max_pct


# Checklist of terms a complete tenancy contract should state.
CHECKLIST: List[Tuple[str, str]] = [
    ("Parties named (landlord and tenant)", r"landlord|lessor|owner|المؤجر|المالك|الموجر"),
    ("Property description / unit number", r"unit|property|premises|apartment|villa|plot|building|وحده|عقار|شقه|فيلا|مبني|قطعه"),
    ("Contract period / dates", r"contract period|lease period|term of|from .* to |start date|commence|فتره|مده|من .* الي|تاريخ"),
    ("Annual rent and payment schedule", r"annual rent|rent of|rental|cheque|instalment|installment|الايجار|شيك|دفعات|اقساط"),
    ("Security deposit and refund terms", r"security deposit|refundable deposit|التامين|وديعه"),
    ("Maintenance responsibilities", r"maintenance|repairs?|الصيانه|الاصلاحات|التصليحات"),
    ("Renewal / termination terms", r"renew|terminat|notice|تجديد|انهاء|اشعار|فسخ"),
    ("Permitted use of property", r"residential|commercial|use of the (?:property|premises)|purpose|سكني|للسكن|السكن|تجاري|الغرض|استخدام"),
    ("Dispute resolution / governing law", r"dispute|governing law|jurisdiction|courts?|rental dispute|نزاع|خلاف|المحاكم|القانون|اختصاص"),
]


def missing_items(text: str) -> List[str]:
    low = (text or "").lower() + " " + normalize_ar(text or "").lower()
    return [name for name, rx in CHECKLIST if not re.search(rx, low)]


# ----------------------------- Reference corpus + retrieval ----------------
def read_articles_texts_from_csv(obj: Any) -> List[str]:
    """Read a reference CSV and return text snippets (column with the most text)."""
    try:
        import pandas as pd  # type: ignore
        df = pd.read_csv(obj)
    except Exception:
        return []
    text_cols = [c for c in df.columns if df[c].dtype == object]
    if not text_cols:
        return []
    best = max(text_cols, key=lambda c: df[c].astype(str).str.len().sum())
    return [str(x).strip() for x in df[best].dropna().astype(str) if str(x).strip()]


_STOP = set("the and for that with this shall will any are not may must from have has been such under which their his her its into upon each other than also".split())


def _tokens(s: str) -> List[str]:
    return [t for t in re.findall(r"[a-z]{3,}", (s or "").lower()) if t not in _STOP]


class Retriever:
    """Tiny TF-IDF retriever over (id, text) documents - no external deps."""

    def __init__(self, docs: List[Tuple[str, str]]):
        self.docs = docs
        self.tf = [Counter(_tokens(t)) for _, t in docs]
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = max(len(docs), 1)
        self.idf = {t: math.log(1 + n / (1 + d)) for t, d in df.items()}

    def top(self, query: str, k: int = 6) -> List[Tuple[str, str, float]]:
        q = Counter(_tokens(query))
        scored: List[Tuple[str, str, float]] = []
        for (doc_id, text), tf in zip(self.docs, self.tf):
            s = sum(self.idf.get(t, 0) * (1 + math.log(tf[t])) for t in q if t in tf)
            if s > 0:
                scored.append((doc_id, text, s))
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored[:k]


# ----------------------------- AI review ------------------------------------
AI_SYSTEM = (
    "You are a careful UAE tenancy-law reviewer. You are given numbered clauses from a rental contract and a set of "
    "REFERENCE texts, each with an id. Judge each clause ONLY against the references and well-established UAE "
    "tenancy principles. Do not invent laws or article numbers. If a clause is ordinary and lawful, say so. "
    "Cite only reference ids you were given. Reply with a single JSON object and nothing else."
)


def _ai_batch_prompt(jurisdiction: str, batch: List[Tuple[int, str]], refs: List[Tuple[str, str]]) -> str:
    ref_txt = "\n".join(f"[{rid}] {txt[:700]}" for rid, txt in refs)
    clause_txt = "\n".join(f"({n}) {t[:900]}" for n, t in batch)
    return (
        f"Jurisdiction: {legal_kb.JURISDICTIONS.get(jurisdiction, jurisdiction)}\n\nREFERENCES:\n{ref_txt}\n\n"
        f"CLAUSES:\n{clause_txt}\n\n"
        'Return JSON: {"clauses":[{"id":<clause number>,"verdict":"compliant"|"risky"|"non_compliant",'
        '"reason":"<one or two plain-English sentences>","law_ids":["<reference ids>"],'
        '"suggestion":"<short fix, or empty>"}]}. Include every clause number.'
    )


def ai_review(
    client: LLMClient,
    clauses: List[ClauseFinding],
    jurisdiction: str,
    articles: Optional[List[str]] = None,
    progress: Optional[Callable[[float, str], None]] = None,
    batch_size: int = 5,
    max_clauses: int = 60,
) -> Tuple[Dict[int, Dict[str, Any]], str]:
    """Review clauses with an open model. Returns ({clause_no: result}, error_message)."""
    kb_entries = legal_kb.applicable_entries(jurisdiction)
    docs = [(e.id, f"{e.citation}: {e.summary}") for e in kb_entries]
    kb_ids = {d[0] for d in docs}
    art_docs = [(f"ART-{i}", a) for i, a in enumerate(articles or [])]
    kb_ret = Retriever(docs)
    art_ret = Retriever(art_docs) if art_docs else None

    targets = [c for c in clauses if len(c.text) >= 40][:max_clauses]
    results: Dict[int, Dict[str, Any]] = {}
    err = ""
    batches = [targets[i:i + batch_size] for i in range(0, len(targets), batch_size)]
    for bi, grp in enumerate(batches):
        if progress:
            progress(bi / max(len(batches), 1), f"AI reviewing clauses {grp[0].clause_no}-{grp[-1].clause_no}…")
        query = " ".join(c.text for c in grp)
        refs: Dict[str, str] = {}
        for rid, txt, _ in kb_ret.top(query, k=7):
            refs[rid] = txt
        for core in ("DXB-33-2008", "DXB-43-2013", "DXB-26-2007"):    # always include the core Dubai statutes
            if core in kb_ids and jurisdiction == "dubai":
                refs.setdefault(core, dict(docs)[core])
        if art_ret:
            for rid, txt, _ in art_ret.top(query, k=4):
                refs[rid] = txt
        ref_list = list(refs.items())
        try:
            reply = client.chat(
                [{"role": "system", "content": AI_SYSTEM},
                 {"role": "user", "content": _ai_batch_prompt(jurisdiction, [(c.clause_no, c.text) for c in grp], ref_list)}],
                json_output=True,
            )
            data = extract_json(reply)
            items = data.get("clauses", data) if isinstance(data, dict) else data
            for it in items:
                n = int(it.get("id"))
                ids = [str(x) for x in (it.get("law_ids") or []) if str(x) in refs]   # drop hallucinated citations
                results[n] = {
                    "verdict": str(it.get("verdict", "compliant")).lower(),
                    "reason": str(it.get("reason", "")).strip(),
                    "law_ids": ids,
                    "suggestion": str(it.get("suggestion", "")).strip(),
                }
        except (LLMError, ValueError, TypeError, KeyError, AttributeError) as ex:
            err = f"AI review stopped at clause {grp[0].clause_no}: {ex}"
            break
    if progress:
        progress(1.0, "AI review complete" if not err else "AI review ended early")
    return results, err


def ai_summarise(client: LLMClient, findings: List[ClauseFinding], checks: List[Check], jurisdiction: str) -> str:
    items = [f"- Clause {f.clause_no} [{f.verdict}] {f.title or f.issues}" for f in findings if f.verdict != "pass"][:20]
    items += [f"- {c.name} [{c.status}] {c.message}" for c in checks if c.status in ("fail", "warn")][:10]
    if not items:
        return ""
    prompt = (
        f"Summarise these tenancy-contract audit findings for a non-lawyer tenant or landlord in "
        f"{legal_kb.JURISDICTIONS.get(jurisdiction, jurisdiction)}, in 3-5 short sentences: what matters most and what "
        "to do first. Do not add new legal claims.\n\n" + "\n".join(items)
    )
    try:
        return client.chat([{"role": "user", "content": prompt}], max_tokens=400)
    except LLMError:
        return ""


LANG_NAMES = {"en": "English", "hi": "Hindi", "ml": "Malayalam", "ar": "Arabic", "ur": "Urdu", "ta": "Tamil"}


def translate_batch(client: LLMClient, items: Dict[str, str], lang: str) -> Dict[str, str]:
    """Translate {key: english_text} into simple {lang} for readers with limited literacy. {} on failure."""
    if lang == "en" or not items:
        return {}
    name = LANG_NAMES.get(lang, lang)
    prompt = (
        f"Translate each value into very simple {name}, for a reader with limited reading ability: short sentences, "
        "everyday words, no legal jargon. Keep numbers, AED amounts and law names (e.g. 'Law 26/2007') unchanged. "
        'Return only JSON mapping the same keys to the translations.\n\n' + json.dumps(items, ensure_ascii=False)
    )
    try:
        data = extract_json(client.chat([{"role": "user", "content": prompt}], json_output=True, max_tokens=4000))
        return {k: str(v) for k, v in data.items() if k in items and str(v).strip()}
    except (LLMError, ValueError, AttributeError):
        return {}


# ----------------------------- Main audit -----------------------------------
def _merge_ai(cf: ClauseFinding, ai: Dict[str, Any]) -> None:
    v = ai.get("verdict", "compliant")
    if v not in ("risky", "non_compliant") or not ai.get("reason"):
        return
    laws = list(dict.fromkeys(cf.laws + ai.get("law_ids", [])))
    if cf.verdict == "pass":
        # AI-only findings: 'fail' only when backed by at least one cited reference
        cf.verdict = "fail" if (v == "non_compliant" and ai.get("law_ids")) else "warn"
        cf.title = "Flagged by AI review"
        cf.explanation = ai["reason"]
        cf.suggestion = ai.get("suggestion", "")
        cf.source = "ai"
    else:
        cf.explanation = f"{cf.explanation} AI note: {ai['reason']}".strip()
        cf.suggestion = cf.suggestion or ai.get("suggestion", "")
        cf.source = "rule+ai"
    cf.laws = laws
    cf.issues = cf.title + (f" - {cf.explanation}" if cf.explanation else "")


def run_audit(
    contract_text: str,
    ejari: EjariFields,
    rera_index_aed: Optional[int] = None,
    llm: Optional[LLMClient] = None,
    ai_articles: Optional[List[str]] = None,
    progress: Optional[Callable[[float, str], None]] = None,
) -> AuditResult:
    jur = ejari.jurisdiction or "dubai"
    clauses = segment_clauses(contract_text)
    findings: List[ClauseFinding] = []
    for i, text in enumerate(clauses, start=1):
        hits = rules_mod.match_rules(text, jur)
        if not hits:
            findings.append(ClauseFinding(i, text))
            continue
        top = hits[0]
        laws = list(dict.fromkeys(l for h in hits for l in h.laws))
        findings.append(ClauseFinding(
            clause_no=i, text=text, verdict=top.severity if top.severity in ("fail", "warn") else "pass",
            title=" / ".join(h.title for h in hits), category=top.category,
            explanation=" ".join(h.explanation for h in hits[:2]),
            suggestion=top.suggestion, laws=laws, source="rule", rule_ids=[h.id for h in hits],
            issues=top.title + " - " + top.explanation,
        ))

    checks, proposed_pct, max_pct = run_numeric_checks(ejari, rera_index_aed)

    ai_used, ai_err, ai_summary, ai_model = False, "", "", ""
    if llm is not None:
        ai_model = llm.model
        results, ai_err = ai_review(llm, findings, jur, ai_articles, progress)
        ai_used = bool(results)
        for cf in findings:
            if cf.clause_no in results:
                _merge_ai(cf, results[cf.clause_no])

    fails = sum(1 for c in findings if c.verdict == "fail") + sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in findings if c.verdict == "warn") + sum(1 for c in checks if c.status == "warn")
    unknown = sum(1 for c in checks if c.status == "unknown")
    miss = missing_items(contract_text)
    score = max(0, 100 - 12 * fails - 4 * warns - 2 * unknown - 2 * len(miss))
    verdict = "fail" if fails else ("review" if (warns or unknown or miss) else "pass")

    issues = [f"Clause {c.clause_no}: {c.title}" for c in findings if c.verdict == "fail"]
    issues += [f"{c.name}: {c.message}" for c in checks if c.status == "fail"]

    if llm is not None and ai_used:
        ai_summary = ai_summarise(llm, findings, checks, jur)

    notes: List[str] = []
    if not clauses:
        notes.append("No clauses could be identified in the text - paste the contract terms to run a full review.")
    if jur != "dubai":
        notes.append("Emirate-specific coverage outside Dubai is limited; federal checks and general rules were applied.")
    unverified = {l for f in findings for l in f.laws if l in legal_kb.KB_BY_ID and not legal_kb.KB_BY_ID[l].verified}
    if unverified:
        notes.append("Some cited sources are marked 'to verify': " + ", ".join(sorted(legal_kb.citation_for(l) for l in unverified)) + ".")

    return AuditResult(
        verdict=verdict, score=score, issues=issues, rera_max_increase_pct=max_pct,
        proposed_increase_pct=proposed_pct, clause_findings=findings, checks=checks, missing_items=miss,
        ejari=ejari, jurisdiction=jur, ai_used=ai_used, ai_model=ai_model, ai_summary=ai_summary,
        ai_error=ai_err, notes=notes, contract_text=contract_text, timestamp=now_iso(),
    )


def result_to_dict(res: AuditResult) -> Dict[str, Any]:
    d = asdict(res)
    d["ejari"] = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d["ejari"].items()}
    d.pop("contract_text", None)
    return d


def render_html_report(res: AuditResult) -> str:
    """Self-contained printable report (Save as PDF from the browser)."""
    from html import escape as h
    colour = {"pass": "#1a7f4b", "warn": "#b36b00", "fail": "#b3261e", "unknown": "#555"}
    rows = []
    for c in res.clause_findings:
        if c.verdict == "pass":
            continue
        laws = ", ".join(legal_kb.citation_for(l) for l in c.laws)
        rows.append(
            f"<div style='border-left:4px solid {colour[c.verdict]};padding:8px 12px;margin:10px 0;background:#fafafa'>"
            f"<b style='color:{colour[c.verdict]}'>{c.verdict.upper()}</b> - Clause {c.clause_no}: {h(c.title)}"
            f"<div style='font-style:italic;margin:4px 0'>“{h(c.text[:400])}”</div>"
            f"<div>{h(c.explanation)}</div>"
            + (f"<div><b>Suggested fix:</b> {h(c.suggestion)}</div>" if c.suggestion else "")
            + (f"<div style='color:#555'>Basis: {h(laws)}</div>" if laws else "") + "</div>"
        )
    chk = "".join(
        f"<li><b style='color:{colour[c.status]}'>{c.status.upper()}</b> {h(c.name)} - {h(c.message)}</li>" for c in res.checks)
    miss = "".join(f"<li>{h(m)}</li>" for m in res.missing_items)
    return (
        "<!doctype html><meta charset='utf-8'><title>Tenancy audit report</title>"
        "<body style='font-family:system-ui,sans-serif;max-width:820px;margin:32px auto;color:#222'>"
        f"<h1>Tenancy contract audit</h1><p>{res.timestamp} · {legal_kb.JURISDICTIONS.get(res.jurisdiction)} · "
        f"Verdict: <b>{res.verdict.upper()}</b> · Score {res.score}/100</p>"
        + (f"<p>{h(res.ai_summary)}</p>" if res.ai_summary else "")
        + f"<h2>Checks</h2><ul>{chk}</ul><h2>Clause findings</h2>{''.join(rows) or '<p>No clause issues found.</p>'}"
        + (f"<h2>Terms not found in the contract</h2><ul>{miss}</ul>" if miss else "")
        + f"<hr><p style='color:#666;font-size:13px'>Automated screening, not legal advice. Legal knowledge base last reviewed "
        f"{res.kb_reviewed}. Confirm important points with a UAE-licensed lawyer or RERA / the Dubai Land Department.</p></body>"
    )


# ----------------------------- Firestore (Admin) ---------------------------
_firebase_ready = False
_firestore = None


def firebase_init_from_mapping(cfg: Dict[str, Any]) -> None:
    global _firebase_ready, _firestore
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import credentials, firestore  # type: ignore

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(cfg))
        _firestore = firestore.client()
        _firebase_ready = True
    except Exception as e:
        _firebase_ready = False
        raise RuntimeError(f"Firebase init error: {e}")


def firebase_init_from_json_string(sa_json: str) -> None:
    firebase_init_from_mapping(json.loads(sa_json))


def firebase_init_from_file(path: str) -> None:
    with open(path, "r") as f:
        firebase_init_from_mapping(json.load(f))


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
    """Append an audit record to /agreements/{id}/ledger/{auto}. id = SHA256(text+landlord+tenant)[:32]."""
    if not firebase_available():
        raise RuntimeError("Firestore not initialized")

    db = _firestore
    seed = (audit.contract_text or "").encode("utf-8") + (landlord or "").encode("utf-8") + (tenant or "").encode("utf-8")
    agreement_id = _sha256_hex(seed)[:32]

    doc = {
        "timestamp": audit.timestamp,
        "tenant": tenant,
        "landlord": landlord,
        "rera_index_aed": rera_index_aed,
        "audit": result_to_dict(audit),
        "contract_text_hash": _sha256_hex((audit.contract_text or "").encode("utf-8")),
        "pdf_sha256": _sha256_hex(pdf_bytes) if pdf_bytes else None,
        "version": 2,
    }
    agreements = db.collection("agreements")
    agreements.document(agreement_id).set({"created_at": audit.timestamp}, merge=True)
    agreements.document(agreement_id).collection("ledger").document().set(doc)
    return agreement_id
