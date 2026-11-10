"""Each collector against simulated responses shaped like the real APIs."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, time

import httpx2 as httpx
import pytest

from collectors import alpaca as alpaca_module
from collectors.alpaca import AlpacaCollector
from collectors.base import SnapshotStatus
from collectors.econ_calendar import EconCalendarCollector
from collectors.finnhub import FinnhubCollector
from collectors.fred import FredCollector, latest_observation
from collectors.fx import FxCollector, latest_rate
from collectors.http import SourceError
from collectors.massive import MassiveCollector
from collectors.rss_news import RssNewsCollector, parse_rss
from collectors.sec_edgar import SecEdgarCollector
from collectors.telegram import TelegramCollector
from collectors.yahoo import YahooCollector
from core import clock
from tests.conftest import TEST_SECRETS

pytestmark = pytest.mark.anyio

# Tuesday 10 November 2026, a regular session.
DAY = date(2026, 11, 10)
MIDSESSION = clock.market_time(DAY, time(11, 0))
AFTER_CLOSE = clock.market_time(DAY, time(17, 0))


class Recorded:
    """Collects requests and answers them with ``respond``."""

    def __init__(self, respond):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _bar(day: str, close: float = 100.0) -> dict:
    return {"t": f"{day}T05:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": 1000}


# --- Alpaca -----------------------------------------------------------------------


async def test_alpaca_daily_bars_request(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, json={"bars": {"SPY": [_bar("2026-11-10")]}}))
    snapshot = await AlpacaCollector(make_runtime(handler)).daily_bars(
        ["spy", "AAPL", "SPY"], start=date(2026, 11, 2), as_of=AFTER_CLOSE
    )

    (request,) = handler.requests
    assert request.url.host == "data.alpaca.markets"
    assert request.url.path == "/v2/stocks/bars"
    assert dict(request.url.params) == {
        "timeframe": "1Day",
        "start": "2026-11-02",
        "adjustment": "split",
        "feed": "sip",
        "symbols": "AAPL,SPY",
        "limit": "10000",
        "sort": "asc",
    }
    assert request.headers["APCA-API-KEY-ID"] == TEST_SECRETS["alpaca_api_key"]
    assert request.headers["APCA-API-SECRET-KEY"] == TEST_SECRETS["alpaca_api_secret"]
    assert snapshot.status is SnapshotStatus.OK
    assert snapshot.payload["feed"] == "sip"
    assert snapshot.request_key == "start=2026-11-02&symbols=AAPL,SPY"


async def test_alpaca_bars_follow_pages_and_split_long_symbol_lists(make_runtime):
    def respond(request: httpx.Request) -> httpx.Response:
        symbols = request.url.params["symbols"].split(",")
        first = symbols[0]
        if "page_token" not in request.url.params:
            return httpx.Response(
                200, json={"bars": {first: [_bar("2026-11-09")]}, "next_page_token": "p2"}
            )
        return httpx.Response(
            200, json={"bars": {first: [_bar("2026-11-10")]}, "next_page_token": None}
        )

    handler = Recorded(respond)
    symbols = [f"T{i:03d}" for i in range(250)]
    snapshot = await AlpacaCollector(make_runtime(handler)).daily_bars(
        symbols, start=date(2026, 11, 2), as_of=AFTER_CLOSE
    )

    sizes = [len(r.url.params["symbols"].split(",")) for r in handler.requests]
    assert sizes == [100, 100, 100, 100, 50, 50]  # three groups, two pages each
    assert [b["t"][:10] for b in snapshot.payload["bars"]["T000"]] == ["2026-11-09", "2026-11-10"]
    assert set(snapshot.payload["bars"]) == {"T000", "T100", "T200"}


@pytest.mark.parametrize(
    ("as_of", "last_bar", "expected_status"),
    [
        (AFTER_CLOSE, "2026-11-10", SnapshotStatus.OK),
        (AFTER_CLOSE, "2026-11-09", SnapshotStatus.STALE),  # today's bar should exist by 17:00
        (MIDSESSION, "2026-11-09", SnapshotStatus.OK),  # mid-session: yesterday is the latest
        (MIDSESSION, "2026-11-06", SnapshotStatus.STALE),  # Monday's bar is missing
    ],
)
async def test_alpaca_daily_bars_missing_the_last_session_are_stale(
    make_runtime, as_of, last_bar, expected_status
):
    handler = Recorded(lambda r: httpx.Response(200, json={"bars": {"SPY": [_bar(last_bar)]}}))

    snapshot = await AlpacaCollector(make_runtime(handler)).daily_bars(
        ["SPY"], start=date(2026, 11, 2), as_of=as_of
    )

    assert snapshot.status is expected_status
    if expected_status is SnapshotStatus.STALE:
        assert f"la última barra diaria es del {last_bar}" in snapshot.message


async def test_alpaca_historical_range_is_not_stale_for_ending_earlier(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, json={"bars": {"SPY": [_bar("2026-05-29")]}}))

    snapshot = await AlpacaCollector(make_runtime(handler)).daily_bars(
        ["SPY"], start=date(2026, 5, 1), end=date(2026, 5, 29), as_of=AFTER_CLOSE
    )

    assert snapshot.status is SnapshotStatus.OK
    assert handler.requests[0].url.params["end"] == "2026-05-29"


async def test_alpaca_intraday_bars_use_the_realtime_feed(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, json={"bars": {}}))

    await AlpacaCollector(make_runtime(handler)).intraday_bars(
        ["SPY"], start=clock.market_time(DAY, time(9, 30)), as_of=MIDSESSION
    )

    params = handler.requests[0].url.params
    assert params["timeframe"] == "5Min"
    assert params["feed"] == "iex"
    assert params["start"] == "2026-11-10T14:30:00Z"


@pytest.mark.parametrize(
    ("as_of", "quote_time", "expected"),
    [
        (MIDSESSION, "2026-11-10T15:59:30.779321638Z", SnapshotStatus.OK),  # 30 s old
        (MIDSESSION, "2026-11-10T15:55:00Z", SnapshotStatus.STALE),  # 5 min old, market open
        (AFTER_CLOSE, "2026-11-10T20:59:00Z", SnapshotStatus.OK),  # market closed: not judged
    ],
)
async def test_alpaca_quotes_older_than_two_minutes_in_session_are_stale(
    make_runtime, as_of, quote_time, expected
):
    quote = {"bp": 748.86, "ap": 748.9, "t": quote_time}
    handler = Recorded(lambda r: httpx.Response(200, json={"quotes": {"SPY": quote}}))

    snapshot = await AlpacaCollector(make_runtime(handler)).latest_quotes(["SPY"], as_of=as_of)

    assert handler.requests[0].url.path == "/v2/stocks/quotes/latest"
    assert handler.requests[0].url.params["feed"] == "iex"
    assert snapshot.status is expected


async def test_alpaca_assets_is_the_only_call_to_the_trading_host(make_runtime):
    handler = Recorded(
        lambda r: httpx.Response(200, json=[{"symbol": "AAPL", "exchange": "NASDAQ"}])
    )

    snapshot = await AlpacaCollector(make_runtime(handler)).assets(as_of=MIDSESSION)

    (request,) = handler.requests
    assert request.method == "GET"
    assert str(request.url).startswith("https://paper-api.alpaca.markets/v2/assets?")
    assert dict(request.url.params) == {"status": "active", "asset_class": "us_equity"}
    assert snapshot.payload == {"assets": [{"symbol": "AAPL", "exchange": "NASDAQ"}]}


def test_alpaca_module_has_no_order_endpoints():
    """Principle 1: nothing in the Alpaca collector can touch orders or positions."""
    source = inspect.getsource(alpaca_module)

    for forbidden in ("/orders", "/positions", "/account", "api.alpaca.markets/v2/orders"):
        assert forbidden not in source
    assert source.count("paper-api.alpaca.markets") == 1  # the asset list URL


async def test_alpaca_news_pages_until_the_cap(make_runtime):
    def respond(request: httpx.Request) -> httpx.Response:
        page = len(handler.requests)
        return httpx.Response(
            200, json={"news": [{"id": page, "headline": f"h{page}"}], "next_page_token": "more"}
        )

    handler = Recorded(respond)
    snapshot = await AlpacaCollector(make_runtime(handler)).news(
        ["AAPL"], start=clock.market_time(DAY, time(0, 0)), max_pages=3, as_of=MIDSESSION
    )

    assert len(handler.requests) == 3
    assert [item["id"] for item in snapshot.payload["news"]] == [1, 2, 3]
    assert snapshot.payload["truncated"] is True
    assert handler.requests[0].url.params["symbols"] == "AAPL"
    assert handler.requests[0].url.params["include_content"] == "false"
    assert handler.requests[1].url.params["page_token"] == "more"


async def test_alpaca_corporate_actions(make_runtime):
    split = {"symbol": "NVDA", "ex_date": "2024-06-10", "new_rate": 10, "old_rate": 1}
    handler = Recorded(
        lambda r: httpx.Response(200, json={"corporate_actions": {"forward_splits": [split]}})
    )

    snapshot = await AlpacaCollector(make_runtime(handler)).corporate_actions(
        ["NVDA"], start=date(2024, 1, 1), as_of=MIDSESSION
    )

    assert handler.requests[0].url.path == "/v1/corporate-actions"
    assert handler.requests[0].url.params["types"] == "forward_split,reverse_split"
    assert snapshot.payload == {"corporate_actions": {"forward_splits": [split]}}


# --- Finnhub ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "path", "params"),
    [
        (
            lambda c: c.company_news("aapl", start=date(2026, 11, 8), end=DAY, as_of=MIDSESSION),
            "/api/v1/company-news",
            {"symbol": "AAPL", "from": "2026-11-08", "to": "2026-11-10"},
        ),
        (
            lambda c: c.earnings_calendar(start=DAY, end=date(2026, 11, 17), as_of=MIDSESSION),
            "/api/v1/calendar/earnings",
            {"from": "2026-11-10", "to": "2026-11-17"},
        ),
        (
            lambda c: c.earnings_surprises("AAPL", as_of=MIDSESSION),
            "/api/v1/stock/earnings",
            {"symbol": "AAPL"},
        ),
        (
            lambda c: c.basic_financials("AAPL", as_of=MIDSESSION),
            "/api/v1/stock/metric",
            {"symbol": "AAPL", "metric": "all"},
        ),
        (
            lambda c: c.company_profile("AAPL", as_of=MIDSESSION),
            "/api/v1/stock/profile2",
            {"symbol": "AAPL"},
        ),
        (
            lambda c: c.insider_transactions("AAPL", start=date(2026, 8, 10), as_of=MIDSESSION),
            "/api/v1/stock/insider-transactions",
            {"symbol": "AAPL", "from": "2026-08-10"},
        ),
    ],
)
async def test_finnhub_requests(make_runtime, call, path, params):
    handler = Recorded(lambda r: httpx.Response(200, json={"ok": True}))

    snapshot = await call(FinnhubCollector(make_runtime(handler)))

    (request,) = handler.requests
    assert request.url.host == "finnhub.io"
    assert request.url.path == path
    assert dict(request.url.params) == params  # the key is not in the URL
    assert request.headers["X-Finnhub-Token"] == TEST_SECRETS["finnhub_api_key"]
    assert snapshot.status is SnapshotStatus.OK
    assert snapshot.payload == {"ok": True}


# --- FRED ---------------------------------------------------------------------------


def _observations(*pairs: tuple[str, str]) -> dict:
    return {"observations": [{"date": day, "value": value} for day, value in pairs]}


async def test_fred_series_request_and_latest_value(make_runtime):
    payload = _observations(("2026-11-06", "25.64"), ("2026-11-09", "19.92"), ("2026-11-10", "."))
    handler = Recorded(lambda r: httpx.Response(200, json=payload))

    snapshot = await FredCollector(make_runtime(handler)).series_observations(
        "VIXCLS", start=date(2026, 10, 20), as_of=MIDSESSION
    )

    (request,) = handler.requests
    assert request.url.path == "/fred/series/observations"
    assert dict(request.url.params) == {
        "series_id": "VIXCLS",
        "observation_start": "2026-10-20",
        "sort_order": "asc",
        "api_key": TEST_SECRETS["fred_api_key"],
        "file_type": "json",
    }
    assert snapshot.status is SnapshotStatus.OK
    assert latest_observation(snapshot.payload) == (date(2026, 11, 9), 19.92)
    assert snapshot.request_key == "series_id=VIXCLS&start=2026-10-20"  # no key in what is stored


@pytest.mark.parametrize(
    ("last_date", "expected"),
    [
        ("2026-11-09", SnapshotStatus.OK),  # 1 trading day behind
        ("2026-11-05", SnapshotStatus.OK),  # 3 trading days behind: the limit
        ("2026-11-04", SnapshotStatus.STALE),  # 4 trading days behind
    ],
)
async def test_fred_series_more_than_three_trading_days_old_is_stale(
    make_runtime, last_date, expected
):
    handler = Recorded(lambda r: httpx.Response(200, json=_observations((last_date, "4.86"))))

    snapshot = await FredCollector(make_runtime(handler)).series_observations(
        "DGS10", start=date(2026, 10, 1), as_of=MIDSESSION
    )

    assert snapshot.status is expected


async def test_fred_series_without_values_is_stale(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, json=_observations(("2026-11-10", "."))))

    snapshot = await FredCollector(make_runtime(handler)).series_observations(
        "DGS10", start=date(2026, 11, 1), as_of=MIDSESSION
    )

    assert snapshot.status is SnapshotStatus.STALE
    assert snapshot.message == "la serie no tiene observaciones"


async def test_fred_error_never_leaks_the_key(make_runtime):
    handler = Recorded(lambda r: httpx.Response(400))

    snapshot = await FredCollector(make_runtime(handler)).series_observations(
        "NOPE", start=date(2026, 11, 1), as_of=MIDSESSION
    )

    assert snapshot.status is SnapshotStatus.ERROR
    assert snapshot.message == "api.stlouisfed.org respondió HTTP 400"
    assert TEST_SECRETS["fred_api_key"] not in repr(snapshot)


# --- economic calendar ---------------------------------------------------------------

RELEASE_DATES = {
    "10": ["2026-12-10", "2026-11-10", "2026-10-14"],  # CPI
    "50": ["2026-12-04", "2026-11-06"],  # employment
    "53": ["2026-12-23", "2026-11-25"],  # GDP
}


def _release_dates(request: httpx.Request) -> httpx.Response:
    dates = RELEASE_DATES[request.url.params["release_id"]]
    return httpx.Response(200, json={"release_dates": [{"date": day} for day in dates]})


async def test_econ_calendar_merges_fred_releases_and_fomc_dates(make_runtime):
    runtime = make_runtime(Recorded(_release_dates))
    runtime.fomc_dates = (date(2026, 10, 28), date(2026, 12, 9), date(2027, 1, 27))

    snapshot = await EconCalendarCollector(runtime).upcoming(horizon_days=45, as_of=MIDSESSION)

    events = [(e["name"], e["scheduled_at"], e["source"]) for e in snapshot.payload["events"]]
    assert events == [
        ("CPI", "2026-11-10T13:30:00+00:00", "fred"),  # 08:30 in New York
        ("PIB", "2026-11-25T13:30:00+00:00", "fred"),
        ("Empleo", "2026-12-04T13:30:00+00:00", "fred"),
        ("FOMC", "2026-12-09T19:00:00+00:00", "config"),  # 14:00 in New York
        ("CPI", "2026-12-10T13:30:00+00:00", "fred"),
        ("PIB", "2026-12-23T13:30:00+00:00", "fred"),
    ]
    assert snapshot.status is SnapshotStatus.OK
    assert snapshot.payload["partial"] is False
    assert all(e["impact"] == "high" and e["country"] == "US" for e in snapshot.payload["events"])
    assert (snapshot.source, snapshot.dataset) == ("fred", "econ_calendar")


async def test_econ_calendar_is_partial_when_fred_fails_for_one_release(make_runtime, recorder):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["release_id"] == "50":
            return httpx.Response(404)
        return _release_dates(request)

    runtime = make_runtime(Recorded(respond))
    runtime.fomc_dates = (date(2026, 12, 9),)

    snapshot = await EconCalendarCollector(runtime).upcoming(as_of=MIDSESSION)

    assert snapshot.status is SnapshotStatus.ERROR
    assert snapshot.payload["partial"] is True
    assert snapshot.message.startswith("calendario incompleto; Empleo: api.stlouisfed.org")
    assert {e["name"] for e in snapshot.payload["events"]} == {"CPI", "PIB", "FOMC"}
    assert recorder.snapshots[-1] is snapshot


async def test_econ_calendar_without_fred_still_knows_fomc_dates(make_runtime, no_secrets):
    handler = Recorded(_release_dates)
    runtime = make_runtime(handler, secrets=no_secrets)
    runtime.fomc_dates = (date(2026, 12, 9),)

    snapshot = await EconCalendarCollector(runtime).upcoming(as_of=MIDSESSION)

    assert handler.requests == []
    assert snapshot.status is SnapshotStatus.NOT_CONFIGURED
    assert "FRED_API_KEY" in snapshot.message
    assert [e["name"] for e in snapshot.payload["events"]] == ["FOMC"]
    assert snapshot.payload["partial"] is True


# --- SEC EDGAR ----------------------------------------------------------------------


async def test_sec_requests_identify_the_user_and_pad_the_cik(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, json={"name": "Apple Inc."}))
    collector = SecEdgarCollector(make_runtime(handler))

    await collector.submissions(320193, as_of=MIDSESSION)
    await collector.company_tickers(as_of=MIDSESSION)

    submissions, tickers = handler.requests
    assert str(submissions.url) == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert str(tickers.url) == "https://www.sec.gov/files/company_tickers.json"
    assert submissions.headers["User-Agent"] == "VERTICE Test test@example.com"
    assert tickers.headers["User-Agent"] == "VERTICE Test test@example.com"


async def test_sec_without_user_agent_is_not_configured(make_runtime, no_secrets):
    handler = Recorded(lambda r: httpx.Response(200, json={}))

    snapshot = await SecEdgarCollector(make_runtime(handler, secrets=no_secrets)).submissions(
        320193, as_of=MIDSESSION
    )

    assert snapshot.status is SnapshotStatus.NOT_CONFIGURED
    assert snapshot.message == "sin configurar: falta SEC_USER_AGENT"
    assert handler.requests == []


# --- RSS ----------------------------------------------------------------------------

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>News</title>
<item><title>Apple hits record high</title><link>https://example.com/a</link>
<pubDate>Tue, 10 Nov 2026 14:05:00 GMT</pubDate><source url="https://example.com">Example</source>
<description>Shares rose.</description></item>
<item><title>Second headline</title><link>https://example.com/b</link>
<pubDate>Tue, 10 Nov 2026 09:00:00 -0500</pubDate></item>
</channel></rss>"""


def test_parse_rss():
    first, second = parse_rss(RSS)

    assert first == {
        "title": "Apple hits record high",
        "link": "https://example.com/a",
        "published": "2026-11-10T14:05:00+00:00",
        "source": "Example",
        "description": "Shares rose.",
    }
    assert second["published"] == "2026-11-10T14:00:00+00:00"  # converted to UTC
    assert second["source"] is None


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>Too many requests</body></html>",
        "not xml at all",
        "<rss><oops></rss>",
        # Entity expansion attack ("billion laughs"): must be refused, not expanded.
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        "<rss><channel><item><title>&lol2;</title></item></channel></rss>",
    ],
)
def test_parse_rss_refuses_what_is_not_a_feed(body):
    with pytest.raises(SourceError, match="no es un RSS válido"):
        parse_rss(body)


async def test_rss_feeds_request_and_payload(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, text=RSS))
    collector = RssNewsCollector(make_runtime(handler))

    google = await collector.google_news("AAPL stock", as_of=MIDSESSION)
    yahoo = await collector.yahoo_finance("aapl", as_of=MIDSESSION)

    assert handler.requests[0].url.host == "news.google.com"
    assert dict(handler.requests[0].url.params) == {
        "q": "AAPL stock",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }
    assert handler.requests[1].url.host == "feeds.finance.yahoo.com"
    assert handler.requests[1].url.params["s"] == "AAPL"
    assert "Mozilla" in handler.requests[0].headers["User-Agent"]
    assert len(parse_rss(google.payload["xml"])) == 2
    assert yahoo.status is SnapshotStatus.OK


async def test_rss_asks_at_most_once_per_ticker_every_15_minutes(make_runtime, fake_time):
    handler = Recorded(lambda r: httpx.Response(200, text=RSS))
    collector = RssNewsCollector(make_runtime(handler))

    for _ in range(5):
        await collector.yahoo_finance("AAPL", as_of=MIDSESSION)
        fake_time.now += 120  # every 2 minutes
    assert len(handler.requests) == 1

    fake_time.now += 900
    await collector.yahoo_finance("AAPL", as_of=MIDSESSION)
    await collector.yahoo_finance("MSFT", as_of=MIDSESSION)  # another ticker has its own slot
    assert len(handler.requests) == 3


async def test_rss_error_page_is_a_failure_not_news(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, text="<html>blocked</html>"))

    snapshot = await RssNewsCollector(make_runtime(handler)).google_news("x", as_of=MIDSESSION)

    assert snapshot.status is SnapshotStatus.ERROR
    assert snapshot.message == "la respuesta no es un RSS válido"


# --- Massive, Yahoo, FX, Telegram -----------------------------------------------------


async def test_massive_requests(make_runtime):
    handler = Recorded(lambda r: httpx.Response(200, json={"results": [{"c": 1}]}))
    collector = MassiveCollector(make_runtime(handler))

    await collector.daily_bars("spy", start=date(2026, 11, 2), end=DAY, as_of=MIDSESSION)
    await collector.grouped_daily(date(2026, 11, 9), as_of=MIDSESSION)

    bars, grouped = handler.requests
    assert bars.url.path == "/v2/aggs/ticker/SPY/range/1/day/2026-11-02/2026-11-10"
    assert grouped.url.path == "/v2/aggs/grouped/locale/us/market/stocks/2026-11-09"
    assert bars.url.host == "api.massive.com"
    assert bars.headers["Authorization"] == f"Bearer {TEST_SECRETS['massive_api_key']}"
    assert bars.url.params["adjusted"] == "true"


async def test_massive_without_key_is_not_configured(make_runtime, no_secrets):
    handler = Recorded(lambda r: httpx.Response(200, json={}))

    snapshot = await MassiveCollector(make_runtime(handler, secrets=no_secrets)).grouped_daily(
        DAY, as_of=MIDSESSION
    )

    assert snapshot.status is SnapshotStatus.NOT_CONFIGURED
    assert snapshot.message == "sin configurar: falta MASSIVE_API_KEY"
    assert handler.requests == []


async def test_yahoo_chart_and_its_failure_modes(make_runtime, data_config):
    chart = {"chart": {"result": [{"meta": {"regularMarketPrice": 19.4}, "timestamp": [1, 2]}]}}
    handler = Recorded(lambda r: httpx.Response(200, json=chart))
    ok = await YahooCollector(make_runtime(handler)).chart("^VIX", as_of=MIDSESSION)

    assert handler.requests[0].url.raw_path.startswith(b"/v8/finance/chart/%5EVIX")
    assert dict(handler.requests[0].url.params) == {"interval": "15m", "range": "1d"}
    assert ok.status is SnapshotStatus.OK

    blocked = Recorded(lambda r: httpx.Response(429))
    failed = await YahooCollector(make_runtime(blocked)).chart("^VIX", as_of=MIDSESSION)
    assert failed.status is SnapshotStatus.ERROR
    assert "demasiadas solicitudes" in failed.message
    assert len(blocked.requests) == 2  # one retry only: config says not to insist
    assert data_config.sources["yahoo"].critical is False

    empty = Recorded(lambda r: httpx.Response(200, json={"chart": {"result": None}}))
    nothing = await YahooCollector(make_runtime(empty)).chart("^NOPE", as_of=MIDSESSION)
    assert nothing.status is SnapshotStatus.ERROR
    assert nothing.message == "Yahoo no devolvió datos para el símbolo"


async def test_fx_uses_banxico_when_there_is_a_token(make_runtime):
    payload = {"bmx": {"series": [{"datos": [{"fecha": "10/11/2026", "dato": "18.6105"}]}]}}
    handler = Recorded(lambda r: httpx.Response(200, json=payload))

    snapshot = await FxCollector(make_runtime(handler)).usd_mxn(as_of=MIDSESSION)

    (request,) = handler.requests
    assert request.url.host == "www.banxico.org.mx"
    assert request.url.path.endswith("/series/SF43718/datos/oportuno")
    assert request.headers["Bmx-Token"] == TEST_SECRETS["banxico_token"]
    assert snapshot.source == "banxico"
    assert latest_rate(snapshot) == (date(2026, 11, 10), 18.6105)


async def test_fx_falls_back_to_fred_without_a_banxico_token(make_runtime, all_secrets):
    secrets = all_secrets.model_copy(update={"banxico_token": None})
    handler = Recorded(lambda r: httpx.Response(200, json=_observations(("2026-11-06", "18.6226"))))

    snapshot = await FxCollector(make_runtime(handler, secrets=secrets)).usd_mxn(as_of=MIDSESSION)

    (request,) = handler.requests
    assert request.url.host == "api.stlouisfed.org"
    assert request.url.params["series_id"] == "DEXMXUS"
    assert snapshot.source == "fred"
    assert latest_rate(snapshot) == (date(2026, 11, 6), 18.6226)


async def test_telegram_check_does_not_send_messages(make_runtime):
    handler = Recorded(
        lambda r: httpx.Response(200, json={"ok": True, "result": {"username": "demo_bot"}})
    )

    snapshot = await TelegramCollector(make_runtime(handler)).get_me(as_of=MIDSESSION)

    (request,) = handler.requests
    assert request.method == "GET"
    assert request.url.path == f"/bot{TEST_SECRETS['telegram_bot_token']}/getMe"
    assert snapshot.status is SnapshotStatus.OK
    assert snapshot.payload["result"]["username"] == "demo_bot"


async def test_telegram_failure_never_leaks_the_token(make_runtime):
    handler = Recorded(lambda r: httpx.Response(401, json={"ok": False}))

    snapshot = await TelegramCollector(make_runtime(handler)).get_me(as_of=MIDSESSION)

    assert snapshot.status is SnapshotStatus.ERROR
    assert snapshot.message == "api.telegram.org respondió HTTP 401 (credenciales rechazadas)"
    assert TEST_SECRETS["telegram_bot_token"] not in repr(snapshot)


def test_midsession_and_after_close_are_what_the_tests_assume():
    assert datetime(2026, 11, 10, 16, 0, tzinfo=UTC) == MIDSESSION
    assert datetime(2026, 11, 10, 22, 0, tzinfo=UTC) == AFTER_CLOSE
