"""Sector of a company from its SIC code, and the sector ETF used as its benchmark.

The SEC gives every registrant a Standard Industrial Classification code. The 11
SPDR sector ETFs follow a different scheme (GICS), so this mapping is an
approximation; it is good enough to compare a stock with its own sector.
"""

from __future__ import annotations

# (first SIC code, last SIC code, sector ETF). The first matching range wins, so
# specific ranges come before the broad one that contains them.
_SIC_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 999, "XLP"),  # agriculture
    (1200, 1399, "XLE"),  # coal, oil and gas extraction
    (1000, 1499, "XLB"),  # metal and non-metal mining
    (1520, 1531, "XLY"),  # home builders
    (1500, 1799, "XLI"),  # construction
    (2000, 2199, "XLP"),  # food, beverages, tobacco
    (2200, 2399, "XLY"),  # textiles and apparel
    (2400, 2499, "XLI"),  # lumber and wood
    (2500, 2599, "XLY"),  # furniture
    (2600, 2699, "XLB"),  # paper
    (2700, 2799, "XLC"),  # publishing
    (2830, 2836, "XLV"),  # pharmaceuticals and biologics
    (2840, 2844, "XLP"),  # soaps and cosmetics
    (2800, 2899, "XLB"),  # chemicals
    (2900, 2999, "XLE"),  # petroleum refining
    (3000, 3099, "XLB"),  # rubber and plastics
    (3100, 3199, "XLY"),  # leather and footwear
    (3200, 3399, "XLB"),  # glass, stone, primary metals
    (3400, 3499, "XLI"),  # fabricated metal
    (3570, 3579, "XLK"),  # computers
    (3500, 3599, "XLI"),  # machinery
    (3630, 3639, "XLY"),  # household appliances
    (3600, 3699, "XLK"),  # electronics, communications equipment, semiconductors
    (3720, 3769, "XLI"),  # aircraft, ships, rail, defense
    (3700, 3799, "XLY"),  # motor vehicles
    (3840, 3851, "XLV"),  # medical devices
    (3800, 3899, "XLK"),  # instruments
    (3900, 3999, "XLY"),  # toys, jewelry, sporting goods
    (4600, 4699, "XLE"),  # pipelines
    (4000, 4799, "XLI"),  # transportation
    (4800, 4899, "XLC"),  # telephone, broadcasting, cable
    (4950, 4959, "XLI"),  # waste management
    (4900, 4999, "XLU"),  # electric, gas and water utilities
    (5120, 5122, "XLV"),  # drug wholesalers
    (5140, 5159, "XLP"),  # food wholesalers
    (5170, 5172, "XLE"),  # petroleum wholesalers
    (5000, 5199, "XLI"),  # wholesale
    (5400, 5499, "XLP"),  # food stores
    (5912, 5912, "XLP"),  # drug stores
    (5200, 5999, "XLY"),  # retail and restaurants
    (6500, 6599, "XLRE"),  # real estate
    (6798, 6798, "XLRE"),  # real estate investment trusts
    (6000, 6799, "XLF"),  # banks, brokers, insurance, holding companies
    (7000, 7299, "XLY"),  # hotels and personal services
    (7310, 7319, "XLC"),  # advertising
    (7370, 7379, "XLK"),  # software and computer services
    (7300, 7399, "XLI"),  # business services
    (7500, 7699, "XLI"),  # auto rental and repair services
    (7800, 7849, "XLC"),  # motion pictures
    (7900, 7999, "XLY"),  # amusement and recreation
    (8000, 8099, "XLV"),  # health services
    (8200, 8299, "XLY"),  # education
    (8731, 8734, "XLV"),  # commercial biological research
    (8700, 8799, "XLI"),  # engineering and management services
)

SECTOR_NAMES = {
    "XLK": "Tecnología",
    "XLF": "Financiero",
    "XLE": "Energía",
    "XLV": "Salud",
    "XLY": "Consumo discrecional",
    "XLP": "Consumo básico",
    "XLI": "Industrial",
    "XLB": "Materiales",
    "XLU": "Servicios públicos",
    "XLRE": "Bienes raíces",
    "XLC": "Comunicaciones",
}


def sector_etf_for_sic(sic: int | str | None) -> str | None:
    """Sector ETF for a SIC code, or None when the code is missing or unclassified."""
    try:
        code = int(sic)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for first, last, etf in _SIC_RANGES:
        if first <= code <= last:
            return etf
    return None
