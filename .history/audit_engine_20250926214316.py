# audit_engine.py — Audit engine for Dubai tenancy contracts (RERA CSV Edition)
# Educational prototype — not legal advice.

from __future__ import annotations
import re
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple, Any

import pandas as pd
from dateutil.parser import parse as dtparse

# Optional NLP for nicer sentence splitting (not strictly required)
try:
    import spacy
    _NLP = spacy.load("en_core_web_sm")
except Exception:
    _NLP = None


# -----------------------------
# Helpers: dates & number parsing
# -----------------------------
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


_MONEY_RE = re.compile(
    r"(?:(?:AED|DHS|د\.إ)\s*)?([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\s*/\s*(month|mo|year|yr))?",
    re.I,
)
_DATE_RE = re.compile(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2})")
_BED_RE = re.compile(r"(studio|\b(\d+)\s*bed(room)?s?)", re.I)
_AREA_RE = re.compile(
    r"\b(Downtown Dubai|Jumeirah Village Circle|Dubai Marina|Business Bay|JLT|Jumeirah|Al Barsha)\b",
    re.I,
)


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


# -----------------------------
# Law references & regex rules
# -----------------------------
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
        "desc": "Rent increase slabs based on gap vs. market/RERA average (0/5/10/15/20%).",
        "law": "Decree No. 43 of 2013 (Dubai)",
    },
    "maintenance_default": {
        "desc": "Landlord typically responsible for major/structural maintenance unless otherwise agreed.",
        "law": "Practice; see Law 26/2007 Art. 16 (interpretations vary)",
    },
}

# Pinpoint rules: invalid and “good” clauses
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
