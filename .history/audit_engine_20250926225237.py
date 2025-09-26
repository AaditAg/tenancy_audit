# audit_engine.py — parsing & auditing core
# -----------------------------------------------------------------------------
from __future__ import annotations
import io
import re
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

# --------------------- helpers ---------------------
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

# --------------------- regexes ---------------------
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

# --------------------- parsing ---------------------
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
    if len(text.strip()) < 120:   # likely scanned
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

    # top fields
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

    # clauses
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

# --------------------- rules ---------------------
LAW_RULES = {
    "notice_90_days": {"law": "Law 26/2007 as amended by Law 33/2008"},
    "eviction_12_months": {"law": "Law 26/2007 Art. 25 (as amended)"},
    "decree_43_2013": {"law": "Decree No. 43 of 2013 (Dubai)"},
    "maintenance_default": {"law": "Practice; see Law 26/2007 Art. 16"},
}

RULES_REGEX = [
    dict(label="Eviction without notice", severity="high",
         regex=r"\bevict\b.*\bwithout\s+notice\b",
         suggestion="Remove ‘without notice’. Evictions require proper legal notice (often 12 months).",
         law_ref="eviction_12_months"),
    dict(label="Arbitrary termination", severity="high",
         regex=r"\b(terminate|end)\b.*\bany\s*time\b",
         suggestion="Specify lawful grounds; arbitrary termination is problematic.",
         law_ref="eviction_12_months"),
    dict(label="All maintenance on tenant", severity="medium",
         regex=r"\btenant\b.*\ball\s+maintenance\b",
         suggestion="Landlord typically covers major/structural maintenance.",
         law_ref="maintenance_default"),
    dict(label="90-day notice present", severity="good",
         regex=r"\b(90|ninety)[-\s]?day(s)?\b.*\bnotice\b",
         suggestion="Good: 90-day notice clause present.",
         law_ref="notice_90_days"),
    dict(label="Blanket rent increase wording", severity="high",
         regex=r"\brent may be increased\b.*\b(absolute discretion|any amount|without reference)\b",
         suggestion="Tie increases to Decree 43/2013 slabs; remove blanket authority.",
         law_ref="decree_43_2013"),
]

def _find_spans(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    bad, good = [], []
    for r in RULES_REGEX:
        for m in re.finditer(r["regex"], text, flags=re.I | re.S):
            span = {
                "issue": r["label"],
                "severity": r["severity"],
                "start": m.start(),
                "end": m.end(),
                "excerpt": text[m.start(): m.end()].strip(),
                "suggestion": r.get("suggestion"),
                "law": LAW_RULES.get(r.get("law_ref",""),{}).get("law"),
            }
            (good if r["severity"] == "good" else bad).append(span)
    return bad, good

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

def audit_clauses(clauses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in (clauses or []):
        txt = c.get("text","")
        bad,_ = _find_spans(txt)
        out.append({
            "clause": c.get("num"),
            "text": txt,
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
    strict_mode: bool = False,   # fail on any issue if True
) -> Dict[str, Any]:

    highlights, positives = _find_spans(text)
    rule_flags: List[Dict[str, Any]] = []

    # 90-day notice check
    if notice_sent_date:
        try:
            r = datetime.fromisoformat(renewal_date)
            n = datetime.fromisoformat(notice_sent_date)
            if (r - n).days < 90:
                rule_flags.append({
                    "label":"notice_lt_90",
                    "issue":"Notice period < 90 days",
                    "severity":"high",
                    "law": LAW_RULES["notice_90_days"]["law"],
                    "suggestion":"Ensure 90-day written notice before renewal changes.",
                })
        except Exception:
            rule_flags.append({"label":"notice_invalid","issue":"Invalid notice/renewal date format","severity":"low"})
    else:
        # informational — not blocking
        rule_flags.append({
            "label":"notice_missing",
            "issue":"No notice date provided.",
            "severity":"info",
            "suggestion":"Enter the date written notice was sent/received."
        })

    # Deposit soft practice check
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

    # Verdict calculation
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

# --------------------- highlighting/report ---------------------
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
    def esc(s:str)->str: return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
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

# --------------------- sample PDF ---------------------
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
