from __future__ import annotations
import os, json
from typing import Optional, Dict, Any, List
import streamlit as st
import pandas as pd
import audit_engine as ae

st.set_page_config(page_title="Dubai Tenancy Auditor — Firestore DB", page_icon="🏠", layout="wide")

# ----------------------------- Sidebar: Cloud & Index --------------------------
with st.sidebar:
    st.header("Cloud & Index")
    st.caption("Initialize Firestore (Admin SDK). Use **one** method below.")
    svc_upload = st.file_uploader("Upload serviceAccount.json (local dev only)", type=["json"], key="svcjson")

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

    st.markdown("---")
    st.subheader("RERA index (CSV upload)")
    st.caption("Optional CSV with columns like: `city,community,property_type,bedrooms,index_aed`.")
    rera_csv = st.file_uploader("Upload RERA index CSV", type=["csv"], key="rera_csv")

    if st.button("Open official RERA calculator", use_container_width=True):
        st.markdown("[Dubai Land Department — Rental Index](https://dubailand.gov.ae/en/eservices/rental-index/rental-index/#/)")

# ----------------------------- Main: Upload --------------------------------
st.title("Dubai Rental Contract Auditor — Ejari + OCR + RERA CSV")

cols = st.columns([1.1, 1.1])
with cols[0]:
    st.subheader("Upload Rental Contract (PDF)")
    up = st.file_uploader("Drag & drop or browse a PDF", type=["pdf"], accept_multiple_files=False, key="pdf_up")

    pdf_text = ""
    ejari_prefill = ae.EjariFields()
    parse_notes: List[str] = []
    if up is not None:
        b = up.read()
        parsed = ae.parse_pdf_smart(b)
        pdf_text = parsed["text"] or ""
        ejari_prefill = parsed["ejari"]
        parse_notes = parsed["notes"]
        if parsed.get("ocr_used"):
            st.success("OCR fallback used.")
        if pdf_text.strip():
            st.success("PDF text extracted.")
            st.info("Ejari-style fields detected and parsed.")
        else:
            st.warning("Couldn’t read text from the PDF. If it’s a scan, enable OCR locally.")

with cols[1]:
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

# ----------------------------- Form fields ---------------------------------
st.markdown("---")
st.subheader("Extracted / Editable Ejari fields")

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

if up is not None:
    parsed_w = _ejari_to_widgets(ejari_prefill)
    for k, v in parsed_w.items():
        if not st.session_state.ejari.get(k):
            st.session_state.ejari[k] = v

form1 = st.columns(2)
with form1[0]:
    st.session_state.ejari["city"] = st.selectbox("City", ["Dubai", "Abu Dhabi", "Sharjah"], index=0)
    st.session_state.ejari["community"] = st.text_input("Area / Community", value=st.session_state.ejari["community"])
    st.session_state.ejari["bedrooms"] = st.number_input("Bedrooms", min_value=0, max_value=15, value=int(st.session_state.ejari["bedrooms"]), step=1)
    st.session_state.ejari["security_deposit_aed"] = st.number_input("Security Deposit (AED)", min_value=0, value=int(st.session_state.ejari["security_deposit_aed"]), step=1000)
with form1[1]:
    st.session_state.ejari["property_type"] = st.selectbox(
        "Property Type",
        ["apartment", "villa", "townhouse"],
        index=["apartment", "villa", "townhouse"].index(st.session_state.ejari["property_type"])
    )
    st.session_state.ejari["current_annual_rent_aed"] = st.number_input("Current Annual Rent (AED)", min_value=0, value=int(st.session_state.ejari["current_annual_rent_aed"]), step=1000)
    st.session_state.ejari["proposed_new_rent_aed"] = st.number_input("Proposed New Rent (AED)", min_value=0, value=int(st.session_state.ejari["proposed_new_rent_aed"]), step=1000)

form2 = st.columns(2)
with form2[0]:
    st.session_state.ejari["renewal_date"] = st.date_input("Renewal Date", value=ae.to_date(st.session_state.ejari.get("renewal_date")))
    st.session_state.ejari["ejari_contact"] = st.text_input("Ejari Contact Number (optional)", value=st.session_state.ejari.get("ejari_contact", ""))
with form2[1]:
    st.session_state.ejari["notice_sent_date"] = st.date_input("Notice Sent Date", value=ae.to_date(st.session_state.ejari.get("notice_sent_date")))
    st.session_state.ejari["furnishing"] = st.selectbox(
        "Furnishing", ["unfurnished", "semi-furnished", "furnished"],
        index=["unfurnished", "semi-furnished", "furnished"].index(st.session_state.ejari["furnishing"])
    )

# ----------------------------- RERA CSV lookup (optional info) -------------
rera_index_aed: Optional[int] = None
if rera_csv is not None:
    try:
        df = pd.read_csv(rera_csv)
        city = st.session_state.ejari["city"]
        comm = st.session_state.ejari["community"]
        ptype = st.session_state.ejari["property_type"]
        beds = int(st.session_state.ejari["bedrooms"])
        q = df.copy()
        for col, val in [("city", city), ("property_type", ptype)]:
            if col in q.columns:
                q = q[q[col].astype(str).str.lower() == str(val).lower()]
        if "bedrooms" in q.columns:
            q = q[q["bedrooms"].astype(int) == beds]
        if "community" in q.columns and comm:
            q = q[q["community"].astype(str).str.contains(comm, case=False, na=False)]
        if not q.empty:
            rera_index_aed = int(float(q["index_aed"].median()))
            st.success(f"RERA index (CSV) match: **AED {rera_index_aed:,}**")
        else:
            st.info("No row matched in your CSV.")
    except Exception as e:
        st.error(f"CSV read error: {e}")

# ----------------------------- Run Audit -----------------------------------
st.markdown("---")
if st.button("Run audit now", use_container_width=True):
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

    res = ae.run_audit(st.session_state.contract_text or "", ej, rera_index_aed=rera_index_aed)

    # Headline verdict based ONLY on number of failed clauses
    failed_count = sum(1 for c in res.clause_findings if c.verdict == "fail")
    if failed_count == 0:
        st.success("PASS — 0 clauses failed.")
    else:
        st.error(f"FAIL — {failed_count} clause(s) failed.")

    # Optional info (not part of pass/fail)
    with st.expander("Optional RERA info (not used for pass/fail)"):
        st.write(f"Max allowed % by slabs (if applicable): {res.rera_max_increase_pct:.0f}%")
        st.write(f"Proposed increase % (informational): {res.proposed_increase_pct:.1f}%")

    # Clauses table with filter
    st.markdown("### 📌 Clause verdicts (from your contract)")
    verdict_filter = st.selectbox("Filter by verdict", ["All", "pass", "warn", "fail"], index=0)
    data = [{
        "clause": c.clause_no,
        "text": c.text,
        "verdict": c.verdict,
        "issues": c.issues,
    } for c in res.clause_findings]
    df = pd.DataFrame(data)
    if verdict_filter != "All":
        df = df[df["verdict"] == verdict_filter]
    st.dataframe(df, use_container_width=True)

    st.markdown("### Text findings (notes / context)")
    for t in res.text_findings:
        st.write("•", t)

    # Issues summary is simply the failed clauses (already reflected above), but we show any extras:
    if res.issues:
        st.markdown("### Non-clause issues (informational)")
        for i in res.issues:
            st.write("•", i)

    # Ledger write (optional)
    if ae.firebase_available():
        try:
            tenant = "tenant@example.com"
            landlord = "landlord@example.com"
            pdf_bytes = up.getvalue() if up is not None else None
            agreement_id = ae.write_ledger(tenant, landlord, ej, res, pdf_bytes=pdf_bytes, rera_index_aed=rera_index_aed)
            st.success(f"Ledger entry written ✓  (agreement id: `{agreement_id}`)")
        except Exception as e:
            st.error(f"Failed to write Firestore ledger: {e}")

st.markdown("---")
st.caption(
    "This uses rule checks aligned with Dubai tenancy regime including Decree 43/2013 rent slabs "
    "and Landlord–Tenant laws. For precise caps, consult the official RERA Rental Index."
)

st.caption(
    "Extractor: "
    + ("pdfminer" if getattr(ae, "_pdfminer_ok", False)
       else "pypdf" if getattr(ae, "_pypdf_ok", False)
       else "none")
)
