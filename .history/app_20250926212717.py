# Project: Dubai Tenancy Audit App (Streamlit UI + Scrapers + RERA Rules)
# ----------------------------------------------------------------------------
# This canvas contains two files:
# 1) app.py — Streamlit front-end (200+ lines)
# 2) audit_engine.py — Core audit engine & scrapers (200+ lines)
#
# Notes & Ethics:
# • This is an educational prototype, not legal advice. Dubai tenancy law is nuanced; Arabic
# texts prevail. Consult qualified professionals for real cases.
# • Scraping: Check robots.txt and Terms of Service for any site you target. Use low request
# volumes, add proper headers, and respect rate limits. When available, prefer official APIs
# or open datasets. The scraping utilities here are conservative and include caching.
# • Replace demo CSS selectors if websites change their markup.
# • For market rents, this prototype aggregates listings to estimate an average; it is NOT the
# official RERA index. Use it only for demo math to illustrate Decree 43/2013 slabs.
# ----------------------------------------------------------------------------


# ================================
# ============ app.py ============
# ================================


import os
import io
import json
import time
from datetime import date
from typing import Dict, Any, List


import streamlit as st
from pdfminer.high_level import extract_text


# Local engine
import audit_engine as ae


st.set_page_config(page_title="Dubai Rental Contract Auditor", layout="wide")


# -------------------------------
# Sidebar: App Settings
# -------------------------------
st.sidebar.title("⚙️ Settings")
st.sidebar.markdown("**Read me**: This is an educational demo. Always respect website ToS & robots.txt.")


# Scraper toggles
use_propertyfinder = st.sidebar.checkbox("Use Property Finder scraper", value=True)
use_bayut = st.sidebar.checkbox("Use Bayut scraper", value=True)
max_listings = st.sidebar.slider("Max listings per source", min_value=10, max_value=120, value=40, step=10)
scrape_timeout = st.sidebar.slider("HTTP timeout (s)", min_value=5, max_value=30, value=12, step=1)
user_agent = st.sidebar.text_input(
"Custom User-Agent",
value="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116 Safari/537.36",
)


st.sidebar.divider()
st.sidebar.markdown("**Caching**")
clear_cache = st.sidebar.button("Clear rent cache")
if clear_cache:
ae.clear_rent_cache()
st.sidebar.success("Rent cache cleared.")


# -------------------------------
# Main Title
# -------------------------------
st.title("🏠 Dubai Rental Contract Auditor (RERA-focused)")
st.caption("Upload a contract PDF, fetch market rent (demo), and highlight compliant/invalid clauses.")


# -------------------------------
# Inputs: Contract File & Details
# -------------------------------
left, right = st.columns([1, 1])


with left:
uploaded = st.file_uploader("Upload Rental Contract (PDF)", type=["pdf"])
contract_text = ""
if uploaded:
with st.spinner("Extracting text from PDF…"):
contract_text = extract_text(uploaded)
else:
st.info("No PDF uploaded. You can paste text manually on the right.")

