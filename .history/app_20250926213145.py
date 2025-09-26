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
import os
import io
import json
import time
from datetime import date
from typing import Dict, Any, List

import streamlit as st
from pdfminer.high_level import extract_text

# Local engine
import audit_engine as ae

st.set_page_config(page_title="Dubai Rental Contract Auditor", layout="wide")

# -------------------------------
# Sidebar: App Settings
# -------------------------------
st.sidebar.title("⚙️ Settings")
st.sidebar.markdown("**Read me**: This is an educational demo. Always respect website ToS & robots.txt.")

# Scraper toggles
use_propertyfinder = st.sidebar.checkbox("Use Property Finder scraper", value=True)
use_bayut = st.sidebar.checkbox("Use Bayut scraper", value=True)
max_listings = st.sidebar.slider("Max listings per source", min_value=10, max_value=120, value=40, step=10)
scrape_timeout = st.sidebar.slider("HTTP timeout (s)", min_value=5, max_value=30, value=12, step=1)
user_agent = st.sidebar.text_input(
    "Custom User-Agent",
    value="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116 Safari/537.36",
)

st.sidebar.divider()
st.sidebar.markdown("**Caching**")
clear_cache = st.sidebar.button("Clear rent cache")
if clear_cache:
    ae.clear_rent_cache()
    st.sidebar.success("Rent cache cleared.")

# -------------------------------
# Main Title
# -------------------------------
st.title("🏠 Dubai Rental Contract Auditor (RERA-focused)")
st.caption("Upload a contract PDF, fetch market rent (demo), and highlight compliant/invalid clauses.")

# -------------------------------
# Inputs: Contract File & Details
# -------------------------------
left, right = st.columns([1, 1])

with left:
    uploaded = st.file_uploader("Upload Rental Contract (PDF)", type=["pdf"]) 
    contract_text = ""
    if uploaded:
        with st.spinner("Extracting text from PDF…"):
            contract_text = extract_text(uploaded)
    else:
        st.info("No PDF uploaded. You can paste text manually on the right.")

with right:
    st.subheader("Contract Text (editable)")
    default_text = (
        "RESIDENTIAL LEASE AGREEMENT (Dubai)\n"
        "The Landlord may evict the Tenant at any time without notice.\n"
        "Rent may be increased at the Landlord’s absolute discretion.\n"
        "Tenant is responsible for all maintenance and repairs.\n"
        "A ninety-day notice is required before renewal to amend rent or terms."
    )
    text_input = st.text_area("Paste or edit contract text", value=contract_text or default_text, height=260)

st.divider()

# -------------------------------
# Contract Meta (for checks)
# -------------------------------
colA, colB, colC, colD = st.columns([1,1,1,1])
with colA:
    city = st.selectbox("City", ["Dubai"], index=0)
    area = st.text_input("Area / Community", value="Jumeirah Village Circle")
with colB:
    ptype = st.selectbox("Property Type", ["apartment", "villa", "townhouse"], index=0)
    bedrooms = st.number_input("Bedrooms", min_value=0, max_value=10, value=1, step=1)
with colC:
    current_rent = st.number_input("Current Annual Rent (AED)", min_value=0, value=55000, step=1000)
    proposed_rent = st.number_input("Proposed New Rent (AED)", min_value=0, value=70000, step=1000)
with colD:
    renewal_date = st.date_input("Renewal Date", value=date(2025, 12, 1))
    notice_sent_date = st.date_input("Notice Sent Date", value=date(2025, 9, 10))

colE, colF = st.columns([1,1])
with colE:
    deposit = st.number_input("Security Deposit (AED)", min_value=0, value=9000, step=500)
with colF:
    furnished = st.selectbox("Furnishing", ["unfurnished", "semi", "furnished"], index=0)

# -------------------------------
# Fetch Market Rent (demo scraping)
# -------------------------------
st.subheader("📊 Market Rent (Demo Scrape → Aggregate)")
fetch = st.button("Fetch market rent from enabled sources")
market_stats: Dict[str, Any] | None = None
if fetch:
    with st.spinner("Scraping (respecting robots & rate limits)…"):
        market_stats = ae.fetch_market_rent(
            city=city,
            area=area,
            property_type=ptype,
            bedrooms=int(bedrooms),
            max_listings=int(max_listings),
            timeout=scrape_timeout,
            user_agent=user_agent,
            use_bayut=use_bayut,
            use_propertyfinder=use_propertyfinder,
        )
    if market_stats and market_stats.get("count", 0) > 0:
        st.success(f"Collected {market_stats['count']} listings. Avg AED {market_stats['avg']:.0f}, Median AED {market_stats['median']:.0f}")
        st.json({k: v for k, v in market_stats.items() if k in ("avg","median","p25","p75","count","source_counts")})
    else:
        st.warning("No listings parsed. Try a different area or increase max listings.")

# -------------------------------
# Run Audit
# -------------------------------
st.subheader("🔎 Audit")
run = st.button("Run audit now")
if run:
    with st.spinner("Auditing text vs. RERA rules…"):
        res = ae.audit_contract(
            text=text_input,
            city=city,
            area=area,
            property_type=ptype,
            bedrooms=int(bedrooms),
            current_rent=float(current_rent),
            proposed_rent=float(proposed_rent),
            renewal_date=renewal_date.isoformat(),
            notice_sent_date=notice_sent_date.isoformat(),
            deposit=float(deposit),
            furnished=furnished,
            market_stats=market_stats,
        )

    # Verdict
    if res["verdict"] == "pass":
        st.success("PASS — No blocking issues detected.")
    else:
        st.error("FAIL — Issues found.")

    # Summary cards
    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        st.metric("Allowed Increase Max (Decree 43/2013)", f"{res['allowed_increase']['max_allowed_pct']}%")
        st.caption(f"Benchmark AED {res['allowed_increase']['avg_index'] or '—'}")
    with c2:
        st.metric("Proposed Increase", f"{res['allowed_increase']['proposed_pct']:.1f}%")
    with c3:
        st.metric("Flag Count", len(res["highlights"]) + len(res["rule_flags"]))

    st.markdown("### 📌 Findings")
    for h in res["highlights"]:
        sev = h.get("severity", "info")
        icon = "🔴" if sev == "high" else ("🟡" if sev == "medium" else "🟢")
        st.markdown(f"{icon} **{h['issue']}** — _{h['excerpt']}_  ")
        if h.get("suggestion"):
            st.caption(f"Suggestion: {h['suggestion']}")

    for rf in res["rule_flags"]:
        sev = rf.get("severity", "info")
        icon = "🔴" if sev == "high" else ("🟡" if sev == "medium" else "🟢")
        st.markdown(f"{icon} **{rf['issue']}**")
        if rf.get("suggestion"):
            st.caption(f"Suggestion: {rf['suggestion']}")

    # Inline highlight render (HTML)
    st.markdown("### 🖍️ Inline Highlights")
    html = ae.render_highlighted_html(text_input, res)
    st.components.v1.html(html, height=260, scrolling=True)

    # Export report
    st.markdown("### ⤵️ Export")
    buf = io.BytesIO()
    report_html = ae.build_report_html(text_input, res)
    buf.write(report_html.encode("utf-8"))
    st.download_button("Download HTML Report", data=buf.getvalue(), file_name="audit_report.html", mime="text/html")

# -------------------------------
# Footer & Law References
# -------------------------------
st.divider()
st.markdown("**Legal references (for orientation only):** Law 26/2007 (tenancy), Law 33/2008 (amendments), Decree 43/2013 (rent caps), common DLD/RERA guidelines including 90-day notice and 12-month eviction notices via notary/registered mail. Always verify with official sources.")