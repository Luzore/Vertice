"""Settings the user changes from the dashboard (ROADMAP section 14).

``config/config.yaml`` holds every parameter. The header and the Configuración
page of the dashboard change a few of them while the system runs: the mode, the
aggressiveness, the cash, the costs... Those changes are stored in the database as
overrides on top of the file, so the API and the worker see the same values and a
restart keeps them.

Every override goes through the same validation as the file, and every change is
audited with its previous value.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from core import clock, settings
from core.settings import AppConfig, ConfigError
from db import models
from db.session import session_scope

OVERRIDES_KEY = "settings/overrides"
AUDIT_ENTITY = "setting"

# What the dashboard may change, and how each one reads in Spanish.
EDITABLE: dict[str, str] = {
    "system.mode": "Modo",
    "system.aggressiveness": "Agresividad",
    "capital.cash_usd": "Dinero que metiste a la cuenta (USD)",
    "capital.fractional_shares": "Acciones fraccionarias",
    "capital.show_mxn": "Mostrar el capital en MXN",
    "costs.commission_pct": "Comisión de tu casa de bolsa (%)",
    "costs.vat_on_commission_pct": "IVA sobre la comisión (%)",
    "costs.slippage_pct": "Deslizamiento estimado (%)",
    "universe.sic_only": "Solo acciones listadas en el SIC",
    "exits.hold_through_earnings": "Mantener posiciones durante resultados",
    "exits.time_stop_days": "Stop de tiempo (días hábiles)",
    "notify.telegram": "Avisos por Telegram",
    "ai.enabled": "Analista IA encendido",
    "ai.daily_call_budget": "Consultas de IA por día",
}


class SettingError(ValueError):
    """A change the dashboard asked for that cannot be applied."""


def _overrides(session: Session) -> dict[str, Any]:
    row = session.get(models.SystemStatus, OVERRIDES_KEY)
    return json.loads(row.message) if row is not None and row.message else {}


def apply(base: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """``base`` with the overrides on top, validated like the file itself."""
    if not overrides:
        return base
    by_section: dict[str, dict[str, Any]] = {}
    for path, value in overrides.items():
        section, _, field = path.partition(".")
        by_section.setdefault(section, {})[field] = value
    updated = {}
    for section, fields in by_section.items():
        current_section = getattr(base, section)
        try:
            # Each section is validated again as a whole, with its own rules.
            updated[section] = type(current_section).model_validate(
                {**current_section.model_dump(), **fields}
            )
        except ValidationError as error:
            problems = "; ".join(
                f"{section}.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in error.errors()
            )
            raise ConfigError(f"configuración inválida: {problems}") from error
    return base.model_copy(update=updated)


def current(engine: Engine | None = None, session: Session | None = None) -> AppConfig:
    """The configuration in force: the file plus what the dashboard changed."""
    base = settings.get_config()
    if session is not None:
        return apply(base, _overrides(session))
    with session_scope(engine) as own:
        return apply(base, _overrides(own))


def read_value(config: AppConfig, path: str) -> Any:
    section, _, field = path.partition(".")
    return getattr(getattr(config, section), field)


def update(session: Session, changes: dict[str, Any]) -> AppConfig:
    """Apply changes from the dashboard. All of them or none: an invalid value
    leaves everything as it was."""
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise SettingError(f"no se puede cambiar desde el dashboard: {', '.join(unknown)}")
    base = settings.get_config()
    before = _overrides(session)
    previous = apply(base, before)
    merged = {**before, **changes}
    try:
        updated = apply(base, merged)
    except ConfigError as error:
        raise SettingError(str(error)) from error
    row = session.get(models.SystemStatus, OVERRIDES_KEY)
    if row is None:
        row = models.SystemStatus(component=OVERRIDES_KEY, status="ok", updated_at=clock.now())
        session.add(row)
    row.message, row.updated_at = (
        json.dumps(merged, ensure_ascii=False, sort_keys=True),
        clock.now(),
    )
    for path in changes:
        old, new = read_value(previous, path), read_value(updated, path)
        if old != new:
            session.add(
                models.StateTransition(
                    entity=AUDIT_ENTITY,
                    entity_id=path,
                    from_state=str(old),
                    to_state=str(new),
                    at=clock.now(),
                    actor="user",
                    reason=f"{EDITABLE[path]}: de {old} a {new}",
                )
            )
    return updated
