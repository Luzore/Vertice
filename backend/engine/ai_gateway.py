"""Node 6 (gateway): every call to the AI goes through here (ROADMAP section 9).

The gateway decides whether a call may happen and records what came back:

* **Budget.** ``ai.daily_call_budget`` calls per market day. Part of it is kept for
  position reviews; at 90% only positions are reviewed; at 100% the day is out of
  quota and the plan is published with rule verdicts. ``ai.max_usd_per_day`` cuts
  by cost when an API key is used.
* **Cache.** The same dossier within ``ai.cache_minutes`` reuses the answer.
* **Retry.** An invalid answer is retried once; if it fails again the gateway
  switches to "rules only" for the rest of the run.
* **Audit.** Every attempt is written to ``ai_calls`` with its raw response, usage
  and cost, and the dossier is saved next to it (principle 8).

The stops never depend on any of this (principle 6): the gateway only ever
returns "no answer", and the callers carry on with rules.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator
from sqlalchemy import Engine, select

from core import clock
from core.logging import get_logger, scrub_text
from core.settings import AIConfig
from db import models
from db.session import session_scope
from engine.ai_client import (
    TASK_LESSONS,
    TASK_NEWS,
    TASK_POSITION_REVIEW,
    AIClient,
    AIRequest,
    AIResponse,
)

log = get_logger(__name__)

POSITION_TASKS = frozenset({TASK_POSITION_REVIEW})
POSITIONS_ONLY_FROM_PCT = 90
# Ledger statuses. The first group consumed quota; the others did not reach the AI.
STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_UNAVAILABLE = "unavailable"
STATUS_CACHE = "cache"
FREE_STATUSES = frozenset({STATUS_CACHE, STATUS_UNAVAILABLE})

BudgetState = Literal["OK", "SOLO_POSICIONES", "SIN_CUOTA"]
ResultStatus = Literal["ok", "cache", "disabled", "no_quota", "failed", "rules_only"]


@dataclass(frozen=True)
class CallRecord:
    """One row of ``ai_calls``."""

    task: str
    model: str
    dossier_hash: str
    status: str
    created_at: datetime
    dossier_path: str | None = None
    raw_response: str | None = None
    structured_output: Any = None
    usage: Any = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    run_id: int | None = None
    id: int | None = None


class Ledger(Protocol):
    """Where the calls are recorded and read back for the budget and the cache."""

    def add(self, record: CallRecord) -> CallRecord: ...

    def since(self, start: datetime) -> list[CallRecord]: ...

    def latest_ok(self, dossier_hash: str, not_before: datetime) -> CallRecord | None: ...


class MemoryLedger:
    """Calls kept in memory: replay and tests, which must not write to the database."""

    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    def add(self, record: CallRecord) -> CallRecord:
        stored = replace(record, id=len(self.records) + 1)
        self.records.append(stored)
        return stored

    def since(self, start: datetime) -> list[CallRecord]:
        return [r for r in self.records if r.created_at >= start]

    def latest_ok(self, dossier_hash: str, not_before: datetime) -> CallRecord | None:
        matches = [
            r
            for r in self.records
            if r.dossier_hash == dossier_hash
            and r.status == STATUS_OK
            and r.created_at >= not_before
        ]
        return matches[-1] if matches else None


def _from_row(row: models.AICall) -> CallRecord:
    return CallRecord(
        id=row.id,
        run_id=row.run_id,
        task=row.task,
        model=row.model,
        dossier_hash=row.dossier_hash,
        dossier_path=row.dossier_path,
        raw_response=row.raw_response,
        structured_output=row.structured_output,
        usage=row.usage,
        cost_usd=row.cost_usd,
        duration_ms=row.duration_ms,
        status=row.status,
        session_id=row.session_id,
        created_at=row.created_at,
    )


class DatabaseLedger:
    """Calls kept in the ``ai_calls`` table: the source of truth in live operation."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def add(self, record: CallRecord) -> CallRecord:
        with session_scope(self._engine) as session:
            row = models.AICall(
                run_id=record.run_id,
                task=record.task,
                model=record.model,
                dossier_hash=record.dossier_hash,
                dossier_path=record.dossier_path,
                raw_response=record.raw_response,
                structured_output=record.structured_output,
                usage=record.usage,
                cost_usd=record.cost_usd,
                duration_ms=record.duration_ms,
                status=record.status,
                session_id=record.session_id,
                created_at=record.created_at,
            )
            session.add(row)
            session.flush()
            return replace(record, id=row.id)

    def since(self, start: datetime) -> list[CallRecord]:
        with session_scope(self._engine) as session:
            rows = session.scalars(
                select(models.AICall)
                .where(models.AICall.created_at >= start)
                .order_by(models.AICall.id)
            )
            return [_from_row(row) for row in rows]

    def latest_ok(self, dossier_hash: str, not_before: datetime) -> CallRecord | None:
        with session_scope(self._engine) as session:
            row = session.scalars(
                select(models.AICall)
                .where(
                    models.AICall.dossier_hash == dossier_hash,
                    models.AICall.status == STATUS_OK,
                    models.AICall.created_at >= not_before,
                )
                .order_by(models.AICall.id.desc())
                .limit(1)
            ).first()
            return _from_row(row) if row is not None else None


# --- budget ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetStatus:
    """How much of today's AI budget is used (the UI header shows used / limit)."""

    day: date
    limit: int
    used: int
    used_positions: int
    # Calls that anything other than a position review may use.
    general_limit: int
    # From this many calls on, only positions are reviewed.
    positions_only_from: int
    cost_usd: float
    max_usd: float | None

    @property
    def used_general(self) -> int:
        return self.used - self.used_positions

    @property
    def out_of_money(self) -> bool:
        return self.max_usd is not None and self.cost_usd >= self.max_usd

    @property
    def state(self) -> BudgetState:
        if self.used >= self.limit or self.out_of_money:
            return "SIN_CUOTA"
        if self.used >= self.positions_only_from or self.used_general >= self.general_limit:
            return "SOLO_POSICIONES"
        return "OK"

    def allows(self, task: str) -> tuple[bool, str]:
        """Whether a call for ``task`` may be made now, and why not."""
        if self.out_of_money:
            return False, (
                f"SIN CUOTA IA: el gasto de hoy (${self.cost_usd:,.2f}) llegó al máximo "
                f"diario de ${self.max_usd:,.2f}"
            )
        if self.used >= self.limit:
            return False, f"SIN CUOTA IA: {self.used} de {self.limit} consultas usadas hoy"
        if task in POSITION_TASKS:
            return True, ""
        if self.used >= self.positions_only_from:
            return False, (
                f"{self.used} de {self.limit} consultas usadas hoy: desde el "
                f"{POSITIONS_ONLY_FROM_PCT}% la IA solo revisa posiciones abiertas"
            )
        if self.used_general >= self.general_limit:
            reserved = self.limit - self.general_limit
            return False, (
                f"{self.used_general} consultas usadas hoy fuera de posiciones: las "
                f"{reserved} restantes están reservadas para revisar posiciones abiertas"
            )
        return True, ""


def budget_status(config: AIConfig, records: Iterable[CallRecord], day: date) -> BudgetStatus:
    counted = [r for r in records if r.status not in FREE_STATUSES]
    reserved = math.ceil(config.daily_call_budget * config.reserve_for_positions_pct / 100)
    return BudgetStatus(
        day=day,
        limit=config.daily_call_budget,
        used=len(counted),
        used_positions=sum(1 for r in counted if r.task in POSITION_TASKS),
        general_limit=config.daily_call_budget - reserved,
        positions_only_from=math.ceil(config.daily_call_budget * POSITIONS_ONLY_FROM_PCT / 100),
        cost_usd=sum(r.cost_usd or 0.0 for r in counted),
        max_usd=config.max_usd_per_day,
    )


# --- gateway -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AIResult:
    status: ResultStatus
    output: Any = None
    # Why there is no answer, in Spanish; empty when there is one.
    note: str = ""
    call_id: int | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "cache")


Check = Callable[[Any], str | None]


class AIGateway:
    """One per run: budget, cache, retry and audit around an AI client."""

    def __init__(
        self,
        client: AIClient,
        config: AIConfig,
        ledger: Ledger,
        *,
        prompts_dir: Path,
        schemas_dir: Path,
        dossier_dir: Path | None = None,
        run_id: int | None = None,
        secret_values: Iterable[str] = (),
    ) -> None:
        self._client = client
        self._config = config
        self._ledger = ledger
        self._prompts_dir = prompts_dir
        self._schemas_dir = schemas_dir
        self._dossier_dir = dossier_dir
        self._run_id = run_id
        self._secret_values = tuple(secret_values)
        self._schemas: dict[str, Mapping[str, Any]] = {}
        # Set after two failed attempts: the rest of the run does not call the AI.
        self.rules_only_reason: str | None = None
        # Attempts that reached the AI client through this gateway.
        self.calls_made = 0

    # The file names of each task: prompts/<prompt>.md and schemas/<schema>.schema.json.
    FILES = {
        "analyst": ("analyst", "analyst_decision"),
        "position_review": ("position_review", "position_review"),
        "news_classifier": ("news_classifier", "news_classification"),
        "lesson_writer": ("lesson_writer", "lesson_writer"),
    }

    def model_for(self, task: str) -> str:
        if task == TASK_NEWS:
            return self._config.news_model
        if task == TASK_LESSONS:
            return self._config.lessons_model
        return self._config.analyst_model

    def schema_for(self, task: str) -> Mapping[str, Any]:
        if task not in self._schemas:
            path = self._schemas_dir / f"{self.FILES[task][1]}.schema.json"
            self._schemas[task] = json.loads(path.read_text(encoding="utf-8"))
        return self._schemas[task]

    def prompt_for(self, task: str) -> Path:
        return self._prompts_dir / f"{self.FILES[task][0]}.md"

    def _day_start(self) -> tuple[date, datetime]:
        day = clock.market_date(clock.now())
        return day, clock.market_time(day, time(0, 0))

    def budget(self) -> BudgetStatus:
        day, start = self._day_start()
        return budget_status(self._config, self._ledger.since(start), day)

    def _hash(self, task: str, model: str, fingerprint: str) -> str:
        digest = hashlib.sha256()
        for part in (
            task,
            model,
            self.prompt_for(task).read_text(encoding="utf-8"),
            json.dumps(self.schema_for(task), sort_keys=True),
            fingerprint,
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def _save_dossier(self, task: str, dossier_hash: str, dossier: str) -> str | None:
        if self._dossier_dir is None:
            return None
        now = clock.to_market(clock.now())
        folder = self._dossier_dir / f"{now:%Y-%m-%d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{now:%H%M%S}-{task}-{dossier_hash[:12]}.md"
        path.write_text(dossier, encoding="utf-8")
        return str(path)

    def _record(
        self,
        task: str,
        model: str,
        dossier_hash: str,
        status: str,
        *,
        dossier_path: str | None,
        response: AIResponse | None = None,
        structured_output: Any = None,
    ) -> CallRecord:
        raw = response.raw if response is not None else None
        return self._ledger.add(
            CallRecord(
                run_id=self._run_id,
                task=task,
                model=model,
                dossier_hash=dossier_hash,
                dossier_path=dossier_path,
                raw_response=scrub_text(raw, self._secret_values) if raw else raw,
                structured_output=structured_output,
                usage=response.usage if response is not None else None,
                cost_usd=response.cost_usd if response is not None else 0.0,
                duration_ms=response.duration_ms if response is not None else None,
                status=status,
                session_id=response.session_id if response is not None else None,
                created_at=clock.now(),
            )
        )

    def call(
        self,
        task: str,
        dossier: str,
        *,
        fingerprint: str | None = None,
        web_search: bool = False,
        check: Check | None = None,
    ) -> AIResult:
        """Ask the AI, or explain why it was not asked.

        ``fingerprint`` identifies the dossier for the cache (by default its text).
        ``check`` looks at an answer that already follows the schema and returns
        what is wrong with it, or None.
        """
        if not self._config.enabled:
            return AIResult("disabled", note="la IA está apagada en la configuración (ai.enabled)")
        if self.rules_only_reason is not None:
            return AIResult("rules_only", note=self.rules_only_reason)

        model = self.model_for(task)
        schema = self.schema_for(task)
        dossier_hash = self._hash(task, model, fingerprint if fingerprint is not None else dossier)
        validator = Draft202012Validator(schema)

        if self._config.cache_minutes > 0:
            not_before = clock.now() - timedelta(minutes=self._config.cache_minutes)
            cached = self._ledger.latest_ok(dossier_hash, not_before)
            if cached is not None:
                record = self._record(
                    task,
                    model,
                    dossier_hash,
                    STATUS_CACHE,
                    dossier_path=cached.dossier_path,
                    structured_output=cached.structured_output,
                )
                log.info("ai_cache_hit", task=task, reused_call=cached.id)
                return AIResult("cache", output=cached.structured_output, call_id=record.id)

        # Web search is only ever allowed for position reviews, and only when enabled.
        web_search = web_search and self._config.allow_web_search and task in POSITION_TASKS
        request = AIRequest(
            task=task,
            model=model,
            system_prompt=self.prompt_for(task),
            schema=schema,
            dossier=dossier,
            timeout_s=self._config.timeout_s,
            web_search=web_search,
        )
        dossier_path: str | None = None
        problem = ""
        attempts = 0
        for attempt in (1, 2):
            allowed, why = self.budget().allows(task)
            if not allowed:
                if attempt == 1:
                    log.warning("ai_budget_blocked", task=task, reason=why)
                    return AIResult("no_quota", note=why)
                problem = f"{problem}; no quedó presupuesto para reintentar"
                break
            if dossier_path is None:
                dossier_path = self._save_dossier(task, dossier_hash, dossier)
            response = self._client.run(request)
            self.calls_made += 1
            attempts = attempt
            status, problem = self._judge(response, validator, check)
            record = self._record(
                task,
                model,
                dossier_hash,
                status,
                dossier_path=dossier_path,
                response=response,
                structured_output=response.structured_output,
            )
            log.info(
                "ai_call",
                task=task,
                model=model,
                status=status,
                attempt=attempt,
                cost_usd=response.cost_usd,
                duration_ms=response.duration_ms,
                problem=problem or None,
            )
            if status == STATUS_OK:
                return AIResult(
                    "ok", output=response.structured_output, call_id=record.id, attempts=attempt
                )
            if response.quota_exhausted:
                self.rules_only_reason = (
                    "SIN CUOTA IA: la cuenta de Claude no tiene cuota disponible; "
                    "esta corrida sigue solo con reglas"
                )
                return AIResult("no_quota", note=self.rules_only_reason, attempts=attempt)
            if status == STATUS_UNAVAILABLE:
                break  # the command is not there: a retry cannot help
        tries = "dos veces" if attempts == 2 else "una vez"
        self.rules_only_reason = (
            f"la IA falló {tries} ({problem}); esta corrida sigue solo con reglas"
        )
        log.warning("ai_rules_only", task=task, reason=self.rules_only_reason)
        return AIResult("failed", note=self.rules_only_reason, attempts=attempts)

    @staticmethod
    def _judge(
        response: AIResponse, validator: Draft202012Validator, check: Check | None
    ) -> tuple[str, str]:
        """The ledger status of a response and, when it is not usable, why."""
        if response.status != "ok":
            return response.status, response.error or response.status
        errors = sorted(validator.iter_errors(response.structured_output), key=str)
        if errors:
            first = errors[0]
            where = "/".join(str(p) for p in first.absolute_path) or "raíz"
            return STATUS_INVALID, f"no cumple el esquema en {where}: {first.message[:160]}"
        complaint = check(response.structured_output) if check is not None else None
        if complaint:
            return STATUS_INVALID, complaint
        return STATUS_OK, ""
