"""SEC EDGAR: company filings (8-K events, Form 4 insiders, share offerings).

No key. The SEC requires a User-Agent with a name and an email, and allows
10 requests per second.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from collectors.base import Collector, RawSnapshot, _Requests, request_key

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Forms that announce new shares for sale (dilution): registrations and the final
# prospectus of an equity deal. 424B2 and FWP are left out on purpose: banks file
# them daily for structured notes, which are not dilution.
OFFERING_FORMS = frozenset({"S-1", "S-1/A", "S-3", "S-3/A", "424B4", "424B5"})


class SecEdgarCollector(Collector):
    source = "sec_edgar"

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._secret("sec_user_agent")}

    async def company_tickers(self, *, as_of: datetime | None = None) -> RawSnapshot:
        """Ticker to CIK mapping for every company registered with the SEC."""

        async def fetch(requests: _Requests) -> Any:
            return await requests.get_json(TICKERS_URL, headers=self._headers())

        return await self._collect("company_tickers", "all", fetch, as_of=as_of)

    async def submissions(self, cik: int, *, as_of: datetime | None = None) -> RawSnapshot:
        """Recent filings of one company, by its CIK number."""

        async def fetch(requests: _Requests) -> Any:
            return await requests.get_json(
                SUBMISSIONS_URL.format(cik=int(cik)), headers=self._headers()
            )

        return await self._collect("submissions", request_key(cik=int(cik)), fetch, as_of=as_of)
