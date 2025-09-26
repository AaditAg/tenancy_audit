# app.py — Dubai Tenancy Auditor + Firestore Database (agreements, snapshots, audits, events, ledger)
# -----------------------------------------------------------------------------
# macOS quickstart:
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install streamlit pdfminer.six pandas python-dateutil pytesseract pdf2image pillow reportlab chardet firebase-admin
#   # OCR for scanned PDFs: brew install tesseract
#   streamlit run app.py
#
# SECURITY:
#   • Use Streamlit secrets or env vars for Firestore service account.
#   • Rotate any key you pasted publicly: IAM → Service Accounts → Keys.

from __future__ import annotations
import io
import os
from typing import Optional, Dict, Any, List

import streamlit as st
import pandas as pd

import audit_engine as ae

st.set_page_config(
    page_title="Dubai Tenancy Auditor — Firestore DB",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------- Sidebar: Support & Firestore ---------------------
st.sidebar.title("⚙️ Settings & Data")

st.sidebar.markdown("**Ejari / DLD Support**")
st.sidebar.markdown("☎️ **DLD unified toll-free:** **8004488**")
st.sidebar.caption("For official clarifications. (Do not rely on this app as legal advice.)")

st.sidebar.divider()
st.sidebar.info(
    "Optional **RERA CSV** columns: city, area, property_type, bedrooms_min, bedrooms_max, average_annual_rent_aed; optional furnished."
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
    value=False,
)

st.sidebar.divider()
st.sidebar.markdown("**Firestore (Database) — initialize**")
st.sidebar.caption(
    "Provide credentials via Streamlit secrets ([firebase] section), "
    "GOOGLE_APPLICATION_CREDENTIALS, or FIREBASE_SERVICE_ACCOUNT_JSON."
)

firebase_ready = False
if st.sidebar.button("Initialize Firestore"):
    try:
        creds = None
        if "firebase" in st.secrets:
            creds = dict(st.secrets["firebase"])
            ae.firebase_init_from_mapping(creds)
        elif os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON"):
            ae.firebase_init_from_json_string(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
        elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            ae.firebase_init_from_file(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        else:
            uploaded = st.sidebar.file_uploader("Upload serviceAccount.json (local dev only)", type=["json"], key="svcjson")
            if uploaded is not None:
                ae.firebase_init_from_bytes(uploaded.read())
            else:
                raise RuntimeError("No Firestore credentials provided.")
        firebase_ready = ae.firebase_is_ready()
        if firebase_ready:
            st.sidebar.success("Firestore initialized ✓")
        else:
            st.sidebar.error("Firestore init incomplete.")
    except Exception as e:
        st.sidebar.error(f"Firestore init failed: {e}")

# Sample PDF
st.sidebar.divider()
if st.sidebar.button("Generate a sample Ejari-style PDF"):
    buf = ae.generate_sample_ejari_pdf()
    st.sidebar.download_button(
        "Download sample_ejari_contract.pdf",
        data=buf.getvalue(),
        file_name="sample_ejari_contract.pdf",
        mime="application/pdf",
    )

# --------------------- Header ---------------------
st.title("🏠 Dubai Tenancy Auditor — Firestore Database")
st.caption(
    "Upload a tenancy contract, parse Ejari-style fields, audit against Dubai laws (Law 26/2007, Law 33/2008, "
    "Decree 43/2013), use RERA CSV or official calculator overrides, and persist results to **Firestore** "
    "(agreements, snapshots, audits, events, ledger)."
)

# --------------------- Upload & parse ---------------------
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

# --------------------- Force-fill UI boxes ---------------------
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
    options = ["apartment", "villa", "townhouse"]
    ptype = st.selectbox("Property Type", options, index=options.index(ptype_val) if ptype_val in options else 0)
    bedrooms = st.number_input("Bedrooms", min_value=0, max_value=10, step=1, value=int(pdf_prefill.get("bedrooms") or 0))
with cC:
    current_rent = st.number_input("Current Annual Rent (AED)", min_value=0, step=500, value=int(pdf_prefill.get("annual_rent") or 0))
    proposed_rent = st.number_input("Proposed New Rent (AED)", min_value=0, step=500, value=int(pdf_prefill.get("proposed_rent") or (current_rent or 0)))
with cD:
    renewal_date = st.date_input("Renewal Date", value=ae.to_date(pdf_prefill.get("renewal_date") or pdf_prefill.get("end_date") or None))
    notice_sent_date = st.date_input("Notice Sent Date", value=ae.to_date(pdf_prefill.get("notice_sent_date") or None))

cE, cF = st.columns([1, 1])
with cE:
    deposit = st.number_input("Security Deposit (AED)", min_value=0, step=500, value=int(pdf_prefill.get("deposit") or 0))
with cF:
    furnished = st.selectbox("Furnishing", ["unfurnished", "semi", "furnished"], index=0)

# --------------------- Terms table ---------------------
st.markdown("### 📜 Parsed Terms & Conditions (from your PDF)")
clauses_df = pd.DataFrame([{"clause": c.get("num"), "text": c.get("text", "").strip()} for c in ejari.get("clauses", [])])
if not clauses_df.empty:
    st.dataframe(clauses_df, width="stretch")
else:
    st.info("No numbered clauses were found under a ‘Terms & Conditions’ section of the PDF.")

st.divider()

# --------------------- RERA CSV + Calculator override ---------------------
st.subheader("📊 RERA Index")
rera_avg = None
if rera_df is not None:
    matched = ae.lookup_rera_row(rera_df, city=city, area=area, property_type=ptype, bedrooms=int(bedrooms), furnished=furnished)
    if matched is not None and not matched.empty:
        st.success("Matched RERA CSV index row:")
        st.dataframe(matched.reset_index(drop=True), width="stretch")
        rera_avg = float(matched.iloc[0]["average_annual_rent_aed"])
    else:
        st.warning("No exact CSV match; you can still audit, or use the official calculator overrides below.")
else:
    st.info("Upload a RERA CSV in the sidebar to enable auto slabs (or use official calculator overrides below).")

with st.expander("Use Official RERA Calculator (manual override)"):
    st.markdown(
        "- Open DLD’s official page in a browser and fill your details.\n"
        "- Paste the **Average market rent (AED)** and **Allowed max increase %** below."
    )
    st.link_button("Open official RERA Rental Index", "https://dubailand.gov.ae/en/eservices/rental-index/rental-index/#/")
    rera_avg_override = st.number_input("‘Average market rent’ (AED) — override", min_value=0, step=500, value=0)
    allowed_pct_override = st.number_input("‘Allowed max increase %’ — override", min_value=0, max_value=100, step=1, value=0)
    if rera_avg_override > 0:
        rera_avg = float(rera_avg_override)

# --------------------- Audit ---------------------
st.subheader("🔎 Audit")
strict_mode = st.checkbox("Strict mode (fail on any issue)", value=False, help="If off, only HIGH severity issues cause FAIL.")

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
        if allowed_pct_override > 0:
            result["allowed_increase"]["max_allowed_pct"] = int(allowed_pct_override)

    if result["verdict"] == "pass":
        st.success("PASS — no blocking issues.")
    else:
        st.error("FAIL — issues found.")

    # KPIs
    k1, k2, k3 = st.columns(3)
    with k1:
        st.metric("RERA Avg (AED)", f"{result['allowed_increase']['avg_index'] or '—'}")
    with k2:
        st.metric("Max Allowed %", f"{result['allowed_increase']['max_allowed_pct']}%")
    with k3:
        st.metric("Proposed %", f"{result['allowed_increase']['proposed_pct']:.1f}%")

    # Clause verdicts
    if result.get("ejari_clause_results"):
        st.markdown("### 📌 Clause verdicts")
        st.dataframe(pd.DataFrame(result["ejari_clause_results"]), width="stretch")

    # Findings
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

    # Highlighted body
    st.markdown("### 🖍️ Inline highlights in contract")
    html = ae.render_highlighted_html(text_input, result)
    st.components.v1.html(html, height=360, scrolling=True)

    # Export HTML
    st.markdown("### ⤵️ Export")
    buf = io.BytesIO()
    report_html = ae.build_report_html(text_input, result)
    buf.write(report_html.encode("utf-8"))
    st.download_button("Download HTML report", data=buf.getvalue(), file_name="audit_report.html", mime="text/html")

    st.divider()

    # ===================== Firestore Database UI =====================
    st.subheader("🗄️ Firestore Database — Agreements, Snapshots, Audits, Events, Ledger")

    if not ae.firebase_is_ready():
        st.warning("Firestore is not initialized. Use the sidebar to initialize.")
    else:
        # Agreement identity
        defaults = {
            "city": city, "area": area, "property_type": ptype, "bedrooms": int(bedrooms),
            "current_rent": float(current_rent), "proposed_rent": float(proposed_rent),
            "renewal_date": renewal_date.isoformat(), "notice_sent_date": notice_sent_date.isoformat(),
            "deposit": float(deposit), "furnished": furnished,
        }
        agreement_id = st.text_input("Agreement ID", value=ae.sha256_text((area or "") + str(current_rent)))
        tenant_id = st.text_input("Tenant ID (email/uid)", value="")
        landlord_id = st.text_input("Landlord ID (email/uid)", value="")

        # Create/Upsert agreement doc
        if st.button("Create/Update Agreement Doc"):
            try:
                doc = ae.fs_upsert_agreement(
                    agreement_id=agreement_id,
                    base_metadata=defaults | {"tenant_id": tenant_id, "landlord_id": landlord_id},
                )
                st.success(f"Agreement upserted at: {doc['path']}")
            except Exception as e:
                st.error(f"Agreement upsert failed: {e}")

        colS1, colS2 = st.columns([1, 1])

        with colS1:
            if st.button("Save Contract Snapshot"):
                try:
                    snap = ae.fs_save_contract_snapshot(
                        agreement_id=agreement_id,
                        raw_text=text_input,
                        parsed_fields=ejari,
                    )
                    st.success(f"Snapshot saved: {snap['path']}")
                except Exception as e:
                    st.error(f"Snapshot save failed: {e}")

        with colS2:
            if st.button("Save Latest Audit Result"):
                try:
                    saved = ae.fs_save_audit_result(agreement_id=agreement_id, audit_result=result)
                    st.success(f"Audit saved: {saved['path']}")
                except Exception as e:
                    st.error(f"Audit save failed: {e}")

        st.markdown("#### Events (free-form timeline)")
        ev_col1, ev_col2 = st.columns([3, 1])
        with ev_col1:
            ev_kind = st.selectbox("Event kind", ["notice_sent", "notice_received", "payment", "dispute", "other"])
            ev_note = st.text_input("Event note", value="")
        with ev_col2:
            if st.button("Append Event"):
                try:
                    ev = ae.fs_append_event(agreement_id=agreement_id, kind=ev_kind, note=ev_note, extra={})
                    st.success(f"Event appended: {ev['path']}")
                except Exception as e:
                    st.error(f"Event append failed: {e}")

        if st.button("List Events / History"):
            try:
                rows = ae.fs_list_events(agreement_id=agreement_id)
                if rows:
                    st.dataframe(pd.DataFrame(rows), width="stretch")
                else:
                    st.info("No events found for this agreement.")
            except Exception as e:
                st.error(f"List events failed: {e}")

        st.markdown("#### Ledger (append-only, hash-chained)")
        contract_hash = ae.sha256_text(text_input or "")
        audit_hash = ae.sha256_json(result)

        cL1, cL2 = st.columns([1, 1])
        with cL1:
            st.text_input("Contract SHA256", value=contract_hash, disabled=True)
        with cL2:
            st.text_input("Audit SHA256", value=audit_hash, disabled=True)

        ledger_namespace = "agreements"
        if st.button("Append ledger entry"):
            try:
                entry = ae.ledger_append(
                    namespace=ledger_namespace,
                    agreement_id=agreement_id,
                    payload={
                        "contract_sha256": contract_hash,
                        "audit_sha256": audit_hash,
                        "rera_avg": result["allowed_increase"]["avg_index"],
                        "max_allowed_pct": result["allowed_increase"]["max_allowed_pct"],
                        "proposed_pct": result["allowed_increase"]["proposed_pct"],
                        "verdict": result["verdict"],
                    },
                )
                st.success(f"Ledger appended: idx={entry['index']} hash={entry['this_hash'][:16]}…")
            except Exception as e:
                st.error(f"Ledger append failed: {e}")

        if st.button("Verify ledger chain"):
            try:
                ok, msg = ae.ledger_verify(namespace=ledger_namespace, agreement_id=agreement_id)
                if ok:
                    st.success("Ledger chain OK ✅")
                else:
                    st.error(f"Ledger chain FAIL ❌ — {msg}")
            except Exception as e:
                st.error(f"Verification error: {e}")

st.divider()
st.caption("Laws followed: Law 26/2007, Law 33/2008 (90-day renewal notice), Decree 43/2013 (rent-increase slabs). Official sources prevail.")
