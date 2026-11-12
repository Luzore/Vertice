"""``python -m replay --from 2026-05-01 --to 2026-10-30``"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from core import settings
from core.logging import configure_logging
from db.session import make_engine
from replay import report
from replay.engine import run_replay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m replay",
        description="Corre el sistema sobre días históricos con un reloj simulado.",
    )
    parser.add_argument(
        "--from", dest="start", type=date.fromisoformat, required=True, metavar="AAAA-MM-DD"
    )
    parser.add_argument(
        "--to", dest="end", type=date.fromisoformat, required=True, metavar="AAAA-MM-DD"
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="capital inicial (por defecto, el de config.yaml)",
    )
    parser.add_argument(
        "--workers", type=int, default=min(4, os.cpu_count() or 1), help="procesos en paralelo"
    )
    parser.add_argument(
        "--out", default=None, help="carpeta del reporte (por defecto runs/replay/<fecha>)"
    )
    parser.add_argument(
        "--ai",
        choices=("none", "simulated"),
        default="none",
        help="'simulated' pasa los finalistas por la IA simulada (la IA real no se usa en replay)",
    )
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--to debe ser igual o posterior a --from")

    config = settings.get_config()
    configure_logging(
        "WARNING", secret_values=settings.get_secrets().secret_values(), stream=sys.stderr
    )
    result = run_replay(
        make_engine(),
        config,
        args.start,
        args.end,
        capital=args.capital,
        workers=args.workers,
        ai=args.ai,
    )
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    directory = settings.repo_root() / "runs" / "replay" / stamp if args.out is None else args.out
    path = report.write(result, settings.Path(directory))
    print(report.render(result))
    print(f"Reporte y CSV en: {path.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
