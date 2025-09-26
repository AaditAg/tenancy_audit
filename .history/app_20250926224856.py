# app.py — Dubai Rental Contract Auditor (Ejari + OCR + RERA CSV)
# -----------------------------------------------------------------------------
# macOS quickstart:
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install streamlit pdfminer.six pandas python-dateutil pytesseract pdf2image pillow reportlab chardet
#   # optional: pip install spacy && python -m spacy download en_core_web_sm
#   # OCR for scanned PDFs: brew install tesseract
#   streamlit run app.py
#
# Educational prototype — not legal advice.

from __future__ import annotations
import io
from typing import Optional, Dict, Any, List

import streamlit as st
import pandas as pd

import audit_engine as ae

st.set_page_config(
    page_title="Dubai Rental Contract Auditor — Ejari + OCR + RERA CSV",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------- Sidebar ---------------------
st.sidebar.title("⚙️ Data & Settings")

st.sidebar.info(
    "**RERA CSV columns (case-insensitive):**\n"
    "city, area, property_type, bedrooms_min, bedrooms_max, average_annual_rent_aed.\n"
    "Optional: furnished."
)

st.sidebar.warning(
    "📷 For scanned PDFs, install OCR: `brew install tesseract` and restart the app."
)

rera_df: Optional[pd.DataFrame] = None
rera_csv = st.sidebar.file_uploader("Upload RERA Index CSV (optional)", type=["csv"])
if rera_csv is not None:
    try:
        rera_df = ae.load_rera_csv(rera_csv)
        st.sidebar.success(f"RERA CSV loaded ✓ ({len(rera_df)} rows)")
    except Exception as e:
        st.sidebar.error(f"CSV error: {e}")

use_text_autofill = st.sidebar.checkbox(
    "If some fields are missing in PDF, try autofill from text",
    value=False,  # OFF so the UI reflects exactly what's in the PDF by default
)

st.sidebar.divider()
if st.sidebar.button("Generate a sample Ejari-style PDF"):
    buf = ae.generate_sample_ejari_pdf()
    st.sidebar.download_button(
        "Download sample_ejari_contract.pdf",
        data=buf.getvalue(),
        file_name="sample_ejari_contract.pdf",
        mime="application/pdf",
    )

# --------------------- Title -----------------------
st.title("🏠 Dubai Rental Contract Auditor — Ejari + OCR + RERA CSV")
st.caption(
    "Upload an Ejari-style tenancy contract (digital or scanned). The app extracts fields and clauses "
    "verbatim from your PDF (OCR fallback for scans), force-fills the form, and audits against Dubai rules "
    "(Law 26/2007, Law 33/2008, Decree 43/2013)."
)

# --------------------- Upload & Parse ---------------
left, right = st.columns([1, 1])

ejari: Dict[str, Any] = {}
raw_text = ""
ocr_used = False
notes: List[str] = []

with left:
    pdf = st.file_uploader("Upload Rental Contract (PDF)", type=["pdf"])
    if pdf:
        with st.spinner("Parsing PDF (pdfminer → OCR if needed)…"):
            parsed = ae.parse_pdf_smart(pdf.read())
            raw_text = parsed.get("text", "")
            ejari = parsed.get("ejari", {}) or {}
            ocr_used = parsed.get("ocr_used", False)
            notes = parsed.get("notes", [])
        if raw_text.strip():
            st.success("PDF text extracted.")
        else:
            st.error("No text found. If the file is scanned, install Tesseract and retry.")
        if ocr_used:
            st.info("OCR fallback was used.")
        if ejari:
            st.info("Ejari-like fields were detected and parsed.")

with right:
    st.subheader("📄 Contract Text (editable, verbatim from your PDF)")
    default_text = (
        "Upload a PDF on the left, or paste text here.\n"
        "This box is overwritten with the file’s contents after each upload (force refresh)."
    )
    text_value = raw_text or default_text
    text_input = st.text_area(
        "Paste or edit contract text (this is the exact text that will be audited)",
        value=text_value,
        height=320,
        key="contract_text",
    )

if notes:
    with st.expander("Parser notes"):
        for n in notes:
            st.caption(f"• {n}")

st.divider()

# --------------------- Force-fill boxes from PDF ---------------------
pdf_prefill = ejari.copy()
if use_text_autofill:
    fallbacks = ae.autofill_from_text(text_input)
    for k, v in fallbacks.items():
        if k not in pdf_prefill or pdf_prefill.get(k) in (None, ""):
            pdf_prefill[k] = v

cA, cB, cC, cD = st.columns([1, 1, 1, 1])
with cA:
    city = st.selectbox("City", ["Dubai"], index=0)
    area = st.text_input("Area / Community", value=pdf_prefill.get("area", ""))
with cB:
    ptype_val = pdf_prefill.get("property_type") or "apartment"
    ptype = st.selectbox("Property Type", ["apartment", "villa", "townhouse"],
                         index=["apartment", "villa", "townhouse"].index(ptype_val) if ptype_val in ["apartment", "villa", "townhouse"] else 0)
    bedrooms = st.number_input("Bedrooms", min_value=0, max_value=10, step=1,
                               value=int(pdf_prefill.get("bedrooms") or 0))
with cC:
    current_rent = st.number_input("Current Annual Rent (AED)", min_value=0, step=500,
                                   value=int(pdf_prefill.get("annual_rent") or 0))
    proposed_rent = st.number_input("Proposed New Rent (AED)", min_value=0, step=500,
                                    value=int(pdf_prefill.get("proposed_rent") or (current_rent or 0)),
                                    help="For renewal simulations only.")
with cD:
    renewal_date = st.date_input("Renewal Date",
                                 value=ae.to_date(pdf_prefill.get("renewal_date") or pdf_prefill.get("end_date") or None))
    notice_sent_date = st.date_input("Notice Sent Date",
                                     value=ae.to_date(pdf_prefill.get("notice_sent_date") or None))

cE, cF = st.columns([1, 1])
with cE:
    deposit = st.number_input("Security Deposit (AED)", min_value=0, step=500,
                              value=int(pdf_prefill.get("deposit") or 0))
with cF:
    furnished = st.selectbox("Furnishing", ["unfurnished", "semi", "furnished"], index=0)

# --------------------- Terms table (from Ejari) ---------------------
st.markdown("### 📜 Parsed Terms & Conditions (from your PDF)")
clauses_df = pd.DataFrame([{"clause": c.get("num"), "text": c.get("text", "").strip()} for c in ejari.get("clauses", [])])
if not clauses_df.empty:
    st.dataframe(clauses_df, width="stretch")
else:
    st.info("No numbered clauses were found under a ‘Terms & Conditions’ section of the PDF.")

st.divider()

# --------------------- RERA match ---------------------
st.subheader("📊 RERA Index (from your CSV)")
rera_avg = None
if rera_df is not None:
    matched = ae.lookup_rera_row(
        rera_df,
        city=city,
        area=area,
        property_type=ptype,
        bedrooms=int(bedrooms),
        furnished=furnished,
    )
    if matched is not None and not matched.empty:
        st.success("Matched index row:")
        st.dataframe(matched.reset_index(drop=True), width="stretch")
        rera_avg = float(matched.iloc[0]["average_annual_rent_aed"])
    else:
        st.warning("No exact CSV match; audit will still run (without rent-cap slabs).")
else:
    st.info("Upload a RERA CSV in the sidebar to enable rent-cap slabs.")

# --------------------- Audit ---------------------
st.subheader("🔎 Audit")
strict_mode = st.checkbox("Strict mode (fail on any issue)", value=False,
                          help="If off, only HIGH severity issues cause FAIL.")

if st.button("Run audit now"):
    with st.spinner("Auditing…"):
        result = ae.audit_contract(
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
            rera_avg_index=rera_avg,
            ejari_clauses=ejari.get("clauses", []),
            strict_mode=strict_mode,
        )
        result["raw_text_for_report"] = text_input

    # Banner
    if result["verdict"] == "pass":
        st.success("PASS — no blocking issues.")
    else:
        st.error("FAIL — issues found.")

    # Explain blocking reasons (so it's obvious why FAIL)
    if result["verdict"] == "fail":
        blocking_text = [h for h in result.get("highlights", []) if h.get("severity") == "high"]
        blocking_rules = [r for r in result.get("rule_flags", []) if r.get("severity") == "high"]
        if blocking_text or blocking_rules:
            with st.expander("See blocking reasons"):
                if blocking_text:
                    st.markdown("**Blocking text hits:**")
                    for h in blocking_text:
                        st.markdown(f"• **{h['issue']}** — _{h['excerpt']}_")
                if blocking_rules:
                    st.markdown("**Blocking rule flags:**")
                    for r in blocking_rules:
                        st.markdown(f"• **{r['issue']}**")
        else:
            st.caption("No HIGH-severity blockers found. Switch off **Strict mode** to pass.")

    # KPI row
    k1, k2, k3 = st.columns(3)
    with k1:
        st.metric("RERA Avg (AED)", f"{result['allowed_increase']['avg_index'] or '—'}")
    with k2:
        st.metric("Max Allowed % (Decree 43/2013)", f"{result['allowed_increase']['max_allowed_pct']}%")
    with k3:
        st.metric("Proposed %", f"{result['allowed_increase']['proposed_pct']:.1f}%")

    # Clause-by-clause verdict table
    if result.get("ejari_clause_results"):
        st.markdown("### 📌 Clause verdicts (from your PDF terms)")
        st.dataframe(pd.DataFrame(result["ejari_clause_results"]), width="stretch")

    # Text findings
    st.markdown("### 📌 Text findings")
    if not result["highlights"] and not result["rule_flags"]:
        st.info("No text-based issues detected.")
    for h in result["highlights"]:
        icon = "🔴" if h.get("severity") == "high" else "🟡"
        st.markdown(f"{icon} **{h['issue']}** — _{h['excerpt']}_")
        if h.get("suggestion"): st.caption(f"Suggestion: {h['suggestion']}")
        if h.get("law"): st.caption(f"Law: {h['law']}")
    for r in result["rule_flags"]:
        icon = "🔴" if r.get("severity") == "high" else "🟡"
        st.markdown(f"{icon} **{r['issue']}**")
        if r.get("suggestion"): st.caption(f"Suggestion: {r['suggestion']}")
        if r.get("law"): st.caption(f"Law: {r['law']}")

    # Inline highlights
    st.markdown("### 🖍️ Inline highlights in contract")
    html = ae.render_highlighted_html(text_input, result)
    st.components.v1.html(html, height=360, scrolling=True)

    # Export HTML report
    st.markdown("### ⤵️ Export")
    buf = io.BytesIO()
    report_html = ae.build_report_html(text_input, result)
    buf.write(report_html.encode("utf-8"))
    st.download_button("Download HTML report", data=buf.getvalue(),
                       file_name="audit_report.html", mime="text/html")

st.divider()
st.caption("Law 26/2007 & Law 33/2008 (tenancy, notice, eviction grounds), Decree 43/2013 (rent-increase slabs). Official sources prevail.")
