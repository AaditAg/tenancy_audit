# app.py — Dubai Rental Contract Auditor (Ejari-aware + OCR + RERA CSV)
# =======================================================================================
# RUN (macOS + VS Code)
# ---------------------------------------------------------------------------------------
# python3 -m venv .venv && source .venv/bin/activate
# pip install streamlit pdfminer.six pandas python-dateutil pytesseract pdf2image pillow reportlab chardet
# # optional (nicer sentence splitting & better noun phrase detection):
# # pip install spacy && python -m spacy download en_core_web_sm
# # OCR on macOS (for scanned PDFs):
# #   brew install tesseract
# streamlit run app.py
#
# WHAT THIS APP DOES
# ---------------------------------------------------------------------------------------
# 1) Upload an Ejari-style tenancy contract PDF (digital or scanned).
# 2) The app extracts structured fields (Annual Rent, Deposit, Period, Bedrooms, Area)
#    and the numbered “Terms & Conditions” clauses.
# 3) All input fields FORCE-REFRESH from the PDF every time you upload a new file.
# 4) You can still edit the contract text and the form values before auditing.
# 5) The audit engine checks clauses vs. Dubai tenancy rules:
#    - Law 26/2007 (as amended by Law 33/2008): 90-day notice for renewal changes,
#      eviction grounds & notice period (12 months via notary/registered mail).
#    - Decree 43/2013: Rent-increase slabs (0/5/10/15/20%) vs. RERA rent index.
# 6) Upload a RERA CSV (your “official” index) to compute admissible rent increase caps.
# 7) Outputs:
#      - Verdict banner (PASS/FAIL)
#      - Clause-by-clause table (verdict + rule issues)
#      - Inline highlights in the contract text
#      - HTML Audit Report export
# 8) Utilities:
#      - Generate a demo Ejari-style PDF (for quick testing)
#
# NOTE: Educational prototype for demo/classroom use — NOT legal advice.
# =======================================================================================

from __future__ import annotations
import io
from datetime import date
from typing import Optional, Dict, Any, List

import streamlit as st
import pandas as pd

import audit_engine as ae


# =======================================================================================
# STREAMLIT PAGE SETUP
# =======================================================================================
st.set_page_config(
    page_title="Dubai Rental Contract Auditor — Ejari + OCR + RERA CSV",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =======================================================================================
# SIDEBAR: DATA & SETTINGS
# =======================================================================================
st.sidebar.title("⚙️ Data & Settings")

st.sidebar.markdown(
    "Upload the **official RERA rent index CSV** to power Decree 43/2013 "
    "rent-increase slabs and comparisons."
)

st.sidebar.info(
    "**RERA CSV columns (case-insensitive):**\n"
    "- city (e.g., Dubai)\n"
    "- area (e.g., Jumeirah Village Circle)\n"
    "- property_type (apartment|villa|townhouse)\n"
    "- bedrooms_min (int)\n"
    "- bedrooms_max (int)\n"
    "- average_annual_rent_aed (float)\n"
    "Optional: furnished (unfurnished|semi|furnished)\n"
)

# OCR instructions (always visible per your request)
st.sidebar.warning(
    "📷 **OCR for scanned PDFs**\n\n"
    "To enable OCR fallback on macOS: run\n\n"
    "`brew install tesseract`\n\n"
    "Then restart this app."
)

# Upload RERA CSV (optional)
rera_csv_file = st.sidebar.file_uploader("Upload RERA Index CSV", type=["csv"], key="rera_csv")
rera_df: Optional[pd.DataFrame] = None
if rera_csv_file is not None:
    try:
        # Robust CSV loading (handles UTF-8/BOM and common encodings)
        rera_df = ae.load_rera_csv(rera_csv_file)
        st.sidebar.success(f"RERA CSV loaded ✓  ({len(rera_df)} rows)")
    except Exception as e:
        st.sidebar.error(f"Failed to read CSV: {e}")

# Fallback auto-extraction toggle
use_text_autoextract = st.sidebar.checkbox(
    "Auto-fill from contract text if some fields are missing",
    value=True,
    help="If Ejari field parsing misses something, heuristics try to fill it from the text body."
)

# Demo utilities
st.sidebar.divider()
if st.sidebar.button("Generate sample Ejari-style PDF"):
    try:
        buf = ae.generate_sample_ejari_pdf()  # returns BytesIO
        st.sidebar.download_button(
            "Download sample_ejari_contract.pdf",
            data=buf.getvalue(),
            file_name="sample_ejari_contract.pdf",
            mime="application/pdf",
        )
        st.sidebar.success("Sample Ejari-style PDF generated.")
    except Exception as e:
        st.sidebar.error(f"Could not generate sample PDF: {e}")


# =======================================================================================
# TITLE
# =======================================================================================
st.title("🏠 Dubai Rental Contract Auditor — Ejari + OCR + RERA CSV")
st.caption(
    "Upload an Ejari-style tenancy contract PDF (digital or scanned). The app extracts structured fields "
    "(OCR fallback for scans), force-fills the form from the PDF, and audits clauses vs. Dubai rules "
    "(Law 26/2007, Law 33/2008, Decree 43/2013). **Educational demo — not legal advice.**"
)


# =======================================================================================
# FILE UPLOAD & PARSING
# =======================================================================================
col_left, col_right = st.columns([1, 1])

ejari_struct: Dict[str, Any] = {}
extracted_text = ""
ocr_used = False
parse_notes: List[str] = []

with col_left:
    uploaded_pdf = st.file_uploader("Upload Rental Contract (PDF)", type=["pdf"], key="pdf")
    if uploaded_pdf:
        with st.spinner("Reading PDF (pdfminer → OCR fallback if needed)…"):
            pdf_bytes = uploaded_pdf.read()
            parsed = ae.parse_pdf_smart(pdf_bytes)
            extracted_text = parsed.get("text") or ""
            ejari_struct = parsed.get("ejari", {}) or {}
            ocr_used = parsed.get("ocr_used", False)
            parse_notes = parsed.get("notes", [])

        if extracted_text.strip():
            st.success("PDF text extracted.")
        else:
            st.warning("No text extracted (possible image-only PDF without OCR). Ensure Tesseract is installed.")

        if ocr_used:
            st.info("OCR fallback was used for this file (scanned/flattened PDF detected).")

        if ejari_struct:
            st.info("Ejari-style fields detected and parsed.")
    else:
        st.info("No PDF uploaded yet. You can also paste / type contract text on the right.")

with col_right:
    st.subheader("📄 Contract Text (editable)")
    default_text = (
        "TENANCY CONTRACT (Ejari-style demo)\n"
        "Property Usage: Residential   Property Type: apartment   Bedrooms: 1\n"
        "Location (Area): Jumeirah Village Circle\n"
        "Contract Period: From 2025-12-01 To 2026-11-30\n"
        "Annual Rent: AED 55,000      Security Deposit Amount: AED 9,000\n"
        "Mode of Payment: 12 cheques\n"
        "---------------------------------\n"
        "Terms & Conditions:\n"
        "1) The tenant has inspected the premises and agreed to lease them.\n"
        "2) The tenant shall pay utility charges as agreed in writing.\n"
        "3) The landlord may evict the tenant at any time without notice.\n"
        "4) Rent may be increased at the landlord’s absolute discretion.\n"
        "5) A ninety-day notice is required before renewal to amend rent or terms.\n"
    )

    # Force-refresh behavior:
    # Every time a new PDF is uploaded, we overwrite the text area with the reconstructed source_text.
    if uploaded_pdf and ejari_struct.get("source_text"):
        text_value = ejari_struct["source_text"]
    else:
        text_value = extracted_text or default_text

    text_input = st.text_area(
        "Paste or edit contract text (auto-populated from the uploaded PDF)",
        value=text_value,
        height=300,
        key="contract_text",
    )

# Optional parse notes (useful for debugging user uploads)
if parse_notes:
    with st.expander("Parser notes"):
        for n in parse_notes:
            st.caption(f"• {n}")

st.divider()


# =======================================================================================
# PREFILL UI FROM EJARI (FORCE REFRESH) → FALLBACK HEURISTICS → DEFAULTS
# =======================================================================================
fallback_vals: Dict[str, Any] = ae.autofill_from_text(text_input) if use_text_autoextract else {}
prefill: Dict[str, Any] = ae.merge_prefill(ejari_struct, fallback_vals)

cA, cB, cC, cD = st.columns([1, 1, 1, 1])
with cA:
    city = st.selectbox("City", ["Dubai"], index=0)
    area = st.text_input("Area / Community", value=prefill.get("area", "Jumeirah Village Circle"))
with cB:
    ptype = st.selectbox("Property Type", ["apartment", "villa", "townhouse"], index=0)
    bedrooms = st.number_input("Bedrooms", min_value=0, max_value=10, step=1, value=int(prefill.get("bedrooms", 1)))
with cC:
    current_rent = st.number_input(
        "Current Annual Rent (AED)",
        min_value=0,
        step=500,
        value=int(prefill.get("current_rent", prefill.get("annual_rent") or 55000)),
        help="Auto-filled from PDF (Annual Rent) or from text heuristics."
    )
    proposed_rent = st.number_input(
        "Proposed New Rent (AED)",
        min_value=0,
        step=500,
        value=int(prefill.get("proposed_rent", max(int(prefill.get("current_rent", 55000) * 1.1), 1000))),
        help="For renewal comparisons. Used only to compute Proposed % vs. current."
    )
with cD:
    renewal_date = st.date_input(
        "Renewal Date",
        value=ae.to_date(prefill.get("renewal_date", prefill.get("end_date", "2025-12-01")))
    )
    notice_sent_date = st.date_input(
        "Notice Sent Date",
        value=ae.to_date(prefill.get("notice_sent_date", "2025-09-10")),
        help="90-day check needs this. Put the date a written notice was sent/received."
    )

cE, cF = st.columns([1, 1])
with cE:
    deposit = st.number_input(
        "Security Deposit (AED)", min_value=0, step=500, value=int(prefill.get("deposit", 9000))
    )
with cF:
    furnished = st.selectbox(
        "Furnishing", ["unfurnished", "semi", "furnished"], index=0,
        help="Used only for deposit reasonableness (soft practice guidance)."
    )

# Show parsed clauses table (from Ejari)
if ejari_struct.get("clauses"):
    st.markdown("### 📜 Parsed Terms & Conditions (from Ejari)")
    st.caption("Each clause is audited separately so you can see which ones pass/fail.")
    ej_tbl = [{"Clause #": c.get("num"), "Text": c.get("text", "")} for c in ejari_struct["clauses"]]
    st.dataframe(pd.DataFrame(ej_tbl), width=True)

st.divider()


# =======================================================================================
# RERA CSV LOOKUP
# =======================================================================================
st.subheader("📊 RERA Index (from your CSV)")
avg_rent = None
matched_df = None
if rera_df is not None:
    matched_df = ae.lookup_rera_row(
        rera_df,
        city=city,
        area=area,
        property_type=ptype,
        bedrooms=int(bedrooms),
        furnished=furnished,
    )
    if matched_df is not None and not matched_df.empty:
        st.success("Matched RERA row:")
        st.dataframe(matched_df.reset_index(drop=True), use_container_width=True)
        if "average_annual_rent_aed" in matched_df.columns:
            avg_rent = float(matched_df.iloc[0]["average_annual_rent_aed"])
    else:
        st.warning(
            "No exact CSV match found for your inputs. "
            "You can still run the audit; the rent-increase cap will use CSV average = None."
        )
else:
    st.info("Upload a RERA CSV in the sidebar to enable index-based caps.")


# =======================================================================================
# AUDIT ACTION
# =======================================================================================
st.subheader("🔎 Audit")
if st.button("Run audit now", use_container_width=False):
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
            ejari_clauses=ejari_struct.get("clauses", []),
        )
        # include the raw text in the report so inline highlight view is consistent
        res["raw_text_for_report"] = text_input

    # Verdict banner
    if res["verdict"] == "pass":
        st.success("PASS — No blocking issues detected.")
    else:
        st.error("FAIL — Issues found.")

    # KPI cards
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        st.metric("RERA Avg (AED)", f"{res['allowed_increase']['avg_index'] or '—'}")
    with c2:
        st.metric("Max Allowed % (Decree 43/2013)", f"{res['allowed_increase']['max_allowed_pct']}%")
    with c3:
        st.metric("Proposed %", f"{res['allowed_increase']['proposed_pct']:.1f}%")

    # Clause-by-clause table first (if we have Ejari terms)
    if res.get("ejari_clause_results"):
        st.markdown("### 📌 Ejari Clause Findings")
        ej = pd.DataFrame(res["ejari_clause_results"])
        st.dataframe(ej, use_container_width=True)

    # Text rules findings
    st.markdown("### 📌 Text Findings")
    if not res["highlights"] and not res["rule_flags"]:
        st.info("No additional text-based issues detected.")
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
    st.markdown("### 🖍️ Inline Highlights in Contract")
    html = ae.render_highlighted_html(text_input, res)
    st.components.v1.html(html, height=340, scrolling=True)

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
    "**Legal references (orientation only):** Law 26/2007 & Law 33/2008 (tenancy, notice, eviction grounds), "
    "Decree 43/2013 (rent increase slabs). Official Arabic texts and DLD/RERA guidance prevail."
)
