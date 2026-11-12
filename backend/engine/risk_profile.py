"""The aggressiveness dial (ROADMAP section 10): 1 is prudent, 10 is aggressive.

One number sets how much is risked per idea, how many positions may be open,
how large one position may be, and how demanding the system is before it says
COMPRAR. Values between the rows of the table are interpolated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# aggressiveness: (risk % of capital per idea, max positions, max % of capital per
# position, minimum score, minimum AI probability, initial stop in ATRs)
_TABLE: dict[int, tuple[float, float, float, float, float, float]] = {
    1: (0.25, 3, 15, 80, 0.60, 1.50),
    3: (0.50, 4, 20, 75, 0.55, 1.75),
    5: (0.75, 5, 25, 70, 0.50, 2.00),
    7: (1.00, 6, 30, 65, 0.47, 2.00),
    10: (1.50, 8, 35, 55, 0.42, 2.50),
}


@dataclass(frozen=True)
class RiskProfile:
    aggressiveness: int
    risk_pct: float
    max_positions: int
    max_position_pct: float
    min_score: float
    min_probability: float
    stop_atr: float


def profile_for(aggressiveness: int) -> RiskProfile:
    if not 1 <= aggressiveness <= 10:
        raise ValueError("aggressiveness must be between 1 and 10")
    anchors = sorted(_TABLE)
    lower = max(a for a in anchors if a <= aggressiveness)
    upper = min(a for a in anchors if a >= aggressiveness)
    weight = 0.0 if upper == lower else (aggressiveness - lower) / (upper - lower)
    values = [
        low + weight * (high - low) for low, high in zip(_TABLE[lower], _TABLE[upper], strict=True)
    ]
    return RiskProfile(
        aggressiveness=aggressiveness,
        risk_pct=round(values[0], 4),
        # Rounded down: between two rows the more prudent count applies.
        max_positions=math.floor(values[1] + 1e-9),
        max_position_pct=round(values[2], 4),
        min_score=round(values[3], 4),
        min_probability=round(values[4], 4),
        stop_atr=round(values[5], 4),
    )
