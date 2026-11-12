"""Node 2: the data engine.

Turns raw payloads into normalized records, stores them, and builds the
``MarketState`` that node 3 works with. Two rules run through everything here:

* **Point in time.** A reader only returns what was knowable at the clock time:
  the daily bar of a session exists once that session has closed, a macro value
  the day after its date, a headline from its publication time. With a simulated
  clock (replay) that is exactly what prevents using the future.
* **Quality is explicit.** Missing or old data lowers a ticker's quality score;
  below 0.8 the ticker cannot be a COMPRAR (control C01).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import pandas as pd
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from collectors.freshness import _parse_instant
from collectors.health import DataHealth, data_health
from collectors.rss_news import parse_rss
from collectors.sec_edgar import OFFERING_FORMS
from core import clock, market_calendar
from core.settings import AppConfig
from db import models
from engine import sentiment
from engine.schemas import Bar, CorporateEvent, EconEvent, Filing, MacroPoint, NewsItem, Quote
from engine.sectors import sector_etf_for_sic
from engine.universe import CONTEXT_TICKERS, normalize_ticker

NEWS_DUPLICATE_WINDOW = timedelta(hours=6)
QUALITY_MIN_FOR_BUY = 0.8
_TIMEFRAME_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60}


# --- parsers: raw payload -> normalized records ------------------------------------


def parse_alpaca_bars(payload: dict) -> tuple[list[Bar], int]:
    """Bars from an Alpaca payload, plus how many were rejected as inconsistent."""
    timeframe = payload.get("timeframe", "1Day")
    source = f"alpaca:{payload.get('feed', 'sip')}"
    bars: list[Bar] = []
    rejected = 0
    for symbol, rows in (payload.get("bars") or {}).items():
        for row in rows:
            ts = _parse_instant(row["t"])
            try:
                bars.append(
                    Bar(
                        ticker=symbol,
                        timeframe=timeframe,
                        ts=ts,
                        session_date=clock.market_date(ts),
                        open=row["o"],
                        high=row["h"],
                        low=row["l"],
                        close=row["c"],
                        volume=row["v"],
                        vwap=row.get("vw") or None,
                        source=source,
                    )
                )
            except ValidationError:
                rejected += 1
    return bars, rejected


def parse_alpaca_quotes(payload: dict) -> list[Quote]:
    quotes = []
    for symbol, row in (payload.get("quotes") or {}).items():
        if not row.get("t"):
            continue
        quotes.append(
            Quote(
                ticker=symbol,
                ts=_parse_instant(row["t"]),
                bid=row.get("bp") or 0,
                ask=row.get("ap") or 0,
                bid_size=row.get("bs") or 0,
                ask_size=row.get("as") or 0,
                source=f"alpaca:{payload.get('feed', 'iex')}",
            )
        )
    return quotes


def _news_item(**fields: Any) -> NewsItem | None:
    headline = (fields.get("headline") or "").strip()
    if not headline:
        return None
    summary = (fields.get("summary") or "").strip() or None
    severe = sentiment.severe_category(headline, summary)
    category = severe or sentiment.special_situation(headline, summary)
    return NewsItem(
        tickers=tuple(sorted({normalize_ticker(t) for t in fields.get("tickers") or () if t})),
        published_at=fields["published_at"],
        source=fields["source"],
        headline=headline,
        summary=summary,
        url=(fields.get("url") or "").strip() or None,
        sentiment=sentiment.score_headline(headline, summary),
        category=category,
        severe=severe is not None,
    )


def parse_alpaca_news(payload: dict) -> list[NewsItem]:
    items = (
        _news_item(
            tickers=row.get("symbols"),
            published_at=_parse_instant(row["created_at"]),
            source=f"alpaca:{row.get('source') or 'benzinga'}",
            headline=row.get("headline"),
            summary=row.get("summary"),
            url=row.get("url"),
        )
        for row in payload.get("news") or []
        if row.get("created_at")
    )
    return [item for item in items if item is not None]


def parse_finnhub_news(payload: list[dict], ticker: str) -> list[NewsItem]:
    items = (
        _news_item(
            tickers=[ticker],
            published_at=datetime.fromtimestamp(row["datetime"], UTC),
            source=f"finnhub:{row.get('source') or '?'}",
            headline=row.get("headline"),
            summary=row.get("summary"),
            url=row.get("url"),
        )
        for row in payload or []
        if row.get("datetime")
    )
    return [item for item in items if item is not None]


def parse_rss_news(payload: dict, source: str, tickers: Sequence[str] = ()) -> list[NewsItem]:
    items = (
        _news_item(
            tickers=tickers,
            published_at=datetime.fromisoformat(row["published"]),
            source=f"{source}:{row.get('source') or 'rss'}",
            headline=row.get("title"),
            summary=None,
            url=row.get("link"),
        )
        for row in parse_rss(payload["xml"])
        if row.get("published")
    )
    return [item for item in items if item is not None]


def parse_fred_series(payload: dict, series_id: str) -> list[MacroPoint]:
    return [
        MacroPoint(
            series_id=series_id,
            obs_date=date.fromisoformat(row["date"]),
            value=float(row["value"]),
            source="fred",
        )
        for row in payload.get("observations") or []
        if row.get("value") not in (None, "", ".")
    ]


def parse_econ_calendar(payload: dict) -> list[EconEvent]:
    return [
        EconEvent(
            name=row["name"],
            country=row["country"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            impact=row["impact"],
            source=row["source"],
        )
        for row in payload.get("events") or []
    ]


def parse_finnhub_earnings_calendar(payload: dict) -> list[CorporateEvent]:
    """Scheduled earnings reports. ``hour`` is bmo (before open), amc (after close) or ''."""
    events = []
    for row in payload.get("earningsCalendar") or []:
        if not row.get("symbol") or not row.get("date"):
            continue
        events.append(
            CorporateEvent(
                ticker=row["symbol"],
                event_type="earnings",
                event_date=date.fromisoformat(row["date"]),
                payload={
                    "kind": "scheduled",
                    "hour": row.get("hour") or "",
                    "eps_estimate": row.get("epsEstimate"),
                    "eps_actual": row.get("epsActual"),
                },
                source="finnhub",
            )
        )
    return events


def parse_finnhub_earnings_surprises(payload: list[dict]) -> list[CorporateEvent]:
    """Reported quarters with actual versus estimated EPS; dated by quarter end."""
    return [
        CorporateEvent(
            ticker=row["symbol"],
            event_type="earnings",
            event_date=date.fromisoformat(row["period"]),
            payload={
                "kind": "surprise",
                "actual": row.get("actual"),
                "estimate": row.get("estimate"),
                "surprise_pct": row.get("surprisePercent"),
            },
            source="finnhub",
        )
        for row in payload or []
        if row.get("symbol") and row.get("period")
    ]


def parse_finnhub_insiders(payload: dict) -> list[CorporateEvent]:
    """Insider transactions (Form 4). Code P is an open-market purchase, S a sale."""
    events = []
    for row in payload.get("data") or []:
        if not row.get("symbol") or not row.get("transactionDate") or row.get("isDerivative"):
            continue
        events.append(
            CorporateEvent(
                ticker=row["symbol"],
                event_type="form_4",
                event_date=date.fromisoformat(row["filingDate"] or row["transactionDate"]),
                payload={
                    "name": row.get("name"),
                    "code": row.get("transactionCode"),
                    "shares": row.get("change"),
                    "price": row.get("transactionPrice"),
                    "transaction_date": row.get("transactionDate"),
                    "id": row.get("id"),
                },
                source="finnhub",
            )
        )
    return events


def parse_sec_filings(payload: dict, ticker: str) -> list[Filing]:
    recent = (payload.get("filings") or {}).get("recent") or {}
    cik = int(payload.get("cik") or 0)
    filings = []
    for index, form in enumerate(recent.get("form") or []):
        accession = recent["accessionNumber"][index]
        document = (recent.get("primaryDocument") or [""] * (index + 1))[index]
        accepted = (recent.get("acceptanceDateTime") or [None] * (index + 1))[index]
        items = (recent.get("items") or [""] * (index + 1))[index]
        filings.append(
            Filing(
                ticker=ticker,
                cik=cik,
                form=form,
                filed_on=date.fromisoformat(recent["filingDate"][index]),
                accepted_at=_parse_instant(accepted) if accepted else None,
                accession=accession,
                items=tuple(item for item in items.split(",") if item),
                url=(
                    f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                    f"{accession.replace('-', '')}/{document}"
                    if document
                    else None
                ),
            )
        )
    return filings


def filings_to_events(filings: Iterable[Filing]) -> list[CorporateEvent]:
    """The filings the system acts on: 8-K (events), Form 4 (insiders), offerings."""
    events = []
    for filing in filings:
        if filing.form == "8-K":
            event_type = "filing_8k"
        elif filing.form == "4":
            event_type = "form_4"
        elif filing.form in OFFERING_FORMS:
            event_type = "offering"
        else:
            continue
        events.append(
            CorporateEvent(
                ticker=filing.ticker,
                event_type=event_type,
                event_date=filing.filed_on,
                event_at=filing.accepted_at,
                payload={"form": filing.form, "items": list(filing.items), "id": filing.accession},
                source="sec_edgar",
                url=filing.url,
            )
        )
    return events


def parse_alpaca_splits(payload: dict) -> list[CorporateEvent]:
    events = []
    for kind, rows in (payload.get("corporate_actions") or {}).items():
        if kind not in ("forward_splits", "reverse_splits"):
            continue
        for row in rows:
            events.append(
                CorporateEvent(
                    ticker=row["symbol"],
                    event_type="split",
                    event_date=date.fromisoformat(row["ex_date"]),
                    payload={
                        "new_rate": row.get("new_rate"),
                        "old_rate": row.get("old_rate"),
                        "id": row.get("id"),
                    },
                    source="alpaca",
                )
            )
    return events


# --- split adjustment and news de-duplication ----------------------------------------


def adjust_for_split(
    bars: pd.DataFrame, ex_date: date, new_rate: float, old_rate: float
) -> pd.DataFrame:
    """Restate bars before ``ex_date`` for a split of ``new_rate`` shares per ``old_rate``.

    Prices are divided by the split ratio and volume multiplied by it, so the series
    is continuous across the split. Bars from the ex-date on are left untouched.
    """
    ratio = new_rate / old_rate
    adjusted = bars.copy()
    before = adjusted.index < pd.Timestamp(ex_date)
    for column in ("open", "high", "low", "close", "vwap"):
        if column in adjusted:
            adjusted.loc[before, column] = adjusted.loc[before, column] / ratio
    adjusted.loc[before, "volume"] = adjusted.loc[before, "volume"] * ratio
    return adjusted


def _headline_key(headline: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip()


def _url_key(url: str) -> str:
    return url.strip().lower().split("#")[0].rstrip("/")


def dedupe_news(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Drop repeats: same URL, or same headline published within 6 hours.

    The earliest copy is kept and gains the tickers of the copies it replaces.
    """
    kept: list[NewsItem] = []
    by_url: dict[str, int] = {}
    by_headline: dict[str, list[int]] = {}
    for item in sorted(items, key=lambda n: n.published_at):
        match: int | None = None
        if item.url and _url_key(item.url) in by_url:
            match = by_url[_url_key(item.url)]
        else:
            for index in by_headline.get(_headline_key(item.headline), []):
                if item.published_at - kept[index].published_at <= NEWS_DUPLICATE_WINDOW:
                    match = index
                    break
        if match is None:
            kept.append(item)
            index = len(kept) - 1
            by_headline.setdefault(_headline_key(item.headline), []).append(index)
        else:
            index = match
            merged = tuple(sorted(set(kept[index].tickers) | set(item.tickers)))
            kept[index] = kept[index].model_copy(update={"tickers": merged})
        if item.url:
            by_url.setdefault(_url_key(item.url), index)
    return kept


# --- storage ---------------------------------------------------------------------------


def _upsert(session: Session, table: Any, rows: list[dict], keys: Sequence[str]) -> None:
    """Insert rows, replacing the ones that already exist with the same key.

    One statement executed for all rows (executemany): building a single
    multi-row statement instead is ten times slower for hundreds of thousands of bars.
    """
    if not rows:
        return
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    updatable = [column for column in rows[0] if column not in keys]
    statement = insert(table)
    if updatable:
        statement = statement.on_conflict_do_update(
            index_elements=list(keys),
            set_={column: getattr(statement.excluded, column) for column in updatable},
        )
    else:
        statement = statement.on_conflict_do_nothing(index_elements=list(keys))
    session.execute(statement, rows)


def store_bars(session: Session, bars: Iterable[Bar]) -> int:
    now = clock.now()
    daily, intraday = [], []
    for bar in bars:
        row = {
            "ticker": bar.ticker,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "vwap": bar.vwap,
            "source": bar.source,
            "updated_at": now,
        }
        if bar.timeframe == "1Day":
            daily.append({**row, "session_date": bar.session_date})
        else:
            intraday.append({**row, "timeframe": bar.timeframe, "ts": bar.ts})
    _upsert(session, models.BarDaily, daily, ("ticker", "session_date"))
    _upsert(session, models.BarIntraday, intraday, ("ticker", "timeframe", "ts"))
    return len(daily) + len(intraday)


def store_macro(session: Session, points: Iterable[MacroPoint]) -> int:
    now = clock.now()
    rows = [
        {
            "series_id": p.series_id,
            "obs_date": p.obs_date,
            "value": p.value,
            "source": p.source,
            "fetched_at": now,
        }
        for p in points
    ]
    _upsert(session, models.MacroPoint, rows, ("series_id", "obs_date"))
    return len(rows)


def store_econ_events(session: Session, events: Iterable[EconEvent]) -> int:
    now = clock.now()
    rows = [
        {
            "name": e.name,
            "country": e.country,
            "scheduled_at": e.scheduled_at,
            "impact": e.impact,
            "source": e.source,
            "fetched_at": now,
        }
        for e in events
    ]
    _upsert(session, models.EconEvent, rows, ("name", "country", "scheduled_at"))
    return len(rows)


def store_news(session: Session, items: Iterable[NewsItem]) -> int:
    """Store news that is not already there (by URL, or headline within 6 hours)."""
    fresh = dedupe_news(items)
    if not fresh:
        return 0
    earliest = min(item.published_at for item in fresh) - NEWS_DUPLICATE_WINDOW
    existing = session.scalars(
        select(models.News).where(models.News.published_at >= earliest)
    ).all()
    urls = {_url_key(row.url) for row in existing if row.url}
    headlines: dict[str, list[datetime]] = {}
    for row in existing:
        headlines.setdefault(_headline_key(row.headline), []).append(row.published_at)
    now = clock.now()
    stored = 0
    for item in fresh:
        if item.url and _url_key(item.url) in urls:
            continue
        times = headlines.get(_headline_key(item.headline), [])
        if any(abs(item.published_at - seen) <= NEWS_DUPLICATE_WINDOW for seen in times):
            continue
        session.add(
            models.News(
                tickers=list(item.tickers),
                published_at=item.published_at,
                source=item.source,
                headline=item.headline,
                summary=item.summary,
                url=item.url,
                sentiment=item.sentiment,
                category=item.category,
                severe=item.severe,
                fetched_at=now,
            )
        )
        stored += 1
    session.flush()
    return stored


def store_corporate_events(session: Session, events: Iterable[CorporateEvent]) -> int:
    """Store events not already present (same ticker, type, date, source and identity)."""
    events = list(events)
    if not events:
        return 0
    tickers = {e.ticker for e in events}
    existing = session.scalars(
        select(models.CorporateEvent).where(models.CorporateEvent.ticker.in_(tickers))
    ).all()

    def identity(ticker: str, kind: str, day: date, source: str, payload: dict | None) -> tuple:
        payload = payload or {}
        return (ticker, kind, day, source, payload.get("id") or payload.get("kind") or "")

    seen = {
        identity(r.ticker, r.event_type, r.event_date, r.source, r.payload): r for r in existing
    }
    now = clock.now()
    stored = 0
    for event in events:
        key = identity(
            event.ticker, event.event_type, event.event_date, event.source, event.payload
        )
        if key in seen:
            seen[key].payload = event.payload  # e.g. an estimate that was revised
            continue
        row = models.CorporateEvent(
            ticker=event.ticker,
            event_type=event.event_type,
            event_date=event.event_date,
            event_at=event.event_at,
            payload=event.payload,
            source=event.source,
            url=event.url,
            fetched_at=now,
        )
        session.add(row)
        seen[key] = row
        stored += 1
    session.flush()
    return stored


# --- point-in-time readers -----------------------------------------------------------


@lru_cache(maxsize=4096)
def last_completed_session(as_of: datetime) -> date:
    """The latest session that has already closed at ``as_of``."""
    day = clock.market_date(as_of)
    session = market_calendar.get_session(day)
    if session is not None and as_of >= session.close:
        return day
    return market_calendar.previous_trading_day(day)


def load_daily_bars(
    session: Session,
    as_of: datetime,
    tickers: Iterable[str] | None = None,
    *,
    lookback_days: int = 760,
) -> dict[str, pd.DataFrame]:
    """Daily bars of sessions that had closed at ``as_of``, per ticker, oldest first."""
    last = last_completed_session(as_of)
    query = select(
        models.BarDaily.ticker,
        models.BarDaily.session_date,
        models.BarDaily.open,
        models.BarDaily.high,
        models.BarDaily.low,
        models.BarDaily.close,
        models.BarDaily.volume,
    ).where(
        models.BarDaily.session_date <= last,
        models.BarDaily.session_date >= last - timedelta(days=lookback_days),
    )
    if tickers is not None:
        query = query.where(models.BarDaily.ticker.in_(sorted(set(tickers))))
    frame = pd.DataFrame(
        session.execute(query.order_by(models.BarDaily.ticker, models.BarDaily.session_date)).all(),
        columns=["ticker", "session_date", "open", "high", "low", "close", "volume"],
    )
    if frame.empty:
        return {}
    frame["session_date"] = pd.to_datetime(frame["session_date"])
    return {
        ticker: group.drop(columns="ticker").set_index("session_date")
        for ticker, group in frame.groupby("ticker", sort=False)
    }


def load_intraday_bars(
    session: Session,
    as_of: datetime,
    tickers: Iterable[str],
    *,
    timeframe: str = "5Min",
    days: int = 21,
) -> dict[str, pd.DataFrame]:
    """Intraday bars that had finished at ``as_of`` (a bar counts once it has ended)."""
    length = timedelta(minutes=_TIMEFRAME_MINUTES[timeframe])
    rows = session.execute(
        select(
            models.BarIntraday.ticker,
            models.BarIntraday.ts,
            models.BarIntraday.open,
            models.BarIntraday.high,
            models.BarIntraday.low,
            models.BarIntraday.close,
            models.BarIntraday.volume,
        )
        .where(
            models.BarIntraday.ticker.in_(sorted(set(tickers))),
            models.BarIntraday.timeframe == timeframe,
            models.BarIntraday.ts <= as_of - length,
            models.BarIntraday.ts >= as_of - timedelta(days=days * 1.5 + 4),
        )
        .order_by(models.BarIntraday.ticker, models.BarIntraday.ts)
    ).all()
    frame = pd.DataFrame(rows, columns=["ticker", "ts", "open", "high", "low", "close", "volume"])
    if frame.empty:
        return {}
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return {
        ticker: group.drop(columns="ticker").set_index("ts")
        for ticker, group in frame.groupby("ticker", sort=False)
    }


def load_macro(
    session: Session, as_of: datetime, *, lookback_days: int = 400
) -> dict[str, pd.Series]:
    """Macro series up to the day before ``as_of``: a daily value is published the next day."""
    today = clock.market_date(as_of)
    rows = session.execute(
        select(models.MacroPoint.series_id, models.MacroPoint.obs_date, models.MacroPoint.value)
        .where(
            models.MacroPoint.obs_date < today,
            models.MacroPoint.obs_date >= today - timedelta(days=lookback_days),
            models.MacroPoint.value.is_not(None),
        )
        .order_by(models.MacroPoint.series_id, models.MacroPoint.obs_date)
    ).all()
    frame = pd.DataFrame(rows, columns=["series_id", "obs_date", "value"])
    if frame.empty:
        return {}
    frame["obs_date"] = pd.to_datetime(frame["obs_date"])
    return {
        series_id: group.set_index("obs_date")["value"]
        for series_id, group in frame.groupby("series_id", sort=False)
    }


def load_news(session: Session, as_of: datetime, *, days: int = 15) -> list[NewsItem]:
    """News published in the ``days`` before ``as_of``, oldest first."""
    rows = session.scalars(
        select(models.News)
        .where(
            models.News.published_at <= as_of,
            models.News.published_at >= as_of - timedelta(days=days),
        )
        .order_by(models.News.published_at)
    ).all()
    return [
        NewsItem(
            tickers=tuple(row.tickers or ()),
            published_at=row.published_at,
            source=row.source,
            headline=row.headline,
            summary=row.summary,
            url=row.url,
            sentiment=row.sentiment,
            category=row.category,
            severe=row.severe,
        )
        for row in rows
    ]


def load_econ_events(
    session: Session, as_of: datetime, *, horizon_days: int = 30
) -> list[EconEvent]:
    """Scheduled events from today to ``horizon_days`` ahead. A schedule is known in advance."""
    start = clock.market_time(clock.market_date(as_of), datetime.min.time())
    rows = session.scalars(
        select(models.EconEvent)
        .where(
            models.EconEvent.scheduled_at >= start,
            models.EconEvent.scheduled_at <= as_of + timedelta(days=horizon_days),
        )
        .order_by(models.EconEvent.scheduled_at)
    ).all()
    return [
        EconEvent(
            name=r.name,
            country=r.country,
            scheduled_at=r.scheduled_at,
            impact=r.impact,
            source=r.source,
        )
        for r in rows
    ]


def _event_known(row: models.CorporateEvent, as_of: datetime) -> bool:
    """Scheduled earnings are known ahead of time. Anything that already happened
    is known from its timestamp, or from the next day when only the date is stored."""
    if row.event_type == "earnings" and (row.payload or {}).get("kind") == "scheduled":
        return True
    if row.event_at is not None:
        return row.event_at <= as_of
    return row.event_date < clock.market_date(as_of)


def load_corporate_events(
    session: Session,
    as_of: datetime,
    tickers: Iterable[str] | None = None,
    *,
    lookback_days: int = 120,
) -> dict[str, list[CorporateEvent]]:
    today = clock.market_date(as_of)
    query = select(models.CorporateEvent).where(
        models.CorporateEvent.event_date >= today - timedelta(days=lookback_days)
    )
    if tickers is not None:
        query = query.where(models.CorporateEvent.ticker.in_(sorted(set(tickers))))
    by_ticker: dict[str, list[CorporateEvent]] = {}
    for row in session.scalars(query.order_by(models.CorporateEvent.event_date)).all():
        if not _event_known(row, as_of):
            continue
        by_ticker.setdefault(row.ticker, []).append(
            CorporateEvent(
                ticker=row.ticker,
                event_type=row.event_type,
                event_date=row.event_date,
                event_at=row.event_at,
                payload=row.payload or {},
                source=row.source,
                url=row.url,
            )
        )
    return by_ticker


# --- data quality ----------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _recent_sessions(expected: date) -> tuple[date, ...]:
    """The 20 sessions ending at ``expected``."""
    return tuple(
        market_calendar.trading_days_between(
            market_calendar.add_trading_days(expected, -19), expected
        )
    )


@dataclass(frozen=True)
class Quality:
    score: float
    # Reasons in Spanish, shown next to the ticker.
    issues: tuple[str, ...] = ()

    @property
    def can_buy(self) -> bool:
        return self.score >= QUALITY_MIN_FOR_BUY


def assess_quality(
    bars: pd.DataFrame | None,
    as_of: datetime,
    *,
    quote: Quote | None = None,
    quote_max_age_s: int = 120,
) -> Quality:
    """Score from 0 to 1 for one ticker's data: 1 is complete and current."""
    if bars is None or bars.empty:
        return Quality(0.0, ("sin barras diarias",))
    score, issues = 1.0, []
    expected = last_completed_session(as_of)
    latest = bars.index[-1].date()
    if latest < expected:
        score -= 0.3
        issues.append(f"falta la barra del {expected:%Y-%m-%d}; la última es del {latest:%Y-%m-%d}")
    recent_sessions = _recent_sessions(expected)
    present = {stamp.date() for stamp in bars.index[-25:]}
    missing = [
        day for day in recent_sessions[:-1] if day not in present and day >= bars.index[0].date()
    ]
    if missing:
        score -= min(0.3, 0.05 * len(missing))
        issues.append(f"faltan {len(missing)} barras en las últimas 20 sesiones")
    if len(bars) < 60:
        score -= 0.3
        issues.append(f"historia corta: {len(bars)} sesiones")
    elif len(bars) < 200:
        score -= 0.1
        issues.append(f"historia menor a 200 sesiones: {len(bars)}")
    idle = int((bars["volume"].iloc[-20:] <= 0).sum())
    if idle:
        score -= min(0.2, 0.05 * idle)
        issues.append(f"{idle} sesiones sin volumen en las últimas 20")
    if quote is not None and market_calendar.market_status(as_of) == "open":
        age = (as_of - quote.ts).total_seconds()
        if age > quote_max_age_s:
            score -= 0.2
            issues.append(f"precio de hace {age:.0f} s")
    return Quality(round(max(0.0, score), 4), tuple(issues))


# --- market state ----------------------------------------------------------------------


@dataclass(frozen=True)
class MarketState:
    """Everything node 3 needs, as it was knowable at ``as_of``. Its only input."""

    as_of: datetime
    # Latest session whose daily bar is complete.
    session_date: date
    # One row per universe ticker: name, exchange, sector_etf, market_cap_usd, sic_listed.
    universe: pd.DataFrame
    # Daily bars per ticker, including benchmarks and sector ETFs.
    daily: dict[str, pd.DataFrame]
    intraday: dict[str, pd.DataFrame] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    macro: dict[str, pd.Series] = field(default_factory=dict)
    econ_events: list[EconEvent] = field(default_factory=list)
    corporate_events: dict[str, list[CorporateEvent]] = field(default_factory=dict)
    news: list[NewsItem] = field(default_factory=list)
    quality: dict[str, Quality] = field(default_factory=dict)
    health: DataHealth | None = None

    @property
    def tickers(self) -> list[str]:
        return list(self.universe.index)

    def news_for(self, ticker: str) -> list[NewsItem]:
        return [item for item in self.news if ticker in item.tickers]


def load_universe(
    session: Session, *, only_active: bool = True, sic_only: bool = False
) -> pd.DataFrame:
    query = select(models.Universe)
    if only_active:
        query = query.where(models.Universe.active.is_(True))
    if sic_only:
        query = query.where(models.Universe.sic_listed.is_(True))
    rows = session.scalars(query.order_by(models.Universe.ticker)).all()
    return pd.DataFrame(
        [
            {
                "ticker": r.ticker,
                "name": r.name,
                "exchange": r.exchange,
                "sector": r.sector,
                "sector_etf": r.sector_etf or sector_etf_for_sic(r.sic),
                "market_cap_usd": r.market_cap_usd,
                "sic_listed": r.sic_listed,
            }
            for r in rows
        ],
        columns=[
            "ticker",
            "name",
            "exchange",
            "sector",
            "sector_etf",
            "market_cap_usd",
            "sic_listed",
        ],
    ).set_index("ticker")


def build_market_state(
    session: Session,
    config: AppConfig,
    *,
    as_of: datetime | None = None,
    intraday_tickers: Iterable[str] = (),
    quotes: Iterable[Quote] = (),
) -> MarketState:
    """Assemble the market state at ``as_of`` (default: the active clock)."""
    as_of = clock.ensure_utc(as_of) if as_of is not None else clock.now()
    universe = load_universe(session, sic_only=config.universe.sic_only)
    tickers = [*universe.index, *CONTEXT_TICKERS]
    daily = load_daily_bars(session, as_of, tickers)
    intraday_tickers = sorted(set(intraday_tickers))
    known_quotes = {q.ticker: q for q in quotes if q.ts <= as_of}
    return MarketState(
        as_of=as_of,
        session_date=last_completed_session(as_of),
        universe=universe,
        daily=daily,
        intraday=load_intraday_bars(session, as_of, intraday_tickers) if intraday_tickers else {},
        quotes=known_quotes,
        macro=load_macro(session, as_of),
        econ_events=load_econ_events(session, as_of),
        corporate_events=load_corporate_events(session, as_of),
        news=load_news(session, as_of),
        quality={
            ticker: assess_quality(
                daily.get(ticker),
                as_of,
                quote=known_quotes.get(ticker),
                quote_max_age_s=config.data.stale.quote_max_age_s,
            )
            for ticker in universe.index
        },
        health=data_health(session, config.data),
    )


class MarketDataCache:
    """Market data loaded once, served point in time.

    Replay asks for the market state of hundreds of moments. Reading the database
    each time takes seconds; this keeps everything in memory and cuts the slice
    that was knowable at each moment, with the same rules as the readers above
    (a test checks both give identical results).
    """

    def __init__(self, session: Session, config: AppConfig, *, start: date, end: date) -> None:
        self._config = config
        horizon = datetime.combine(end + timedelta(days=2), datetime.min.time(), tzinfo=UTC)
        earliest = datetime.combine(start - timedelta(days=30), datetime.min.time(), tzinfo=UTC)
        self.universe = load_universe(session, sic_only=config.universe.sic_only)
        tickers = [*self.universe.index, *CONTEXT_TICKERS]
        # Everything up to the end of the period; state_at() cuts what each moment may see.
        self.daily = load_daily_bars(
            session, horizon, tickers, lookback_days=(end - start).days + 800
        )
        self.macro = load_macro(session, horizon, lookback_days=(end - start).days + 440)
        self._events_raw = session.scalars(
            select(models.CorporateEvent).where(
                models.CorporateEvent.event_date >= start - timedelta(days=130)
            )
        ).all()
        self._event_records: dict[str, list[tuple[models.CorporateEvent, CorporateEvent]]] = {}
        for row in self._events_raw:
            record = CorporateEvent(
                ticker=row.ticker,
                event_type=row.event_type,
                event_date=row.event_date,
                event_at=row.event_at,
                payload=row.payload or {},
                source=row.source,
                url=row.url,
            )
            self._event_records.setdefault(row.ticker, []).append((row, record))
        self._news = load_news(session, horizon, days=(horizon - earliest).days)
        self._econ = [
            EconEvent(
                name=r.name,
                country=r.country,
                scheduled_at=r.scheduled_at,
                impact=r.impact,
                source=r.source,
            )
            for r in session.scalars(
                select(models.EconEvent).order_by(models.EconEvent.scheduled_at)
            ).all()
        ]
        self._health = data_health(session, config.data)

    def bars_until(
        self, ticker: str, last: date, *, lookback_days: int = 760
    ) -> pd.DataFrame | None:
        bars = self.daily.get(ticker)
        if bars is None:
            return None
        stop = bars.index.searchsorted(pd.Timestamp(last), side="right")
        start = bars.index.searchsorted(
            pd.Timestamp(last - timedelta(days=lookback_days)), side="left"
        )
        return bars.iloc[start:stop] if stop > start else None

    def bar_on(self, ticker: str, day: date) -> pd.Series | None:
        """The full bar of one session. For simulating what happened next, never for deciding."""
        bars = self.daily.get(ticker)
        stamp = pd.Timestamp(day)
        if bars is None or stamp not in bars.index:
            return None
        return bars.loc[stamp]

    def events_for(
        self, ticker: str, as_of: datetime, *, lookback_days: int = 120
    ) -> list[CorporateEvent]:
        oldest = clock.market_date(as_of) - timedelta(days=lookback_days)
        return [
            record
            for row, record in self._event_records.get(ticker, [])
            if row.event_date >= oldest and _event_known(row, as_of)
        ]

    def state_at(self, as_of: datetime, tickers: Iterable[str] | None = None) -> MarketState:
        """The market state at ``as_of``; ``tickers`` limits the universe (benchmarks stay)."""
        as_of = clock.ensure_utc(as_of)
        last = last_completed_session(as_of)
        today = clock.market_date(as_of)
        universe = (
            self.universe
            if tickers is None
            else self.universe.loc[[t for t in self.universe.index if t in set(tickers)]]
        )
        daily = {}
        for ticker in [*universe.index, *CONTEXT_TICKERS]:
            bars = self.bars_until(ticker, last)
            if bars is not None:
                daily[ticker] = bars
        macro = {}
        for series_id, series in self.macro.items():
            visible = series[
                (series.index < pd.Timestamp(today))
                & (series.index >= pd.Timestamp(today - timedelta(days=400)))
            ]
            if len(visible):
                macro[series_id] = visible
        day_start = clock.market_time(today, datetime.min.time())
        events = {}
        for ticker in universe.index:
            known = self.events_for(ticker, as_of)
            if known:
                events[ticker] = known
        return MarketState(
            as_of=as_of,
            session_date=last,
            universe=universe,
            daily=daily,
            macro=macro,
            econ_events=[
                e for e in self._econ if day_start <= e.scheduled_at <= as_of + timedelta(days=30)
            ],
            corporate_events=events,
            news=[n for n in self._news if as_of - timedelta(days=15) <= n.published_at <= as_of],
            quality={ticker: assess_quality(daily.get(ticker), as_of) for ticker in universe.index},
            health=self._health,
        )
