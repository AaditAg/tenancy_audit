# audit_engine.py — PDF parsing, law checks, RERA slabs, highlighting, and Firestore database helpers
# -----------------------------------------------------------------------------
from __future__ import annotations
import io
import re
import json
import hashlib
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple, Any

import io

_pdfminer_ok = False
_pypdf_ok = False

try:
    from pdfminer.high_level import extract_text as _pdfminer_extract_text
    _pdfminer_ok = True
except Exception:
    _pdfminer_ok = False

try:
    import pypdf
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
    # Try pdfminer first (better), then PyPDF
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
from dateutil.parser import parse as dtparse

# --- add this to audit_engine.py ---
from datetime import date as _date
from dateutil.parser import parse as _dtparse

def to_date(value: str | _date | None) -> _date:
    """Best-effort parser that always returns a date (defaults to a sensible future date if parsing fails)."""
    if value is None:
        return _date(2025, 12, 1)  # default so Streamlit date_input always has a value
    if isinstance(value, _date):
        return value
    try:
        return _dtparse(str(value)).date()
    except Exception:
        return _date(2025, 12, 1)


# OCR deps (optional)
_OCR_READY = False
try:
    import pytesseract  # type: ignore
    from pdf2image import convert_from_bytes  # type: ignore
    from PIL import Image  # type: ignore
    _OCR_READY = True
except Exception:
    _OCR_READY = False

# Sample PDF generator
from reportlab.pdfgen import canvas  # type: ignore
from reportlab.lib.pagesizes import A4  # type: ignore
from reportlab.lib.units import cm  # type: ignore

import pandas as pd
import chardet

# ===================== Firestore (Database) =====================
_FB_INIT = False
try:
    import firebase_admin  # type: ignore
    from firebase_admin import credentials, firestore  # type: ignore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

# ---------- Firestore init ----------
def firebase_init_from_mapping(mapping: Dict[str, Any]) -> None:
    global _FB_INIT
    if _FB_INIT: return
    if firebase_admin is None: raise RuntimeError("firebase-admin not installed")
    cred = credentials.Certificate(mapping)
    firebase_admin.initialize_app(cred)
    _FB_INIT = True

def firebase_init_from_file(path: str) -> None:
    global _FB_INIT
    if _FB_INIT: return
    if firebase_admin is None: raise RuntimeError("firebase-admin not installed")
    cred = credentials.Certificate(path)
    firebase_admin.initialize_app(cred)
    _FB_INIT = True

def firebase_init_from_json_string(json_str: str) -> None:
    global _FB_INIT
    if _FB_INIT: return
    if firebase_admin is None: raise RuntimeError("firebase-admin not installed")
    data = json.loads(json_str)
    cred = credentials.Certificate(data)
    firebase_admin.initialize_app(cred)
    _FB_INIT = True

def firebase_init_from_bytes(blob: bytes) -> None:
    global _FB_INIT
    if _FB_INIT: return
    if firebase_admin is None: raise RuntimeError("firebase-admin not installed")
    data = json.loads(blob.decode("utf-8"))
    cred = credentials.Certificate(data)
    firebase_admin.initialize_app(cred)
    _FB_INIT = True

def firebase_is_ready() -> bool:
    return _FB_INIT and (firestore is not None)

def _db() -> "firestore.Client":
    if not firebase_is_ready():
        raise RuntimeError("Firestore not initialized")
    return firestore.client()

# ---------- Firestore schema helpers ----------
# Schema (Native mode):
#   agreements/{agreement_id}
#     fields: city, area, property_type, bedrooms, current_rent, proposed_rent, renewal_date, notice_sent_date, deposit, furnished, tenant_id, landlord_id, created_at, updated_at
#     snapshots/{timestamp_iso}
#       raw_text, parsed_fields, text_sha256
#     audits/{timestamp_iso}
#       audit_sha256, verdict, allowed_increase, highlights, rule_flags, ejari_clause_results
#     events/{timestamp_iso}
#       kind, note, extra
#     ledger/{000000, 000001, ...}
#       index, ts, payload, payload_hash, prev_hash, this_hash

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def fs_upsert_agreement(*, agreement_id: str, base_metadata: Dict[str, Any]) -> Dict[str, Any]:
    db = _db()
    doc_ref = db.collection("agreements").document(agreement_id)
    data = {
        **base_metadata,
        "updated_at": _now_iso(),
    }
    # create if missing (set merge True), also set created_at once
    doc = doc_ref.get()
    if not doc.exists:
        data["created_at"] = _now_iso()
    doc_ref.set(data, merge=True)
    return {"path": f"agreements/{agreement_id}"}

def fs_save_contract_snapshot(*, agreement_id: str, raw_text: str, parsed_fields: Dict[str, Any]) -> Dict[str, Any]:
    db = _db()
    ts = _now_iso()
    text_hash = sha256_text(raw_text or "")
    snap_ref = db.collection("agreements").document(agreement_id).collection("snapshots").document(ts)
    snap_ref.set({
        "ts": ts,
        "raw_text": raw_text,
        "parsed_fields": parsed_fields or {},
        "text_sha256": text_hash,
    })
    return {"path": f"agreements/{agreement_id}/snapshots/{ts}", "text_sha256": text_hash}

def fs_save_audit_result(*, agreement_id: str, audit_result: Dict[str, Any]) -> Dict[str, Any]:
    db = _db()
    ts = audit_result.get("timestamp") or _now_iso()
    audit_hash = sha256_json(audit_result)
    audit_ref = db.collection("agreements").document(agreement_id).collection("audits").document(ts)
    audit_ref.set({
        "ts": ts,
        "audit_sha256": audit_hash,
        "verdict": audit_result.get("verdict"),
        "allowed_increase": audit_result.get("allowed_increase", {}),
        "highlights": audit_result.get("highlights", []),
        "rule_flags": audit_result.get("rule_flags", []),
        "ejari_clause_results": audit_result.get("ejari_clause_results", []),
        "raw_text_for_report": audit_result.get("raw_text_for_report", None),
    })
    return {"path": f"agreements/{agreement_id}/audits/{ts}", "audit_sha256": audit_hash}

def fs_append_event(*, agreement_id: str, kind: str, note: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    db = _db()
    ts = _now_iso()
    ev_ref = db.collection("agreements").document(agreement_id).collection("events").document(ts)
    ev_ref.set({"ts": ts, "kind": kind, "note": note, "extra": extra or {}})
    return {"path": f"agreements/{agreement_id}/events/{ts}"}

def fs_list_events(*, agreement_id: str) -> List[Dict[str, Any]]:
    db = _db()
    evs = db.collection("agreements").document(agreement_id).collection("events").order_by("ts").stream()
    rows = []
    for d in evs:
        rows.append(d.to_dict() | {"_id": d.id})
    return rows

# ===================== Hashing & Ledger (hash-chained) =====================
def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def sha256_json(obj: Any) -> str:
    b = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()

def _ledger_col(namespace: str, agreement_id: str):
    db = _db()
    return db.collection(namespace).document(agreement_id).collection("ledger")

def _ledger_head(namespace: str, agreement_id: str) -> Optional[Dict[str, Any]]:
    col = _ledger_col(namespace, agreement_id)
    docs = col.order_by("index", direction=firestore.Query.DESCENDING).limit(1).stream()
    last = None
    for d in docs:
        last = d.to_dict()
    return last

def ledger_append(*, namespace: str, agreement_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    col = _ledger_col(namespace, agreement_id)
    last = _ledger_head(namespace, agreement_id)
    idx = 0 if last is None else int(last["index"]) + 1
    ts = _now_iso()
    payload_hash = sha256_json(payload)
    prev_hash = last["this_hash"] if last else "GENESIS"
    preimage = json.dumps({"index": idx, "ts": ts, "payload_hash": payload_hash, "prev_hash": prev_hash}, sort_keys=True)
    this_hash = sha256_text(preimage)
    entry = {
        "index": idx,
        "ts": ts,
        "payload": payload,
        "payload_hash": payload_hash,
        "prev_hash": prev_hash,
        "this_hash": this_hash,
    }
    col.document(f"{idx:06d}").set(entry)
    return entry

def ledger_verify(*, namespace: str, agreement_id: str) -> Tuple[bool, str]:
    col = _ledger_col(namespace, agreement_id)
    docs = list(col.order_by("index").stream())
    prev = None
    for d in docs:
        item = d.to_dict()
        preimage = json.dumps(
            {"index": item["index"], "ts": item["ts"], "payload_hash": item["payload_hash"], "prev_hash": item["prev_hash"]},
            sort_keys=True,
        )
        recomputed = sha256_text(preimage)
        if recomputed != item["this_hash"]:
            return False, f"Hash mismatch at index {item['index']}"
        if prev and item["prev_hash"] != prev["this_hash"]:
            return False, f"Broken link at index {item['index']}"
        prev = item
    return True, "OK"

# ===================== Normalization & regex =====================
def _normalize_text(s: str) -> str:
    if not s: return s
    s = (s.replace("\u2018", "'").replace("\u2019", "'")
           .replace("\u201C", '"').replace("\u201D", '"')
           .replace("\u2013", "-").replace("\u2014", "-"))
    s = re.sub(r"\s+", " ", s)
    return s

_MONEY_RE = re.compile(r"(?:AED|DHS|د\.إ)?\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)", re.I)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.I)
EJ_TERMS_HEADER = re.compile(r"terms\s*&?\s*conditions", re.I)
EJ_CLAUSE = re.compile(r"^\s*([0-9]{1,2})\)\s*(.+)$")

EJ_LABELS = {
    "annual_rent": re.compile(r"\bannual\s+rent\b.*?aed\s*([0-9,]+)", re.I),
    "deposit": re.compile(r"\bsecurity\s+deposit\b.*?aed\s*([0-9,]+)", re.I),
    "from": re.compile(r"\bfrom\b\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|\d{1,2}\s+\w+\s+\d{4})", re.I),
    "to": re.compile(r"\b(to|until)\b\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|\d{1,2}\s+\w+\s+\d{4})", re.I),
    "bed": re.compile(r"\bbedrooms?\b[:\-]?\s*(studio|\d+)", re.I),
    "area": re.compile(r"\b(area|location)\b[:\-]?\s*([A-Za-z ]{3,})", re.I),
    "ptype": re.compile(r"\bproperty\s*type\b[:\-]?\s*(apartment|villa|townhouse|residential)", re.I),
}

def _to_int(val: Optional[str]) -> Optional[int]:
    if not val: return None
    try: return int(val.replace(",", ""))
    except Exception: return None

def _to_date_str(val: Optional[str]) -> Optional[str]:
    if not val: return None
    try: return dtparse(val).date().isoformat()
    except Exception: return None

# ===================== PDF parsing =====================
def _ocr_pdf_to_text(pdf_bytes: bytes) -> str:
    if not _OCR_READY: return ""
    pages = convert_from_bytes(pdf_bytes, dpi=300)
    out = []
    for img in pages:
        if not isinstance(img, Image.Image): img = img.convert("RGB")
        out.append(pytesseract.image_to_string(img, lang="eng"))
    return "\n".join(out)

def parse_pdf_smart(pdf_bytes: bytes) -> Dict[str, Any]:
    notes: List[str] = []
    text = ""
    try:
        text = extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        notes.append(f"pdfminer error: {e}")
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

def parse_ejari_text(text: str) -> Dict[str, Any]:
    if not text: return {}
    t = text
    rent = dep = None
    start = end = ptype = None
    beds = None
    area = None
    m = EJ_LABELS["annual_rent"].search(t); rent = _to_int(m.group(1)) if m else None
    m = EJ_LABELS["deposit"].search(t);     dep = _to_int(m.group(1)) if m else None
    m = EJ_LABELS["from"].search(t);        start = _to_date_str(m.group(1)) if m else None
    m = EJ_LABELS["to"].search(t);          end = _to_date_str(m.group(2) if m and m.lastindex>=2 else None)
    m = EJ_LABELS["bed"].search(t)
    if m:
        v = m.group(1).lower()
        beds = 0 if v == "studio" else _to_int(v)
    m = EJ_LABELS["area"].search(t);        area = (m.group(2).strip() if m else None)
    m = EJ_LABELS["ptype"].search(t)
    if m:
        p = m.group(1).lower()
        ptype = "apartment" if p == "residential" else p

    clauses: List[Dict[str, Any]] = []
    terms_start = EJ_TERMS_HEADER.search(t)
    if terms_start:
        for line in t[terms_start.start():].splitlines():
            cm = EJ_CLAUSE.match(line)
            if cm:
                clauses.append({"num": int(cm.group(1)), "text": cm.group(2).strip()})

    return {
        "annual_rent": rent,
        "deposit": dep,
        "start_date": start,
        "end_date": end,
        "renewal_date": end,
        "bedrooms": beds,
        "area": area,
        "property_type": ptype,
        "clauses": clauses,
    }

def autofill_from_text(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    t = text or ""
    m = re.search(r"\b(studio|[1-9])\s*br\b|\bbedrooms?\b[:\-]?\s*(studio|[1-9])", t, re.I)
    if m:
        v = (m.group(1) or m.group(2) or "").lower()
        out["bedrooms"] = 0 if v == "studio" else int(re.sub(r"\D", "", v))
    m = re.search(r"\b(area|location)\b[:\-]?\s*([A-Za-z ]{3,})", t, re.I)
    if m: out["area"] = m.group(2).strip()
    m = re.search(r"\bannual\s+rent\b.*?aed\s*([0-9,]+)", t, re.I)
    if m: out["annual_rent"] = int(m.group(1).replace(",", ""))
    m = re.search(r"\bsecurity\s+deposit\b.*?aed\s*([0-9,]+)", t, re.I)
    if m: out["deposit"] = int(m.group(1).replace(",", ""))
    m = re.search(r"\bfrom\b\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|\d{1,2}\s+\w+\s+\d{4})", t, re.I)
    if m: out["start_date"] = _to_date_str(m.group(1))
    m = re.search(r"\b(to|until)\b\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|\d{1,2}\s+\w+\s+\d{4})", t, re.I)
    if m:
        out["end_date"] = _to_date_str(m.group(2))
        out["renewal_date"] = out["end_date"]
    m = re.search(r"\bproperty\s*type\b[:\-]?\s*(apartment|villa|townhouse|residential)", t, re.I)
    if m:
        p = m.group(1).lower()
        out["property_type"] = "apartment" if p == "residential" else p
    return out

# ===================== Law rules =====================
LAW_RULES = {
    "ejari_registration": {"law": "Law 26/2007 Art. 4(2): tenancy contracts & amendments must be registered with RERA (Ejari)."},
    "rent_review": {"law": "Law 33/2008 Art. 13 & Art. 14: renewal changes require 90-day notice unless otherwise agreed."},
    "maintenance_landlord": {"law": "Law 26/2007 Art. 16: landlord responsible for maintenance/repairs unless agreed otherwise."},
    "no_unilateral_termination": {"law": "Law 26/2007 Art. 7: lease cannot be unilaterally terminated during term except by consent or law."},
    "eviction_during_term": {"law": "Law 26/2007 Art. 25(1): limited grounds (non-payment after notice, illegal use, unsafe changes, etc.)."},
    "eviction_post_expiry_12m": {"law": "Law 26/2007 Art. 25(2): owner use/sale/works require 12-month notice via notary/registered post."},
    "decree_43_2013": {"law": "Decree 43/2013 Art. 1 & 3: rent-increase slabs relative to RERA average rental value."},
}

RULES_REGEX = [
    dict(
        label="Blanket/Discretionary rent increase",
        severity="high",
        regex=(
            r"(?:"
            r"\b(?:landlord'?s?\s+)?(?:sole|absolute)\s+discretion\b.*?\b(increase|adjust)\b.*?\brent\b"
            r"|"
            r"\b(?:increase|adjust)\b.*?\brent\b.*?\bat\s+(?:the\s+)?(?:landlord'?s?\s+)?(?:sole|absolute)\s+discretion\b"
            r"|"
            r"\brent\b.*?\bmay\s+be\s+(?:increased|adjusted)\b.*?\b(?:at\s+any\s+time|from\s+time\s+to\s+time)\b"
            r"|"
            r"\brent\b.*?\bmay\s+be\s+(?:increased|adjusted)\b.*?\b(?:as\s+the\s+landlord\s+deems\s+fit)\b"
            r"|"
            r"\brent\b.*?\bmay\s+be\s+(?:increased|adjusted)\b.*?\b(?:without\s+(?:cap|limit|reference\s+to\s+law|reference\s+to\s+decree))\b"
            r")"
        ),
        suggestion="Remove discretionary wording. Tie increases to Decree 43/2013 slabs / RERA calculator on renewal.",
        law_ref="decree_43_2013",
    ),
    dict(
        label="Eviction without proper notice",
        severity="high",
        regex=r"\bevict\b.*\bwithout\s+notice\b|\bterminate\b.*\bat\s+any\s*time\b",
        suggestion="Remove. Eviction follows strict grounds & notices (incl. 12-month notice for certain post-expiry cases).",
        law_ref="eviction_post_expiry_12m",
    ),
    dict(
        label="Expiry eviction without 12-month notice",
        severity="high",
        regex=r"\b(vacate|evict)\b.*\bon\s+expiry\b.*\bwithout\b.*\b(12|twelve)\b|\bno\s+further\s+notice\b.*\b(on|upon)\s+expiry\b",
        suggestion="Owner use/sale/rebuild/major works require 12-month notice via notary/registered post.",
        law_ref="eviction_post_expiry_12m",
    ),
    dict(
        label="90-day renewal notice present",
        severity="good",
        regex=r"\b(90|ninety)[-\s]?day(s)?\b.*\bnotice\b.*\b(renewal|increase|amend)\b",
        suggestion="Good: renewal changes require 90-day notice.",
        law_ref="rent_review",
    ),
    dict(
        label="All maintenance shifted to tenant",
        severity="medium",
        regex=r"\btenant\b.*\ball\s+maintenance\b",
        suggestion="Landlord generally handles major/structural unless agreed; clarify split.",
        law_ref="maintenance_landlord",
    ),
    dict(
        label="No Ejari registration reference",
        severity="info",
        regex=r"\bejari\b(?!\w)|\bregister(ed)?\b.*\b(rera|tenancy|contract)\b",
        suggestion="Contracts and amendments should be registered with RERA (Ejari).",
        law_ref="ejari_registration",
    ),
]

def _find_spans(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    bad, good = [], []
    norm = _normalize_text(text)
    for r in RULES_REGEX:
        for m in re.finditer(r["regex"], norm, flags=re.I):
            snippet = norm[m.start():m.end()]
            orig_start = text.lower().find(snippet.lower())
            orig_end = (orig_start + len(snippet)) if orig_start != -1 else m.end()
            if orig_start == -1: orig_start = m.start()
            span = {
                "issue": r["label"],
                "severity": r["severity"],
                "start": orig_start,
                "end": orig_end,
                "excerpt": text[orig_start:orig_end].strip(),
                "suggestion": r.get("suggestion"),
                "law": LAW_RULES.get(r.get("law_ref",""),{}).get("law"),
            }
            (good if r["severity"] == "good" else bad).append(span)
    return bad, good

# ===================== RERA slabs =====================
def allowed_increase_pct(current: float, avg: Optional[float]) -> int:
    if not avg or avg <= 0: return 0
    if current >= avg * 0.90: return 0
    if current >= avg * 0.80: return 5
    if current >= avg * 0.70: return 10
    if current >= avg * 0.60: return 15
    return 20

def lookup_rera_row(df: pd.DataFrame, *, city: str, area: str, property_type: str, bedrooms: int, furnished: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty: return None
    d = df.copy()
    for c in ("city","area","property_type"):
        if c in d.columns:
            d[c] = d[c].astype(str).str.strip().str.lower()
    def _norm(x): return (x or "").strip().lower()
    d = d[(d["city"] == _norm(city)) & (d["area"] == _norm(area)) & (d["property_type"] == _norm(property_type))]
    if d.empty: return None
    if "furnished" in d.columns:
        pref = d[d["furnished"].fillna("").str.lower() == _norm(furnished)]
        if not pref.empty: d = pref
    if {"bedrooms_min","bedrooms_max"}.issubset(d.columns):
        d = d[(d["bedrooms_min"] <= bedrooms) & (bedrooms <= d["bedrooms_max"])]
    if d.empty: return None
    if {"bedrooms_min","bedrooms_max"}.issubset(d.columns):
        d = d.assign(band=d["bedrooms_max"] - d["bedrooms_min"]).sort_values(["band","average_annual_rent_aed"])
    return d.head(1)

# ===================== Audit core =====================
def audit_clauses(clauses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in (clauses or []):
        raw = c.get("text","") or ""
        bad,_ = _find_spans(raw)
        out.append({
            "clause": c.get("num"),
            "text": raw,
            "verdict": "pass" if not bad else "fail",
            "issues": [b["issue"] for b in bad],
        })
    return out

def audit_contract(
    *,
    text: str,
    city: str,
    area: str,
    property_type: str,
    bedrooms: int,
    current_rent: float,
    proposed_rent: float,
    renewal_date: str,
    notice_sent_date: Optional[str],
    deposit: Optional[float],
    furnished: str,
    rera_avg_index: Optional[float],
    ejari_clauses: Optional[List[Dict[str, Any]]] = None,
    strict_mode: bool = False,
) -> Dict[str, Any]:

    highlights, positives = _find_spans(text)
    rule_flags: List[Dict[str, Any]] = []

    # 90-day renewal notice (Law 33/2008 Art. 14)
    if notice_sent_date:
        try:
            r = datetime.fromisoformat(renewal_date)
            n = datetime.fromisoformat(notice_sent_date)
            if (r - n).days < 90:
                rule_flags.append({
                    "label":"notice_lt_90",
                    "issue":"Notice period < 90 days",
                    "severity":"high",
                    "law": LAW_RULES["rent_review"]["law"],
                    "suggestion":"Ensure 90-day written notice before renewal changes.",
                })
        except Exception:
            rule_flags.append({"label":"notice_invalid","issue":"Invalid notice/renewal date format","severity":"low"})
    else:
        rule_flags.append({
            "label":"notice_missing",
            "issue":"No notice date provided.",
            "severity":"info",
            "suggestion":"Enter the date written notice was sent/received."
        })

    # Deposit heuristic (info/medium)
    if deposit and deposit > 0 and current_rent > 0:
        soft = 0.10 if (furnished or "").lower()=="furnished" else 0.08
        if deposit > soft*current_rent:
            rule_flags.append({
                "label":"deposit_high",
                "issue":f"Deposit appears high vs common practice ({deposit:.0f} AED)",
                "severity":"medium",
                "suggestion":"Typical range ≈5–10% depending on furnishings.",
            })

    # Decree 43/2013 slabs
    allowed_pct = allowed_increase_pct(current_rent, rera_avg_index)
    proposed_pct = ((proposed_rent - current_rent)/max(current_rent,1))*100.0
    if rera_avg_index and proposed_pct > allowed_pct:
        rule_flags.append({
            "label":"over_cap",
            "issue":f"Proposed increase {proposed_pct:.1f}% exceeds allowed {allowed_pct}%.",
            "severity":"high",
            "law": LAW_RULES["decree_43_2013"]["law"],
            "suggestion":"Adjust to within the slab derived from the RERA index.",
        })

    clause_results = audit_clauses(ejari_clauses or [])
    clauses_ok = all(c["verdict"] == "pass" for c in clause_results)

    if strict_mode:
        fail = bool(highlights or rule_flags or not clauses_ok)
    else:
        has_blocking_text = any(h.get("severity") == "high" for h in highlights)
        has_blocking_rules = any(r.get("severity") == "high" for r in rule_flags)
        fail = (has_blocking_text or has_blocking_rules or not clauses_ok)

    verdict = "fail" if fail else "pass"

    return {
        "verdict": verdict,
        "highlights": highlights,
        "valid_points": positives,
        "rule_flags": rule_flags,
        "allowed_increase": {
            "avg_index": rera_avg_index,
            "max_allowed_pct": allowed_pct,
            "proposed_pct": proposed_pct,
        },
        "ejari_clause_results": clause_results,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

# ===================== Highlighting & report =====================
def _merge_markers(m: List[Tuple[int,int,str]]) -> List[List[Any]]:
    if not m: return []
    m = sorted(m, key=lambda x:(x[0], -x[1]))
    out: List[List[Any]] = []
    for s,e,k in m:
        if not out: out.append([s,e,k]); continue
        ps,pe,pk = out[-1]
        if s <= pe:
            out[-1][1] = max(pe, e)
            out[-1][2] = "bad" if ("bad" in (k, pk)) else "good"
        else:
            out.append([s,e,k])
    return out

def render_highlighted_html(text: str, result: Dict[str, Any]) -> str:
    markers: List[Tuple[int,int,str]] = []
    for h in result.get("highlights", []): markers.append((h["start"], h["end"], "bad"))
    for g in result.get("valid_points", []): markers.append((g["start"], g["end"], "good"))
    merged = _merge_markers(markers)
    def esc(s:str)->str: return s.replace("&","&amp;").replace("<","&lt;").replace(">",">")
    # (fix escaping typo)
    def esc(s:str)->str: return s.replace("&","&amp;").replace("<","&lt;").replace(">", "&gt;")
    parts: List[str] = []; i = 0
    for s,e,k in merged:
        if i < s: parts.append(esc(text[i:s]))
        seg = esc(text[s:e]); cls = "bad" if k=="bad" else "good"
        parts.append(f'<mark class="{cls}">{seg}</mark>'); i = e
    if i < len(text): parts.append(esc(text[i:]))
    style = (
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5;padding:10px;}"
        "mark.bad{background:#ffe2e2;padding:0 2px;border-radius:3px;}"
        "mark.good{background:#e3ffe6;padding:0 2px;border-radius:3px;}"
        "</style>"
    )
    return style + "<div>" + "".join(parts).replace("\n","<br>") + "</div>"

def build_report_html(text: str, result: Dict[str, Any]) -> str:
    head = (
        "<meta charset='utf-8'>"
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px;}"
        "h1{margin:0 0 8px;} .meta{color:#666;margin-bottom:16px;}"
        "mark.bad{background:#ffe2e2;padding:0 2px;border-radius:3px;}"
        "mark.good{background:#e3ffe6;padding:0 2px;border-radius:3px;}"
        "table{border-collapse:collapse;width:100%;} td,th{border:1px solid #ddd;padding:8px;}"
        "</style>"
    )
    html: List[str] = ["<html><head>", head, "</head><body>"]
    html.append("<h1>Dubai Tenancy Audit Report</h1>")
    html.append(f"<div class='meta'>Generated at {result['timestamp']}</div>")
    verdict = result.get("verdict","")
    badge = "background:#e7f8ec;" if verdict=="pass" else "background:#ffefef;"
    html.append(f"<div style='padding:10px;border-radius:6px;{badge}'>Verdict: <b>{verdict.upper()}</b></div>")
    ai = result.get("allowed_increase", {})
    html.append("<h2>Rent Increase Summary (Decree 43/2013)</h2>")
    html.append("<table><tr><th>RERA Avg (AED)</th><th>Max Allowed %</th><th>Proposed %</th></tr>")
    html.append(f"<tr><td>{ai.get('avg_index') or '—'}</td><td>{ai.get('max_allowed_pct')}</td><td>{ai.get('proposed_pct'):.1f}</td></tr></table>")
    if result.get("ejari_clause_results"):
        html.append("<h2>Ejari Clause Findings</h2>")
        html.append("<table><tr><th>#</th><th>Verdict</th><th>Issues</th><th>Text</th></tr>")
        for c in result["ejari_clause_results"]:
            html.append(
                f"<tr><td>{c.get('clause')}</td><td>{c.get('verdict')}</td>"
                f"<td>{', '.join(c.get('issues', [])) or '—'}</td><td>{c.get('text','')}</td></tr>"
            )
        html.append("</table>")
    html.append("<h2>Text Findings</h2><ul>")
    for h in result.get("highlights", []):
        html.append(f"<li><b>{h['issue']}</b> — <i>{h['excerpt']}</i><br><small>{(h.get('law')+' — ') if h.get('law') else ''}{h.get('suggestion','')}</small></li>")
    for r in result.get("rule_flags", []):
        html.append(f"<li><b>{r['issue']}</b><br><small>{(r.get('law')+' — ') if r.get('law') else ''}{r.get('suggestion','')}</small></li>")
    html.append("</ul>")
    html.append("<h2>Annotated Contract</h2>")
    html.append(render_highlighted_html(text, result))
    html.append("</body></html>")
    return "".join(html)

# ===================== Sample PDF =====================
def generate_sample_ejari_pdf() -> io.BytesIO:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w,h = A4; margin = 1.5*cm
    c.setFont("Helvetica-Bold", 18); c.drawString(margin, h-margin, "TENANCY CONTRACT (Sample)")
    c.setFont("Helvetica", 10); c.drawString(margin, h-margin-14, "Government of Dubai — Land Department (Demo Layout)")
    y = h - margin - 40; line = 14; c.setFont("Helvetica", 11)
    fields = [
        "Property Usage: Residential   Property Type: apartment   Bedrooms: 2",
        "Location (Area): Dubai Marina",
        "Contract Period: From 2025-11-01   To 2026-10-31",
        "Annual Rent: AED 120,000      Security Deposit Amount: AED 10,000",
        "Mode of Payment: 4 cheques",
    ]
    for f in fields: c.drawString(margin, y, f); y -= line
    y -= 10; c.setFont("Helvetica-Bold", 12); c.drawString(margin, y, "Terms & Conditions:"); y -= line
    c.setFont("Helvetica", 10)
    import textwrap
    clauses = [
        "1) The tenant has inspected the premises and agreed to lease them.",
        "2) The tenant shall pay utility charges as agreed in writing.",
        "3) The landlord may evict the tenant at any time without notice.",
        "4) Rent may be increased at the landlord’s absolute discretion.",
        "5) A ninety-day notice is required before renewal to amend rent or terms.",
    ]
    for cl in clauses:
        for wline in textwrap.wrap(cl, width=100):
            if y < margin + 40: c.showPage(); y = h - margin; c.setFont("Helvetica", 10)
            c.drawString(margin, y, wline); y -= line
    y -= 10; c.setFont("Helvetica", 10)
    c.rect(margin, y-60, 7.5*cm, 60); c.rect(margin+9*cm, y-60, 7.5*cm, 60)
    c.drawString(margin+1*cm, y-65, "Tenant Signature"); c.drawString(margin+10*cm, y-65, "Landlord Signature")
    c.showPage(); c.save(); buf.seek(0); return buf
