# Contributing

Thanks for helping tenants understand their contracts. All kinds of contributions are welcome: code, law checks, translations, bug reports.

## Setup

```bash
git clone https://github.com/AaditAg/tenancy_audit.git
cd tenancy_audit
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests
streamlit run app.py
```

## Workflow

1. Fork the repo and create a branch from `main` (`git checkout -b fix/short-description`).
2. Make your change and add or update tests.
3. Run `python -m pytest tests` and make sure it passes.
4. Open a pull request describing what changed and why.

## Ways to contribute

### Add or improve a rule
- Add a `Rule` in `rules.py` with a clear plain-English `explanation` and `suggestion`, and the `laws` ids it relies on.
- If the clause can appear in Arabic, add a pattern to `ARABIC_PATTERNS`.
- Add a test in `tests/test_engine.py` with a short example clause.

### Update the law
- Edit `legal_kb.py`, cite a primary source (gazette, RERA, DLD, official portal) in the PR, and bump `LAST_REVIEWED`.
- Only set `verified=True` if you checked the primary source. Otherwise leave it `False`.

### Review a translation
- Strings live in `translations.py`. Fix wording, keep it simple (aim for low-literacy readers), and mention your language in the PR.

### Report a bug
- Open an issue with the steps, what you expected and what happened.
- **Redact contracts.** Never post real names, Emirates IDs, phone numbers or property details.

## Ground rules

- Keep findings honest: when unsure, prefer a "verify" warning over a "this is illegal" claim.
- Don't commit API keys, real contracts or personal data.
- Be kind and constructive in reviews and discussions.
