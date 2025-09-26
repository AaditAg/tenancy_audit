# audit_engine.py
# Core Dubai tenancy audit rules (educational demo, not legal advice)

import re
from datetime import datetime

# --- Demo RERA rent index (replace with real data if available) ---
RENT_INDEX = {
    ("Dubai", "Jumeirah Village Circle", "apartment", 1): 60000,
    ("Dubai", "Downtown Dubai", "apartment", 1): 90000,
}

def allowed_increase_pct(current: float, avg: float) -> int:
    if current >= avg * 0.90: return 0
    if current >= avg * 0.80: return 5
    if current >= avg * 0.70: return 10
    if current >= avg * 0.60: return 15
    return 20

RULES = [
    dict(label="Eviction without notice", severity="high",
         regex=r"\bevict\b.*\bwithout\s+notice\b",
         suggestion="Dubai law requires proper notice (12 months)."),
    dict(label="Arbitrary termination", severity="high",
         regex=r"\bterminate\b.*\bany\s*time\b",
         suggestion="Termination must follow legal grounds."),
    dict(label="All maintenance on tenant", severity="medium",
         regex=r"\btenant\b.*\ball maintenance\b",
         suggestion="Landlord covers major works by default."),
    dict(label="90-day notice present", severity="good",
         regex=r"\b90[-\s]?day(s)?\b.*\bnotice\b",
         suggestion="Correct notice clause included."),
]

def audit_contract(text: str, city="Dubai", area="Jumeirah Village Circle",
                   property_type="apartment", bedrooms=1,
                   current_rent=60000, proposed_rent=60000,
                   renewal_date="2025-12-01", notice_sent_date=None,
                   deposit=None):

    highlights = []
    for rule in RULES:
        for m in re.finditer(rule["regex"], text, re.I):
            highlights.append({
                "excerpt": text[m.start():m.end()],
                "issue": rule["label"],
                "severity": rule["severity"],
                "suggestion": rule["suggestion"]
            })

    # Notice period check
    if notice_sent_date:
        try:
            r = datetime.fromisoformat(renewal_date)
            n = datetime.fromisoformat(notice_sent_date)
            days = (r - n).days
            if days < 90:
                highlights.append({
                    "excerpt": f"Notice given: {days} days",
                    "issue": "Notice period < 90 days",
                    "severity": "high",
                    "suggestion": "Increase notice to minimum 90 days."
                })
        except Exception:
            pass

    # Deposit check
    if deposit and deposit > 0.10 * current_rent:
        highlights.append({
            "excerpt": f"Deposit {deposit} AED",
            "issue": "High security deposit",
            "severity": "medium",
            "suggestion": "Should be ≤10% (5–10% typical)."
        })

    # Rent increase check
    avg = RENT_INDEX.get((city, area, property_type, bedrooms))
    if avg:
        allowed = allowed_increase_pct(current_rent, avg)
        pct = (proposed_rent - current_rent) / current_rent * 100
        if pct > allowed:
            highlights.append({
                "excerpt": f"Proposed increase {pct:.1f}%",
                "issue": "Rent increase above RERA cap",
                "severity": "high",
                "suggestion": f"Allowed max: {allowed}% (per Decree 43/2013)."
            })

    verdict = "PASS ✅" if not [h for h in highlights if h["severity"] != "good"] else "FAIL ❌"
    return verdict, highlights
