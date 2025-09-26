# app.py — Dubai Rental Contract Auditor (Ejari-aware + OCR + RERA CSV)
# ---------------------------------------------------------------------------------
# Run (Mac + VS Code):
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install streamlit pdfminer.six pandas python-dateutil pytesseract pdf2image pillow reportlab
#   # (optional) pip install spacy && python -m spacy download en_core_web_sm
#   # If OCR on Mac: brew install tesseract
#   streamlit run app.py
#
# Notes:
# - This is an educational prototype, not legal advice.
# - RERA CSV is used as the source of truth for average annual rents.
# - Ejari-style PDFs are parsed: we read top fields + terms section and prefill the UI.

from __future__ import annotations
import io
from datetime import date
from typing import Optional, Dict, Any

import streamlit as st
import pandas as pd
from pdfminer.high_level import extract_text

import audit_engine as ae


# -------------------------------
# Streamlit page config
# -------------------------------
st.set_page_config(page_title="Dubai Rental Contract Auditor — Ejari + RERA CSV", layout="wide")


# -------------------------------
# Sidebar — Data sources & settings
# -------------------------------
st.sidebar.title("⚙️ Data & Settings")
st.sidebar.write(
    "Upload the **official RERA rent index CSV** to power Decree 43/2013 rent-increase slabs."
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

use_text_autoextract = st.sidebar.checkbox("Auto-fill fields from contract text (fallback)", value=True)
st.sidebar.caption("If Ejari field parsing fails, we fall back to heuristics from the text body.")

# Demo utilities
st.sidebar.divider()
if st.sidebar.button("Generate sample Ejari-style PDF"):
    try:
        buf = ae.generate_sample_ejari_pdf()  # returns bytes
        st.sidebar.download_button(
            "Download sample_ejari_contract.pdf",
            data=buf.getvalue(),
            file_name="sample_ejari_contract.pdf",
            mime="application/pdf",
        )
        st.sidebar.success("Sample Ejari-style PDF generated.")
    except Exception as e:
        st.sidebar.error(f"Could not generate sample PDF: {e}")


# -------------------------------
# Title & intro
# -------------------------------
st.title("🏠 Dubai Rental Contract Auditor — Ejari + OCR + RERA CSV")
st.caption(
    "Upload an Ejari-style tenancy contract PDF. The app extracts structured fields with OCR if needed, "
    "auto-fills the form, and audits clauses vs. Dubai tenancy rules (Law 26/2007, Law 33/2008, Decree 43/2013). "
    "**Educational demo — not legal advice.**"
)


# -------------------------------
# Upload PDF & extract smartly
# -------------------------------
left, right = st.columns([1, 1])

ejari_struct: Dict[str, Any] = {}
extracted_text = ""

with left:
    uploaded_pdf = st.file_uploader("Upload Rental Contract (PDF)", type=["pdf"], key="pdf")
    if uploaded_pdf:
        with st.spinner("Reading PDF (text → OCR fallback)…"):
            # Read raw bytes once
            pdf_bytes = uploaded_pdf.read()
            # Smart parse: try pdfminer; if poor text, OCR
            parsed = ae.parse_pdf_smart(pdf_bytes)
            extracted_text = parsed["text"] or ""
            ejari_struct = parsed.get("ejari", {}) or {}

        if extracted_text.strip():
            st.success("PDF text extracted (smart).")
        else:
            st.warning("No text extracted (scanned PDF?). Try another file or ensure Tesseract is installed.")

        if ejari_struct:
            st.info("Ejari-style fields detected and parsed.")
    else:
        st.info("No PDF uploaded yet. You can paste text manually in the box on the right.")

with right:
    st.subheader("📄 Contract Text (editable)")
    default_text = (
        "TENANCY CONTRACT (Ejari-style demo)\n"
        "Property Type: Residential (apartment)\n"
        "Bedrooms: 1\n"
        "Annual Rent: AED 55,000\n"
        "Security Deposit Amount: AED 9,000\n"
        "Contract Period: From 2025-12-01 To 2026-11-30\n"
        "Area: Jumeirah Village Circle\n"
        "---------------------------------\n"
        "Terms & Conditions:\n"
        "1) The tenant has inspected the premises.\n"
        "2) The tenant shall pay charges as agreed in writing.\n"
        "3) The landlord may evict the tenant at any time without notice.\n"
        "4) Rent may be increased at the landlord’s absolute discretion.\n"
        "5) A ninety-day notice is required before renewal to amend terms.\n"
    )
    text_input = st.text_area(
        "Paste or edit contract text",
        value=(extracted_text or default_text),
        height=260,
        key="contract_text",
    )

st.divider()


# -------------------------------
# Build UI values (Ejari -> boxes)
# -------------------------------
# Priority: Ejari parsed fields → fallback auto-fill from text (if enabled) → defaults
fallback_vals: Dict[str, Any] = ae.autofill_from_text(text_input) if use_text_autoextract else {}
prefill: Dict[str, Any] = ae.merge_prefill(ejari_struct, fallback_vals)

colA, colB, colC, colD = st.columns([1, 1, 1, 1])
with colA:
    city = st.selectbox("City", ["Dubai"], index=0)
    area = st.text_input("Area / Community", value=prefill.get("area", "Jumeirah Village Circle"))
with colB:
    ptype = st.selectbox("Property Type", ["apartment", "villa", "townhouse"], index=0)
    bedrooms = st.number_input("Bedrooms", min_value=0, max_value=10, step=1, value=int(prefill.get("bedrooms", 1)))
with colC:
    current_rent = st.number_input(
        "Current Annual Rent (AED)", min_value=0, step=500, value=int(prefill.get("current_rent", 55000))
    )
    proposed_rent = st.number_input(
        "Proposed New Rent (AED)", min_value=0, step=500, value=int(prefill.get("proposed_rent", 70000))
    )
with colD:
    renewal_date = st.date_input("Renewal Date", value=ae.to_date(prefill.get("renewal_date", "2025-12-01")))
    notice_sent_date = st.date_input("Notice Sent Date", value=ae.to_date(prefill.get("notice_sent_date", "2025-09-10")))

colE, colF = st.columns([1, 1])
with colE:
    deposit = st.number_input(
        "Security Deposit (AED)", min_value=0, step=500, value=int(prefill.get("deposit", 9000))
    )
with colF:
    furnished = st.selectbox("Furnishing", ["unfurnished", "semi", "furnished"], index=0)

# Show parsed clauses (if any) from Ejari terms
if ejari_struct.get("clauses"):
    st.markdown("### 📜 Parsed Terms & Conditions (from Ejari)")
    st.caption("Each clause is audited separately so you can see which ones pass/fail.")
    ej_tbl = []
    for c in ejari_struct["clauses"]:
        ej_tbl.append({"Clause #": c.get("num"), "Text": c.get("text", "")})
    st.dataframe(pd.DataFrame(ej_tbl), use_container_width=True)

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
            ejari_clauses=ejari_struct.get("clauses", []),  # audit each clause if present
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

    # Findings — clause-by-clause (Ejari terms first if any)
    if res.get("ejari_clause_results"):
        st.markdown("### 📌 Ejari Clause Findings")
        ej = pd.DataFrame(res["ejari_clause_results"])
        st.dataframe(ej, use_container_width=True)

    st.markdown("### 📌 Text Findings")
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
    st.components.v1.html(html, height=320, scrolling=True)

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
