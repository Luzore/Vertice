"""General headlines from Google News and Yahoo Finance RSS.

Unofficial and without a published limit, so each query is cached for 15 minutes
and the source is never critical: it is context and a backup for news.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from collectors.base import Collector, RawSnapshot, _Requests, request_key
from collectors.http import SourceError

GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
YAHOO_FINANCE_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
# Both sites answer with an error page to clients that do not look like a browser.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VERTICE/0.1)"}


def parse_rss(xml: str) -> list[dict[str, Any]]:
    """Items of an RSS 2.0 feed: title, link, published (UTC), source, description."""
    try:
        root = ElementTree.fromstring(xml)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise SourceError("la respuesta no es un RSS válido") from exc
    if root.tag != "rss" or root.find("channel") is None:
        raise SourceError("la respuesta no es un RSS válido")
    items = []
    for item in root.iter("item"):
        published = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                moment = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                moment = None
            if moment is not None:
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                published = moment.astimezone(UTC).isoformat()
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "published": published,
                "source": (item.findtext("source") or "").strip() or None,
                "description": (item.findtext("description") or "").strip() or None,
            }
        )
    return items


class RssNewsCollector(Collector):
    source = "rss_news"

    async def _feed(
        self, dataset: str, url: str, params: dict[str, str], key: str, as_of: datetime | None
    ) -> RawSnapshot:
        async def fetch(requests: _Requests) -> dict:
            xml = await requests.get_text(url, params=params, headers=_HEADERS)
            parse_rss(xml)  # an error page instead of a feed must count as a failure
            return {"xml": xml}

        return await self._collect(dataset, key, fetch, as_of=as_of)

    async def google_news(self, query: str, *, as_of: datetime | None = None) -> RawSnapshot:
        """Google News search, in English for the US edition."""
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        return await self._feed("google_news", GOOGLE_NEWS_URL, params, request_key(q=query), as_of)

    async def yahoo_finance(self, ticker: str, *, as_of: datetime | None = None) -> RawSnapshot:
        """Yahoo Finance headlines for one ticker."""
        symbol = ticker.upper()
        params = {"s": symbol, "region": "US", "lang": "en-US"}
        return await self._feed(
            "yahoo_finance", YAHOO_FINANCE_URL, params, request_key(s=symbol), as_of
        )
