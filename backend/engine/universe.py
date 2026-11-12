"""The universe: which listed securities the system may ever recommend.

Built from Alpaca's asset list. Only common stocks (and ADRs) listed on the
configured exchanges are eligible. Everything else is excluded, with the reason
recorded: funds, leveraged and inverse ETFs (principle 2: by list AND by name),
SPACs, warrants, units, rights, preferred shares, notes and OTC securities.

Price, liquidity and market capitalization are applied afterwards with data
(see ``liquidity_filter`` and the controls of node 5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from core.settings import UniverseConfig

Category = Literal[
    "common", "leveraged_etf", "inverse_etf", "fund", "spac", "warrant", "non_common", "otc"
]

# Benchmarks and the 11 sector ETFs: tracked as market context, never recommended.
BENCHMARKS = ("SPY", "QQQ", "IWM")
SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC")
CONTEXT_TICKERS = BENCHMARKS + SECTOR_ETFS

# Principle 2, "by list": widely traded leveraged and inverse products. The name
# rules below catch the rest; this list is the safety net for the liquid ones.
LEVERAGED_TICKERS = frozenset(
    """
    TQQQ UPRO SPXL SSO QLD UDOW DDM TNA URTY UWM SOXL TECL FAS LABU NUGT JNUG ERX GUSH
    DFEN DPST CURE NAIL RETL DRN MIDU YINN EDC BOIL UCO AGQ UGL USD ROM UYG TMF UBT
    NVDL NVDU NVDX TSLL TSLT TSLR MSTU MSTX CONL AMDL AAPU MSFU GGLL AMZU METU FBL
    BITX BITU ETHU UVXY UVIX FNGU SPUU WEBL HIBL BULZ TARK
    """.split()  # noqa: SIM905 - a readable block of tickers
)
INVERSE_TICKERS = frozenset(
    """
    SQQQ SPXU SPXS SDS SH PSQ QID DOG DXD SDOW RWM TWM TZA SRTY SOXS TECS FAZ LABD DUST
    JDST ERY DRIP DRV YANG EDZ KOLD SCO ZSL GLL TMV TBT TBF SVXY SVIX NVDS NVDQ NVDD
    TSLQ TSLS TSLZ MSTZ SARK BITI SBIT FNGD SPDN HIBS WEBS BERZ SJB EUM EFZ
    """.split()  # noqa: SIM905 - a readable block of tickers
)


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.IGNORECASE)


_INVERSE = _any(
    r"\binverse\b",
    r"\bshort\b",
    r"\bbear\b",
    r"ultrashort",
    r"-\s?[1-5](\.\d)?x\b",
)
_LEVERAGED = _any(
    r"\b[1-5](\.\d+)?x\b",
    r"\bultra(pro)?\b",
    r"ultrapro",
    r"\bleveraged?\b",
    r"\bbull\b",
    r"daily target",
    r"leverage shares",
)
# Issuers whose listed products are funds even when the name does not say "ETF".
_FUND_BRANDS = _any(
    r"\bproshares\b",
    r"\bdirexion\b",
    r"\bishares\b",
    r"\bspdr\b",
    r"\binvesco\b.*\b(trust|etf|fund|portfolio)\b",
    r"\bvanguard\b",
    r"\bwisdomtree\b",
    r"\bvaneck\b",
    r"\bgraniteshares\b",
    r"\byieldmax\b",
    r"\broundhill\b",
    r"\bfirst trust\b",
    r"\bglobal x\b",
    r"\bark (etf|innovation|next|genomic|fintech|autonomous)",
    r"\bt-rex\b",
    r"\btradr\b",
    r"\bdefiance\b",
    r"\bgrayscale\b.*\btrust\b",
)
_FUND = _any(
    r"\betfs?\b",
    r"\betns?\b",
    r"exchange[- ]traded",
    r"\bfunds?\b",
    r"\bportfolios?\b",
    r"\bindex\b",
    r"\btrust,? series\b",
    r"\bunits? of beneficial interest\b",
    r"\bshares of beneficial interest\b.*\b(fund|trust)\b",
    r"\bclosed[- ]end\b",
    r"\bmunicipal\b",
    r"\btreasury\b",
    r"\bbitcoin\b.*\btrust\b",
    r"\bethereum\b.*\btrust\b",
)
_SPAC = _any(r"\bacquisition (corp|corporation|company|co)\b", r"\bblank check\b", r"\bspac\b")
_WARRANT = _any(r"\bwarrants?\b", r"\bunits?\b", r"\brights?\b")
_NON_COMMON = _any(
    r"\bpreferred\b",
    r"\bpreference\b",
    r"\bnotes?\b",
    r"\bdebentures?\b",
    r"\bbonds?\b",
    r"\bdue 20\d\d\b",
    r"\b\d+(\.\d+)?%",
    r"\bcontingent value\b",
    r"\bwhen[- ]issued\b",
    r"\bsubordinated\b",
)
_NON_COMMON_SUFFIX = re.compile(r"[.\-](PR[A-Z]*|WS[A-Z]*|WI|U|RT?)$")


def normalize_ticker(symbol: str) -> str:
    """One spelling per security: upper case, with class shares as ``BRK.B``."""
    return symbol.strip().upper().replace("-", ".").replace("/", ".")


@dataclass(frozen=True)
class Classification:
    category: Category
    # Short reason in Spanish for the UI ("descartados").
    reason: str

    @property
    def eligible(self) -> bool:
        return self.category == "common"


def classify(symbol: str, name: str, exchange: str) -> Classification:
    """Decide what kind of security this is from its ticker, name and exchange."""
    ticker = normalize_ticker(symbol)
    if exchange.upper() == "OTC":
        return Classification("otc", "cotiza fuera de bolsa (OTC)")

    # Leveraged and inverse products first: they are excluded by principle, and a
    # name such as "ProShares UltraPro Short QQQ" must not be filed as a plain fund.
    looks_like_fund = bool(_FUND.search(name) or _FUND_BRANDS.search(name))
    if ticker in INVERSE_TICKERS:
        return Classification("inverse_etf", "ETF inverso (lista)")
    if ticker in LEVERAGED_TICKERS:
        return Classification("leveraged_etf", "ETF apalancado (lista)")
    if looks_like_fund and _INVERSE.search(name):
        return Classification("inverse_etf", "ETF inverso (por nombre)")
    if looks_like_fund and _LEVERAGED.search(name):
        return Classification("leveraged_etf", "ETF apalancado (por nombre)")

    if _SPAC.search(name):
        return Classification("spac", "SPAC (empresa de adquisición sin operación)")
    if _WARRANT.search(name):
        return Classification("warrant", "warrant, unidad o derecho; no es acción común")
    if _NON_COMMON_SUFFIX.search(ticker) or _NON_COMMON.search(name):
        return Classification("non_common", "acción preferente, nota o bono; no es acción común")
    if looks_like_fund:
        return Classification("fund", "fondo o ETF; el universo es solo de acciones comunes")
    return Classification("common", "acción común")


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    name: str
    exchange: str


def eligible_assets(
    assets: Iterable[dict], config: UniverseConfig
) -> tuple[list[UniverseMember], dict[str, Classification]]:
    """Split Alpaca's asset list into eligible common stocks and excluded securities.

    Returns the members and, for every excluded listed asset, why it was excluded.
    """
    exchanges = set(config.exchanges)
    members: dict[str, UniverseMember] = {}
    excluded: dict[str, Classification] = {}
    for asset in assets:
        if asset.get("status") != "active" or not asset.get("tradable"):
            continue
        exchange = str(asset.get("exchange", "")).upper()
        ticker = normalize_ticker(asset["symbol"])
        if ticker in CONTEXT_TICKERS:
            continue
        verdict = classify(ticker, asset.get("name") or "", exchange)
        if verdict.category == "otc" or exchange not in exchanges:
            if verdict.category == "otc":
                excluded[ticker] = verdict
            continue
        if verdict.eligible:
            members[ticker] = UniverseMember(ticker, asset.get("name") or ticker, exchange)
        else:
            excluded[ticker] = verdict
    return sorted(members.values(), key=lambda m: m.ticker), excluded
