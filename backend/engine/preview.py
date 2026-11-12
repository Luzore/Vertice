"""``python -m engine.preview``: the plan of the day from the data already downloaded.

A tool to look at what the system would publish right now, with rule verdicts,
with the simulated AI or with the real one. It does not save the plan (that is
the daily pipeline's job); a real AI call is recorded in ``ai_calls`` and counts
against the daily budget like any other.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from datetime import date

from core import clock, settings
from core.logging import configure_logging
from db.session import make_engine, session_scope
from engine.ai_analyst import Analyst, live_analyst, simulated_analyst
from engine.data_engine import build_market_state
from engine.indicators import compute_indicators
from engine.portfolio import Portfolio
from engine.recommender import build_plan, render_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.preview",
        description="Arma el Plan del día con los datos ya descargados y lo muestra.",
    )
    parser.add_argument(
        "--ai",
        choices=("none", "simulated", "real"),
        default="none",
        help="'real' consulta a Claude y gasta una consulta del presupuesto diario",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        metavar="AAAA-MM-DD",
        help="arma el plan como se habría publicado ese día (sin IA real)",
    )
    args = parser.parse_args(argv)
    if args.date is not None and args.ai == "real":
        parser.error("la IA real no se usa sobre fechas pasadas: ya conoce lo que pasó después")

    config = settings.get_config()
    configure_logging(
        "WARNING", secret_values=settings.get_secrets().secret_values(), stream=sys.stderr
    )
    engine = make_engine()
    as_of = (
        clock.market_time(args.date, config.plan.publish_time_et)
        if args.date is not None
        else clock.now()
    )
    # A past date runs on a simulated clock; today runs on the real one.
    with clock.simulated(as_of) if args.date is not None else contextlib.nullcontext():
        with session_scope(engine) as session:
            state = build_market_state(session, config, as_of=as_of)
        indicators = compute_indicators(state, config.regime)
        portfolio = Portfolio(cash=config.capital.cash_usd)
        analyst: Analyst | None = None
        if args.ai == "simulated":
            analyst = simulated_analyst(config)
        elif args.ai == "real":
            analyst = live_analyst(config, engine)
        plan = build_plan(state, indicators, portfolio, config, analyst=analyst)
        print(render_plan(plan, config, portfolio.capital))
        if analyst is not None:
            budget = analyst.gateway.budget()
            print(f"\nConsultas IA usadas hoy: {budget.used} / {budget.limit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
