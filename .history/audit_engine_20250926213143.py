# Project: Dubai Tenancy Audit App (Streamlit UI + Scrapers + RERA Rules)
# ----------------------------------------------------------------------------
# This canvas contains two files:
# 1) app.py              — Streamlit front-end (200+ lines)
# 2) audit_engine.py     — Core audit engine & scrapers (200+ lines)
#
# Notes & Ethics:
# • This is an educational prototype, not legal advice. Dubai tenancy law is nuanced; Arabic
#   texts prevail. Consult qualified professionals for real cases.
# • Scraping: Check robots.txt and Terms of Service for any site you target. Use low request
#   volumes, add proper headers, and respect rate limits. When available, prefer official APIs
#   or open datasets. The scraping utilities here are conservative and include caching.
# • Replace demo CSS selectors if websites change their markup.
# • For market rents, this prototype aggregates listings to estimate an average; it is NOT the
#   official RERA index. Use it only for demo math to illustrate Decree 43/2013 slabs.
# ----------------------------------------------------------------------------

import os as _os
import re as _re
import json as _json
import math as _math
import time as _time
import random as _random
from dataclasses import dataclass
from typing import Optional as _Optional, List as _List, Dict as _Dict, Tuple as _Tuple, Any as _Any
from datetime import datetime as _dt

import requests as _requests
from bs4 import BeautifulSoup as _BS

# -----------------------------
# Cache for scraped rents
# -----------------------------
_CACHE_PATH = "rent_cache.json"

def _load_cache() -> _Dict[str, _Any]:
    if _os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}
    return {}

def _save_cache(d: _Dict[str, _Any]) -> None:
    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
        _json.dump(d, f, ensure_ascii=False, indent=2)

def clear_rent_cache():
    if _os.path.exists(_CACHE_PATH):
        _os.remove(_CACHE_PATH)

# -----------------------------
# Utility: percentiles
# -----------------------------

def _percentile(values: _List[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values)-1) * p
    f = _math.floor(k)
    c = _math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] + (values[c] - values[f]) * (k - f)

# -----------------------------
# Decree 43/2013 slab logic
# -----------------------------

def allowed_increase_pct(current: float, avg: float) -> int:
    if avg is None or avg <= 0:
        return 0
    if current >= avg * 0.90: return 0
    if current >= avg * 0.80: return 5
    if current >= avg * 0.70: return 10
    if current >= avg * 0.60: return 15
    return 20

# -----------------------------
# Scrapers (lightweight, polite)
# -----------------------------
_HEADERS = lambda ua: {"User-Agent": ua}

_DEF_QS = {
    # NOTE: These are illustrative patterns. Adjust paths & params as sites evolve.
    "propertyfinder": "https://www.propertyfinder.ae/en/search?c=1&rp=y&bu=rent&l=1&ob=mr&page={page}&q={area}%20{ptype}%20{bedrooms}"
                        "&t=0&fu=0&bf=0&ph=0",
    "bayut": "https://www.bayut.com/to-rent/property/dubai/{area}/?property_type={ptype}&bedrooms={bedrooms}&page={page}",
}

_DEF_SELECTORS = {
    "propertyfinder": {
        "card": ["div.card", "div.listing-card", "article"],
        "price": ["span.price", "div.price", "strong", "span.card__price-value"],
    },
    "bayut": {
        "card": ["article.ca2f5673", "div._4041eb80"],
        "price": ["span._105b8a67", "span.f343d9ce"],
    }
}

_DEF_MAX_PAGES = 5


def _parse_price(text: str) -> float:
    if not text:
        return 0.0
    t = text.replace(",", "").replace("AED", "").replace("د.إ", "").strip()
    # try yearly normalization; many sites show monthly, detect '/year' vs '/month'
    # naive: look for 'year' or 'month' in the original text
    lower = text.lower()
    num = 0.0
    m = _re.search(r"(\d+[\,\.]?\d*)", t)
    if m:
        try:
            num = float(m.group(1))
        except Exception:
            num = 0.0
    if "month" in lower or "/mo" in lower or "/month" in lower:
        num *= 12.0
    return num


def _scrape_source(area: str, ptype: str, bedrooms: int, *, source: str, max_pages: int, max_listings: int,
                   timeout: int, user_agent: str) -> _List[float]:
    prices: _List[float] = []
    url_template = _DEF_QS.get(source)
    selectors = _DEF_SELECTORS.get(source, {})
    if not url_template:
        return prices

    for page in range(1, max_pages + 1):
        if len(prices) >= max_listings:
            break
        url = url_template.format(
            area=_requests.utils.quote(area.replace(" ", "-")),
            ptype=_requests.utils.quote(ptype),
            bedrooms=bedrooms,
            page=page,
        )
        try:
            resp = _requests.get(url, headers=_HEADERS(user_agent), timeout=timeout)
            if resp.status_code != 200:
                _time.sleep(1.0)
                continue
            soup = _BS(resp.text, "html.parser")
            card_selectors = selectors.get("card", [])
            price_selectors = selectors.get("price", [])
            cards = []
            for sel in card_selectors:
                cards.extend(soup.select(sel))
            if not cards:
                # fallback: try body-level search for price nodes
                nodes = []
                for sel in price_selectors:
                    nodes.extend(soup.select(sel))
                for n in nodes:
                    val = _parse_price(n.get_text(" "))
                    if val > 10000:
                        prices.append(val)
                continue
            for c in cards:
                text = c.get_text(" ") if hasattr(c, "get_text") else str(c)
                for sel in price_selectors:
                    node = c.select_one(sel)
                    if node:
                        text = node.get_text(" ")
                        break
                val = _parse_price(text)
                if val > 10000:
                    prices.append(val)
                if len(prices) >= max_listings:
                    break
            # politeness
            _time.sleep(0.8 + _random.random()*0.6)
        except Exception:
            _time.sleep(1.0)
            continue
    return prices


def fetch_market_rent(*, city: str, area: str, property_type: str, bedrooms: int,
                      max_listings: int = 40, timeout: int = 12, user_agent: str = "",
                      use_bayut: bool = True, use_propertyfinder: bool = True) -> _Dict[str, _Any]:
    """Fetch market rents from enabled sources with caching. Returns aggregate stats."""
    cache = _load_cache()
    key = _json.dumps({
        "city": city, "area": area.lower(), "ptype": property_type.lower(), "bedrooms": bedrooms,
        "max": max_listings
    }, sort_keys=True)
    if key in cache and cache[key].get("ts") and (_time.time() - cache[key]["ts"]) < 3600:
        return cache[key]["data"]

    all_prices: _List[float] = []
    source_counts: _Dict[str, int] = {}

    if use_propertyfinder:
        pf = _scrape_source(area, property_type, bedrooms, source="propertyfinder",
                            max_pages=_DEF_MAX_PAGES, max_listings=max_listings,
                            timeout=timeout, user_agent=user_agent)
        all_prices.extend(pf)
        source_counts["propertyfinder"] = len(pf)

    if use_bayut:
        by = _scrape_source(area, property_type, bedrooms, source="bayut",
                            max_pages=_DEF_MAX_PAGES, max_listings=max_listings,
                            timeout=timeout, user_agent=user_agent)
        all_prices.extend(by)
        source_counts["bayut"] = len(by)

    if not all_prices:
        data = {"avg": None, "median": None, "p25": None, "p75": None, "count": 0, "source_counts": source_counts}
    else:
        avg = sum(all_prices) / len(all_prices)
        med = _percentile(all_prices, 0.5)
        p25 = _percentile(all_prices, 0.25)
        p75 = _percentile(all_prices, 0.75)
        data = {"avg": avg, "median": med, "p25": p25, "p75": p75, "count": len(all_prices), "source_counts": source_counts}

    cache[key] = {"ts": _time.time(), "data": data}
    _save_cache(cache)
    return data

# -----------------------------
# Law-aware clause checks
# -----------------------------

LAW_RULES = {
    # References are descriptive; verify with official sources.
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
    },
    "maintenance_default": {
        "desc": "Landlord generally responsible for major/structural maintenance unless agreed otherwise.",
    }
}

_RULES_REGEX = [
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
         suggestion="Reallocate: landlord covers major/structural by default.",
         law_ref="maintenance_default"),
    dict(label="90-day notice present", severity="good",
         regex=r"\b(90|ninety)[-\s]?day(s)?\b.*\bnotice\b",
         suggestion="Good: 90-day notice clause present.",
         law_ref="notice_90_days"),
    dict(label="Blanket rent increase wording", severity="high",
         regex=r"\brent may be increased\b.*\b(absolute discretion|any amount|without reference)\b",
         suggestion="Tie increases to RERA slabs and index; remove blanket authority.",
         law_ref="decree_43_2013"),
]


def _find_spans(text: str) -> _Tuple[_List[_Dict[str, _Any]], _List[_Dict[str, _Any]]]:
    invalid, valid = [], []
    for r in _RULES_REGEX:
        for m in _re.finditer(r["regex"], text, flags=_re.I | _re.S):
            span = {
                "issue": r["label"],
                "severity": r["severity"],
                "start": m.start(), "end": m.end(),
                "excerpt": text[m.start():m.end()].strip(),
                "suggestion": r.get("suggestion"),
                "law": LAW_RULES.get(r.get("law_ref", ""), {}).get("desc"),
            }
            if r["severity"] == "good":
                valid.append(span)
            else:
                invalid.append(span)
    return invalid, valid


def _check_notice(renewal_date: str, notice_date: _Optional[str]) -> _Optional[_Dict[str, _Any]]:
    if not notice_date:
        return {"label": "notice_missing", "issue": "No notice date provided", "severity": "medium"}
    try:
        r = _dt.fromisoformat(renewal_date)
        n = _dt.fromisoformat(notice_date)
        days = (r - n).days
        if days < 90:
            return {
                "label": "notice_lt_90", "issue": f"Notice period < 90 days ({days} days)",
                "severity": "high", "law": LAW_RULES["notice_90_days"]["desc"],
                "suggestion": "Send/require at least 90 days written notice before renewal."
            }
    except Exception:
        return {"label": "notice_invalid_date", "issue": "Invalid date format (YYYY-MM-DD)", "severity": "low"}
    return None


def _check_deposit(rent: float, deposit: _Optional[float], furnished: str) -> _Optional[_Dict[str, _Any]]:
    if not deposit or deposit <= 0:
        return None
    # Typical practice: ~5% unfurnished, ~10% furnished (not statutory). We'll warn above 10%.
    soft_cap = 0.10 if furnished.lower() == "furnished" else 0.08
    if deposit > soft_cap * rent:
        return {
            "label": "deposit_high",
            "issue": f"Security deposit {deposit:.0f} AED appears high for market practice",
            "severity": "medium",
            "suggestion": "Consider 5–10% depending on furnishings; confirm current norms.",
        }
    return None


# -----------------------------
# Public API: audit_contract
# -----------------------------

def audit_contract(*, text: str, city: str, area: str, property_type: str, bedrooms: int,
                   current_rent: float, proposed_rent: float,
                   renewal_date: str, notice_sent_date: _Optional[str],
                   deposit: _Optional[float], furnished: str,
                   market_stats: _Optional[_Dict[str, _Any]] = None) -> _Dict[str, _Any]:
    invalid_spans, valid_spans = _find_spans(text)

    rule_flags: _List[_Dict[str, _Any]] = []

    # Notice rule
    ni = _check_notice(renewal_date, notice_sent_date)
    if ni: rule_flags.append(ni)

    # Deposit rule
    di = _check_deposit(current_rent, deposit, furnished)
    if di: rule_flags.append(di)

    # Market average (from scrapers) has priority, else a fallback None
    avg = None
    if market_stats and market_stats.get("avg"):
        avg = market_stats["avg"]
    
    # Decree 43/2013 slab
    allowed_pct = allowed_increase_pct(current_rent, avg if avg else 0)
    proposed_pct = ((proposed_rent - current_rent) / max(current_rent, 1)) * 100
    if avg and proposed_pct > allowed_pct:
        rule_flags.append({
            "label": "increase_over_cap",
            "issue": f"Proposed increase {proposed_pct:.1f}% exceeds allowed {allowed_pct}% per Decree 43/2013",
            "severity": "high",
            "suggestion": "Adjust to within the RERA slab calculated from market average.",
        })

    verdict = "pass" if (not invalid_spans and not rule_flags) else "fail"

    return {
        "verdict": verdict,
        "highlights": invalid_spans,
        "valid_points": valid_spans,
        "rule_flags": rule_flags,
        "allowed_increase": {
            "avg_index": avg,
            "max_allowed_pct": allowed_pct,
            "proposed_pct": proposed_pct,
        },
        "timestamp": _dt.utcnow().isoformat() + "Z",
    }

# -----------------------------
# Rendering Helpers
# -----------------------------

def render_highlighted_html(text: str, result: _Dict[str, _Any]) -> str:
    """Return minimal HTML with <mark> wrappers for invalid/valid spans."""
    markers: _List[_Tuple[int,int,str]] = []
    for h in result.get("highlights", []):
        markers.append((h["start"], h["end"], "bad"))
    for g in result.get("valid_points", []):
        markers.append((g["start"], g["end"], "good"))
    markers.sort(key=lambda x: (x[0], -x[1]))

    merged: _List[_List[_Any]] = []
    for s,e,kind in markers:
        if not merged:
            merged.append([s,e,kind]); continue
        ps,pe,pk = merged[-1]
        if s <= pe:
            if kind == "bad" or pk == "bad":
                merged[-1][1] = max(pe, e)
                merged[-1][2] = "bad"
            else:
                merged[-1][1] = max(pe, e)
        else:
            merged.append([s,e,kind])

    def esc(s: str) -> str:
        return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    html_parts: _List[str] = []
    i = 0
    for s,e,kind in merged:
        if i < s:
            html_parts.append(esc(text[i:s]))
        seg = esc(text[s:e])
        cls = "bad" if kind == "bad" else "good"
        html_parts.append(f'<mark class="{cls}">{seg}</mark>')
        i = e
    if i < len(text):
        html_parts.append(esc(text[i:]))

    style = (
        "<style>body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif;" \
        "line-height:1.5;padding:10px;} mark.bad{background:#ffe2e2;}" \
        "mark.good{background:#e3ffe6;}</style>"
    )
    return style + "<div>" + "".join(html_parts).replace("\n","<br>") + "</div>"


def build_report_html(text: str, result: _Dict[str, _Any]) -> str:
    head = (
        "<meta charset='utf-8'>"
        "<style>body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif;padding:24px;}"
        "h1{margin:0 0 8px;} .meta{color:#666;margin-bottom:16px;}"
        "mark.bad{background:#ffe2e2;} mark.good{background:#e3ffe6;}"
        "table{border-collapse:collapse;width:100%;} td,th{border:1px solid #ddd;padding:8px;}"
        "</style>"
    )
    html = ["<html><head>", head, "</head><body>"]
    html.append("<h1>Dubai Tenancy Audit Report</h1>")
    html.append(f"<div class='meta'>Generated at {result['timestamp']}</div>")

    # Verdict
    verdict = result.get("verdict", "")
    badge = "background:#e7f8ec;" if verdict == "pass" else "background:#ffefef;"
    html.append(f"<div style='padding:10px;border-radius:6px;{badge}'>Verdict: <b>{verdict.upper()}</b></div>")

    # Allowed increase summary
    ai = result.get("allowed_increase", {})
    html.append("<h2>Rent Increase Summary (Decree 43/2013)</h2>")
    html.append("<table><tr><th>Avg Index (AED)</th><th>Max Allowed %</th><th>Proposed %</th></tr>")
    html.append(f"<tr><td>{ai.get('avg_index') or '—'}</td><td>{ai.get('max_allowed_pct')}</td><td>{ai.get('proposed_pct'):.1f}</td></tr></table>")

    # Findings
    html.append("<h2>Findings</h2>")
    html.append("<ul>")
    for h in result.get("highlights", []):
        sev = h.get("severity", "info")
        html.append(f"<li><b>{h['issue']}</b> — <i>{h['excerpt']}</i><br><small>{h.get('suggestion','')}</small></li>")
    for r in result.get("rule_flags", []):
        html.append(f"<li><b>{r['issue']}</b><br><small>{r.get('suggestion','')}</small></li>")
    html.append("</ul>")

    # Inline marked text
    html.append("<h2>Annotated Contract</h2>")
    html.append(render_highlighted_html(text, result))

    html.append("</body></html>")
    return "".join(html)
