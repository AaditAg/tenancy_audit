# app.py
# ----------------------------------------------------------------------
# Streamlit UI designed for low-literacy, multilingual, phone-first use:
#   language -> show contract (photo / PDF / type / example) -> 3 questions -> traffic-light result
# Big buttons, icons and colours carry the meaning; every result can be read aloud.
# ----------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import urllib.parse
from datetime import date
from html import escape
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

import audit_engine as ae
import legal_kb
from llm_client import DEFAULT_PROVIDER, PROVIDERS, LLMClient, LLMError
from translations import (LANG_NATIVE, LANGS, RTL, SPEECH_CODE, check_text, emirate_name, rule_title, t)

HERE = pathlib.Path(__file__).parent
SAMPLES = {"en": HERE / "samples" / "sample_contract.txt", "ar": HERE / "samples" / "sample_contract_ar.txt"}

st.set_page_config(page_title="UAE Tenancy Auditor", page_icon="🏠", layout="centered",
                   initial_sidebar_state="collapsed")

# ----------------------------- state ----------------------------------------
_DEFAULTS: Dict[str, Any] = {
    "step": "lang", "lang": "en", "mode": None, "contract_text": "", "source_sig": "", "pdf_bytes": None,
    "result": None, "tr_cache": {}, "photos": [], "cam_n": 0, "show_all": False,
    "f_jurisdiction": "dubai", "f_community": "", "f_property_type": "apartment", "f_bedrooms": 1,
    "f_usage": "residential", "f_furnishing": "unfurnished", "f_tenant": "", "f_landlord": "",
    "f_ejari_number": "", "f_rent": 0, "f_deposit": 0, "f_start": None, "f_end": None,
    "f_increase": "no", "f_proposed": 0, "f_notice": None, "f_index": 0,
}
for _k, _v in _DEFAULTS.items():
    st.session_state.setdefault(_k, _v)
ss = st.session_state
L: str = ss.lang


def T(key: str, **fmt) -> str:
    return t(key, ss.lang, **fmt)


# ----------------------------- styling --------------------------------------
_FONTS = {
    "hi": "'Noto Sans Devanagari'", "ml": "'Noto Sans Malayalam'", "ta": "'Noto Sans Tamil'",
    "ar": "'Noto Naskh Arabic'", "ur": "'Noto Nastaliq Urdu'", "en": "'Noto Sans'",
}
_rtl_css = (".block-container, .stMarkdown, .stRadio, .stTextInput, .stNumberInput, .stDateInput, "
            ".stTextArea, .stSelectbox { direction: rtl; text-align: right; }") if L in RTL else ""
_ur_css = ".stApp * { line-height: 2.1; }" if L == "ur" else ""
st.markdown(
    f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;700&family=Noto+Sans+Devanagari:wght@400;700&family=Noto+Sans+Malayalam:wght@400;700&family=Noto+Sans+Tamil:wght@400;700&family=Noto+Naskh+Arabic:wght@400;700&family=Noto+Nastaliq+Urdu:wght@400;700&display=swap');
      html, body, .stApp, .stApp * {{ font-family: {_FONTS.get(L, _FONTS['en'])}, system-ui, sans-serif; }}
      .block-container {{ max-width: 720px; padding-top: 1.2rem; padding-bottom: 4rem; }}
      {_rtl_css}
      {_ur_css}
      .stButton > button, .stDownloadButton > button, .stLinkButton > a {{
          min-height: 3.6rem; font-size: 1.2rem; font-weight: 700; border-radius: 16px; width: 100%; }}
      .stButton > button[kind="primary"] {{ min-height: 4.2rem; font-size: 1.4rem; }}
      .stNumberInput input, .stTextInput input, .stTextArea textarea, .stDateInput input {{ font-size: 1.25rem; min-height: 3rem; }}
      .stRadio label p {{ font-size: 1.15rem; }}
      h1 {{ font-size: 1.9rem !important; }}
      h2, h3 {{ font-size: 1.45rem !important; }}
      .bigicon {{ font-size: 4.5rem; text-align: center; line-height: 1.1; }}
      .langbtn button {{ min-height: 4.6rem !important; font-size: 1.6rem !important; }}
      .verdict {{ border-radius: 20px; padding: 1.1rem 1.2rem; display: flex; gap: 1rem; align-items: center;
                 color: #111; border: 3px solid; margin: .4rem 0 1rem; }}
      .verdict .light {{ font-size: 4.2rem; line-height: 1; }}
      .verdict .msg {{ font-size: 1.4rem; font-weight: 700; line-height: 1.5; }}
      .verdict.fail {{ background:#FDECEA; border-color:#C62828; }}
      .verdict.review {{ background:#FFF3E0; border-color:#EF6C00; }}
      .verdict.pass {{ background:#E8F5E9; border-color:#2E7D32; }}
      .card {{ border-radius: 16px; padding: .9rem 1.1rem; margin: .7rem 0 .2rem; color:#111; border-left: 10px solid; }}
      .card.fail {{ background:#FDECEA; border-color:#C62828; }}
      .card.warn {{ background:#FFF3E0; border-color:#EF6C00; }}
      .card.pass {{ background:#E8F5E9; border-color:#2E7D32; }}
      .card.unknown {{ background:#EEEEEE; border-color:#757575; }}
      .card .ttl {{ font-size: 1.3rem; font-weight: 700; line-height: 1.5; }}
      .card .sub {{ font-size: .95rem; opacity: .75; margin-bottom: .3rem; }}
      .card .fix {{ margin-top: .4rem; font-weight: 700; }}
      .todo {{ font-size: 1.15rem; padding: .35rem 0; }}
      .steps {{ display:flex; justify-content:center; gap:1.2rem; font-size:2rem; margin-bottom:.8rem; }}
      .steps span {{ opacity:.3; }} .steps span.on {{ opacity:1; transform: scale(1.25); }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------- helpers --------------------------------------
def _secret(*path: str) -> Optional[str]:
    try:
        cur: Any = st.secrets
        for p in path:
            cur = cur[p]
        return str(cur)
    except Exception:
        return None


def speak(text: str) -> None:
    """Read-aloud button using the phone/browser's own speech engine (works offline, no data leaves the device)."""
    code = SPEECH_CODE[ss.lang]
    nov = json.dumps(T("no_voice"), ensure_ascii=False)
    html = f"""
    <div dir="{'rtl' if ss.lang in RTL else 'ltr'}" style="font-family:system-ui,sans-serif">
      <button id="b" style="width:100%;min-height:54px;font-size:20px;font-weight:700;border-radius:14px;
              border:2px solid #1565C0;background:#E3F2FD;color:#0D47A1;cursor:pointer">{escape(T('listen'))}</button>
      <div id="m" style="color:#B71C1C;font-size:15px;margin-top:4px"></div>
    </div>
    <script>
      const text = {json.dumps(text, ensure_ascii=False)}, lang = {json.dumps(code)};
      const b = document.getElementById('b'), m = document.getElementById('m');
      function voice() {{
        const vs = speechSynthesis.getVoices(), p = lang.split('-')[0].toLowerCase();
        return vs.find(v => v.lang.replace('_','-').toLowerCase() === lang.toLowerCase()) ||
               vs.find(v => v.lang.toLowerCase().startsWith(p));
      }}
      b.onclick = () => {{
        if (!('speechSynthesis' in window)) {{ m.textContent = {nov}; return; }}
        if (speechSynthesis.speaking) {{ speechSynthesis.cancel(); return; }}
        const u = new SpeechSynthesisUtterance(text); u.lang = lang; u.rate = 0.9;
        const v = voice(); if (v) u.voice = v; else if (speechSynthesis.getVoices().length) m.textContent = {nov};
        speechSynthesis.speak(u);
      }};
    </script>"""
    components.html(html, height=88)


def steps(active: int) -> None:
    icons = ["📄", "❓", "✅"]
    st.markdown("<div class='steps'>" + "".join(
        f"<span class='{'on' if i == active else ''}'>{ic}</span>" for i, ic in enumerate(icons)) + "</div>",
        unsafe_allow_html=True)


def go(step: str) -> None:
    ss.step = step
    st.rerun()


def _fill_fields(e: ae.EjariFields) -> None:
    ss.f_jurisdiction = e.jurisdiction
    ss.f_community, ss.f_property_type = e.community, e.property_type
    ss.f_bedrooms, ss.f_usage, ss.f_furnishing = int(e.bedrooms or 1), e.usage, e.furnishing
    ss.f_tenant, ss.f_landlord, ss.f_ejari_number = e.tenant_name, e.landlord_name, e.ejari_number
    ss.f_rent, ss.f_deposit = int(e.current_annual_rent_aed), int(e.security_deposit_aed)
    ss.f_start, ss.f_end, ss.f_notice = e.start_date, e.end_date, e.notice_sent_date
    ss.f_increase = "yes" if (e.proposed_new_rent_aed and e.proposed_new_rent_aed != e.current_annual_rent_aed) else "no"
    ss.f_proposed = int(e.proposed_new_rent_aed or e.current_annual_rent_aed)
    ss.f_index = 0
    ss.result = None


def accept_document(text: str, sig: str, pdf_bytes: Optional[bytes] = None) -> None:
    if len((text or "").strip()) < 30:
        st.error("❌ " + T("read_fail"))
        return
    ss.contract_text, ss.source_sig, ss.pdf_bytes = text, sig, pdf_bytes
    _fill_fields(ae.parse_ejari_text(text))
    go("details")


@st.cache_data(show_spinner=False)
def _articles(path: str) -> List[str]:
    return ae.read_articles_texts_from_csv(path) if os.path.exists(path) else []


# ----------------------------- sidebar: advanced ---------------------------
llm_client: Optional[LLMClient] = None
use_articles = False
with st.sidebar:
    st.header("⚙️ " + T("settings"))
    default_key = _secret("llm", "api_key") or os.environ.get(PROVIDERS[DEFAULT_PROVIDER].key_env, "")
    use_ai = st.toggle("AI", value=bool(default_key),
                       help="An open-source model (Kimi K3 / Llama / gpt-oss / Qwen / local) reviews each clause.")
    if use_ai:
        pkey = st.selectbox("Model", list(PROVIDERS), index=list(PROVIDERS).index(DEFAULT_PROVIDER),
                            format_func=lambda k: PROVIDERS[k].label)
        prov = PROVIDERS[pkey]
        api_key = ""
        if prov.needs_key:
            api_key = st.text_input("API key", value=_secret("llm", "api_key") or os.environ.get(prov.key_env, ""),
                                    type="password")
        with st.expander("Endpoint"):
            base_url = st.text_input("Base URL", value=prov.base_url)
            model_name = st.text_input("Model id", value=prov.model)
        if not prov.needs_key or api_key:
            try:
                llm_client = LLMClient.from_provider(pkey, api_key=api_key, base_url=base_url, model=model_name)
            except LLMError as e:
                st.error(str(e))
        else:
            st.info("Add an API key to enable AI.")
        use_articles = st.checkbox("Search bundled reference extract", value=False)
    rera_csv = st.file_uploader("Rental index CSV (city, community, property_type, bedrooms, index_aed)", type=["csv"])
    st.link_button("Dubai Smart Rental Index ↗", "https://dubailand.gov.ae/en/eservices/rental-index/rental-index/")
    with st.expander("Firestore"):
        svc = st.file_uploader("Service account JSON", type=["json"], key="svcjson")
        if st.button("Connect Firestore"):
            try:
                if "firebase" in st.secrets:
                    ae.firebase_init_from_mapping(dict(st.secrets["firebase"]))
                elif os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON"):
                    ae.firebase_init_from_json_string(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
                elif svc is not None:
                    ae.firebase_init_from_bytes(svc.read())
                else:
                    raise RuntimeError("No credentials found.")
                st.success("Connected ✓")
            except Exception as e:
                st.error(str(e))
    st.caption(f"Law data reviewed {legal_kb.LAST_REVIEWED} · OCR: {'on' if ae.ocr_available() else 'off'}")


# ============================= screens ======================================
def screen_lang() -> None:
    st.markdown("<div class='bigicon'>🏠</div>", unsafe_allow_html=True)
    st.markdown("<h1 style='text-align:center'>" + escape(t("app_title", "en")) + "<br>" + escape(t("app_title", "hi")) +
                "</h1>", unsafe_allow_html=True)
    st.markdown("<h3 style='text-align:center'>🌐 " + " · ".join(t("pick_lang", l) for l in LANGS) + "</h3>",
                unsafe_allow_html=True)
    for row in (LANGS[:3], LANGS[3:]):
        for col, lg in zip(st.columns(3), row):
            with col:
                st.markdown("<div class='langbtn'>", unsafe_allow_html=True)
                if st.button(LANG_NATIVE[lg], key=f"lg_{lg}"):
                    ss.lang = lg
                    go("doc")
                st.markdown("</div>", unsafe_allow_html=True)
    st.caption(T("privacy"))


def lang_bar() -> None:
    st.radio("lang", LANGS, key="lang", format_func=lambda l: LANG_NATIVE[l], horizontal=True,
             label_visibility="collapsed")


def screen_doc() -> None:
    lang_bar()
    steps(0)
    st.markdown(f"## {T('step_doc')}")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("📷  " + T("btn_photo"), key="m_photo"):
            ss.mode = "photo"
        if st.button("✍️  " + T("btn_type"), key="m_type"):
            ss.mode = "type"
    with c2:
        if st.button("📄  " + T("btn_pdf"), key="m_pdf"):
            ss.mode = "pdf"
        if st.button("🧪  " + T("btn_example"), key="m_example"):
            ss.mode = "example"

    if ss.mode == "photo":
        st.info("💡 " + T("photo_tip"))
        if not ae.ocr_available():
            st.warning(T("no_ocr"))
        shot = st.camera_input(T("btn_photo"), key=f"cam_{ss.cam_n}", label_visibility="collapsed")
        if shot is not None:
            ss.photos.append(shot.getvalue())
            ss.cam_n += 1
            st.rerun()
        gallery = st.file_uploader(T("btn_gallery"), type=["png", "jpg", "jpeg", "webp"],
                                   accept_multiple_files=True, key="gallery")
        all_photos = list(ss.photos) + [g.getvalue() for g in (gallery or [])]
        if all_photos:
            st.markdown(f"### 🖼️ × {len(all_photos)}")
            if st.button("▶  " + T("check_btn"), type="primary", key="read_photos", disabled=not ae.ocr_available()):
                with st.spinner(T("reading")):
                    text = ae.ocr_image_bytes(all_photos)
                accept_document(text, "photos:" + hashlib.sha256(b"".join(all_photos)).hexdigest())
            if ss.photos and st.button("🗑", key="clear_photos"):
                ss.photos = []
                st.rerun()

    elif ss.mode == "pdf":
        up = st.file_uploader(T("btn_pdf"), type=["pdf"], key="pdf_up", label_visibility="collapsed")
        if up is not None:
            b = up.getvalue()
            with st.spinner(T("reading")):
                parsed = ae.parse_pdf_smart(b)
            accept_document(parsed["text"] or "", "pdf:" + hashlib.sha256(b).hexdigest(), b)

    elif ss.mode == "type":
        txt = st.text_area(T("paste_here"), height=240, key="paste_box")
        if st.button("▶  " + T("use_text"), type="primary", disabled=not txt.strip(), key="use_text"):
            accept_document(txt, "paste:" + hashlib.sha256(txt.encode()).hexdigest())

    elif ss.mode == "example":
        accept_document((SAMPLES["ar"] if ss.lang == "ar" else SAMPLES["en"]).read_text(), "sample")

    if st.button("◀  " + T("back"), key="back_lang"):
        go("lang")


def _index_from_csv() -> int:
    if rera_csv is None or ss.f_jurisdiction != "dubai":
        return 0
    try:
        rera_csv.seek(0)
        q = pd.read_csv(rera_csv)
        for col, val in (("city", "Dubai"), ("property_type", ss.f_property_type)):
            if col in q.columns:
                q = q[q[col].astype(str).str.lower() == str(val).lower()]
        if "bedrooms" in q.columns:
            q = q[q["bedrooms"].astype(int) == int(ss.f_bedrooms)]
        if "community" in q.columns and ss.f_community:
            q = q[q["community"].astype(str).str.contains(ss.f_community, case=False, na=False)]
        return int(float(q["index_aed"].median())) if (not q.empty and "index_aed" in q.columns) else 0
    except Exception:
        return 0


_DMIN, _DMAX = date(2000, 1, 1), date(2100, 1, 1)


def screen_details() -> None:
    lang_bar()
    steps(1)
    st.success("✅ " + T("read_ok"))
    st.markdown(f"## {T('step_q')}")

    st.markdown(f"### 📍 {T('q_emirate')}")
    st.radio("emirate", list(legal_kb.JURISDICTIONS), key="f_jurisdiction", horizontal=True,
             format_func=lambda j: emirate_name(j, ss.lang), label_visibility="collapsed")

    st.markdown(f"### 💰 {T('q_rent')}")
    st.number_input("rent", min_value=0, step=500, key="f_rent", label_visibility="collapsed")

    st.markdown(f"### 📈 {T('q_more')}")
    st.radio("more", ["no", "yes"], key="f_increase", horizontal=True,
             format_func=lambda v: ("✅ " if v == "yes" else "❌ ") + T(v), label_visibility="collapsed")
    if ss.f_increase == "yes":
        st.markdown(f"**{T('q_new_rent')}**")
        st.number_input("new", min_value=0, step=500, key="f_proposed", label_visibility="collapsed")
        st.markdown(f"**{T('q_letter')}**")
        st.date_input("letter", value=None, key="f_notice", format="DD/MM/YYYY", min_value=_DMIN, max_value=_DMAX,
                      label_visibility="collapsed")
        st.markdown(f"**{T('q_end')}**")
        st.date_input("end", value=None, key="f_end", format="DD/MM/YYYY", min_value=_DMIN, max_value=_DMAX,
                      label_visibility="collapsed")
        if ss.f_jurisdiction == "dubai":
            looked = _index_from_csv()
            if looked and not ss.f_index:
                ss.f_index = looked
            st.markdown(f"**{T('q_index')}**")
            st.number_input("index", min_value=0, step=1000, key="f_index", label_visibility="collapsed")
            st.caption(T("index_help"))

    with st.expander("➕ " + T("more_details")):
        st.text_area("📄", key="contract_text", height=160)
        a, b = st.columns(2)
        a.selectbox("Use", ["residential", "commercial"], key="f_usage")
        b.selectbox("Type", ["apartment", "villa", "townhouse"], key="f_property_type")
        a.number_input("Bedrooms", 0, 15, key="f_bedrooms")
        b.selectbox("Furnishing", ["unfurnished", "semi-furnished", "furnished"], key="f_furnishing")
        a.number_input("Deposit (AED)", 0, step=500, key="f_deposit")
        b.text_input("Ejari / registration no.", key="f_ejari_number")
        a.text_input("Landlord", key="f_landlord")
        b.text_input("Tenant", key="f_tenant")
        a.date_input("Start", value=None, key="f_start", format="DD/MM/YYYY", min_value=_DMIN, max_value=_DMAX)
        b.text_input("Area", key="f_community")

    st.write("")
    if st.button("🔍  " + T("check_btn"), type="primary", key="run"):
        run_audit_now()
    if st.button("◀  " + T("back"), key="back_doc"):
        go("doc")


def run_audit_now() -> None:
    inc = ss.f_increase == "yes"
    ej = ae.EjariFields(
        jurisdiction=ss.f_jurisdiction, city=legal_kb.JURISDICTIONS[ss.f_jurisdiction], community=ss.f_community,
        property_type=ss.f_property_type, bedrooms=int(ss.f_bedrooms), tenant_name=ss.f_tenant,
        landlord_name=ss.f_landlord, ejari_number=ss.f_ejari_number, security_deposit_aed=int(ss.f_deposit),
        current_annual_rent_aed=int(ss.f_rent), proposed_new_rent_aed=int(ss.f_proposed) if inc else int(ss.f_rent),
        furnishing=ss.f_furnishing, usage=ss.f_usage, start_date=ss.f_start, end_date=ss.f_end,
        renewal_date=ss.f_end, notice_sent_date=ss.f_notice if inc else None,
        contract_language=ae.detect_language(ss.contract_text),
    )
    index = (int(ss.f_index) or None) if (inc and ss.f_jurisdiction == "dubai") else None
    articles = _articles(str(HERE / "articles_export.csv")) if (llm_client and use_articles) else None
    bar = st.progress(0.0, text=T("checking"))
    ss.result = ae.run_audit(ss.contract_text, ej, rera_index_aed=index, llm=llm_client, ai_articles=articles,
                             progress=lambda f, m: bar.progress(min(max(f, 0.0), 1.0), text=T("checking")))
    bar.empty()
    ss.show_all = False
    if ae.firebase_available():
        try:
            ae.write_ledger(ej.tenant_name or "unknown", ej.landlord_name or "unknown", ej, ss.result,
                            pdf_bytes=ss.pdf_bytes, rera_index_aed=index)
        except Exception as e:
            st.error(f"Firestore: {e}")
    go("result")


# ----------------------------- results --------------------------------------
def translations_for(res: ae.AuditResult) -> Dict[str, str]:
    """AI-translated explanations (cached per result+language). Empty when English or no model."""
    if ss.lang == "en" or llm_client is None:
        return {}
    key = (res.timestamp, ss.lang)
    if key in ss.tr_cache:
        return ss.tr_cache[key]
    items: Dict[str, str] = {}
    for cf in res.clause_findings:
        if cf.verdict != "pass":
            items[f"e{cf.clause_no}"] = cf.explanation[:500]
            if cf.suggestion:
                items[f"s{cf.clause_no}"] = cf.suggestion[:300]
    for i, c in enumerate(res.checks):
        if c.status != "pass" and not check_text(c.id, c.status, "en", **c.args):
            items[f"k{i}"] = c.message
    keys = [k for k in items if items[k]]
    out: Dict[str, str] = {}
    with st.spinner(T("reading")):
        for i in range(0, len(keys), 8):
            out.update(ae.translate_batch(llm_client, {k: items[k] for k in keys[i:i + 8]}, ss.lang))
    ss.tr_cache[key] = out
    return out


def finding_title(cf: ae.ClauseFinding) -> str:
    if ss.lang != "en":
        titles = [rule_title(r, ss.lang) for r in cf.rule_ids if rule_title(r, ss.lang)]
        return " · ".join(titles[:2]) if titles else T("ai_flag")
    return cf.title or "—"


def check_message(i: int, c: ae.Check, tr: Dict[str, str]) -> str:
    status = c.status
    if c.id == "RENT-CAP" and c.args.get("proposed") in ("0.0", "0", ""):
        status = "none"
    elif c.id == "RENT-CAP" and ss.f_jurisdiction != "dubai" and status in ("warn", "unknown"):
        status = "warn"
    if ss.lang != "en":
        txt = check_text(c.id, status, ss.lang, **c.args)
        return txt or tr.get(f"k{i}", c.message)
    return c.message


def screen_result() -> None:
    res: Optional[ae.AuditResult] = ss.result
    if res is None:
        go("details")
        return
    lang_bar()
    steps(2)
    tr = translations_for(res)

    fails = [c for c in res.clause_findings if c.verdict == "fail"] + [c for c in res.checks if c.status == "fail"]
    warns = [c for c in res.clause_findings if c.verdict == "warn"] + [c for c in res.checks if c.status == "warn"]
    light = {"fail": "🔴", "review": "🟠", "pass": "🟢"}[res.verdict]
    headline = T({"fail": "v_fail", "review": "v_review", "pass": "v_pass"}[res.verdict])
    st.markdown(f"<div class='verdict {res.verdict}'><div class='light'>{light}</div>"
                f"<div class='msg'>{escape(headline)}</div></div>", unsafe_allow_html=True)
    if fails or warns:
        st.markdown(f"### 🔴 {len(fails)} &nbsp;&nbsp; 🟠 {len(warns)}")

    flagged = sorted((c for c in res.clause_findings if c.verdict != "pass"),
                     key=lambda c: 0 if c.verdict == "fail" else 1)
    todo = [T("todo_fail") if res.verdict == "fail" else T("todo_ask") if res.verdict == "review" else "",
            T("todo_keep"), T("todo_help")]
    todo = [x for x in todo if x]

    speak(" . ".join([headline] + todo[:1] + [f"{i}. {finding_title(c)}" for i, c in enumerate(flagged[:6], 1)]))

    st.markdown(f"## 📝 {T('what_to_do')}")
    for x in todo:
        st.markdown(f"<div class='todo'>👉 {escape(x)}</div>", unsafe_allow_html=True)

    for cf in flagged:
        cls = cf.verdict
        icon = "🔴" if cls == "fail" else "🟠"
        label = T("sev_fail") if cls == "fail" else T("sev_warn")
        expl = tr.get(f"e{cf.clause_no}") if ss.lang != "en" else cf.explanation
        fix = tr.get(f"s{cf.clause_no}") if ss.lang != "en" else cf.suggestion
        body = f"<div>{escape(expl)}</div>" if expl else ""
        if fix:
            body += f"<div class='fix'>✍️ {escape(T('fix_label'))}: {escape(fix)}</div>"
        st.markdown(f"<div class='card {cls}'><div class='sub'>{icon} {escape(T('clause_word'))} {cf.clause_no} · "
                    f"{escape(label)}</div><div class='ttl'>{escape(finding_title(cf))}</div>{body}</div>",
                    unsafe_allow_html=True)
        with st.expander("🔎 " + T("why")):
            st.markdown(f"> {cf.text[:700]}")
            if ss.lang == "en" or not expl:
                if ss.lang != "en":
                    st.caption(T("english_note"))
                st.write(cf.explanation)
                if cf.suggestion:
                    st.write("✍️ " + cf.suggestion)
            st.caption(" · ".join(
                legal_kb.citation_for(l) + ("" if (l not in legal_kb.KB_BY_ID or legal_kb.KB_BY_ID[l].verified) else " ⚠")
                for l in cf.laws))

    st.markdown(f"## 💰 {T('checks_title')}")
    for i, c in enumerate(res.checks):
        if c.id in ("TERM", "DEPOSIT", "EXPIRED") and c.status == "pass":
            continue
        ic = {"fail": "🔴", "warn": "🟠", "pass": "🟢", "unknown": "⚪"}[c.status]
        st.markdown(f"<div class='card {c.status}'><div class='ttl' style='font-size:1.1rem'>{ic} "
                    f"{escape(check_message(i, c, tr))}</div></div>", unsafe_allow_html=True)

    if res.missing_items:
        with st.expander("📋 " + T("missing_title")):
            for m in res.missing_items:
                st.markdown(f"- {m}")

    if st.toggle(T("all_clauses"), key="show_all"):
        for cf in res.clause_findings:
            if cf.verdict == "pass":
                st.markdown(f"<div class='card pass'><div class='sub'>🟢 {escape(T('clause_word'))} {cf.clause_no}</div>"
                            f"<div>{escape(cf.text[:400])}</div></div>", unsafe_allow_html=True)

    if res.ejari.jurisdiction != "dubai":
        st.warning("⚠️ " + T("verify_note"))

    st.write("")
    share = headline + "\n" + "\n".join(f"{'🔴' if c.verdict == 'fail' else '🟠'} {finding_title(c)}" for c in flagged[:8])
    st.link_button("💬  " + T("whatsapp"), "https://wa.me/?text=" + urllib.parse.quote(share + "\n\nUAE Tenancy Auditor"))
    st.download_button("⬇️  " + T("download"), ae.render_html_report(res), "tenancy_report.html", "text/html")
    if st.button("🔄  " + T("again"), type="primary", key="again"):
        for k in ("contract_text", "source_sig", "pdf_bytes", "result", "mode"):
            ss[k] = _DEFAULTS[k]
        ss.photos = []
        _fill_fields(ae.EjariFields())
        go("doc")
    if st.button("◀  " + T("back"), key="back_details"):
        go("details")

    if res.ai_error:
        st.caption("AI: " + res.ai_error)
    st.caption(T("disclaimer"))
    st.caption(T("privacy"))


# ----------------------------- router ---------------------------------------
{"lang": screen_lang, "doc": screen_doc, "details": screen_details, "result": screen_result}[ss.step]()
