# app.py
# ----------------------------------------------------------------------
# Streamlit UI: upload PDF → parse → edit fields → run audit → (optional) read regulations
# ----------------------------------------------------------------------

from __future__ import annotations

import os
from typing import Optional, Dict, Any, List

import streamlit as st
import pandas as pd

import audit_engine as ae


# ------------------------- Page config -------------------------
st.set_page_config(
    page_title="Dubai Tenancy Auditor",
    page_icon="🏠",
    layout="wide",
)


# ------------------------- Sidebar: Firestore + Gemini -------------------------
with st.sidebar:
    st.header("Cloud & LLM")

    # --- Firestore init (optional, only if you want to load /regulations and write a ledger) ---
    st.caption("Initialize Firestore (Admin SDK). Use any ONE method below.")
    svc_upload = st.file_uploader("Upload serviceAccount.json (local dev)", type=["json"], key="svcjson")

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
        st.warning("Firestore not connected (LLM can still run without regs).")

    st.markdown("---")

    # --- Gemini API key (recommended path) ---
    st.subheader("LLM (Gemini)")
    st.caption("Paste your Google Generative AI API key below. For public hosting, use secrets/env instead of hard-coding.")

    # ⚠️ You asked to wire your key directly. It's insecure to hard-code in public repos.
    DEFAULT_GEMINI_KEY = "AIzaSyA3MbD2aXrFME6z0L5KvizA8YXL3kEQM0o"  # ← your key (replace/remove for public hosting)

    gemini_key = st.text_input(
        "Gemini API Key",
        type="password",
        value=os.environ.get("GEMINI_API_KEY", DEFAULT_GEMINI_KEY),
        help="For production, remove the default and use Streamlit Secrets or env vars.",
    )

    if gemini_key:
        os.environ["GEMINI_API_KEY"] = gemini_key
        st.success("Gemini key set for this session ✓")

    st.markdown("---")

    # (Optional) Quick link to RERA calculator (reference)
    if st.button("Open RERA Rental Index", use_container_width=True):
        st.markdown(
            "[Dubai Land Department — Rental Index](https://dubailand.gov.ae/en/eservices/rental-index/rental-index/#/)"
        )


# ------------------------- Helpers -------------------------
def _ejari_to_widgets(e: ae.EjariFields) -> Dict[str, Any]:
    return {
        "city": e.city or "Dubai",
        "community": e.community or "",
        "property_type": e.property_type or "apartment",
        "bedrooms": e.bedrooms or 1,
        "security_deposit_aed": e.security_deposit_aed or 0,
        "current_annual_rent_aed": e.current_annual_rent_aed or 0,
        "proposed_new_rent_aed": e.proposed_new_rent_aed or (e.current_annual_rent_aed or 0),
        "furnishing": e.furnishing or "unfurnished",
        "renewal_date": ae.to_date(e.renewal_date),
        "notice_sent_date": ae.to_date(e.notice_sent_date),
        "ejari_contact": e.ejari_contact or "",
    }


@st.cache_data(show_spinner=False)
def _load_regulations_from_firestore() -> List[Dict[str, str]]:
    """
    Returns a list of dicts with keys: title, article, text
    Only works if Firestore is initialized and a 'regulations' collection exists.
    """
    if not ae.firebase_available():
        return []
    try:
        db = ae._firestore  # type: ignore
        docs = list(db.collection("regulations").stream())
        out = []
        for d in docs:
            data = d.to_dict() or {}
            # keep only relevant keys
            out.append({
                "title": str(data.get("title", "")),
                "article": str(data.get("article", "")),
                "text": str(data.get("text", "")),
            })
        # Basic sanity filter
        out = [x for x in out if x["text"]]
        return out
    except Exception:
        return []


# ------------------------- Main: Upload & Edit -------------------------
st.title("Dubai Rental Contract Auditor (Ejari-style)")

left, right = st.columns([1.1, 1.1], gap="large")

with left:
    st.subheader("1) Upload Rental Contract (PDF)")
    up = st.file_uploader("Drag & drop or browse a PDF", type=["pdf"], accept_multiple_files=False, key="pdf_up")

    pdf_text = ""
    ejari_prefill = ae.EjariFields()
    parse_notes = []
    if up is not None:
        b = up.read()
        parsed = ae.parse_pdf_smart(b)
        pdf_text = parsed["text"] or ""
        ejari_prefill = parsed["ejari"]
        parse_notes = parsed["notes"]
        if parsed.get("ocr_used"):
            st.info("OCR fallback was used (PDF looked like images).")
        if pdf_text.strip():
            st.success("PDF text extracted.")
            if ejari_prefill:
                st.caption("Ejari-style fields parsed from the PDF where possible.")

with right:
    st.subheader("2) Contract Text (editable)")
    if "contract_text" not in st.session_state:
        st.session_state.contract_text = pdf_text

    # Replace text if a new PDF was uploaded and text differs
    if up is not None and pdf_text and pdf_text != st.session_state.get("contract_text", ""):
        st.session_state.contract_text = pdf_text

    st.session_state.contract_text = st.text_area(
        "Paste or edit contract text",
        value=st.session_state.contract_text or "",
        height=300,
        placeholder="The full terms & conditions from your contract…",
    )

# ------------------------- Ejari-style fields -------------------------
st.markdown("---")
st.subheader("3) Extracted / Editable Ejari fields")

if "ejari" not in st.session_state:
    st.session_state.ejari = _ejari_to_widgets(ejari_prefill)

# If new upload parsed fresh fields, merge (only fill blanks)
if up is not None:
    parsed_w = _ejari_to_widgets(ejari_prefill)
    for k, v in parsed_w.items():
        if not st.session_state.ejari.get(k):
            st.session_state.ejari[k] = v

col_a, col_b = st.columns(2, gap="large")

with col_a:
    st.session_state.ejari["city"] = st.selectbox(
        "City", ["Dubai", "Abu Dhabi", "Sharjah"], index=["Dubai", "Abu Dhabi", "Sharjah"].index(st.session_state.ejari["city"])
    )
    st.session_state.ejari["community"] = st.text_input("Area / Community", value=st.session_state.ejari["community"])
    st.session_state.ejari["bedrooms"] = st.number_input("Bedrooms", min_value=0, max_value=15, value=int(st.session_state.ejari["bedrooms"]), step=1)
    st.session_state.ejari["security_deposit_aed"] = st.number_input("Security Deposit (AED)", min_value=0, value=int(st.session_state.ejari["security_deposit_aed"]), step=1000)

with col_b:
    st.session_state.ejari["property_type"] = st.selectbox(
        "Property Type",
        ["apartment", "villa", "townhouse"],
        index=["apartment", "villa", "townhouse"].index(st.session_state.ejari["property_type"]),
    )
    st.session_state.ejari["current_annual_rent_aed"] = st.number_input("Current Annual Rent (AED)", min_value=0, value=int(st.session_state.ejari["current_annual_rent_aed"]), step=1000)
    st.session_state.ejari["proposed_new_rent_aed"] = st.number_input("Proposed New Rent (AED)", min_value=0, value=int(st.session_state.ejari["proposed_new_rent_aed"]), step=1000)

col_c, col_d = st.columns(2, gap="large")
with col_c:
    st.session_state.ejari["renewal_date"] = st.date_input("Renewal Date", value=ae.to_date(st.session_state.ejari.get("renewal_date")))
    st.session_state.ejari["ejari_contact"] = st.text_input("Ejari Contact Number (optional)", value=st.session_state.ejari.get("ejari_contact", ""))

with col_d:
    st.session_state.ejari["notice_sent_date"] = st.date_input("Notice Sent Date", value=ae.to_date(st.session_state.ejari.get("notice_sent_date")))
    st.session_state.ejari["furnishing"] = st.selectbox(
        "Furnishing",
        ["unfurnished", "semi-furnished", "furnished"],
        index=["unfurnished", "semi-furnished", "furnished"].index(st.session_state.ejari["furnishing"]),
    )

# ------------------------- Load regulations (optional) -------------------------
regulations = []
if ae.firebase_available():
    with st.spinner("Loading regulations from Firestore (/regulations)…"):
        regulations = _load_regulations_from_firestore()
        if regulations:
            st.caption(f"Loaded {len(regulations)} regulation articles from Firestore.")
        else:
            st.caption("No regulations found in Firestore. You can still audit; LLM will work with general rules.")

# ------------------------- Run audit -------------------------
st.markdown("---")
run_col = st.container()
with run_col:
    if st.button("Run audit", use_container_width=True):
        # Build EjariFields
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

        # Pull Gemini key from env (set by sidebar)
        key = os.environ.get("GEMINI_API_KEY", "")

        with st.spinner("Auditing clauses…"):
            result = ae.run_audit(
                st.session_state.contract_text or "",
                ej,
                rera_index_aed=None,          # not used in the simplified UI
                regulations=regulations,       # Firestore regs if available
                gemini_api_key=key             # enable LLM comparisons
            )

        # Enforce the simple final verdict rule in the UI:
        failures = [c for c in result.clause_findings if c.verdict == "fail"]
        fail_count = len(failures)
        if fail_count == 0:
            st.success("✅ PASS — No failing clauses found.")
        else:
            st.error(f"❌ FAIL — {fail_count} failing clause(s) found.")

        # Optional: show blocking issues (if the engine provided any)
        if result.issues:
            st.markdown("### Issues summary (blocking)")
            for item in result.issues:
                st.write("•", item)

        # Table + filter
        st.markdown("### Clause results")
        df = pd.DataFrame([{
            "clause": c.clause_no,
            "verdict": c.verdict,
            "issues": c.issues,
            "text": c.text,
        } for c in result.clause_findings])

        filter_choice = st.selectbox("Filter", ["All", "Pass", "Warn", "Fail"], index=0)
        if filter_choice != "All":
            df_show = df[df["verdict"].str.lower() == filter_choice.lower()]
        else:
            df_show = df

        st.dataframe(df_show, use_container_width=True)

# ------------------------- Footer -------------------------
st.markdown("---")
st.caption(
    "This prototype runs rule-based checks and optional Gemini comparisons against Dubai tenancy regulations you seed into Firestore (`/regulations`). "
    "Final UI verdict is PASS iff there are ZERO failing clauses."
)
st.caption(
    "Extractor backend: "
    + ("pdfminer" if getattr(ae, "_pdfminer_ok", False)
       else "pypdf" if getattr(ae, "_pypdf_ok", False)
       else "none")
)
