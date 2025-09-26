# app.py — Dubai Tenancy Auditor with Firestore (agreements + audits + ledger)
# -----------------------------------------------------------------------------
# macOS quickstart:
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install streamlit pdfminer.six pandas python-dateutil pytesseract pdf2image pillow reportlab chardet firebase-admin
#   # OCR for scanned PDFs: brew install tesseract
#   streamlit run app.py
#
# SECURITY:
#   • Do NOT hard-code your Firebase private key. Use Streamlit secrets or env vars.
#   • After testing, rotate any key you pasted in public.

from __future__ import annotations
import io
import os
from typing import Optional, Dict, Any, List

import streamlit as st
import pandas as pd

import audit_engine as ae

st.set_page_config(
    page_title="Dubai Tenancy Auditor — Ejari + RERA + Firestore",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------- Sidebar: Support, RERA, Firestore init ---------------------
st.sidebar.title("⚙️ Settings & Data")

# Ejari / DLD Contact (UI-only)
st.sidebar.markdown("**Ejari / DLD Support**")
st.sidebar.markdown("☎️ **DLD unified toll-free:** **8004488**")
st.sidebar.caption("For official guidance on Ejari & tenancy matters.")

# RERA CSV (optional)
st.sidebar.divider()
st.sidebar.info(
    "**RERA CSV columns (case-insensitive):**\n"
    "city, area, property_type, bedrooms_min, bedrooms_max, average_annual_rent_aed.\n"
    "Optional: furnished."
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
    "If PDF misses some fields, try autofill from text",
    value=False,
)

# Firestore initialization
st.sidebar.divider()
st.sidebar.markdown("**Firestore (Cloud Firestore) — init**")
st.sidebar.caption("Provide credentials via Streamlit secrets or environment variables. You can also upload JSON for local dev only.")

firebase_ready = False

firebase_creds_dict = None
if "firebase" in st.secrets:
    firebase_creds_dict = dict(st.secrets["firebase"])

creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
inline_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

firebase_upload = None
if not firebase_creds_dict and not creds_path and not inline_json:
    firebase_upload = st.sidebar.file_uploader("Upload serviceAccount.json (local dev only)", type=["json"])

if st.sidebar.button("Initialize Firestore"):
    try:
        if firebase_creds_dict:
            ae.firebase_init_from_mapping(firebase_creds_dict)
        elif inline_json:
            ae.firebase_init_from_json_string(inline_json)
        elif creds_path and os.path.exists(creds_path):
            ae.firebase_init_from_file(creds_path)
        elif firebase_upload is not None:
            ae.firebase_init_from_bytes(firebase_upload.read())
        else:
            raise RuntimeError("No Firebase credentials found.")
        firebase_ready = True
        st.sidebar.success("Firestore initialized ✓")
    except Exception as e:
        st.sidebar.error(f"Init failed: {e}")
else:
    st.sidebar.caption("Status: click to initialize when ready.")

# Sample PDF generator
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
st.title("🏠 Dubai Tenancy Auditor — Ejari + RERA + Firestore")
st.caption(
    "Upload a tenancy contract (PDF). We extract fields & clauses verbatim (OCR fallback), audit against Dubai laws "
    "(Law 26/2007, Law 33/2008, Decree 43/2013), match a RERA CSV or override with the official RERA calculator, and "
    "persist to Firestore as agreements + audits + a hash-chained ledger."
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
            st.error("No text found. If scanned, install Tesseract and retry.")
        if ocr_used:
            st.info("OCR fallback was used.")
        if ejari:
            st.info("Ejari-like fields were detected and parsed.")

with right:
    st.subheader("📄 Contract Text (editable, verbatim from your PDF)")
    default_text = (
        "Upload a PDF on the left, or paste text here.\n"
        "This box is overwritten with the file’s contents after each upload."
    )
    text_value = raw_text or default_text
    # Use session_state so we can programmatically load Firestore docs later
    if "contract_text" not in st.session_state:
        st.session_state.contract_text = text_value
    # If a new PDF arrived, overwrite
    if raw_text:
        st.session_state.contract_text = raw_text
    text_input = st.text_area(
        "Paste or edit contract text (this exact text is audited)",
        value=st.session_state.contract_text,
        height=320,
        key="contract_text",
    )

if notes:
    with st.expander("Parser notes"):
        for n in notes:
            st.caption(f"• {n}")

st.divider()

# --------------------- Force-fill fields from PDF (with optional text autofill) ---------------------
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

# --------------------- Terms table (from Ejari) ---------------------
st.markdown("### 📜 Parsed Terms & Conditions (from your PDF)")
clauses_df = pd.DataFrame([{"clause": c.get("num"), "text": c.get("text", "").strip()} for c in ejari.get("clauses", [])])
if not clauses_df.empty:
    st.dataframe(clauses_df, width="stretch")
else:
    st.info("No numbered clauses were found under a ‘Terms & Conditions’ section of the PDF.")

st.divider()

# --------------------- RERA: CSV match + Official Calculator overrides ---------------------
st.subheader("📊 RERA Index")

rera_avg = None
if rera_df is not None:
    matched = ae.lookup_rera_row(
        rera_df, city=city, area=area, property_type=ptype, bedrooms=int(bedrooms), furnished=furnished
    )
    if matched is not None and not matched.empty:
        st.success("Matched RERA CSV row:")
        st.dataframe(matched.reset_index(drop=True), width="stretch")
        rera_avg = float(matched.iloc[0]["average_annual_rent_aed"])
    else:
        st.warning("No exact CSV match; use the official calculator overrides below if needed.")
else:
    st.info("Upload a RERA CSV in the sidebar to enable auto slabs (or use overrides below).")

with st.expander("Use Official RERA Calculator (override)"):
    st.link_button("Open the official RERA Rental Index", "https://dubailand.gov.ae/en/eservices/rental-index/rental-index/#/")
    rera_avg_override = st.number_input("Average market rent (AED) from calculator", min_value=0, step=500, value=0)
    allowed_pct_override = st.number_input("Allowed max increase % from calculator", min_value=0, max_value=100, step=1, value=0)
    if rera_avg_override > 0:
        rera_avg = float(rera_avg_override)

# --------------------- Audit ---------------------
st.subheader("🔎 Audit")
strict_mode = st.checkbox("Strict mode (fail on any issue)", value=False, help="If off, only HIGH severity issues cause FAIL.")

run_audit = st.button("Run audit now")
result: Optional[Dict[str, Any]] = None

if run_audit:
    with st.spinner("Auditing…"):
        result = ae.audit_contract(
            text=st.session_state.contract_text,
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
        result["raw_text_for_report"] = st.session_state.contract_text
        # If user pasted calculator % override, apply it
        if allowed_pct_override > 0:
            result["allowed_increase"]["max_allowed_pct"] = int(allowed_pct_override)

if result:
    # Banner
    if result["verdict"] == "pass":
        st.success("PASS — no blocking issues.")
    else:
        st.error("FAIL — issues found.")

    # Blocking explanation
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

    # KPIs
    k1, k2, k3 = st.columns(3)
    with k1:
        st.metric("RERA Avg (AED)", f"{result['allowed_increase']['avg_index'] or '—'}")
    with k2:
        st.metric("Max Allowed %", f"{result['allowed_increase']['max_allowed_pct']}%")
    with k3:
        st.metric("Proposed %", f"{result['allowed_increase']['proposed_pct']:.1f}%")

    # Clause-by-clause table
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
        if h.get("law"):        st.caption(f"Law: {h['law']}")
    for r in result["rule_flags"]:
        icon = "🔴" if r.get("severity") == "high" else "🟡"
        st.markdown(f"{icon} **{r['issue']}**")
        if r.get("suggestion"): st.caption(f"Suggestion: {r['suggestion']}")
        if r.get("law"):        st.caption(f"Law: {r['law']}")

    # Inline highlights
    st.markdown("### 🖍️ Inline highlights in contract")
    html = ae.render_highlighted_html(st.session_state.contract_text, result)
    st.components.v1.html(html, height=360, scrolling=True)

    # Export HTML report
    st.markdown("### ⤵️ Export")
    buf = io.BytesIO()
    report_html = ae.build_report_html(st.session_state.contract_text, result)
    buf.write(report_html.encode("utf-8"))
    st.download_button("Download HTML report", data=buf.getvalue(), file_name="audit_report.html", mime="text/html")

    st.divider()

    # ===================== Firestore: agreements + audits + ledger =====================
    st.subheader("🗄️ Save / Load in Firestore")

    # Agreement identity
    contract_hash = ae.sha256_text(st.session_state.contract_text or "")
    agreement_id_default = contract_hash  # deterministic default
    colID1, colID2 = st.columns([1, 1])
    with colID1:
        namespace = st.text_input("Collection (namespace)", value="agreements", help="Root collection for agreements.")
    with colID2:
        agreement_id = st.text_input("Agreement ID (doc id)", value=agreement_id_default, help="Default uses SHA256(contract).")

    # Save current agreement + audit
    if st.button("Save Agreement & Audit to Firestore"):
        if not ae.firebase_is_ready():
            st.error("Firestore not initialized (sidebar → Initialize Firestore).")
        else:
            try:
                meta = {
                    "city": city,
                    "area": area,
                    "property_type": ptype,
                    "bedrooms": int(bedrooms),
                    "furnished": furnished,
                    "current_rent": float(current_rent),
                    "proposed_rent": float(proposed_rent),
                    "deposit": float(deposit),
                    "renewal_date": renewal_date.isoformat(),
                    "notice_sent_date": notice_sent_date.isoformat(),
                    "ocr_used": bool(ocr_used),
                    "rera_avg_snapshot": result["allowed_increase"]["avg_index"],
                    "rera_allowed_pct_snapshot": result["allowed_increase"]["max_allowed_pct"],
                }
                doc_info = ae.fs_save_agreement(namespace, agreement_id, meta, st.session_state.contract_text)
                audit_id = ae.fs_save_audit(namespace, agreement_id, result, contract_sha=contract_hash)
                # Also append to ledger
                entry = ae.ledger_append(
                    namespace=namespace, agreement_id=agreement_id,
                    payload={
                        "contract_sha256": contract_hash,
                        "audit_sha256": ae.sha256_json(result),
                        "verdict": result["verdict"],
                        "rera_avg": result["allowed_increase"]["avg_index"],
                        "max_allowed_pct": result["allowed_increase"]["max_allowed_pct"],
                        "proposed_pct": result["allowed_increase"]["proposed_pct"],
                        "audit_id": audit_id,
                    },
                )
                st.success(f"Saved ✓  doc={doc_info['path']}  audit={audit_id}  ledger_index={entry['index']}")
            except Exception as e:
                st.error(f"Save failed: {e}")

    # Load existing agreements list
    st.markdown("#### 🔎 Load existing agreements")
    if ae.firebase_is_ready():
        colL, colBtn = st.columns([3, 1])
        with colL:
            list_limit = st.number_input("List limit", min_value=1, max_value=100, value=20, step=1)
        with colBtn:
            refresh = st.button("Refresh list")
        if refresh:
            try:
                rows = ae.fs_list_agreements(namespace, limit=int(list_limit))
                if not rows:
                    st.info("No agreements found.")
                else:
                    df = pd.DataFrame(rows)
                    st.dataframe(df, width="stretch")
                    choices = [r["id"] for r in rows]
                    picked = st.selectbox("Pick an agreement id to load", choices)
                    if st.button("Load selected agreement into editor"):
                        loaded = ae.fs_load_agreement(namespace, picked)
                        if not loaded:
                            st.error("Failed to load document.")
                        else:
                            # Update form + text and rerun to refresh UI
                            st.session_state.contract_text = loaded.get("contract_text", st.session_state.contract_text)
                            # Basic field sync (only if present)
                            f = loaded.get("meta", {})
                            area = f.get("area", area)
                            st.success(f"Loaded {picked}.")
                            st.experimental_rerun()
                    st.markdown("##### Recent audits for selected agreement")
                    if "picked" in locals():
                        audits = ae.fs_list_audits(namespace, picked, limit=5)
                        if audits:
                            st.dataframe(pd.DataFrame(audits), width="stretch")
                        else:
                            st.caption("No audits yet.")
            except Exception as e:
                st.error(f"Listing failed: {e}")

        # Ledger verification
        st.markdown("#### 🔐 Verify ledger chain")
        verify_ns = st.text_input("Namespace", value=namespace, key="verify_ns")
        verify_id = st.text_input("Agreement ID", value=agreement_id, key="verify_id")
        if st.button("Verify ledger chain"):
            try:
                ok, msg = ae.ledger_verify(namespace=verify_ns, agreement_id=verify_id)
                if ok:
                    st.success("Ledger chain OK ✅")
                else:
                    st.error(f"Ledger chain FAIL ❌ — {msg}")
            except Exception as e:
                st.error(f"Verification error: {e}")
    else:
        st.info("Initialize Firestore to enable save/load and ledger verification.")

st.divider()
st.caption(
    "Laws enforced: Law 26/2007 & Law 33/2008 (tenancy, renewal notice, eviction), Decree 43/2013 (rent-increase slabs). "
    "Official sources prevail."
)
