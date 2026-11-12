"""News classification with the AI (ROADMAP section 9): batches of 20 headlines.

The rule-based classifier (``engine.sentiment``) always runs first and works
without the AI. This step refines it: a better sentiment and a category per
headline. One limit never moves: the AI may flag a headline as severe, but it
can never clear a severe flag the rules set, because that flag feeds control
C21 and the controls veto the AI, not the other way round (principle 5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from engine.ai_client import TASK_NEWS
from engine.ai_gateway import AIGateway
from engine.dossier import NEWS_BATCH_SIZE, build_news_dossier
from engine.schemas import NewsItem
from engine.sentiment import SEVERE_CATEGORIES

# Category of the AI -> category the controls know, for the severe ones.
_SEVERE_FROM_AI = {
    "legal_regulatorio": "investigation",
    "quiebra": "bankruptcy",
    "oferta_acciones": "offering",
}
_SPECIAL_FROM_AI = {"suspension": "halt"}


@dataclass(frozen=True)
class NewsClassification:
    id: int
    sentiment: float
    category: str
    severe: bool


def classify_news(
    gateway: AIGateway, items: Sequence[tuple[int, NewsItem]]
) -> tuple[dict[int, NewsClassification], str]:
    """Classify ``(id, item)`` pairs in batches. Returns what was classified and,
    when the AI could not finish, why (the rest keep their rule-based values)."""
    classified: dict[int, NewsClassification] = {}
    for start in range(0, len(items), NEWS_BATCH_SIZE):
        batch = items[start : start + NEWS_BATCH_SIZE]
        expected = {identifier for identifier, _ in batch}

        def check(output: Any, expected: set[int] = expected) -> str | None:
            returned = {row["id"] for row in output["noticias"]}
            if not returned & expected:
                return "la respuesta no trae ninguno de los titulares enviados"
            return None

        dossier = build_news_dossier(batch)
        result = gateway.call(TASK_NEWS, dossier.text, fingerprint=dossier.fingerprint, check=check)
        if not result.ok:
            return classified, result.note
        for row in result.output["noticias"]:
            if row["id"] in expected and row["id"] not in classified:
                classified[row["id"]] = NewsClassification(
                    id=row["id"],
                    sentiment=float(row["sentimiento"]),
                    category=str(row["categoria"]),
                    severe=bool(row["grave"]),
                )
    return classified, ""


def refine(item: NewsItem, classification: NewsClassification) -> NewsItem:
    """The news item with the AI's reading on top of the rules'."""
    if item.category in SEVERE_CATEGORIES or item.severe:
        # The rules already found it severe: the AI only refines the sentiment,
        # and never upward past zero for a severe headline.
        return item.model_copy(update={"sentiment": min(classification.sentiment, 0.0)})
    category = item.category
    severe = False
    if classification.severe and classification.category in _SEVERE_FROM_AI:
        category, severe = _SEVERE_FROM_AI[classification.category], True
    elif classification.category in _SPECIAL_FROM_AI and category is None:
        category = _SPECIAL_FROM_AI[classification.category]
    sentiment = min(classification.sentiment, 0.0) if severe else classification.sentiment
    return item.model_copy(update={"sentiment": sentiment, "category": category, "severe": severe})
