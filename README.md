# UAE Tenancy Auditor

Checks a rental contract clause-by-clause against Dubai and UAE law, with plain-English reasons, suggested fixes and
citations. Rules always run; an optional open-source model (Kimi K3 or Llama 4) adds a second opinion.

## Run

```bash
pip install -r requirements.txt
streamlit run app.py
python -m pytest tests          # unit tests
```

## How it works

| Module | Role |
|---|---|
| `legal_kb.py` | Curated law entries (federal + Dubai in depth; Abu Dhabi/Sharjah pointers) with a `verified` flag |
| `rules.py` | Deterministic clause rules → severity, explanation, fix, law ids |
| `audit_engine.py` | PDF/OCR, field parsing, clause segmentation, rent-cap/notice/deposit checks, AI merge, scoring, report, Firestore |
| `llm_client.py` | Stdlib client for any OpenAI-compatible endpoint; presets for Kimi K3, Llama 4 (Groq / Together / OpenRouter), local Ollama/vLLM |
| `app.py` | Streamlit UI |

## Languages, Arabic contracts, photos

- **UI languages:** English, Hindi, Malayalam, Arabic, Urdu, Tamil (right-to-left for Arabic/Urdu), with read-aloud
  using the device's own speech engine. UI text and rule titles are in `translations.py` and were machine-written -
  **have native speakers review them**. Longer explanations are translated at runtime by the AI model; without a key
  they show in English.
- **Arabic contracts:** Arabic-Indic digits, hamza/ya variants and Arabic Ejari labels are normalised (`textnorm.py`);
  most rules have Arabic patterns (`ARABIC_PATTERNS` in `rules.py`).
- **Photos / scanned PDFs:** need Tesseract plus `pytesseract` and `pdf2image`. Language packs for English, Arabic,
  Hindi, Malayalam, Tamil and Urdu live in `tessdata/` (from tesseract-ocr/tessdata_fast).
  macOS: `brew install tesseract poppler`.
- **Emirates:** all seven. Dubai has firm rules; the others use *reported* rules from secondary sources (e.g. Sharjah
  Law 5/2024, an Abu Dhabi rent-increase suspension reported June 2026) and only ever produce "verify" warnings.

## AI model setup

Open the sidebar (⚙️) to pick a model and paste a key (or set `MOONSHOT_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`,
`OPENROUTER_API_KEY`, or `[llm] api_key` in `.streamlit/secrets.toml`). For a fully private setup choose
"Local server" and run Llama 4 via Ollama or vLLM. **Model ids and base URLs change; confirm them in your
provider's console** (they can be edited under "Endpoint (advanced)").

The model only sees retrieved law excerpts, and any citation it returns that wasn't in those excerpts is discarded.
AI-only findings are capped at "worth negotiating" unless backed by a cited reference.

## Keeping the law current

Update `legal_kb.py` and bump `LAST_REVIEWED`. Entries marked `verified=False` are pointers to confirm against the
official gazette / RERA / DLD. Add a rule in `rules.py` plus a test in `tests/test_engine.py` for each new clause type.

**Not legal advice.** This is automated screening; confirm important points with a UAE-licensed lawyer.
