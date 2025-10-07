# app.py
# ----------------------------------------------------------------------
# Streamlit UI (public-friendly):
# - Upload PDF → extract → edit → run audit
# - Verdict shows ONLY "failed clauses count"
# - Table has filter (All / Pass / Warn / Fail)
# - Optional Gemini LLM cross-check against Firestore /pdf_articles
# ----------------------------------------------------------------------

from __future__ import annotations

import os
from typing import Optional, Dict, Any

import streamlit as st
import pandas as pd

import audit_engine as ae

st.set_page_config(
    page_title="Dubai Tenancy Auditor — LLM + Firestore",
    page_icon="🏠",
    layout="wide",
)

# ----------------------------- Sidebar: Firestore & Gemini -----------------
with st.sidebar:
    st.header("Cloud")
    st.caption("Load Firestore (Admin SDK). Use secrets, env, or local JSON.")
    service_json_uploaded = st.file_uploader("serviceAccountKeypee.json (local dev)", type=["json"], key="svcjson")

    if st.button("Initialize Firestore", use_container_width=True):
        try:
            if "firebase" in st.secrets:
                ae.firebase_init_from_mapping(dict(st.secrets["firebase"]))
            elif os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON"):
                ae.firebase_init_from_mapping(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
            elif service_json_uploaded is not None:
                ae.firebase_init_from_mapping(service_json_uploaded.read().decode("utf-8"))
            elif os.path.exists("serviceAccountKeypee.json"):
                ae.firebase_init_from_file("serviceAccountKeypee.json")
            else:
                raise RuntimeError("No credentials found (secrets/env/upload/local file).")
            st.success("Firestore initialized ✓")
        except Exception as e:
            st.error(f"Firestore init failed: {e}")

    if ae.firebase_available():
        st.info("Firestore: **connected**")
    else:
        st.warning("Firestore: not connected (LLM check still works without it, but needs regs).")

    st.markdown("---")
    st.subheader("Gemini API")
    st.caption("Provide your Gemini API key to enable clause-vs-law checks.")
    default_key = st.secrets.get("GEMINI_API_KEY", "") if "GEMINI_API_KEY" in st.secrets else ""
    gemini_key = st.text_input("Gemini API Key", value=default_key, type="password")

# ----------------------------- Main: Upload --------------------------------
st.title("Dubai Rental Contract Auditor")

col_up, col_text = st.columns(2)
with col_up:
    st.subheader("1) Upload Rental Contract (PDF)")
    up = st.file_uploader("Drag & drop or browse PDF", type=["pdf"], accept_multiple_files=False, key="pdf_up")

    pdf_text = ""
    parse_notes = []
    ejari_prefill = ae.EjariFields()

    if up is not None:
        b = up.read()
        parsed = ae.parse_pdf_smart(b)
        pdf_text = parsed["text"] or ""
        ejari_prefill = parsed["ejari"]
        parse_notes = parsed["notes"]
        if parsed.get("ocr_used"):
            st.info("OCR fallback used.")
        if pdf_text.strip():
            st.success("PDF text extracted.")
        else:
            st.error("Could not extract text from the PDF.")

with col_text:
    st.subheader("2) Contract Text (editable)")
    if "contract_text" not in st.session_state:
        st.session_state.contract_text = pdf_text
    # If new upload has fresh text, sync
    if up is not None and pdf_text and pdf_text != st.session_state.get("contract_text", ""):
        st.session_state.contract_text = pdf_text

    st.session_state.contract_text = st.text_area(
        "Paste or edit contract text",
        value=st.session_state.contract_text or "",
        height=320,
        placeholder="Paste your contract terms here…",
    )

st.markdown("---")
st.subheader("3) Extracted Ejari-like Fields (editable)")
def _ejari_to_widgets(e: ae.EjariFields) -> Dict[str, Any]:
    return {
        "city": e.city or "Dubai",
        "community": e.community or "",
        "property_type": e.property_type or "apartment",
        "bedrooms": e.bedrooms or 1,
        "security_deposit_aed": e.security_deposit_aed or 0,
        "current_annual_rent_aed": e.current_annual_rent_aed or 0,
        "proposed_new_rent_aed": e.proposed_new_rent_aed or e.current_annual_rent_aed or 0,
        "furnishing": e.furnishing or "unfurnished",
        "renewal_date": ae.to_date(e.renewal_date),
        "notice_sent_date": ae.to_date(e.notice_sent_date),
        "ejari_contact": e.ejari_contact or "",
    }

if "ejari" not in st.session_state:
    st.session_state.ejari = _ejari_to_widgets(ejari_prefill)

# If new upload changed parsed values, only fill blanks
if up is not None:
    parsed_w = _ejari_to_widgets(ejari_prefill)
    for k, v in parsed_w.items():
        if not st.session_state.ejari.get(k):
            st.session_state.ejari[k] = v

f1, f2 = st.columns(2)
with f1:
    st.session_state.ejari["city"] = st.selectbox("City", ["Dubai", "Abu Dhabi", "Sharjah"], index=0)
    st.session_state.ejari["community"] = st.text_input("Area / Community", value=st.session_state.ejari["community"])
    st.session_state.ejari["bedrooms"] = st.number_input("Bedrooms", 0, 15, int(st.session_state.ejari["bedrooms"]), 1)
    st.session_state.ejari["security_deposit_aed"] = st.number_input("Security Deposit (AED)", 0, 1_000_000, int(st.session_state.ejari["security_deposit_aed"]), 1000)
with f2:
    st.session_state.ejari["property_type"] = st.selectbox("Property Type", ["apartment", "villa", "townhouse"],
        index=["apartment", "villa", "townhouse"].index(st.session_state.ejari["property_type"]))
    st.session_state.ejari["current_annual_rent_aed"] = st.number_input("Current Annual Rent (AED)", 0, 10_000_000, int(st.session_state.ejari["current_annual_rent_aed"]), 1000)
    st.session_state.ejari["proposed_new_rent_aed"] = st.number_input("Proposed New Rent (AED)", 0, 10_000_000, int(st.session_state.ejari["proposed_new_rent_aed"]), 1000)

f3, f4 = st.columns(2)
with f3:
    st.session_state.ejari["renewal_date"] = st.date_input("Renewal Date", value=ae.to_date(st.session_state.ejari.get("renewal_date")))
    st.session_state.ejari["ejari_contact"] = st.text_input("Ejari Contact Number (optional)", value=st.session_state.ejari.get("ejari_contact",""))
with f4:
    st.session_state.ejari["notice_sent_date"] = st.date_input("Notice Sent Date", value=ae.to_date(st.session_state.ejari.get("notice_sent_date")))
    st.session_state.ejari["furnishing"] = st.selectbox("Furnishing", ["unfurnished","semi-furnished","furnished"],
        index=["unfurnished","semi-furnished","furnished"].index(st.session_state.ejari["furnishing"]))

# ----------------------------- Run Audit -----------------------------------
st.markdown("---")
if st.button("Run audit", use_container_width=True):
    ej = ae.EjariFields(
        city=st.session_state.ejari["city"],
        community=st.session_state.ejari["community"],
        property_type=st.session_state.ejari["property_type"],
        bedrooms=int(st.session_state.ejari["bedrooms"]),
        security_deposit_aed=int(st.session_state.ejari["security_deposit_aed"]),
        current_annual_rent_aed=int(st.session_state.ejari["current_annual_rent_aed"]),
        proposed_new_rent_aed=int(st.session_state.ejari["proposed_new_rent_aed"]),
        furnishing=st.session_state.ejari["furnishing"],
        renewal_date=ae.to_date(st.session_state.ejari["renewal_date"]),
        notice_sent_date=ae.to_date(st.session_state.ejari["notice_sent_date"]),
        ejari_contact=st.session_state.ejari.get("ejari_contact") or None,
    )

    # LLM key optional; if provided, LLM will judge each clause against all regs.
    key = gemini_key.strip() or None

    res = ae.run_audit(
        st.session_state.contract_text or "",
        ej,
        gemini_api_key=key,
        regs_collection="pdf_articles",
    )

    # Header verdict: show only failed-clause count
    verdict_color = st.success if res.verdict == "pass" else st.error
    verdict_color(f"{'PASS' if res.verdict=='pass' else 'FAIL'} — Failed clauses: {res.failed_count}")

    # Filter control for the table
    filter_choice = st.selectbox("Filter clauses", ["All", "Pass", "Warn", "Fail"], index=0)
    df = pd.DataFrame([{
        "clause_no": c.clause_no,
        "text": c.text,
        "verdict": c.verdict,
        "issues": c.issues,
        "llm_reason": c.llm_reason or "",
        "matched_regs": ", ".join(c.matched_regs or []),
    } for c in res.clause_findings])

    if filter_choice != "All":
        df = df[df["verdict"].str.lower() == filter_choice.lower()]

    st.markdown("### Clause checks")
    st.dataframe(df, use_container_width=True)

    if res.notes:
        st.markdown("### Notes")
        for n in res.notes:
            st.write("•", n)

    # Optional: write to ledger if Firestore ready
    if ae.firebase_available():
        try:
            tenant = "tenant@example.com"
            landlord = "landlord@example.com"
            pdf_bytes = up.getvalue() if up is not None else None
            agreement_id = ae.write_ledger(tenant, landlord, ej, res, pdf_bytes=pdf_bytes)
            st.success(f"Ledger entry written ✓  (agreement id: `{agreement_id}`)")
        except Exception as e:
            st.error(f"Failed to write Firestore ledger: {e}")

# Footer
st.markdown("---")
st.caption(
    "LLM outputs are best-effort checks against the regulation articles you've loaded in Firestore. "
    "Always verify with the official RERA Rental Index and current Dubai tenancy legislation."
)
