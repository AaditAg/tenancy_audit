# app.py — Dubai Tenancy Audit Microservice (with pinpointed highlights)
# Educational use. Not legal advice.

from __future__ import annotations
from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime
import re
import pdfminer.high_level

# Optional NLP for sentence splitting (safe if installed)
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = None

app = FastAPI(title="Dubai Tenancy Audit Service", version="0.2")

# --- Demo rent index (AED/yr) — replace with RERA index when you have it ---
RENT_INDEX = {
    ("Dubai", "Jumeirah Village Circle", "apartment", 1): 60000,
    ("Dubai", "Jumeirah Village Circle", "apartment", 2): 80000,
    ("Dubai", "Downtown Dubai", "apartment", 1): 90000,
    ("Dubai", "Downtown Dubai", "apartment", 2): 140000,
}

# --- Decree 43/2013 slab logic ---
def allowed_increase_pct(current: float, avg: float) -> int:
    if current >= avg * 0.90: return 0
    if current >= avg * 0.80: return 5
    if current >= avg * 0.70: return 10
    if current >= avg * 0.60: return 15
    return 20

# ---------- Input schemas ----------
class AuditRequest(BaseModel):
    text: str
    contract_city: str = "Dubai"
    area: str
    property_type: str
    bedrooms: int
    current_annual_rent_aed: float
    proposed_new_annual_rent_aed: float
    renewal_date: str
    notice_sent_date: Optional[str] = None
    security_deposit_aed: Optional[float] = None

class HtmlAuditRequest(AuditRequest):
    theme: Optional[str] = "light"  # for future style switches

# ---------- Pattern library (pinpointing) ----------
# Each rule has: label, severity, regex, suggestion, validity (False = flag, True = good)
RULES = [
    dict(label="eviction_notice", severity="high",
         regex=r"\bevict\b.*\bwithout\s+notice\b",
         suggestion="Dubai law requires proper legal notice; remove ‘without notice’.",
         validity=False),
    dict(label="arbitrary_termination", severity="high",
         regex=r"\b(terminate|end)\b.*\bany\s*time\b",
         suggestion="Open-ended termination is generally not enforceable; specify lawful grounds.",
         validity=False),
    dict(label="all_maintenance_on_tenant", severity="medium",
         regex=r"\btenant\b.*\bresponsible\b.*\ball\s+maintenance\b",
         suggestion="Landlord typically covers major/structural; reallocate fairly.",
         validity=False),
    dict(label="good_90_day_notice", severity="low",
         regex=r"\b(90|ninety)[-\s]?day(s)?\b.*\bnotice\b",
         suggestion="Good: 90-day notice for amendments is present.",
         validity=True),
    # very rough “large increase” phrase catch; exact cap is computed separately
    dict(label="blanket_increase", severity="high",
         regex=r"\brent may be increased\b.*\b(absolute discretion|any amount|without reference)\b",
         suggestion="Tie increases to RERA index and legal slabs; remove arbitrary wording.",
         validity=False),
]

def find_spans(text: str, rules=RULES):
    """
    Returns two lists of spans: invalid and valid.
    Span item: {label, severity, start, end, excerpt, suggestion}
    """
    invalid, valid = [], []
    for r in rules:
        for m in re.finditer(r["regex"], text, flags=re.I | re.S):
            span = {
                "label": r["label"],
                "severity": r["severity"],
                "start": m.start(),
                "end": m.end(),
                "excerpt": text[m.start():m.end()].strip(),
                "suggestion": r["suggestion"]
            }
            (valid if r["validity"] else invalid).append(span)
    return invalid, valid

def split_sentences(text: str) -> List[Dict]:
    """Return sentences with start/end offsets (for nicer UI mapping)."""
    sents = []
    if nlp:
        doc = nlp(text)
        for s in doc.sents:
            sents.append({"start": s.start_char, "end": s.end_char, "text": s.text})
    else:
        # simple fallback: split on period; compute offsets
        idx, start = 0, 0
        for part in text.split("."):
            part = part.strip()
            if not part: 
                start += 1
                continue
            end = start + len(part)
            sents.append({"start": start, "end": end, "text": part})
            start = end + 1
    return sents

def check_notice_rule(renewal_date: str, notice_date: Optional[str]) -> Optional[Dict]:
    if not notice_date:
        return {"label":"notice_missing","issue":"No notice date provided","severity":"medium"}
    try:
        r = datetime.fromisoformat(renewal_date)
        n = datetime.fromisoformat(notice_date)
        days = (r - n).days
        if days < 90:
            return {"label":"notice_lt_90","issue":"Notice period < 90 days","severity":"high","days":days}
    except Exception:
        return {"label":"notice_invalid_date","issue":"Invalid date format; use YYYY-MM-DD","severity":"low"}
    return None

def check_security_deposit(rent: float, deposit: Optional[float]) -> Optional[Dict]:
    if deposit and deposit > 0.10 * rent:
        return {
            "label":"deposit_high",
            "issue": f"Security deposit {deposit:.0f} AED > 10% of annual rent",
            "severity": "medium",
            "suggestion": "Market practice ~5–10% depending on furnishings."
        }
    return None

# ---------- Core audit ----------
@app.post("/audit")
def audit(req: AuditRequest):
    invalid_spans, valid_spans = find_spans(req.text)

    # Notice rule
    notice_issue = check_notice_rule(req.renewal_date, req.notice_sent_date)
    rule_flags: List[Dict] = []
    if notice_issue:
        rule_flags.append(notice_issue)

    # Deposit rule
    dep_issue = check_security_deposit(req.current_annual_rent_aed, req.security_deposit_aed)
    if dep_issue:
        rule_flags.append(dep_issue)

    # Decree 43/2013 rent slab
    avg = RENT_INDEX.get((req.contract_city, req.area, req.property_type, req.bedrooms))
    allowed_pct = None
    increase_flag = None
    if avg:
        allowed_pct = allowed_increase_pct(req.current_annual_rent_aed, avg)
        proposed_pct = (req.proposed_new_annual_rent_aed - req.current_annual_rent_aed) / max(req.current_annual_rent_aed,1) * 100
        if proposed_pct > allowed_pct:
            increase_flag = {
                "label":"increase_over_cap",
                "issue": f"Proposed increase {proposed_pct:.1f}% exceeds allowed {allowed_pct}% per Decree 43/2013",
                "severity": "high",
                "suggestion": "Adjust to within the RERA slab."
            }
            rule_flags.append(increase_flag)

    verdict = "fail" if (invalid_spans or rule_flags) else "pass"

    return {
        "verdict": verdict,
        "highlights": invalid_spans,     # pinpointed invalidities (start/end/excerpt)
        "valid_points": valid_spans,     # pinpointed good clauses (e.g., 90-day notice present)
        "rule_flags": rule_flags,        # non-span checks (notice days, deposit %, slabs)
        "allowed_increase": {"avg_index": avg, "max_allowed_pct": allowed_pct},
        "sentences": split_sentences(req.text),  # so the frontend can map spans to sentences
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

# ---------- HTML-marked version (quick to render) ----------
@app.post("/audit/html")
def audit_html(req: HtmlAuditRequest):
    res = audit(req)  # reuse logic
    text = req.text
    # Build non-overlapping markers for invalid first, then valid
    markers = []
    for h in res["highlights"]:
        markers.append((h["start"], h["end"], "bad"))
    for g in res["valid_points"]:
        markers.append((g["start"], g["end"], "good"))
    markers.sort(key=lambda x: (x[0], -x[1]))

    # Merge overlaps (keep 'bad' priority)
    merged = []
    for s,e,kind in markers:
        if not merged:
            merged.append([s,e,kind]); continue
        ps,pe,pk = merged[-1]
        if s <= pe:
            # overlap
            if kind == "bad" or pk == "bad":
                merged[-1][1] = max(pe, e)
                merged[-1][2] = "bad"
            else:
                merged[-1][1] = max(pe, e)
        else:
            merged.append([s,e,kind])

    # Stitch HTML
    html_parts = []
    i = 0
    for s,e,kind in merged:
        if i < s:
            html_parts.append(escape_html(text[i:s]))
        seg = escape_html(text[s:e])
        cls = "bad" if kind == "bad" else "good"
        html_parts.append(f'<mark class="{cls}">{seg}</mark>')
        i = e
    if i < len(text):
        html_parts.append(escape_html(text[i:]))

    style = """
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; line-height:1.6; padding:16px; }
      mark.bad { background: #ffe2e2; padding:1px 2px; border-radius:3px; }
      mark.good { background: #e3ffe6; padding:1px 2px; border-radius:3px; }
      .banner.pass { background:#e7f8ec; padding:10px; border-radius:6px; margin-bottom:12px; }
      .banner.fail { background:#ffefef; padding:10px; border-radius:6px; margin-bottom:12px; }
      code { background:#f6f8fa; padding:2px 4px; border-radius:3px; }
    </style>
    """
    banner = f'<div class="banner {"fail" if res["verdict"]=="fail" else "pass"}">Verdict: <b>{res["verdict"].upper()}</b></div>'
    body = "".join(html_parts).replace("\n", "<br>")
    return {
        "html": style + banner + f"<div>{body}</div>",
        "meta": {k: res[k] for k in ("verdict","allowed_increase","timestamp")}
    }

def escape_html(s: str) -> str:
    return (s.replace("&","&amp;")
             .replace("<","&lt;")
             .replace(">","&gt;"))

# ---------- PDF text extraction ----------
@app.post("/extract")
def extract(file: UploadFile = File(...)):
    text = pdfminer_highlevel_extract(file)
    return {"text": text}

def pdfminer_highlevel_extract(file: UploadFile):
    try:
        return pdfminer.high_level.extract_text(file.file)
    except Exception:
        file.file.seek(0)
        return ""

# ---------- Friendly root ----------
@app.get("/")
def root():
    return {"message": "Dubai Tenancy Audit Service running. Use /docs to test."}
