# textnorm.py
# ----------------------------------------------------------------------
# Text normalisation so one set of parsers/rules works for English and
# Arabic contracts (including OCR output with Arabic-Indic digits).
# ----------------------------------------------------------------------

from __future__ import annotations

import re

_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGITS.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})      # Persian / Urdu digits
_DIGITS.update({ord("٪"): "%", ord("٫"): ".", ord("٬"): ",", ord("،"): ","})

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")   # harakat + tatweel
_ARABIC_LETTER = re.compile(r"[؀-ۿݐ-ݿ]")


def to_ascii_digits(s: str) -> str:
    """Arabic-Indic / Persian digits and separators -> ASCII (display text keeps its meaning)."""
    return (s or "").translate(_DIGITS)


def normalize_ar(s: str) -> str:
    """Match-time normalisation: ASCII digits, no diacritics/tatweel, unified alef/ya forms."""
    s = to_ascii_digits(s)
    s = _DIACRITICS.sub("", s)
    s = re.sub("[أإآٱ]", "ا", s)
    s = s.replace("ى", "ي").replace("ئ", "ي").replace("ؤ", "و")
    return s


def has_arabic(s: str, min_ratio: float = 0.0) -> bool:
    letters = _ARABIC_LETTER.findall(s or "")
    if not letters:
        return False
    if min_ratio <= 0:
        return True
    return len(letters) / max(len(re.findall(r"\w", s)), 1) >= min_ratio


def detect_language(s: str) -> str:
    """'ar' if the text is mostly Arabic-script, else 'en'."""
    return "ar" if has_arabic(s, 0.3) else "en"
