"""
Quote location against real brochure text — deterministic, no AI.

Why this exists (found via real audit runs that scored 0%): the original
check was a plain lowercase substring test, so a quote that was genuinely
copied from a brochure still failed on:
  - symbols the PDF extracts differently: "HealthReturns™" vs "HealthReturnsTM",
    "₹" vs "INR" vs "Rs.", curly vs straight quotes, "–" vs "-", ligatures (ﬁ);
  - PDF layout scrambling word order: on ABHI page 1 the heading
    "CLAIM PROTECT#" is extracted AFTER the sentence it introduces, so a
    faithful quote "Claim Protect feature ... takes care of 100% out-of-pocket
    expenses" is never contiguous in the extracted text;
  - quotes stitched with "..." between two real fragments.

Match levels returned by locate_in_text():
  EXACT   — every token of the quote appears contiguously (after normalisation)
  STITCHED— each "..."-separated fragment is EXACT, or >= 85% of the quote's
            word trigrams occur in the text (real text, layout-reordered)
  FUZZY   — >= 70% of the quote's significant words occur in the text
  NONE    — not found
Only EXACT/STITCHED count as the quote being located verbatim; FUZZY means
"similar wording exists" and can never on its own verify a claim.
"""

from __future__ import annotations

import re
import unicodedata

_REPLACEMENTS = [
    ("™", " "), ("®", " "), ("©", " "), ("₹", " inr "), ("rs.", " inr "), ("rs ", " inr "),
    ("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-"), ("•", " "),
    ("\xa0", " "), ("ʎ", " "), ("#", " "), ("*", " "), ("^", " "), ("~", " "), ("@", " "),
    ("lacs", "lakh"), ("lac ", "lakh "), ("lakhs", "lakh"),
]

STITCHED_TRIGRAM_COVERAGE = 0.85
FUZZY_WORD_COVERAGE = 0.70


def normalize(text: str) -> str:
    """Lowercase, NFKC (fixes ligatures), unify symbols/currency/quotes, collapse whitespace."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    for a, b in _REPLACEMENTS:
        t = t.replace(a, b)
    t = re.sub(r"(\d),(\d)", r"\1\2", t)          # 1,00,000 -> 100000 ; 10,000 -> 10000
    t = re.sub(r"([a-z]{3,})tm\b", r"\1", t)        # PDF 'HealthReturnsTM' == 'HealthReturns™' 
    return " ".join(t.split())


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9%]+", normalize(text))


def _contains_seq(hay: list[str], needle: list[str]) -> bool:
    if not needle:
        return False
    n = len(needle)
    first = needle[0]
    for i, tok in enumerate(hay):
        if tok == first and hay[i:i + n] == needle:
            return True
    return False


def _trigrams(toks: list[str]) -> set[tuple]:
    return {tuple(toks[i:i + 3]) for i in range(len(toks) - 2)}


def locate_in_text(quote: str, text: str) -> tuple[str, float]:
    """Returns (level, score) — see module docstring for levels."""
    q = tokens(quote)
    if not q:
        return "NONE", 0.0
    hay = tokens(text)
    if _contains_seq(hay, q):
        return "EXACT", 1.0

    fragments = [tokens(f) for f in re.split(r"\.{3}|…", quote or "") if tokens(f)]
    if len(fragments) > 1 and all(_contains_seq(hay, f) for f in fragments):
        return "STITCHED", 1.0

    if len(q) >= 5:
        qt = _trigrams(q)
        cov = len(qt & _trigrams(hay)) / len(qt) if qt else 0.0
        if cov >= STITCHED_TRIGRAM_COVERAGE:
            return "STITCHED", round(cov, 3)

    sig = {w for w in q if len(w) >= 4 or w.isdigit() or w.endswith("%")}
    if sig:
        cov = len(sig & set(hay)) / len(sig)
        if cov >= FUZZY_WORD_COVERAGE:
            return "FUZZY", round(cov, 3)
    return "NONE", 0.0
