from __future__ import annotations

from datetime import date, time

import httpx2 as httpx
import pytest

from collectors import check
from core import clock

pytestmark = pytest.mark.anyio

MOMENT = clock.market_time(date(2026, 11, 10), time(11, 0))

RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel>'
    "<item><title>a</title></item><item><title>b</title></item></channel></rss>"
)


def healthy(request: httpx.Request) -> httpx.Response:
    """Every provider answering well, with responses shaped like the real ones."""
    host, path = request.url.host, request.url.path
    if host == "data.alpaca.markets" and path.endswith("/quotes/latest"):
        quote = {"bp": 748.86, "ap": 748.9, "t": "2026-11-10T15:59:50Z"}
        return httpx.Response(200, json={"quotes": {"SPY": quote}})
    if host == "data.alpaca.markets":
        bars = [{"t": "2026-11-09T05:00:00Z", "c": 748.58}]
        return httpx.Response(200, json={"bars": {"SPY": bars}})
    if host == "finnhub.io" and path.endswith("/profile2"):
        return httpx.Response(200, json={"name": "Apple Inc.", "marketCapitalization": 5096290.8})
    if host == "finnhub.io":
        return httpx.Response(200, json={"earningsCalendar": [{"symbol": "X"}] * 3})
    if host == "api.stlouisfed.org" and path.endswith("/release/dates"):
        return httpx.Response(200, json={"release_dates": [{"date": "2026-12-10"}]})
    if host == "api.stlouisfed.org":
        return httpx.Response(
            200, json={"observations": [{"date": "2026-11-09", "value": "19.92"}]}
        )
    if host == "data.sec.gov":
        recent = {"form": ["8-K"], "filingDate": ["2026-11-05"]}
        return httpx.Response(200, json={"name": "Apple Inc.", "filings": {"recent": recent}})
    if host in ("news.google.com", "feeds.finance.yahoo.com"):
        return httpx.Response(200, text=RSS)
    if host == "www.banxico.org.mx":
        point = {"fecha": "10/11/2026", "dato": "18.6105"}
        return httpx.Response(200, json={"bmx": {"series": [{"datos": [point]}]}})
    if host == "api.massive.com":
        return httpx.Response(200, json={"results": [{"c": 1}, {"c": 2}]})
    if host == "query1.finance.yahoo.com":
        meta = {"regularMarketPrice": 19.4}
        return httpx.Response(200, json={"chart": {"result": [{"meta": meta}]}})
    if host == "api.telegram.org":
        return httpx.Response(200, json={"ok": True, "result": {"username": "demo_bot"}})
    raise AssertionError(f"unexpected request to {host}{path}")


async def test_every_source_ok_with_its_latency(make_runtime, fake_time):
    def handler(request: httpx.Request) -> httpx.Response:
        fake_time.now += 0.12
        return healthy(request)

    results = await check.run_checks(make_runtime(handler), MOMENT)

    assert {r.source for r in results} == {
        "alpaca", "finnhub", "fred", "sec_edgar", "rss_news", "banxico", "massive", "yahoo",
        "telegram",
    }  # fmt: skip
    assert all(r.status == "ok" for r in results), [(r.source, r.status, r.detail) for r in results]
    assert all(r.latency_ms and r.latency_ms >= 120 for r in results)
    assert check.exit_code(results) == 0

    report = check.render(results, MOMENT)
    assert "11:00 NY · 10:00 Monterrey" in report
    assert "SPY compra 748.86 / venta 748.90 (IEX)" in report
    assert "VIX 19.92 al 2026-11-09" in report
    assert "bot @demo_bot responde" in report
    assert "120 ms" in report
    assert "Fuentes críticas: todas responden (alpaca, finnhub, fred)." in report
    assert "PRECAUCIÓN" not in report


async def test_without_keys_sources_show_as_not_configured_and_are_not_contacted(
    make_runtime, no_secrets
):
    contacted: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.add(request.url.host)
        return healthy(request)

    results = await check.run_checks(make_runtime(handler, secrets=no_secrets), MOMENT)

    status = {(r.source, r.label): r.status for r in results}
    needs_key = {"alpaca", "finnhub", "fred", "sec_edgar", "banxico", "massive", "telegram"}
    assert all(s == "not_configured" for (source, _), s in status.items() if source in needs_key)
    assert all(s == "ok" for (source, _), s in status.items() if source not in needs_key)
    # Only the sources that need no key were called.
    assert contacted == {"news.google.com", "feeds.finance.yahoo.com", "query1.finance.yahoo.com"}

    report = check.render(results, MOMENT)
    assert "SIN CONFIGURAR" in report
    assert "falta ALPACA_API_KEY, ALPACA_API_SECRET" in report
    assert "falta MASSIVE_API_KEY (opcional)" in report
    assert "PRECAUCIÓN: fuentes críticas con problema: alpaca, finnhub, fred." in report
    assert check.exit_code(results) == 1


async def test_optional_source_failing_does_not_fail_the_check(make_runtime):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "query1.finance.yahoo.com":
            return httpx.Response(429)
        return healthy(request)

    results = await check.run_checks(make_runtime(handler), MOMENT)

    yahoo = next(r for r in results if r.source == "yahoo")
    assert yahoo.status == "error"
    assert yahoo.detail.endswith("se usa el VIX diario de FRED")
    assert check.exit_code(results) == 0
    report = check.render(results, MOMENT)
    assert "Opcionales sin servicio o sin configurar: yahoo." in report
    assert "Fuentes críticas: todas responden" in report


async def test_critical_source_failing_fails_the_check_and_is_named(make_runtime):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "finnhub.io":
            return httpx.Response(401)
        return healthy(request)

    results = await check.run_checks(make_runtime(handler), MOMENT)

    assert check.exit_code(results) == 1
    report = check.render(results, MOMENT)
    assert "PRECAUCIÓN: fuentes críticas con problema: finnhub." in report
    assert "finnhub.io respondió HTTP 401 (credenciales rechazadas)" in report


async def test_report_never_contains_a_credential(make_runtime, all_secrets):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403) if request.url.host != "news.google.com" else healthy(request)

    results = await check.run_checks(make_runtime(handler), MOMENT)
    report = check.render(results, MOMENT)

    for secret in all_secrets.secret_values():
        assert secret not in report


async def test_stale_market_data_is_reported_as_old(make_runtime):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/stocks/bars"):
            bars = [{"t": "2026-11-04T05:00:00Z", "c": 740.0}]
            return httpx.Response(200, json={"bars": {"SPY": bars}})
        return healthy(request)

    results = await check.run_checks(make_runtime(handler), MOMENT)

    bars = next(r for r in results if r.label == "barras diarias")
    assert bars.status == "stale"
    assert "se esperaba la del 2026-11-09" in bars.detail
    assert check.exit_code(results) == 1
    assert "DATOS VIEJOS" in check.render(results, MOMENT)
