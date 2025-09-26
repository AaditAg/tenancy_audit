# app.py
# Educational Dubai Tenancy Audit Microservice
# Checks contracts against RERA / Dubai tenancy rules
# Aadit-friendly: clear flags, simple JSON responses

from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime
import re
import pdfminer.high_level

import spacy
nlp = spacy.load("en_core_web_sm")

app = FastAPI(title="Dubai Tenancy Audit Service")

# --- Demo rent index (AED per year) ---
RENT_INDEX = {
    ("Dubai", "Jumeirah Village Circle", "apartment", 1): 60000,
    ("Dubai", "Jumeirah Village Circle", "apartment", 2): 80000,
    ("Dubai", "Downtown", "apartment", 1): 90000,
    ("Dubai", "Downtown", "apartment", 2): 140000,
}

# --- Rent increase slabs (Decree 43/2013) ---
def allowed_increase_pct(current: float, avg: float) -> int:
    if current >= avg * 0.9: return 0
    if current >= avg * 0.8: return 5
    if current >= avg * 0.7: return 10
    if current >= avg * 0.6: return 15
    return 20

# --- Input schema ---
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

# --- Rule checks ---
def check_clauses(text: str) -> List[Dict]:
    flags = []

    ILLEGAL_PATTERNS = [
        (r"evict.*without notice", "Eviction without 12-month notice", "high"),
        (r"terminate.*any time", "Unlawful early termination clause", "high"),
        (r"rent increase.*25%|30%", "Rent increase exceeds RERA limits", "high"),
        (r"tenant.*responsible.*all maintenance", "Landlord must cover major maintenance", "medium"),
    ]

    for pat, issue, sev in ILLEGAL_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            flags.append({
                "clause": m.group(0),
                "issue": issue,
                "severity": sev,
                "suggestion": "Revise per RERA guidelines"
            })

    return flags

def check_notice(renewal_date: str, notice_date: Optional[str]) -> Optional[Dict]:
    if not notice_date:
        return {"issue": "No notice date provided", "severity": "medium"}
    try:
        r = datetime.fromisoformat(renewal_date)
        n = datetime.fromisoformat(notice_date)
        days = (r - n).days
        if days < 90:
            return {"issue": "Notice period < 90 days", "severity": "high"}
    except Exception:
        return {"issue": "Invalid date format", "severity": "low"}
    return None

def check_security_deposit(rent: float, deposit: Optional[float]) -> Optional[Dict]:
    if deposit and deposit > 0.1 * rent:
        return {
            "issue": f"Security deposit {deposit} AED > 10% of rent",
            "severity": "medium",
            "suggestion": "Standard practice is 5–10%"
        }
    return None

# --- Audit endpoint ---
@app.post("/audit")
def audit(req: AuditRequest):
    flags = []

    # Clause scan
    flags.extend(check_clauses(req.text))

    # Notice check
    notice_issue = check_notice(req.renewal_date, req.notice_sent_date)
    if notice_issue: flags.append(notice_issue)

    # Security deposit check
    dep_issue = check_security_deposit(req.current_annual_rent_aed, req.security_deposit_aed)
    if dep_issue: flags.append(dep_issue)

    # Rent increase check
    avg = RENT_INDEX.get((req.contract_city, req.area, req.property_type, req.bedrooms))
    allowed_pct = None
    if avg:
        allowed_pct = allowed_increase_pct(req.current_annual_rent_aed, avg)
        proposed_pct = (req.proposed_new_annual_rent_aed - req.current_annual_rent_aed) / req.current_annual_rent_aed * 100
        if proposed_pct > allowed_pct:
            flags.append({
                "issue": f"Proposed rent increase {proposed_pct:.1f}% exceeds allowed {allowed_pct}%",
                "severity": "high"
            })

    verdict = "fail" if flags else "pass"
    return {
        "verdict": verdict,
        "flags": flags,
        "allowed_increase": {"avg_index": avg, "max_allowed_pct": allowed_pct},
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

# --- Extract text from PDF ---
@app.post("/extract")
def extract(file: UploadFile = File(...)):
    text = pdfminer.high_level.extract_text(file.file)
    return {"text": text}
