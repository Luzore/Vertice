"""Rule-based news sentiment and severe-news detection.

This is the classifier that works without the AI: it scores a headline from
-1 to +1 with word lists and flags the news that control C21 blocks on
(regulatory investigation, bankruptcy, share offering, sharp downgrade). The AI
classifier of Phase 4 refines these scores; when the AI is out of quota this
one keeps the pipeline running.
"""

from __future__ import annotations

import re

_POSITIVE = (
    "beats", "beat", "tops", "surges", "soars", "jumps", "rallies", "record high", "upgrade",
    "upgrades", "upgraded", "raises guidance", "raises forecast", "raises outlook", "outperform",
    "strong demand", "wins contract", "approval", "approved", "buyback", "repurchase",
    "dividend increase", "raises dividend", "profit rises", "revenue growth", "breakthrough",
    "partnership", "expands", "exceeds", "better than expected", "all-time high", "bullish",
)  # fmt: skip
_NEGATIVE = (
    "misses", "miss", "falls", "plunges", "tumbles", "slumps", "sinks", "drops", "downgrade",
    "downgrades", "downgraded", "cuts guidance", "cuts forecast", "lowers outlook", "warns",
    "warning", "lawsuit", "sued", "probe", "investigation", "subpoena", "fraud", "recall",
    "bankruptcy", "chapter 11", "default", "layoffs", "resigns", "delisting", "short seller",
    "worse than expected", "weak demand", "loss widens", "offering", "dilution", "bearish",
    "halted", "data breach",
)  # fmt: skip

# Category -> phrases. Any match marks the news as severe (control C21).
_SEVERE: dict[str, tuple[str, ...]] = {
    "investigation": (
        "sec investigation",
        "doj investigation",
        "investigation",
        "probe",
        "subpoena",
        "fraud",
        "accounting irregularities",
        "restatement",
        "indicted",
        "charged with",
    ),  # fmt: skip
    "bankruptcy": ("bankruptcy", "chapter 11", "chapter 7", "going concern", "insolvency"),
    "offering": (
        "public offering",
        "secondary offering",
        "follow-on offering",
        "registered direct",
        "at-the-market offering",
        "prices offering",
        "announces offering",
        "share sale",
        "convertible notes offering",
        "dilutive",
    ),  # fmt: skip
    "downgrade": ("downgrade", "downgrades", "downgraded", "cut to sell", "cut to underperform"),
    "halt": ("trading halted", "trading halt", "delisting", "delisted"),
}


def _count(text: str, phrases: tuple[str, ...]) -> int:
    return sum(1 for phrase in phrases if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def score_headline(headline: str, summary: str | None = None) -> float:
    """Sentiment from -1 (very negative) to +1 (very positive); 0 when neutral."""
    text = f"{headline} {summary or ''}".lower()
    positive, negative = _count(text, _POSITIVE), _count(text, _NEGATIVE)
    if positive == negative == 0:
        return 0.0
    return round((positive - negative) / (positive + negative), 4)


def severe_category(headline: str, summary: str | None = None) -> str | None:
    """Kind of severe news (investigation, bankruptcy, offering, downgrade, halt) or None."""
    text = f"{headline} {summary or ''}".lower()
    for category, phrases in _SEVERE.items():
        if _count(text, phrases):
            return category
    return None
