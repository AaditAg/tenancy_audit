# audit_engine.py — parsing, law checks, RERA slabs, highlighting, Firebase ledger utils
# -----------------------------------------------------------------------------
from __future__ import annotations
import io
import re
import json
import hashlib
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple, Any

from pdfminer.high_level import extract_text
from dateutil.parser import parse as dtparse

# OCR deps
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

# Firebase admin (optional)
_FB_INIT = False
try:
    import firebase_admin  # type: ignore
    from firebase_admin import credentials, firestore  # type: ignore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

# ===================== General helpers =====================
def to_date(value: str | date | None) -> date:
    from datetime import date as _date
    if value is None:
        return _date(2025, 12, 1)
    if isinstance(value, _date):
        return value
    try:
        return dtparse(str(value)).date()
    except Exception:
        return _date(2025, 12, 1)

def load_rera_csv(file_like) -> pd.DataFrame:
    raw = file_like.read()
    if isinstance(raw, bytes):
        enc = chardet.detect(raw).get("encoding") or "utf-8"
        df = pd.read_csv(io.BytesIO(raw), encoding=enc)
    else:
        df = pd.read_csv(file_like)
    df.columns = [c.strip().lower() for c in df.columns]
    must = {"city", "area", "property_type", "bedrooms_min", "bedrooms_max", "average_annual_rent_aed"}
    miss = must - set(df.columns)
    if miss:
        raise ValueError(f"CSV missing columns: {sorted(list(miss))}")
    return df

def merge_prefill(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(fallback or {})
    for k, v in (primary or {}).items():
        if v is not None:
            out[k] = v
    return out

# ===================== Normalization & regex =====================
def _normalize_text(s: str) -> str:
    if not s:
        return s
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
    if len(text.strip()) < 120:  # likely scanned
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

    m = EJ_LABELS["annual_rent"].search(t);   rent = _to_int(m.group(1)) if m else None
    m = EJ_LABELS["deposit"].search(t);       dep = _to_int(m.group(1)) if m else None
    m = EJ_LABELS["from"].search(t);          start = _to_date_str(m.group(1)) if m else None
    m = EJ_LABELS["to"].search(t);            end = _to_date_str(m.group(2) if m and m.lastindex>=2 else None)
    m = EJ_LABELS["bed"].search(t)
    if m:
        v = m.group(1).lower()
        beds = 0 if v == "studio" else _to_int(v)
    m = EJ_LABELS["area"].search(t);          area = (m.group(2).strip() if m else None)
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

# ===================== Laws & rules =====================
LAW_RULES = {
    "ejari_registration": {
        "law": "Law 26/2007 Art. 4(2): tenancy contracts & amendments must be registered with RERA (Ejari)."
    },
    "rent_review": {
        "law": "Law 33/2008 Art. 13 & Art. 14: renewal changes require 90-day notice unless otherwise agreed."
    },
    "maintenance_landlord": {
        "law": "Law 26/2007 Art. 16: landlord responsible for maintenance/repairs unless agreed otherwise."
    },
    "no_unilateral_termination": {
        "law": "Law 26/2007 Art. 7: lease cannot be unilaterally terminated during term except by consent or law."
    },
    "eviction_during_term": {
        "law": "Law 26/2007 Art. 25(1): limited grounds (non-payment after notice, illegal use, unsafe changes, etc.)."
    },
    "eviction_post_expiry_12m": {
        "law": "Law 26/2007 Art. 25(2): owner use/sale/works require 12-month notice via notary/registered post."
    },
    "decree_43_2013": {
        "law": "Decree 43/2013 Art. 1 & 3: rent-increase slabs relative to RERA average rental value."
    },
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
        suggestion="Remove. Eviction follows strict grounds & notices (incl. 12-month notice post-expiry in certain cases).",
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

    # Deposit heuristic
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
            out[-1][2] = "bad" if ("bad" in
