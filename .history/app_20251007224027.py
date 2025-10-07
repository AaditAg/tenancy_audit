# app.py
# ----------------------------------------------------------------------
# Streamlit UI: upload PDF → edit fields → click "Run audit"
# All regulation fetching & LLM comparison happens INSIDE audit_engine
# (no regs loaded or shown in frontend).
# - Headline shows PASS/FAIL with number of failing clauses (no %)
# - Clause table has a verdict filter (All / Fail / Warn / Pass)
# - Optional Gemini refinement (key from Secrets only)
# - Firestore init kept in sidebar
# ----------------------------------------------------------------------

from __future__ import annotations

import os
from typing import Dict, Any, List

import streamlit as st
import pandas as pd

import audit_engine as ae


# ------------------------- Page config -------------------------
st.set_page_config(
    page_title="Dubai Tenancy Auditor",
    page_icon="🏠",
    layout="wide",
)


# ------------------------- Sidebar -------------------------
with st.sidebar:
    st.header("Cloud")
    st.caption("Initialize Firestore (Admin SDK). Backend will read regulations from Firestore.")

    svc_upload = st.file_uploader("Upload serviceAccount.json (dev/local)", type=["json"], key="svcjson")
    if st.button("Initialize Firestore", use_container_width=True):
        try:
            if "firebase" in st.secrets:
                ae.firebase_init_from_mapping(dict(st.secrets["firebase"]))
            elif os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON"):
                ae.firebase_init_from_json_string(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
            elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                ae.firebase_init_from_file(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
            elif svc_upload is not None:
                ae.firebase_init_from_bytes(svc_upload.read())
            else:
                raise RuntimeError("No credentials in secrets/env/upload.")
            st.success("Firestore initialized ✓")
        except Exception as e:
            st.error(f"Firestore init failed: {e}")

    if ae.firebase_available():
        st.info("Firestore: **connected**")
    else:
        st.warning("Firestore not connected yet.")

    st.markdown("---")
    st.subheader("Audit speed / depth")

    use_llm = st.toggle(
        "Use Gemini refinement",
        value=True,
        help="If on, backend will send suspicious clauses to Gemini against Firestore regs."
    )
    clause_cap = st.slider(
        "Max suspicious clauses to LLM-check (backend)",
        min_value=5, max_value=100, value=25, step=5,
        help="Rule scan still runs on all lines; this caps LLM passes."
    )
    regs_limit = st.slider(
        "Max regulation articles to fetch (backend)",
        min_value=50, max_value=800, value=200, step=50,
        help="Backend fetch cap to keep audit < ~1 minute."
    )

    st.caption("Backend time budgets: regs fetch hard cap ~4s; total audit target < 60s.")

    st.markdown("---")
    st.subheader("Gemini API key (Secrets only)")
    if "GEMINI_API_KEY" in st.secrets and st.secrets["GEMINI_API_KEY"]:
        os.environ["GEMINI_API_KEY"] = st.secrets["GEMINI_API_KEY"]
        st.success("Gemini key found in Secrets ✓")
    else:
        if use_llm:
            st.error("Add GEMINI_API_KEY in Streamlit Secrets (Settings → Secrets). LLM will be skipped.")
        else:
            st.info("Gemini disabled.")


# ------------------------- UI Helpers -------------------------
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


# ------------------------- Main layout -------------------------
st.title("Dubai Rental Contract Auditor")

top = st.columns([1.15, 1.15])

# Upload + parse
with top[0]:
    st.subheader("Upload Rental Contract (PDF)")
    up = st.file_uploader("Drag & drop or browse", type=["pdf"], accept_multiple_files=False, key="pdf_up")

    pdf_text = ""
    ejari_prefill = ae.EjariFields()
    parse_notes: List[str] = []
    if up is not None:
        b = up.read()
        parsed = ae.parse_pdf_smart(b)
        pdf_text = parsed.get("text") or ""
        ejari_prefill = parsed.get("ejari") or ae.EjariFields()
        parse_notes = parsed.get("notes") or []
        if parsed.get("ocr_used"):
            st.info("OCR fallback used (scanned PDF).")
        if pdf_text.strip():
            st.success("PDF text extracted.")
        else:
            st.error("No text extracted from PDF — try a different file.")

# Editable contract text
with top[1]:
    st.subheader("Contract Text (editable)")
    if "contract_text" not in st.session_state:
        st.session_state.contract_text = pdf_text
    if up is not None and pdf_text and pdf_text != st.session_state.get("contract_text", ""):
        st.session_state.contract_text = pdf_text

    st.session_state.contract_text = st.text_area(
        "Paste or edit contract text",
        value=st.session_state.contract_text or "",
        height=280,
        placeholder="Paste contract text here…",
    )

# Ejari fields
st.markdown("---")
st.subheader("Extracted / Editable terms")

if "ejari" not in st.session_state:
    st.session_state.ejari = _ejari_to_widgets(ejari_prefill)

if up is not None:
    parsed_w = _ejari_to_widgets(ejari_prefill)
    for k, v in parsed_w.items():
        if not st.session_state.ejari.get(k):
            st.session_state.ejari[k] = v

col1, col2 = st.columns(2)
with col1:
    st.session_state.ejari["city"] = st.selectbox("City", ["Dubai", "Abu Dhabi", "Sharjah"], index=0)
    st.session_state.ejari["community"] = st.text_input("Area / Community", value=st.session_state.ejari["community"])
    st.session_state.ejari["bedrooms"] = st.number_input(
        "Bedrooms", min_value=0, max_value=15, value=int(st.session_state.ejari["bedrooms"]), step=1
    )
    st.session_state.ejari["security_deposit_aed"] = st.number_input(
        "Security Deposit (AED)", min_value=0, value=int(st.session_state.ejari["security_deposit_aed"]), step=1000
    )
with col2:
    st.session_state.ejari["property_type"] = st.selectbox(
        "Property Type", ["apartment", "villa", "townhouse"],
        index=["apartment", "villa", "townhouse"].index(st.session_state.ejari["property_type"])
    )
    st.session_state.ejari["current_annual_rent_aed"] = st.number_input(
        "Current Annual Rent (AED)", min_value=0, value=int(st.session_state.ejari["current_annual_rent_aed"]), step=1000
    )
    st.session_state.ejari["proposed_new_rent_aed"] = st.number_input(
        "Proposed New Rent (AED)", min_value=0, value=int(st.session_state.ejari["proposed_new_rent_aed"]), step=1000
    )

col3, col4 = st.columns(2)
with col3:
    st.session_state.ejari["renewal_date"] = st.date_input(
        "Renewal Date", value=ae.to_date(st.session_state.ejari.get("renewal_date"))
    )
    st.session_state.ejari["ejari_contact"] = st.text_input(
        "Ejari Contact Number (optional)", value=st.session_state.ejari.get("ejari_contact", "")
    )
with col4:
    st.session_state.ejari["notice_sent_date"] = st.date_input(
        "Notice Sent Date", value=ae.to_date(st.session_state.ejari.get("notice_sent_date"))
    )
    st.session_state.ejari["furnishing"] = st.selectbox(
        "Furnishing", ["unfurnished", "semi-furnished", "furnished"],
        index=["unfurnished", "semi-furnished", "furnished"].index(st.session_state.ejari["furnishing"])
    )

# ------------------------- Run Audit (backend handles regs & LLM) -------------------------
st.markdown("---")

if st.button("Run audit", use_container_width=True):
    if not ae.firebase_available():
        st.error("Firestore not connected. Initialize it from the sidebar first.")
        st.stop()

    if not hasattr(ae, "audit_from_firestore"):
        st.error("Backend is missing: audit_engine.audit_from_firestore(...). Add this function in audit_engine.py.")
        st.stop()

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

    with st.spinner("Auditing (Firestore regs + optional Gemini)…"):
        # backend must fetch regulations and do all comparisons/LLM there
        res = ae.audit_from_firestore(
            contract_text=st.session_state.contract_text or "",
            ejari=ej,
            use_llm=bool(use_llm and os.environ.get("GEMINI_API_KEY")),
            clause_cap=int(clause_cap),
            regs_limit=int(regs_limit),
            hard_timeout_sec=4.0,
            time_budget_sec=60,  # keep total under ~1 minute
        )

    # Verdict header: number of failing clauses only
    fail_count = sum(1 for c in res.clause_findings if c.verdict == "fail")
    if fail_count == 0:
        st.success("PASS — 0 failing clauses.")
    else:
        st.error(f"FAIL — {fail_count} failing clause(s).")

    # Clause table with filter
    st.markdown("### 📌 Clause verdicts")
    filter_val = st.selectbox("Filter by verdict", ["All", "Fail", "Warn", "Pass"], index=0)
    rows = [{
        "clause": c.clause_no,
        "verdict": c.verdict,
        "issues": c.issues,
        "text": c.text,
    } for c in res.clause_findings]
    df = pd.DataFrame(rows)
    if filter_val != "All":
        df = df[df["verdict"].str.lower() == filter_val.lower()]
    st.dataframe(df, use_container_width=True)

    # Issues summary (if any)
    if res.issues:
        st.markdown("### Issues summary")
        for msg in res.issues:
            st.write("•", msg)

    # Ledger write (optional)
    if ae.firebase_available():
        try:
            tenant = "tenant@example.com"
            landlord = "landlord@example.com"
            pdf_bytes = up.getvalue() if up is not None else None
            agreement_id = ae.write_ledger(
                tenant, landlord, ej, res, pdf_bytes=pdf_bytes, rera_index_aed=None
            )
            st.success(f"Ledger entry written ✓  (agreement id: `{agreement_id}`)")
        except Exception as e:
            st.error(f"Failed to write Firestore ledger: {e}")

# ------------------------- Footer -------------------------
st.markdown("---")
st.caption(
    "This UI never fetches or shows regulations; the backend (audit_engine.py) loads Firestore regs and compares every clause."
)
st.caption(
    "Extractor backend: "
    + ("pdfminer" if getattr(ae, "_pdfminer_ok", False)
       else "pypdf" if getattr(ae, "_pypdf_ok", False)
       else "none")
)