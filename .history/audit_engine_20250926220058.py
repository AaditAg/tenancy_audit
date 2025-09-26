# audit_engine.py — Ejari-aware audit engine with OCR fallback + RERA CSV
# ---------------------------------------------------------------------------------
# Educational prototype — not legal advice.
# Provides:
#   - parse_pdf_smart(pdf_bytes): text via pdfminer, auto-fallback to OCR (pdf2image + pytesseract)
#   - Ejari field/terms parser: extract top fields + numbered clauses
#   - Rules (Law 26/2007, Law 33/2008, Decree 43/2013) with pinpointed highlights
#   - RERA CSV lookup helpers (called from app.py)
#   - Sample Ejari-style PDF generator for demos

from __future__ import annotations
import io
import re
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple, Any

# Text & PDF utils
from pdfminer.high_level import extract_text
from dateutil.parser import parse as dtparse

# OCR (install tesseract & pdf2image + pillow)
try:
    import pytesseract
    from pdf2image import convert_from_bytes
    from PIL import Image
    _OCR_READY = True
except Exception:
    _OCR_READY = False

# PDF sample generator
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

# Optional NLP (sentence splitting nicer, not required)
try:
    import spacy
    _NLP = spacy.load("en_core_web_sm")
except Exception:
    _NLP = None


# =========================
# Generic helpers
# =========================
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


def merge_prefill(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer primary keys; fill missing with fallback."""
    out = dict(fallback or {})
    for k, v in (primary or {}).items():
        if v is not None:
            out[k] = v
    return out


# =========================
# Text parsing regexes
# =========================
_MONEY_RE = re.compile(
    r"(?:(?:AED|DHS|د\.إ)\s*)?([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\s*/\s*(month|mo|year|yr))?",
    re.I,
)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})")
_BED_RE = re.compile(r"(studio|\b(\d+)\s*bed(room)?s?)", re.I)
_AREA_RE = re.compile(
    r"\b(Downtown Dubai|Jumeirah Village Circle|Dubai Marina|Business Bay|JLT|Jumeirah|Al Barsha)\b",
    re.I,
)

# Ejari specific label cues (English, simplified)
EJ_LABELS = {
    "annual_rent": re.compile(r"\b(annual\s+rent|contract\s+value)\b.*?aed\s*([0-9,]+)", re.I),
    "deposit": re.compile(r"\b(security\s+deposit)\b.*?aed\s*([0-9,]+)", re.I),
    "contract_from": re.compile(r"\bfrom\b\s*[:\-]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|\d{1,2}\s+\w+\s+\d{4})", re.I),
    "contract_to": re.compile(r"\b(to|until)\b\s*[:\-]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|\d{1,2}\s+\w+\s+\d{4})", re.I),
    "property_type": re.compile(r"\bproperty\s*type\b\s*[:\-]?\s*(apartment|villa|townhouse|residential)", re.I),
    "bedrooms": re.compile(r"\bbedroom[s]?:?\s*(studio|\d+)\b", re.I),
    "area": re.compile(r"\b(area|location)\b\s*[:\-]?\s*([A-Za-z ]{3,})", re.I),
}

EJ_TERMS_HEADER = re.compile(r"terms\s*&?\s*conditions", re.I)
EJ_CLAUSE = re.compile(r"^\s*([0-9]{1,2})\)\s*(.+)$")


# =========================
# Text heuristics (fallback)
# =========================
def _parse_all_amounts(text: str) -> List[int]:
    vals: List[int] = []
    for m in _MONEY_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            amt = int(raw)
        except Exception:
            continue
        period = (m.group(2) or "").lower()
        if period.startswith("mo"):
            amt *= 12
        vals.append(amt)
    return vals


def _parse_first_date(text: str, default: Optional[str] = None) -> Optional[str]:
    m = _DATE_RE.search(text)
    if not m:
        return default
    try:
        return dtparse(m.group(1)).date().isoformat()
    except Exception:
        return default


def _parse_bedrooms(text: str, default: int = 1) -> int:
    m = _BED_RE.search(text)
    if not m:
        return default
    if m.group(1) and m.group(1).lower() == "studio":
        return 0
    if m.group(2):
        try:
            return int(m.group(2))
        except Exception:
            return default
    return default


def _parse_area(text: str, default: str = "Jumeirah Village Circle") -> str:
    m = _AREA_RE.search(text)
    return m.group(0) if m else default


def autofill_from_text(text: str) -> Dict[str, Any]:
    """Heuristic extraction from contract text to prefill UI fields."""
    vals = _parse_all_amounts(text)
    vals_sorted = sorted(vals, reverse=True)
    current = vals_sorted[0] if vals_sorted else 55000
    proposed = vals_sorted[1] if len(vals_sorted) > 1 else max(current + 10000, int(current * 1.1))

    # deposit heuristic: keyword window
    dep = None
    for m in _MONEY_RE.finditer(text):
        window = text[max(0, m.start() - 40) : m.end() + 40].lower()
        if "deposit" in window:
            dep = int(m.group(1).replace(",", ""))
            break

    return {
        "area": _parse_area(text),
        "bedrooms": _parse_bedrooms(text),
        "current_rent": current,
        "proposed_rent": proposed,
        "deposit": dep if dep is not None else int(current * 0.1),
        "renewal_date": _parse_first_date(text, "2025-12-01"),
        "notice_sent_date": _parse_first_date(text, "2025-09-10"),
    }


# =========================
# Smart PDF extraction (text → OCR)
# =========================
def _ocr_pdf_to_text(pdf_bytes: bytes, lang: str = "eng") -> str:
    """Convert PDF pages to images, OCR each, join text."""
    if not _OCR_READY:
        return ""
    pages = convert_from_bytes(pdf_bytes, dpi=300)
    out = []
    for img in pages:
        if not isinstance(img, Image.Image):
            img = img.convert("RGB")
        t = pytesseract.image_to_string(img, lang=lang)
        out.append(t)
    return "\n".join(out)


def parse_pdf_smart(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Try pdfminer first; if too little text, fallback to OCR.
    Also attempt Ejari top-field & terms parsing.
    Returns:
      {
        "text": "...",
        "ejari": {
           "annual_rent": int|None, "deposit": int|None,
           "start_date": "YYYY-MM-DD"|None, "end_date": "YYYY-MM-DD"|None,
           "bedrooms": int|None, "area": str|None, "property_type": str|None,
           "clauses": [{"num": 1, "text": "..."}] | []
        }
      }
    """
    # 1) try pdfminer
    text = ""
    try:
        text = extract_text(io.BytesIO(pdf_bytes))
    except Exception:
        text = ""

    miner_len = len(text.strip())
    if miner_len < 120:  # likely an image-based Ejari scan
        ocr_text = _ocr_pdf_to_text(pdf_bytes, lang="eng")
        # If OCR failed entirely, keep whatever we got
        text = ocr_text or text

    ejari_struct = parse_ejari_text(text)
    return {"text": text, "ejari": ejari_struct}


# =========================
# Ejari text parser
# =========================
def _safe_int(token: str | None) -> Optional[int]:
    if not token:
        return None
    try:
        return int(str(token).replace(",", "").strip())
    except Exception:
        return None


def _safe_date(token: str | None) -> Optional[str]:
    if not token:
        return None
    try:
        return dtparse(token).date().isoformat()
    except Exception:
        return None


def parse_ejari_text(text: str) -> Dict[str, Any]:
    """
    Parse Ejari-style English blocks. We use label cues (Annual Rent, Security Deposit Amount, From/To, Bedrooms, etc.)
    and the 'Terms & Conditions' numbered list.
    """
    if not text:
        return {}

    upper = text  # keep case; labels regex is case-insensitive

    # Top fields
    annual_rent = None
    deposit = None
    start_date = None
    end_date = None
    prop_type = None
    bedrooms = None
    area = None

    m = EJ_LABELS["annual_rent"].search(upper)
    if m:
        annual_rent = _safe_int(m.group(2))

    m = EJ_LABELS["deposit"].search(upper)
    if m:
        deposit = _safe_int(m.group(2))

    m = EJ_LABELS["contract_from"].search(upper)
    if m:
        start_date = _safe_date(m.group(1))

    m = EJ_LABELS["contract_to"].search(upper)
    if m:
        # group 2 when using (to|until)
        end_date = _safe_date(m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1))

    m = EJ_LABELS["property_type"].search(upper)
    if m:
        p = m.group(1).lower()
        if p == "residential":
            p = "apartment"  # the Ejari header often says "Residential"; map to apartment for demo
        prop_type = p

    m = EJ_LABELS["bedrooms"].search(upper)
    if m:
        b = m.group(1).lower()
        bedrooms = 0 if b == "studio" else _safe_int(b)

    m = EJ_LABELS["area"].search(upper)
    if m:
        area = m.group(2).strip()

    # Terms & Conditions
    clauses: List[Dict[str, Any]] = []
    terms_idx = None
    for m in EJ_TERMS_HEADER.finditer(upper):
        terms_idx = m.start()
        break

    if terms_idx is not None:
        tail = upper[terms_idx:]
        for line in tail.splitlines():
            cm = EJ_CLAUSE.match(line)
            if cm:
                num = int(cm.group(1))
                txt = cm.group(2).strip()
                if txt:
                    clauses.append({"num": num, "text": txt})

    # If we still didn’t get fields but the text is in the demo format we generate, try a looser pass:
    if annual_rent is None:
        loose = re.search(r"annual\s+rent[:\-]?\s*aed\s*([0-9,]+)", upper, re.I)
        if loose:
            annual_rent = _safe_int(loose.group(1))
    if deposit is None:
        loose = re.search(r"security\s+deposit[:\-]?\s*aed\s*([0-9,]+)", upper, re.I)
        if loose:
            deposit = _safe_int(loose.group(1))
    if start_date is None or end_date is None:
        rng = re.search(r"contract\s+period[:\-]?.*?from[:\-]?\s*([^\s]+).*?(to|until)[:\-]?\s*([^\s]+)", upper, re.I)
        if rng:
            start_date = _safe_date(rng.group(1)) or start_date
            end_date = _safe_date(rng.group(3)) or end_date

    # Build struct
    out = {
        "current_rent": annual_rent,
        "deposit": deposit,
        "renewal_date": end_date or None,  # renewal triggers at end date
        "notice_sent_date": None,          # not in the Ejari header; user can fill or we heuristic later
        "bedrooms": bedrooms,
        "area": area,
        "property_type": prop_type,
        "clauses": clauses,
    }
    # Clean Nones
    clean = {k: v for k, v in out.items() if v is not None and v != ""}
    return clean


# =========================
# Law references & regex rules (text)
# =========================
LAW_RULES: Dict[str, Dict[str, str]] = {
    "notice_90_days": {
        "desc": "90-day prior written notice required to amend terms (incl. rent) on renewal.",
        "law": "Law 26/2007 as amended by Law 33/2008",
    },
    "eviction_12_months": {
        "desc": "12-month notice via notary/registered mail for certain evictions (sale, personal use, major works).",
        "law": "Law 26/2007 Art. 25 (as amended)",
    },
    "decree_43_2013": {
        "desc": "Rent increase slabs based on gap vs. RERA average (0/5/10/15/20%).",
        "law": "Decree No. 43 of 2013 (Dubai)",
    },
    "maintenance_default": {
        "desc": "Landlord typically responsible for major/structural maintenance unless otherwise agreed.",
        "law": "Practice; see Law 26/2007 Art. 16 (interpretations vary)",
    },
}

RULES_REGEX: List[Dict[str, Any]] = [
    dict(
        label="Eviction without notice",
        severity="high",
        regex=r"\bevict\b.*\bwithout\s+notice\b",
        suggestion="Remove ‘without notice’. Evictions require proper legal notice (often 12 months).",
        law_ref="eviction_12_months",
    ),
    dict(
        label="Arbitrary termination",
        severity="high",
        regex=r"\b(terminate|end)\b.*\bany\s*time\b",
        suggestion="Specify lawful grounds; arbitrary termination is problematic.",
        law_ref="eviction_12_months",
    ),
    dict(
        label="All maintenance on tenant",
        severity="medium",
        regex=r"\btenant\b.*\ball\s+maintenance\b",
        suggestion="Reallocate: landlord covers major/structural by default.",
        law_ref="maintenance_default",
    ),
    dict(
        label="90-day notice present",
        severity="good",
        regex=r"\b(90|ninety)[-\s]?day(s)?\b.*\bnotice\b",
        suggestion="Good: 90-day notice clause present.",
        law_ref="notice_90_days",
    ),
    dict(
        label="Blanket rent increase wording",
        severity="high",
        regex=r"\brent may be increased\b.*\b(absolute discretion|any amount|without reference)\b",
        suggestion="Tie increases to Decree 43/2013 slabs; remove blanket authority.",
        law_ref="decree_43_2013",
    ),
]


def _find_spans(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    invalid: List[Dict[str, Any]] = []
    valid: List[Dict[str, Any]] = []
    for r in RULES_REGEX:
        for m in re.finditer(r["regex"], text, flags=re.I | re.S):
            span = {
                "issue": r["label"],
                "severity": r["severity"],
                "start": m.start(),
                "end": m.end(),
                "excerpt": text[m.start() : m.end()].strip(),
                "suggestion": r.get("suggestion"),
                "law": LAW_RULES.get(r.get("law_ref", ""), {}).get("law"),
            }
            if r["severity"] == "good":
                valid.append(span)
            else:
                invalid.append(span)
    return invalid, valid


# =========================
# RERA CSV lookup helpers
# =========================
def _norm(s: str) -> str:
    return (s or "").strip().lower()


def lookup_rera_row(
    df: "pd.DataFrame",
    *,
    city: str,
    area: str,
    property_type: str,
    bedrooms: int,
    furnished: str,
) -> Optional["pd.DataFrame"]:
    """Pick the row that matches city/area/type and bedrooms ∈ [min,max].
    If multiple rows match, return the one with the narrowest bedroom band (smallest range)."""
    if df is None or df.empty:
        return None
    d = df.copy()
    for c in ("city", "area", "property_type"):
        if c in d.columns:
            d[c] = d[c].astype(str).str.strip().str.lower()
    d = d[(d["city"] == _norm(city)) & (d["area"] == _norm(area)) & (d["property_type"] == _norm(property_type))]
    if d.empty:
        return None
    if "furnished" in d.columns:
        pref = d[d["furnished"].fillna("").str.lower() == _norm(furnished)]
        if not pref.empty:
            d = pref
    d = d[(d["bedrooms_min"] <= bedrooms) & (bedrooms <= d["bedrooms_max"])]
    if d.empty:
        return None
    if {"bedrooms_min", "bedrooms_max"}.issubset(d.columns):
        d = d.assign(band=(d["bedrooms_max"] - d["bedrooms_min"]))
        d = d.sort_values(by=["band", "average_annual_rent_aed"], ascending=[True, True])
    return d.head(1)


def allowed_increase_pct(current: float, avg: Optional[float]) -> int:
    """Return 0/5/10/15/20 based on gap of current vs. average (per Decree 43/2013)."""
    if not avg or avg <= 0:
        return 0
    if current >= avg * 0.90:
        return 0
    if current >= avg * 0.80:
        return 5
    if current >= avg * 0.70:
        return 10
    if current >= avg * 0.60:
        return 15
    return 20


# =========================
# Clause-by-clause (Ejari terms) audit
# =========================
def audit_clauses(clauses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run RULES_REGEX against every clause text separately; return verdict per clause."""
    out = []
    for c in (clauses or []):
        txt = c.get("text", "")
        hits_invalid, hits_valid = _find_spans(txt)
        issue = "pass" if not hits_invalid else "fail"
        out.append({
            "clause": c.get("num"),
            "text": txt,
            "verdict": issue,
            "issues": [h["issue"] for h in hits_invalid],
        })
    return out


# =========================
# Main audit function
# =========================
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
) -> Dict[str, Any]:
    invalid_spans, valid_spans = _find_spans(text)
    rule_flags: List[Dict[str, Any]] = []

    # 90-day notice check
    if notice_sent_date:
        try:
            r = datetime.fromisoformat(renewal_date)
            n = datetime.fromisoformat(notice_sent_date)
            days = (r - n).days
            if days < 90:
                rule_flags.append(
                    {
                        "label": "notice_lt_90",
                        "issue": f"Notice period < 90 days ({days} days)",
                        "severity": "high",
                        "law": LAW_RULES["notice_90_days"]["law"],
                        "suggestion": "Provide/require at least 90 days written notice before renewal.",
                    }
                )
        except Exception:
            rule_flags.append(
                {"label": "notice_invalid_date", "issue": "Invalid date format (use YYYY-MM-DD)", "severity": "low"}
            )
    else:
        rule_flags.append(
            {
                "label": "notice_missing",
                "issue": "No notice date provided",
                "severity": "medium",
                "law": LAW_RULES["notice_90_days"]["law"],
                "suggestion": "Capture the date the notice was sent/received.",
            }
        )

    # Deposit (soft practice guidance)
    if deposit and deposit > 0:
        # Common practice: ~5% unfurnished, ~10% furnished (not a statutory cap).
        soft_cap = 0.10 if furnished.lower() == "furnished" else 0.08
        if deposit > soft_cap * current_rent:
            rule_flags.append(
                {
                    "label": "deposit_high",
                    "issue": f"Security deposit {deposit:.0f} AED appears high vs market practice",
                    "severity": "medium",
                    "suggestion": "Typical range ≈5–10% depending on furnishings.",
                }
            )

    # Decree 43/2013: compute max allowed vs. CSV average
    allowed_pct = allowed_increase_pct(current_rent, rera_avg_index)
    proposed_pct = ((proposed_rent - current_rent) / max(current_rent, 1)) * 100.0
    if rera_avg_index and proposed_pct > allowed_pct:
        rule_flags.append(
            {
                "label": "increase_over_cap",
                "issue": f"Proposed increase {proposed_pct:.1f}% exceeds allowed {allowed_pct}% (Decree 43/2013).",
                "severity": "high",
                "law": LAW_RULES["decree_43_2013"]["law"],
                "suggestion": "Adjust to within slab calculated from the official index.",
            }
        )

    # Clause-by-clause audit (Ejari terms)
    clause_results = audit_clauses(ejari_clauses or [])

    verdict = "pass" if (not invalid_spans and not rule_flags and all(c["verdict"] == "pass" for c in clause_results)) else "fail"

    return {
        "verdict": verdict,
        "highlights": invalid_spans,
        "valid_points": valid_spans,
        "rule_flags": rule_flags,
        "allowed_increase": {
            "avg_index": rera_avg_index,
            "max_allowed_pct": allowed_pct,
            "proposed_pct": proposed_pct,
        },
        "ejari_clause_results": clause_results,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# =========================
# Highlight / Report renderers
# =========================
def _merge_markers(markers: List[Tuple[int, int, str]]) -> List[List[Any]]:
    """Merge overlapping spans. 'bad' has priority over 'good'."""
    if not markers:
        return []
    markers = sorted(markers, key=lambda x: (x[0], -x[1]))
    merged: List[List[Any]] = []
    for s, e, kind in markers:
        if not merged:
            merged.append([s, e, kind])
            continue
        ps, pe, pk = merged[-1]
        if s <= pe:
            if kind == "bad" or pk == "bad":
                merged[-1][1] = max(pe, e)
                merged[-1][2] = "bad"
            else:
                merged[-1][1] = max(pe, e)
        else:
            merged.append([s, e, kind])
    return merged


def render_highlighted_html(text: str, result: Dict[str, Any]) -> str:
    markers: List[Tuple[int, int, str]] = []
    for h in result.get("highlights", []):
        markers.append((h["start"], h["end"], "bad"))
    for g in result.get("valid_points", []):
        markers.append((g["start"], g["end"], "good"))

    merged = _merge_markers(markers)

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts: List[str] = []
    i = 0
    for s, e, k in merged:
        if i < s:
            parts.append(esc(text[i:s]))
        seg = esc(text[s:e])
        cls = "bad" if k == "bad" else "good"
        parts.append(f'<mark class="{cls}">{seg}</mark>')
        i = e
    if i < len(text):
        parts.append(esc(text[i:]))

    style = (
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5;padding:10px;}"
        "mark.bad{background:#ffe2e2;padding:0 2px;border-radius:3px;}"
        "mark.good{background:#e3ffe6;padding:0 2px;border-radius:3px;}"
        "</style>"
    )
    return style + "<div>" + "".join(parts).replace("\n", "<br>") + "</div>"


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

    verdict = result.get("verdict", "")
    badge = "background:#e7f8ec;" if verdict == "pass" else "background:#ffefef;"
    html.append(f"<div style='padding:10px;border-radius:6px;{badge}'>Verdict: <b>{verdict.upper()}</b></div>")

    ai = result.get("allowed_increase", {})
    html.append("<h2>Rent Increase Summary (Decree 43/2013)</h2>")
    html.append("<table><tr><th>RERA Avg (AED)</th><th>Max Allowed %</th><th>Proposed %</th></tr>")
    html.append(
        f"<tr><td>{ai.get('avg_index') or '—'}</td>"
        f"<td>{ai.get('max_allowed_pct')}</td>"
        f"<td>{ai.get('proposed_pct'):.1f}</td></tr></table>"
    )

    # Ejari clause findings table
    if result.get("ejari_clause_results"):
        html.append("<h2>Ejari Clause Findings</h2>")
        html.append("<table><tr><th>#</th><th>Verdict</th><th>Issues</th><th>Text</th></tr>")
        for c in result["ejari_clause_results"]:
            issues = ", ".join(c.get("issues", []))
            html.append(
                f"<tr><td>{c.get('clause')}</td><td>{c.get('verdict')}</td>"
                f"<td>{issues or '—'}</td><td>{c.get('text','')}</td></tr>"
            )
        html.append("</table>")

    html.append("<h2>Text Findings</h2><ul>")
    for h in result.get("highlights", []):
        html.append(
            f"<li><b>{h['issue']}</b> — <i>{h['excerpt']}</i>"
            f"<br><small>{(h.get('law') + ' — ') if h.get('law') else ''}{h.get('suggestion','')}</small></li>"
        )
    for r in result.get("rule_flags", []):
        html.append(
            f"<li><b>{r['issue']}</b>"
            f"<br><small>{(r.get('law') + ' — ') if r.get('law') else ''}{r.get('suggestion','')}</small></li>"
        )
    html.append("</ul>")

    html.append("<h2>Annotated Contract</h2>")
    html.append(render_highlighted_html(text, result))

    html.append("</body></html>")
    return "".join(html)


# =========================
# Demo: Ejari-style PDF generator
# =========================
def generate_sample_ejari_pdf() -> io.BytesIO:
    """
    Create a simple Ejari-like bilingual header with top boxes and an English 'Terms & Conditions'
    section containing a few clauses (some intentionally problematic).
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    margin = 1.5 * cm

    # Header
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, h - margin, "TENANCY CONTRACT")
    c.setFont("Helvetica", 10)
    c.drawString(margin, h - margin - 14, "Government of Dubai — Land Department (Demo Layout)")

    # Top fields (Ejari-style)
    y = h - margin - 40
    line = 14
    c.setFont("Helvetica", 11)
    fields = [
        "Property Usage: Residential   Property Type: apartment   Bedrooms: 1",
        "Location (Area): Jumeirah Village Circle",
        "Contract Period: From 2025-12-01   To 2026-11-30",
        "Annual Rent: AED 55,000      Security Deposit Amount: AED 9,000",
        "Mode of Payment: 12 cheques",
    ]
    for f in fields:
        c.drawString(margin, y, f)
        y -= line

    # Terms & Conditions
    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Terms & Conditions:")
    y -= line
    c.setFont("Helvetica", 10)

    clauses = [
        "1) The tenant has inspected the premises and agreed to lease them.",
        "2) The tenant shall pay utility charges as agreed in writing.",
        "3) The landlord may evict the tenant at any time without notice.",
        "4) Rent may be increased at the landlord’s absolute discretion.",
        "5) A ninety-day notice is required before renewal to amend rent or terms.",
    ]

    # Simple text block rendering (wrap manually)
    import textwrap
    for cl in clauses:
        wrapped = textwrap.wrap(cl, width=100)
        for wline in wrapped:
            if y < margin + 40:
                c.showPage()
                y = h - margin
                c.setFont("Helvetica", 10)
            c.drawString(margin, y, wline)
            y -= line

    # Signatures boxes (just to mimic layout)
    y -= 10
    c.setFont("Helvetica", 10)
    c.rect(margin, y - 60, 7.5 * cm, 60)
    c.rect(margin + 9 * cm, y - 60, 7.5 * cm, 60)
    c.drawString(margin + 1 * cm, y - 65, "Tenant Signature")
    c.drawString(margin + 10 * cm, y - 65, "Landlord Signature")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf
