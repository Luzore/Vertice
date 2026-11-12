from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from core import clock, settings
from db import models
from db.session import session_scope
from engine import data_engine as de
from engine import sentiment
from engine.schemas import Bar, CorporateEvent, EconEvent, MacroPoint, NewsItem, Quote
from engine.sectors import sector_etf_for_sic
from engine.universe import classify, eligible_assets, normalize_ticker
from tests.conftest import SHIPPED_CONFIG

DAY = date(2026, 11, 10)  # Tuesday
PREMARKET = clock.market_time(DAY, time(8, 45))
MIDSESSION = clock.market_time(DAY, time(11, 0))
AFTER_CLOSE = clock.market_time(DAY, time(16, 30))


# --- schemas and normalization -----------------------------------------------------------


def test_tickers_have_one_spelling():
    assert normalize_ticker(" brk-b ") == normalize_ticker("BRK.B") == normalize_ticker("BRK/B")
    assert normalize_ticker("aapl") == "AAPL"
    bar = Bar(ticker="brk-b", timeframe="1Day", ts=PREMARKET, session_date=DAY, open=1, high=2,
              low=1, close=1.5, volume=10, source="t")  # fmt: skip
    assert bar.ticker == "BRK.B"


def test_records_store_utc_and_refuse_naive_times():
    monterrey = datetime(2026, 11, 10, 8, 0, tzinfo=clock.USER_TZ)
    item = NewsItem(published_at=monterrey, source="t", headline="h")

    assert item.published_at == datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="naive"):
        NewsItem(published_at=datetime(2026, 11, 10, 8, 0), source="t", headline="h")  # noqa: DTZ001


@pytest.mark.parametrize(
    "fields",
    [
        {"high": 9.0, "low": 10.0, "close": 9.5},  # high below low
        {"high": 10.0, "low": 9.0, "close": 11.0},  # close outside the range
        {"high": 10.0, "low": 9.0, "close": 0.0},  # no price
        {"high": 10.0, "low": 9.0, "close": 9.5, "volume": -1},
    ],
)
def test_inconsistent_bars_are_refused(fields):
    base = {"ticker": "X", "timeframe": "1Day", "ts": PREMARKET, "session_date": DAY, "open": 9.5,
            "volume": 100, "source": "t"}  # fmt: skip

    with pytest.raises(ValidationError):
        Bar(**{**base, **fields})


def test_quote_mid_and_spread():
    quote = Quote(ticker="SPY", ts=MIDSESSION, bid=99.9, ask=100.1, source="t")

    assert quote.mid == pytest.approx(100.0)
    assert quote.spread_pct == pytest.approx(0.2)
    assert Quote(ticker="SPY", ts=MIDSESSION, bid=0, ask=100, source="t").spread_pct is None


# --- parsers -------------------------------------------------------------------------------


def test_alpaca_bars_are_dated_by_the_new_york_session():
    payload = {
        "timeframe": "1Day",
        "feed": "sip",
        "bars": {
            "AAPL": [{"t": "2026-11-10T05:00:00Z", "o": 351.35, "h": 353.87, "l": 346.46,
                      "c": 348.37, "v": 18234191, "vw": 348.61}],
            "BAD": [{"t": "2026-11-10T05:00:00Z", "o": 5, "h": 4, "l": 6, "c": 5, "v": 1}],
        },
    }  # fmt: skip

    bars, rejected = de.parse_alpaca_bars(payload)

    (bar,) = bars
    assert (bar.ticker, bar.session_date, bar.close, bar.source) == (
        "AAPL",
        DAY,
        348.37,
        "alpaca:sip",
    )
    assert bar.ts == datetime(2026, 11, 10, 5, 0, tzinfo=UTC)
    assert rejected == 1


def test_intraday_bar_before_midnight_utc_belongs_to_the_same_session():
    payload = {"timeframe": "5Min", "feed": "iex",
               "bars": {"SPY": [{"t": "2026-11-10T20:55:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 5}]}}  # fmt: skip

    bars, _ = de.parse_alpaca_bars(payload)

    assert bars[0].session_date == DAY and bars[0].timeframe == "5Min"


def test_alpaca_quotes_and_news():
    quotes = de.parse_alpaca_quotes(
        {"feed": "iex", "quotes": {"SPY": {"bp": 748.86, "ap": 748.9, "bs": 120, "as": 80,
                                            "t": "2026-11-10T15:59:30.779321638Z"}}}
    )  # fmt: skip
    assert quotes[0].ts == datetime(2026, 11, 10, 15, 59, 30, 779321, tzinfo=UTC)
    assert quotes[0].spread_pct == pytest.approx(0.04 / 748.88 * 100)

    news = de.parse_alpaca_news(
        {"news": [
            {"created_at": "2026-11-10T13:05:00Z", "headline": "Apple beats estimates, raises guidance",
             "summary": "Strong demand.", "symbols": ["AAPL"], "source": "benzinga", "url": "https://x/a"},
            {"created_at": "2026-11-10T13:06:00Z", "headline": "  ", "symbols": ["AAPL"]},
        ]}
    )  # fmt: skip
    (item,) = news
    assert item.tickers == ("AAPL",)
    assert item.sentiment == 1.0 and item.severe is False
    assert item.source == "alpaca:benzinga"


def test_finnhub_parsers():
    news = de.parse_finnhub_news(
        [{"datetime": 1794315600, "headline": "Company faces SEC investigation", "source": "Reuters",
          "summary": "", "url": "https://x/b"}], "xyz"
    )  # fmt: skip
    assert news[0].tickers == ("XYZ",)
    assert (news[0].severe, news[0].category) == (True, "investigation")
    assert news[0].sentiment == -1.0

    scheduled = de.parse_finnhub_earnings_calendar(
        {"earningsCalendar": [{"symbol": "AAPL", "date": "2027-01-28", "hour": "amc",
                               "epsEstimate": 2.4, "epsActual": None}, {"symbol": "", "date": "x"}]}
    )  # fmt: skip
    assert (scheduled[0].event_type, scheduled[0].event_date) == ("earnings", date(2027, 1, 28))
    assert scheduled[0].payload == {"kind": "scheduled", "hour": "amc", "eps_estimate": 2.4,
                                    "eps_actual": None}  # fmt: skip

    surprises = de.parse_finnhub_earnings_surprises(
        [{"symbol": "AAPL", "period": "2026-09-30", "actual": 2.1, "estimate": 2.0263,
          "surprisePercent": 3.6372}]
    )  # fmt: skip
    assert surprises[0].payload["surprise_pct"] == 3.6372

    insiders = de.parse_finnhub_insiders(
        {"data": [
            {"symbol": "AAPL", "name": "A", "change": -2399, "transactionCode": "S", "id": "1",
             "transactionPrice": 325.75, "transactionDate": "2026-11-03", "filingDate": "2026-11-05",
             "isDerivative": False},
            {"symbol": "AAPL", "change": 100, "transactionCode": "M", "transactionDate": "2026-11-03",
             "filingDate": "2026-11-05", "isDerivative": True},
        ]}
    )  # fmt: skip
    (trade,) = insiders  # derivative transactions are left out
    assert (trade.event_type, trade.event_date, trade.payload["code"]) == (
        "form_4",
        date(2026, 11, 5),
        "S",
    )


def test_fred_series_skips_missing_values():
    points = de.parse_fred_series(
        {
            "observations": [
                {"date": "2026-11-06", "value": "25.64"},
                {"date": "2026-11-09", "value": "."},
            ]
        },
        "VIXCLS",
    )

    assert points == [
        MacroPoint(series_id="VIXCLS", obs_date=date(2026, 11, 6), value=25.64, source="fred")
    ]


def test_sec_filings_become_events_only_for_the_forms_that_matter():
    payload = {
        "cik": "320193",
        "filings": {"recent": {
            "form": ["8-K", "4", "424B5", "424B2", "10-K"],
            "filingDate": ["2026-11-05", "2026-11-04", "2026-11-03", "2026-11-02", "2026-10-30"],
            "accessionNumber": ["0000320193-26-000101", "0001140361-26-042149", "a-3", "a-4", "a-5"],
            "primaryDocument": ["a8k.htm", "form4.xml", "p.htm", "n.htm", "k.htm"],
            "acceptanceDateTime": ["2026-11-05T21:30:12.000Z", None, None, None, None],
            "items": ["2.02,9.01", "", "", "", ""],
        }},
    }  # fmt: skip

    filings = de.parse_sec_filings(payload, "aapl")
    events = de.filings_to_events(filings)

    assert [f.form for f in filings] == ["8-K", "4", "424B5", "424B2", "10-K"]
    assert filings[0].items == ("2.02", "9.01")
    assert (
        filings[0].url
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000101/a8k.htm"
    )
    assert [e.event_type for e in events] == ["filing_8k", "form_4", "offering"]
    # Not the 10-K, and not the 424B2: banks file those for notes, which is not dilution.
    assert events[0].event_at == datetime(2026, 11, 5, 21, 30, 12, tzinfo=UTC)
    assert events[0].ticker == "AAPL"


def test_alpaca_splits():
    events = de.parse_alpaca_splits(
        {"corporate_actions": {"forward_splits": [
            {"symbol": "NVDA", "ex_date": "2024-06-10", "new_rate": 10, "old_rate": 1, "id": "s1"}]}}
    )  # fmt: skip

    assert (events[0].event_type, events[0].event_date) == ("split", date(2024, 6, 10))
    assert events[0].payload == {"new_rate": 10, "old_rate": 1, "id": "s1"}


# --- sentiment rules -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Apple beats estimates and raises guidance", 1.0),
        ("Shares plunge after company cuts forecast", -1.0),
        ("Company beats on revenue but warns on margins", 0.0),
        ("Annual shareholder meeting scheduled for May", 0.0),
        ("Stock surges, tops estimates despite lawsuit", pytest.approx(1 / 3, abs=1e-4)),
    ],
)
def test_headline_sentiment(headline, expected):
    assert sentiment.score_headline(headline) == expected


@pytest.mark.parametrize(
    ("headline", "category"),
    [
        ("DOJ opens investigation into accounting practices", "investigation"),
        ("Retailer files for Chapter 11 bankruptcy protection", "bankruptcy"),
        ("Biotech announces $200 million public offering of common stock", "offering"),
        ("Analyst downgrades stock to sell", "downgrade"),
        ("Trading halted pending news", "halt"),
        ("Company launches new product line", None),
        ("Probes into deep space continue", None),  # 'probes' is not 'probe'
    ],
)
def test_severe_news_categories(headline, category):
    assert sentiment.severe_category(headline) == category


# --- news de-duplication -------------------------------------------------------------------


def _item(headline: str, hours: float, *, url: str | None = None, tickers=("AAPL",)) -> NewsItem:
    return NewsItem(tickers=tickers, published_at=PREMARKET + timedelta(hours=hours), source="t",
                    headline=headline, url=url)  # fmt: skip


def test_same_url_is_one_story():
    kept = de.dedupe_news([
        _item("Apple hits record", 0, url="https://x.com/a"),
        _item("Apple Hits Record (update)", 9, url="https://X.com/a/"),
        _item("Other story", 1, url="https://x.com/b"),
    ])  # fmt: skip

    assert [item.headline for item in kept] == ["Apple hits record", "Other story"]


def test_same_headline_within_six_hours_is_one_story_and_tickers_merge():
    kept = de.dedupe_news([
        _item("Fed holds rates steady", 0, tickers=("SPY",)),
        _item("FED HOLDS RATES STEADY!", 5.9, tickers=("QQQ",)),
        _item("Fed holds rates steady", 6.5),  # more than 6 hours after the first
    ])  # fmt: skip

    assert len(kept) == 2
    assert kept[0].tickers == ("QQQ", "SPY")
    assert kept[1].published_at - kept[0].published_at > de.NEWS_DUPLICATE_WINDOW


def test_stored_news_is_not_stored_twice(engine):
    with session_scope(engine) as session:
        first = de.store_news(session, [_item("Apple hits record", 0, url="https://x.com/a")])
    with session_scope(engine) as session:
        again = de.store_news(session, [
            _item("Apple hits record", 0, url="https://x.com/a"),  # same URL
            _item("Apple hits record", 2),  # same headline, 2 hours later
            _item("Apple hits record", 30),  # same headline, next day: a new story
        ])  # fmt: skip
        total = session.scalar(select(func.count()).select_from(models.News))

    assert (first, again, total) == (1, 1, 2)


# --- split adjustment ----------------------------------------------------------------------


def test_split_adjustment_restates_earlier_bars():
    index = pd.to_datetime(["2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11"])
    bars = pd.DataFrame(
        {"open": [1200.0, 1210.0, 121.0, 122.0], "high": [1220.0, 1215.0, 123.0, 124.0],
         "low": [1190.0, 1200.0, 120.0, 121.0], "close": [1210.0, 1208.0, 122.0, 123.0],
         "volume": [100.0, 110.0, 1000.0, 1100.0]},
        index=index,
    )  # fmt: skip

    adjusted = de.adjust_for_split(bars, date(2024, 6, 10), new_rate=10, old_rate=1)

    assert adjusted["close"].tolist() == [121.0, 120.8, 122.0, 123.0]  # no jump at the split
    assert adjusted["volume"].tolist() == [1000.0, 1100.0, 1000.0, 1100.0]
    assert adjusted["high"].iloc[0] == 122.0
    assert bars["close"].iloc[0] == 1210.0  # the input is not modified


def test_reverse_split_adjustment():
    index = pd.to_datetime(["2026-03-02", "2026-03-03"])
    bars = pd.DataFrame({"open": [1.0, 20.0], "high": [1.1, 21.0], "low": [0.9, 19.0],
                         "close": [1.0, 20.0], "volume": [2000.0, 100.0]}, index=index)  # fmt: skip

    adjusted = de.adjust_for_split(bars, date(2026, 3, 3), new_rate=1, old_rate=20)

    assert adjusted["close"].tolist() == [20.0, 20.0]
    assert adjusted["volume"].tolist() == [100.0, 100.0]


# --- storage -------------------------------------------------------------------------------


def _daily(ticker: str, day: date, close: float, volume: float = 1000.0) -> Bar:
    return Bar(ticker=ticker, timeframe="1Day", ts=clock.market_time(day, time(0, 0)), session_date=day,
               open=close, high=close + 1, low=close - 1, close=close, volume=volume, source="t")  # fmt: skip


def test_bars_are_upserted(engine):
    with session_scope(engine) as session:
        de.store_bars(session, [_daily("AAPL", DAY, 100.0), _daily("MSFT", DAY, 400.0)])
    with session_scope(engine) as session:
        de.store_bars(session, [_daily("AAPL", DAY, 101.5)])  # the revised bar replaces the old
        rows = session.execute(
            select(models.BarDaily.ticker, models.BarDaily.close).order_by(models.BarDaily.ticker)
        ).all()

    assert [tuple(row) for row in rows] == [("AAPL", 101.5), ("MSFT", 400.0)]


def test_corporate_events_are_not_duplicated_and_estimates_update(engine):
    def scheduled(estimate: float) -> CorporateEvent:
        return CorporateEvent(ticker="AAPL", event_type="earnings", event_date=date(2027, 1, 28),
                              payload={"kind": "scheduled", "eps_estimate": estimate}, source="finnhub")  # fmt: skip

    with session_scope(engine) as session:
        assert de.store_corporate_events(session, [scheduled(2.4), scheduled(2.4)]) == 1
    with session_scope(engine) as session:
        assert de.store_corporate_events(session, [scheduled(2.5)]) == 0
        (row,) = session.scalars(select(models.CorporateEvent)).all()
        assert row.payload["eps_estimate"] == 2.5


# --- point in time -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (PREMARKET, date(2026, 11, 9)),
        (MIDSESSION, date(2026, 11, 9)),
        (clock.market_time(DAY, time(15, 59)), date(2026, 11, 9)),
        (clock.market_time(DAY, time(16, 0)), DAY),  # the session has just closed
        (AFTER_CLOSE, DAY),
        (clock.market_time(date(2026, 11, 14), time(12, 0)), date(2026, 11, 13)),  # Saturday
        (clock.market_time(date(2026, 11, 26), time(12, 0)), date(2026, 11, 25)),  # Thanksgiving
        (clock.market_time(date(2026, 11, 27), time(13, 0)), date(2026, 11, 27)),  # early close
        (clock.market_time(date(2026, 11, 27), time(12, 59)), date(2026, 11, 25)),
    ],
)
def test_last_completed_session(moment, expected):
    assert de.last_completed_session(moment) == expected


def test_daily_bars_of_an_open_session_are_not_visible(engine):
    with session_scope(engine) as session:
        de.store_bars(
            session, [_daily("AAPL", date(2026, 11, 9), 100.0), _daily("AAPL", DAY, 105.0)]
        )

    with session_scope(engine) as session:
        before = de.load_daily_bars(session, MIDSESSION, ["AAPL"])["AAPL"]
        after = de.load_daily_bars(session, AFTER_CLOSE, ["AAPL"])["AAPL"]

    assert before["close"].tolist() == [100.0]  # today's bar is not complete at 11:00
    assert after["close"].tolist() == [100.0, 105.0]


def test_intraday_bars_count_once_they_have_ended(engine):
    def five_minute(at: time) -> Bar:
        ts = clock.market_time(DAY, at)
        return Bar(ticker="SPY", timeframe="5Min", ts=ts, session_date=DAY, open=1, high=1, low=1,
                   close=1, volume=10, source="t")  # fmt: skip

    with session_scope(engine) as session:
        de.store_bars(
            session,
            [five_minute(time(10, 50)), five_minute(time(10, 55)), five_minute(time(11, 0))],
        )
    with session_scope(engine) as session:
        bars = de.load_intraday_bars(session, MIDSESSION, ["SPY"])["SPY"]

    # At 11:00 the 10:55 bar has just ended; the 11:00 bar has only started.
    assert [stamp.astimezone(clock.MARKET_TZ).time() for stamp in bars.index] == [
        time(10, 50),
        time(10, 55),
    ]


def test_macro_values_are_visible_from_the_next_day(engine):
    with session_scope(engine) as session:
        de.store_macro(session, [
            MacroPoint(series_id="VIXCLS", obs_date=date(2026, 11, 9), value=19.92, source="fred"),
            MacroPoint(series_id="VIXCLS", obs_date=DAY, value=21.5, source="fred"),
        ])  # fmt: skip
    with session_scope(engine) as session:
        today = de.load_macro(session, AFTER_CLOSE)["VIXCLS"]
        tomorrow = de.load_macro(session, PREMARKET + timedelta(days=1))["VIXCLS"]

    assert today.tolist() == [19.92]
    assert tomorrow.tolist() == [19.92, 21.5]


def test_news_is_visible_from_its_publication_time(engine):
    with session_scope(engine) as session:
        de.store_news(session, [_item("early", -1), _item("exactly now", 0), _item("later", 1)])
    with session_scope(engine) as session:
        visible = de.load_news(session, PREMARKET)

    assert [item.headline for item in visible] == ["early", "exactly now"]


def test_what_happened_is_known_after_it_happened_but_schedules_are_known_ahead(engine):
    events = [
        CorporateEvent(ticker="AAPL", event_type="earnings", event_date=date(2026, 11, 12),
                       payload={"kind": "scheduled"}, source="finnhub"),
        CorporateEvent(ticker="AAPL", event_type="filing_8k", event_date=DAY, payload={"id": "f1"},
                       event_at=clock.market_time(DAY, time(16, 5)), source="sec_edgar"),
        CorporateEvent(ticker="AAPL", event_type="form_4", event_date=DAY, payload={"id": "f2"},
                       source="finnhub"),
        CorporateEvent(ticker="AAPL", event_type="form_4", event_date=date(2026, 11, 9),
                       payload={"id": "f3"}, source="finnhub"),
    ]  # fmt: skip
    with session_scope(engine) as session:
        de.store_corporate_events(session, events)

    def visible(moment: datetime) -> list[str]:
        with session_scope(engine) as session:
            found = de.load_corporate_events(session, moment, ["AAPL"]).get("AAPL", [])
        return sorted(e.payload.get("id") or e.payload.get("kind") for e in found)

    assert visible(MIDSESSION) == ["f3", "scheduled"]  # today's filings are not known yet
    assert visible(AFTER_CLOSE) == ["f1", "f3", "scheduled"]  # filed at 16:05, seen at 16:30
    assert visible(PREMARKET + timedelta(days=1)) == ["f1", "f2", "f3", "scheduled"]


def test_econ_events_cover_today_and_the_coming_weeks(engine):
    def event(name: str, day: date) -> EconEvent:
        return EconEvent(name=name, country="US", scheduled_at=clock.market_time(day, time(8, 30)),
                         impact="high", source="fred")  # fmt: skip

    with session_scope(engine) as session:
        de.store_econ_events(session, [event("past", date(2026, 11, 6)), event("CPI", DAY),
                                       event("PIB", date(2026, 11, 25)), event("far", date(2027, 3, 1))])  # fmt: skip
    with session_scope(engine) as session:
        names = [e.name for e in de.load_econ_events(session, MIDSESSION)]

    assert names == ["CPI", "PIB"]  # CPI was at 08:30 today: still part of today's context


# --- quality -------------------------------------------------------------------------------


def _history(sessions: int, *, until: date, volume: float = 1000.0) -> pd.DataFrame:
    days = pd.DatetimeIndex([pd.Timestamp(d) for d in clock_sessions(until, sessions)])
    return pd.DataFrame(
        {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": volume}, index=days
    )


def clock_sessions(until: date, count: int) -> list[date]:
    from core import market_calendar

    start = market_calendar.add_trading_days(until, -(count - 1)) if count > 1 else until
    return market_calendar.trading_days_between(start, until)


def test_complete_current_history_scores_one():
    quality = de.assess_quality(_history(260, until=date(2026, 11, 9)), PREMARKET)

    assert quality.score == 1.0 and quality.issues == () and quality.can_buy


def test_missing_the_latest_session_blocks_buying():
    quality = de.assess_quality(_history(260, until=date(2026, 11, 6)), PREMARKET)

    assert quality.score == pytest.approx(0.7)
    assert not quality.can_buy
    assert quality.issues == ("falta la barra del 2026-11-09; la última es del 2026-11-06",)


def test_gaps_short_history_and_idle_days_lower_the_score():
    gaps = _history(260, until=date(2026, 11, 9)).drop(
        [pd.Timestamp("2026-10-20"), pd.Timestamp("2026-10-27")]
    )
    assert de.assess_quality(gaps, PREMARKET).score == pytest.approx(0.9)

    recent_listing = de.assess_quality(_history(40, until=date(2026, 11, 9)), PREMARKET)
    assert recent_listing.score == pytest.approx(0.7)
    assert recent_listing.issues == ("historia corta: 40 sesiones",)
    assert de.assess_quality(
        _history(150, until=date(2026, 11, 9)), PREMARKET
    ).score == pytest.approx(0.9)

    idle = _history(260, until=date(2026, 11, 9))
    idle.loc[idle.index[-3:], "volume"] = 0.0
    assert de.assess_quality(idle, PREMARKET).score == pytest.approx(0.85)


def test_no_bars_at_all_scores_zero():
    assert de.assess_quality(None, PREMARKET).score == 0.0
    assert de.assess_quality(pd.DataFrame(), PREMARKET).issues == ("sin barras diarias",)


def test_old_quote_lowers_the_score_only_while_the_market_is_open():
    history = _history(260, until=date(2026, 11, 9))
    old = Quote(ticker="X", ts=MIDSESSION - timedelta(minutes=5), bid=10, ask=10.1, source="t")
    fresh = Quote(ticker="X", ts=MIDSESSION - timedelta(seconds=30), bid=10, ask=10.1, source="t")

    assert de.assess_quality(history, MIDSESSION, quote=old).score == pytest.approx(0.8)
    assert de.assess_quality(history, MIDSESSION, quote=fresh).score == 1.0
    assert de.assess_quality(history, PREMARKET, quote=old).score == 1.0


# --- universe ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "name", "exchange", "category"),
    [
        ("AAPL", "Apple Inc. Common Stock", "NASDAQ", "common"),
        ("BRK.B", "BERKSHIRE HATHAWAY Class B", "NYSE", "common"),
        ("O", "Realty Income Corporation", "NYSE", "common"),
        ("CPT", "Camden Property Trust", "NYSE", "common"),
        ("NTRS", "Northern Trust Corporation Common Stock", "NASDAQ", "common"),
        ("BABA", "Alibaba Group Holding Limited American Depositary Shares", "NYSE", "common"),
        ("TQQQ", "ProShares UltraPro QQQ", "NASDAQ", "leveraged_etf"),
        ("SQQQ", "ProShares UltraPro Short QQQ", "NASDAQ", "inverse_etf"),
        (
            "NVDL",
            "GraniteShares ETF Trust GraniteShares 2x Long NVDA Daily ETF",
            "NASDAQ",
            "leveraged_etf",
        ),
        ("ZZZX", "Direxion Daily Something Bear 3X Shares ETF", "NASDAQ", "inverse_etf"),
        ("ZZZY", "Tidal Trust II Defiance Daily Target 2x Short XYZ ETF", "NASDAQ", "inverse_etf"),
        ("QQQ", "Invesco QQQ Trust, Series 1", "NASDAQ", "fund"),
        ("IBIT", "iShares Bitcoin Trust ETF Shares", "NASDAQ", "fund"),
        ("NMCO", "Nuveen Municipal Credit Opportunities Fund", "NYSE", "fund"),
        ("DSACU", "Daedalus Special Acquisition Corp. Unit", "NASDAQ", "spac"),
        ("CLBR", "Colombier Acquisition Corp. III", "NYSE", "spac"),
        ("DRTSW", "Alpha Tau Medical Ltd. Warrant", "NASDAQ", "warrant"),
        ("WFC.PRD", "Wells Fargo & Company Depositary Shares", "NYSE", "non_common"),
        ("SAY", "Saratoga Investment Corp. 8.125% Notes due 2027", "NYSE", "non_common"),
        ("BLMZF", "HARRISON GLOBAL HLDGS INC Ordinary Shares", "OTC", "otc"),
    ],
)
def test_classification(symbol, name, exchange, category):
    assert classify(symbol, name, exchange).category == category


def test_listed_leveraged_tickers_are_excluded_whatever_their_name_says():
    """Principle 2: excluded by list as well as by name."""
    assert (
        classify("TQQQ", "Some Ordinary Company Inc. Common Stock", "NASDAQ").category
        == "leveraged_etf"
    )
    assert classify("SQQQ", "Another Plain Name Corp", "NASDAQ").category == "inverse_etf"


def test_eligible_assets_keeps_only_listed_tradable_common_stocks():
    config = settings.load_config(SHIPPED_CONFIG).universe
    assets = [
        {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "status": "active", "tradable": True},
        {"symbol": "TQQQ", "name": "ProShares UltraPro QQQ", "exchange": "NASDAQ", "status": "active", "tradable": True},
        {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "exchange": "ARCA", "status": "active", "tradable": True},
        {"symbol": "XLK", "name": "Technology Select Sector SPDR Fund", "exchange": "ARCA", "status": "active", "tradable": True},
        {"symbol": "HALT", "name": "Halted Corp Common Stock", "exchange": "NYSE", "status": "active", "tradable": False},
        {"symbol": "OLD", "name": "Old Corp Common Stock", "exchange": "NYSE", "status": "inactive", "tradable": True},
        {"symbol": "ARCX", "name": "Plain Company Inc.", "exchange": "ARCA", "status": "active", "tradable": True},
        {"symbol": "OTCC", "name": "Pink Sheet Co", "exchange": "OTC", "status": "active", "tradable": True},
    ]  # fmt: skip

    members, excluded = eligible_assets(assets, config)

    assert [m.ticker for m in members] == ["AAPL"]
    assert excluded["TQQQ"].category == "leveraged_etf"
    assert excluded["TQQQ"].reason == "ETF apalancado (lista)"
    assert excluded["OTCC"].category == "otc"
    assert "SPY" not in excluded and "ARCX" not in excluded  # context tickers; other exchanges


@pytest.mark.parametrize(
    ("sic", "etf"),
    [(3571, "XLK"), (7372, "XLK"), (3674, "XLK"), (2834, "XLV"), (3841, "XLV"), (6021, "XLF"),
     (6798, "XLRE"), (1311, "XLE"), (2911, "XLE"), (4911, "XLU"), (5812, "XLY"), (3711, "XLY"),
     (2080, "XLP"), (5411, "XLP"), (3721, "XLI"), (4011, "XLI"), (2821, "XLB"), (1040, "XLB"),
     (4813, "XLC"), (7812, "XLC"), ("3571", "XLK"), (9995, None), (None, None), ("", None)],
)  # fmt: skip
def test_sector_etf_from_sic_code(sic, etf):
    assert sector_etf_for_sic(sic) == etf


def test_daily_index_is_sorted_and_numeric(engine):
    rng = np.random.default_rng(1)
    days = clock_sessions(date(2026, 11, 9), 30)
    with session_scope(engine) as session:
        de.store_bars(
            session, [_daily("AAPL", day, 100 + float(rng.normal())) for day in reversed(days)]
        )
    with session_scope(engine) as session:
        bars = de.load_daily_bars(session, PREMARKET, ["AAPL"])["AAPL"]

    assert bars.index.is_monotonic_increasing and len(bars) == 30
    assert bars.index[-1] == pd.Timestamp("2026-11-09")
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
