# app.py — Dubai Rental Contract Auditor (RERA CSV Edition)
# Educational prototype for school demos. Not legal advice.
# Run:
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install streamlit pdfminer.six pandas python-dateutil
#   # optional (better sentence splitting):
#   # pip install spacy && python -m spacy download en_core_web_sm
#   streamlit run app.py

from __future__ import annotations
import io
from datetime import date
from typing import Optional, Dict, Any

import streamlit as st
import pandas as pd
from pdfminer.high_level import extract_text

import audit_engine as ae


# -------------------------------
# Page setup
# -------------------------------
st.set_page_config(page_title="Dubai Rental Contract Auditor — RERA CSV", layout="wide")


# -------------------------------
# Sidebar — Data sources & settings
# -------------------------------
st.sidebar.title("⚙️ Data & Settings")
st.sidebar.write(
    "Upload the **official RERA rent index CSV** to use as the benchmark for "
    "Decree 43/2013 rent-increase slabs."
)

# CSV schema docs
st.sidebar.info(
    "**Required columns (case-insensitive):**\n"
    "- city (e.g., Dubai)\n"
    "- area (e.g., Jumeirah Village Circle)\n"
    "- property_type (apartment|villa|townhouse)\n"
    "- bedrooms_min (int)\n"
    "- bedrooms_max (int)\n"
    "- average_annual_rent_aed (float)\n"
    "\n**Optional:** furnished (unfurnished|semi|furnished)\n"
)

rera_csv_file = st.sidebar.file_uploader("Upload RERA Index CSV", type=["csv"], key="rera_csv")
rera_df: Optional[pd.DataFrame] = None
if rera_csv_file is not None:
    try:
        df = pd.read_csv(rera_csv_file)
        df.columns = [c.strip().lower() for c in df.columns]
        required = {"city", "area", "property_type", "bedrooms_min", "bedrooms_max", "average_annual_rent_aed"}
        missing = required - set(df.columns)
        if missing:
            st.sidebar.error(f"CSV missing columns: {sorted(list(missing))}")
        else:
            rera_df = df
            st.sidebar.success(f"RERA CSV loaded ✓  ({len(rera_df)} rows)")
    except Exception as e:
        st.sidebar.error(f"Failed to read CSV: {e}")

use_text_autoextract = st.sidebar.checkbox("Auto-fill fields from contract text", value=True)
st.sidebar.caption("Heuristics will try to read amounts, dates, area, and bedrooms from the uploaded PDF text.")


# -------------------------------
# Title & intro
# -------------------------------
st.title("🏠 Dubai Rental Contract Auditor — RERA CSV")
st.caption(
    "Upload a rental contract PDF. The app extracts text, auto-fills fields, "
    "and audits clauses vs. Dubai tenancy rules (Law 26/2007, Law 33/2008, Decree 43/2013). "
    "**Educational demo — not legal advice.**"
)


# -------------------------------
# Upload PDF & show text
# -------------------------------
left, right = st.columns([1, 1])

with left:
    uploaded_pdf = st.file_uploader("Upload Rental Contract (PDF)", type=["pdf"], key="pdf")
    extracted_text = ""
    if uploaded_pdf:
        with st.spinner("Extracting text from PDF…"):
            extracted_text = extract_text(uploaded_pdf)
        if extracted_text.strip():
            st.success("PDF text extracted.")
        else:
            st.warning("No text extracted (scanned PDF?). Try another file or paste text manually.")

with right:
    st.subheader("📄 Contract Text (editable)")
    default_text = (
        "RESIDENTIAL LEASE AGREEMENT (Dubai)\n"
        "Premises: 1BR apartment in JVC, Dubai.\n"
        "Annual Rent: AED 55,000 payable in 12 cheques.\n"
        "Security Deposit: AED 9,000.\n"
        "Renewal Date: 1 December 2025. Notice sent on 10 September 2025.\n"
        "The Landlord may evict the Tenant at any time without notice.\n"
        "Rent may be increased at the Landlord’s absolute discretion.\n"
        "A ninety-day notice is required before renewal to amend rent or terms.\n"
    )
    text_input = st.text_area(
        "Paste or edit contract text",
        value=extracted_text or default_text,
        height=260,
        key="contract_text",
    )

st.divider()


# -------------------------------
# Auto-extract fields from text
# -------------------------------
init_vals: Dict[str, Any] = ae.autofill_from_text(text_input) if use_text_autoextract else {}

colA, colB, colC, colD = st.columns([1, 1, 1, 1])
with colA:
    city = st.selectbox("City", ["Dubai"], index=0)
    area = st.text_input("Area / Community", value=init_vals.get("area", "Jumeirah Village Circle"))
with colB:
    ptype = st.selectbox("Property Type", ["apartment", "villa", "townhouse"], index=0)
    bedrooms = st.number_input("Bedrooms", min_value=0, max_value=10, step=1, value=int(init_vals.get("bedrooms", 1)))
with colC:
    current_rent = st.number_input(
        "Current Annual Rent (AED)", min_value=0, step=500, value=int(init_vals.get("current_rent", 55000))
    )
    proposed_rent = st.number_input(
        "Proposed New Rent (AED)", min_value=0, step=500, value=int(init_vals.get("proposed_rent", 70000))
    )
with colD:
    renewal_date = st.date_input("Renewal Date", value=ae.to_date(init_vals.get("renewal_date", "2025-12-01")))
    notice_sent_date = st.date_input("Notice Sent Date", value=ae.to_date(init_vals.get("notice_sent_date", "2025-09-10")))

colE, colF = st.columns([1, 1])
with colE:
    deposit = st.number_input(
        "Security Deposit (AED)", min_value=0, step=500, value=int(init_vals.get("deposit", 9000))
    )
with colF:
    furnished = st.selectbox("Furnishing", ["unfurnished", "semi", "furnished"], index=0)

st.divider()


# -------------------------------
# RERA CSV lookup for benchmark
# -------------------------------
st.subheader("📊 RERA Index (from your CSV)")
avg_rent = None
if rera_df is not None:
    match_df = ae.lookup_rera_row(
        rera_df,
        city=city,
        area=area,
        property_type=ptype,
        bedrooms=int(bedrooms),
        furnished=furnished,
    )
    if match_df is not None and not match_df.empty:
        st.success("Matched RERA row:")
        st.dataframe(match_df.reset_index(drop=True), use_container_width=True)
        if "average_annual_rent_aed" in match_df.columns:
            avg_rent = float(match_df.iloc[0]["average_annual_rent_aed"])
    else:
        st.warning(
            "No exact CSV match found for your inputs. "
            "You can still run the audit; the rent-increase cap will use CSV average = None."
        )
else:
    st.info("Upload a RERA CSV in the sidebar to enable index-based caps.")


# -------------------------------
# Run Audit
# -------------------------------
st.subheader("🔎 Audit")
if st.button("Run audit now"):
    with st.spinner("Auditing text vs. Dubai tenancy rules…"):
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
            rera_avg_index=avg_rent,
        )

    # Verdict banner
    if res["verdict"] == "pass":
        st.success("PASS — No blocking issues detected.")
    else:
        st.error("FAIL — Issues found.")

    # Metrics
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        st.metric("RERA Avg (AED)", f"{res['allowed_increase']['avg_index'] or '—'}")
    with c2:
        st.metric("Max Allowed % (Decree 43/2013)", f"{res['allowed_increase']['max_allowed_pct']}%")
    with c3:
        st.metric("Proposed %", f"{res['allowed_increase']['proposed_pct']:.1f}%")

    # Findings
    st.markdown("### 📌 Findings")
    for h in res["highlights"]:
        sev = h.get("severity", "info")
        icon = "🔴" if sev == "high" else ("🟡" if sev == "medium" else "🟢")
        st.markdown(f"{icon} **{h['issue']}** — _{h['excerpt']}_")
        if h.get("suggestion"):
            st.caption(f"Suggestion: {h['suggestion']}")
        if h.get("law"):
            st.caption(f"Law: {h['law']}")

    for rf in res["rule_flags"]:
        sev = rf.get("severity", "info")
        icon = "🔴" if sev == "high" else ("🟡" if sev == "medium" else "🟢")
        st.markdown(f"{icon} **{rf['issue']}**")
        if rf.get("suggestion"):
            st.caption(f"Suggestion: {rf['suggestion']}")
        if rf.get("law"):
            st.caption(f"Law: {rf['law']}")

    # Inline annotated text
    st.markdown("### 🖍️ Inline Highlights")
    html = ae.render_highlighted_html(text_input, res)
    st.components.v1.html(html, height=300, scrolling=True)

    # Export HTML report
    st.markdown("### ⤵️ Export")
    buf = io.BytesIO()
    report_html = ae.build_report_html(text_input, res)
    buf.write(report_html.encode("utf-8"))
    st.download_button(
        "Download HTML Report",
        data=buf.getvalue(),
        file_name="audit_report.html",
        mime="text/html",
    )

st.divider()
st.markdown(
    "**Legal references (orientation only):** Law 26/2007 & Law 33/2008 (tenancy, notice, "
    "eviction grounds), Decree 43/2013 (rent increase slabs). Official Arabic texts and DLD/RERA guidance prevail."
)
