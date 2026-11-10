<div align="center">

# 🏠 UAE Tenancy Auditor

**Check a rental contract clause by clause against UAE and Dubai law, in plain language, in your own language.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Built with Streamlit](https://img.shields.io/badge/built%20with-Streamlit-FF4B4B.svg)](https://streamlit.io/)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

</div>

> ⚠️ **Not legal advice.** This is automated screening to help tenants spot problems early. Confirm anything important with a UAE-licensed lawyer or the Rental Dispute Centre.

## Why this exists

Many tenants in the UAE sign contracts in a language they don't read fluently, under time pressure, without knowing which clauses are even enforceable. This project reads a tenancy contract, flags clauses that are likely unlawful or one-sided, explains **why** in simple words, suggests a fix, and cites the law it relies on.

## Features

- **Clause-by-clause audit** with three severities: 🔴 likely unlawful, 🟠 worth negotiating, 🔵 good to know.
- **Deterministic rules first.** Rules always run and need no API key or internet connection.
- **Optional open-weight AI second opinion** (Kimi K3, gpt-oss, Qwen, Llama 4, or a fully local model). The model only sees retrieved law excerpts, and any citation it invents is discarded.
- **Numeric checks** for rent-increase caps, notice periods and security deposits.
- **All seven emirates.** Dubai has firm rules; the others use *reported* rules and only ever produce "please verify" warnings.
- **6 UI languages:** English, हिन्दी, മലയാളം, العربية, اردو, தமிழ் (right-to-left for Arabic and Urdu), with read-aloud using the device's speech engine. Built for low-literacy users.
- **Arabic contracts** are supported (Arabic-Indic digits, hamza/ya variants and Ejari labels are normalised).
- **Photos and scanned PDFs** via Tesseract OCR, with language packs for all six languages bundled.
- **Private by design.** Run a local model through Ollama or vLLM and nothing leaves your machine.

## Quick start

```bash
git clone https://github.com/AaditAg/tenancy_audit.git
cd tenancy_audit
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Try it with the bundled samples in [`samples/`](samples) (one English, one Arabic contract).

**OCR (optional, for photos and scanned PDFs):**

```bash
brew install tesseract poppler          # macOS
sudo apt install tesseract-ocr poppler-utils   # Debian / Ubuntu
```

## Enabling the AI second opinion

Open the sidebar (⚙️) to choose a model and paste a key, or set an environment variable:

| Provider | Variable |
|---|---|
| Kimi K3 (Moonshot AI) | `MOONSHOT_API_KEY` |
| gpt-oss-120b / Qwen (Groq) | `GROQ_API_KEY` |
| Llama 4 (Together AI) | `TOGETHER_API_KEY` |
| Llama 4 (OpenRouter) | `OPENROUTER_API_KEY` |
| Local (Ollama / vLLM / llama.cpp) | none needed |
| Any OpenAI-compatible endpoint | `LLM_API_KEY` |

You can also set `[llm] api_key` in `.streamlit/secrets.toml`. Model ids and base URLs change often, so confirm them in your provider's console and edit them under *Endpoint (advanced)*.

**Guardrails:** citations not present in the retrieved excerpts are dropped, and AI-only findings are capped at "worth negotiating" unless backed by a cited reference.

## How it works

```
contract (PDF / photo / text)
        │  extract + OCR, normalise Arabic
        ▼
  clause segmentation ──► deterministic rules ──┐
        │                                       ├─► merge ► score ► report
        └─► retrieve law excerpts ► LLM ────────┘   (optional)
```

| File | Role |
|---|---|
| [`app.py`](app.py) | Streamlit UI |
| [`audit_engine.py`](audit_engine.py) | PDF/OCR, field parsing, clause segmentation, rent-cap/notice/deposit checks, AI merge, scoring, report |
| [`rules.py`](rules.py) | Clause rules → severity, explanation, fix, law ids (English and Arabic patterns) |
| [`legal_kb.py`](legal_kb.py) | Curated law entries with a `verified` flag and `LAST_REVIEWED` date |
| [`llm_client.py`](llm_client.py) | Stdlib client for any OpenAI-compatible endpoint |
| [`textnorm.py`](textnorm.py) | Arabic text normalisation |
| [`translations.py`](translations.py) | UI strings and rule titles in six languages |
| [`tests/`](tests) | Unit tests |

## Testing

```bash
python -m pytest tests
```

## Contributing

Contributions are very welcome, especially from people who know UAE tenancy law or speak the supported languages.

- 🌍 **Native speakers:** UI text and rule titles in `translations.py` were machine-written and **need human review**.
- ⚖️ **Legal reviewers:** entries in `legal_kb.py` marked `verified=False` are pointers to confirm against the official gazette, RERA or DLD.
- 🧩 **New rules:** add the rule in `rules.py` and a test in `tests/test_engine.py`.
- 🐛 **Bugs and ideas:** open an issue with a (redacted) example clause where possible.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow. Never commit real contracts or personal data.

### Keeping the law current

Update `legal_kb.py` and bump `LAST_REVIEWED`. Laws and reported rules for the non-Dubai emirates change; treat unverified entries as hints, not facts.

## Roadmap

- [ ] Native-speaker review of all six translations
- [ ] Verify the remaining non-Dubai law entries against primary sources
- [ ] Per-emirate rent-index calculators
- [ ] Exportable PDF report
- [ ] More languages (Bengali, Tagalog, Pashto)

## Disclaimer

This software provides general information and automated screening only. It does not create a lawyer-client relationship and may be wrong, incomplete or out of date. The authors accept no liability for decisions made using its output.

## License

Released under the [MIT License](LICENSE). Tesseract language data in `tessdata/` comes from [tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) and is licensed under Apache 2.0.
