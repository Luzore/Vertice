"""Node 6 (transport): how the system talks to the AI (ROADMAP section 9).

Two clients share one interface:

* ``ClaudeCLI`` runs Claude Code headless (``claude -p``) as a subprocess from an
  empty folder, hands it the dossier on stdin and asks for JSON that follows a
  schema. It has no tools: it can only read the dossier and answer.
* ``SimulatedAI`` answers from the dossier with fixed rules. Tests and replay use
  it, so they cost nothing and always give the same answer.

A client never raises: a failure comes back as a response whose status says why,
so the caller can retry or fall back to rules.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from engine import sentiment
from engine.dossier import extract_json_blocks

Status = Literal["ok", "error", "timeout", "unavailable"]

TASK_ANALYST = "analyst"
TASK_POSITION_REVIEW = "position_review"
TASK_NEWS = "news_classifier"
TASK_LESSONS = "lesson_writer"
TASK_EXPLAIN = "explain"
TASKS = (TASK_ANALYST, TASK_POSITION_REVIEW, TASK_NEWS, TASK_LESSONS, TASK_EXPLAIN)

MAX_TURNS = 3
_ERROR_TEXT_LIMIT = 300
# Variables of this project that the AI process has no reason to see.
_PRIVATE_ENV = (
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "FINNHUB_API_KEY",
    "FRED_API_KEY",
    "SEC_USER_AGENT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MASSIVE_API_KEY",
    "BANXICO_TOKEN",
    "VPS_HOST",
    "VPS_USER",
    "VPS_SSH_KEY_PATH",
    "DATABASE_URL",
)
_QUOTA_PATTERN = re.compile(
    r"usage limit|rate limit|hit your limit|limit reached|out of (extra )?usage|quota", re.I
)


@dataclass(frozen=True)
class AIRequest:
    task: str
    model: str
    # File with the instructions of the task (prompts/<task>.md).
    system_prompt: Path
    # JSON Schema the answer must follow.
    schema: Mapping[str, Any]
    # What the AI reads: the dossier, as Markdown.
    dossier: str
    timeout_s: int
    # Only position reviews triggered by news may search the web, and only when
    # ``ai.allow_web_search`` is on.
    web_search: bool = False


@dataclass(frozen=True)
class AIResponse:
    status: Status
    # Exactly what the client returned, kept for the audit trail.
    raw: str = ""
    structured_output: Any = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    # Why it failed, in Spanish; empty when it worked.
    error: str = ""
    # The provider said the account has no quota left: retrying cannot help.
    quota_exhausted: bool = False


class AIClient(Protocol):
    def run(self, request: AIRequest) -> AIResponse: ...


def parse_cli_output(stdout: str) -> AIResponse:
    """Turn the JSON that ``claude -p --output-format json`` prints into a response."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return AIResponse("error", raw=stdout or "", error="la respuesta no es JSON")
    if isinstance(payload, list):
        # Some versions print every event; the last "result" one is the answer.
        results = [e for e in payload if isinstance(e, dict) and e.get("type") == "result"]
        payload = results[-1] if results else None
    if not isinstance(payload, dict):
        return AIResponse("error", raw=stdout, error="la respuesta no trae un resultado")

    usage = {
        "usage": payload.get("usage"),
        "model_usage": payload.get("modelUsage"),
        "num_turns": payload.get("num_turns"),
    }
    cost = payload.get("total_cost_usd")
    common = {
        "raw": stdout,
        "usage": usage,
        "cost_usd": float(cost) if isinstance(cost, int | float) else None,
        "duration_ms": payload.get("duration_ms"),
        "session_id": payload.get("session_id"),
    }
    if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
        detail = str(payload.get("result") or payload.get("subtype") or "error sin detalle")
        return AIResponse(
            "error",
            error=f"Claude Code devolvió un error: {detail[:_ERROR_TEXT_LIMIT]}",
            quota_exhausted=bool(_QUOTA_PATTERN.search(detail)),
            **common,
        )
    output = payload.get("structured_output")
    if output is None:
        return AIResponse("error", error="la respuesta no trae structured_output", **common)
    return AIResponse("ok", structured_output=output, **common)


class ClaudeCLI:
    """Claude Code in headless mode, run from an empty folder."""

    def __init__(
        self,
        sandbox: Path,
        *,
        executable: str = "claude",
        api_key: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self._sandbox = sandbox
        self._executable = executable
        # With an API key the CLI runs in --bare mode, which ignores the subscription login.
        self._api_key = api_key
        self._runner = runner

    def command(self, request: AIRequest) -> list[str]:
        # Claude Code rejects a schema that names its dialect ("$schema"): it is dropped
        # here and the answer is validated again with the full schema by the gateway.
        schema = {key: value for key, value in request.schema.items() if key != "$schema"}
        command = [
            self._executable,
            "-p",
            "--model",
            request.model,
            "--append-system-prompt-file",
            str(request.system_prompt.resolve()),
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "--permission-mode",
            "dontAsk",
            "--max-turns",
            str(MAX_TURNS),
            # Nothing from the user's own Claude Code setup leaks into the analysis: no
            # settings or CLAUDE.md files (the sandbox sits inside the repository, whose
            # CLAUDE.md would otherwise be loaded), no MCP servers, no saved session.
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if request.web_search:
            command += ["--tools", "WebSearch", "--allowedTools", "WebSearch"]
        else:
            command += ["--tools", ""]
        if self._api_key:
            command.append("--bare")
        return command

    def environment(self) -> dict[str, str]:
        """The parent environment without this project's own secrets."""
        env = {name: value for name, value in os.environ.items() if name not in _PRIVATE_ENV}
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        else:
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def run(self, request: AIRequest) -> AIResponse:
        self._sandbox.mkdir(parents=True, exist_ok=True)
        if any(self._sandbox.iterdir()):
            return AIResponse(
                "unavailable",
                error=f"la carpeta de la IA ({self._sandbox}) debe estar vacía y no lo está",
            )
        try:
            completed = self._runner(
                self.command(request),
                input=request.dossier,
                cwd=self._sandbox,
                env=self.environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=request.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AIResponse("timeout", error=f"sin respuesta en {request.timeout_s} s")
        except FileNotFoundError:
            return AIResponse(
                "unavailable", error=f"no se encontró el comando '{self._executable}'"
            )
        except OSError as error:
            return AIResponse("unavailable", error=f"no se pudo ejecutar Claude Code: {error}")

        stdout, stderr = completed.stdout or "", completed.stderr or ""
        if stdout.strip():
            response = parse_cli_output(stdout)
            if response.status == "ok" and completed.returncode != 0:
                return AIResponse(
                    "error",
                    raw=stdout,
                    error=f"Claude Code terminó con código {completed.returncode}",
                )
            return response
        detail = stderr.strip()[-_ERROR_TEXT_LIMIT:] or "sin salida"
        return AIResponse(
            "error",
            raw=stderr,
            error=f"Claude Code terminó con código {completed.returncode}: {detail}",
            quota_exhausted=bool(_QUOTA_PATTERN.search(stderr)),
        )


# --- simulated AI ------------------------------------------------------------------------------

Fault = Literal["timeout", "error", "invalid", "no_quota"]
Mutation = Callable[[Any, AIRequest], Any]

SIMULATED_BASE_PROBABILITY_STEP = 0.005
SIMULATED_MAX_PROBABILITY = 0.75
SIMULATED_WATCH_MARGIN = 10.0


def simulated_analyst(dossier: str) -> dict[str, Any]:
    """The analyst's answer by fixed rules: buy what reaches the minimum score, with
    the levels of the rule-based plan. It mirrors the rules on purpose, so a replay
    with the simulated AI tests the plumbing and nothing else."""
    blocks = extract_json_blocks(dossier)
    context = next((b for b in blocks if isinstance(b, dict) and "exigencia" in b), {})
    demand = context.get("exigencia", {})
    min_score = float(demand.get("puntaje_minimo", 70.0))
    min_probability = float(demand.get("probabilidad_minima", 0.5))
    decisions = []
    for block in blocks:
        if not isinstance(block, dict) or "ticker" not in block or "plan_de_reglas" not in block:
            continue
        score = float(block.get("puntaje", 0.0))
        plan = block["plan_de_reglas"]
        if score >= min_score:
            verdict = "COMPRAR"
        elif score >= min_score - SIMULATED_WATCH_MARGIN:
            verdict = "VIGILAR"
        else:
            verdict = "DESCARTAR"
        probability = min_probability + (score - min_score) * SIMULATED_BASE_PROBABILITY_STEP
        probability = round(min(SIMULATED_MAX_PROBABILITY, max(0.05, probability)), 3)
        decisions.append(
            {
                "ticker": block["ticker"],
                "veredicto": verdict,
                "setup": block.get("setup") or "RUPTURA",
                "prob_objetivo_antes_que_stop": probability,
                "confianza": 0.5,
                "entrada": {
                    "tipo": "limite",
                    "zona_min": plan["zona_min"],
                    "zona_max": plan["zona_max"],
                    "no_perseguir_sobre": plan["no_perseguir_sobre"],
                },
                "disparador_vigilar": (
                    f"precio dentro de la zona {plan['zona_min']:.2f}–{plan['zona_max']:.2f}"
                    if verdict == "VIGILAR"
                    else None
                ),
                "stop": plan["stop"],
                "objetivos": list(plan["objetivos"]),
                "horizonte_dias": 5,
                "tesis": f"IA simulada: {block.get('setup')} con puntaje {score:.0f}.",
                "razones": ["respuesta de la IA simulada, calculada con reglas fijas"],
                "riesgos": ["la IA simulada no analiza riesgos"],
                "invalidacion": f"cierre debajo de {plan['stop']:.2f}",
                "lecciones_aplicadas": [],
                "datos_faltantes": [],
            }
        )
    regime = context.get("regimen", "desconocido")
    return {"lectura_mercado": f"IA simulada. Régimen {regime}.", "decisiones": decisions}


def simulated_news(dossier: str) -> dict[str, Any]:
    """News classified with the same word lists the system uses without the AI."""
    items = next((b for b in extract_json_blocks(dossier) if isinstance(b, list)), [])
    classified = []
    for item in items:
        headline, summary = str(item.get("titular", "")), item.get("resumen")
        severe = sentiment.severe_category(headline, summary)
        classified.append(
            {
                "id": int(item["id"]),
                "sentimiento": sentiment.score_headline(headline, summary),
                "categoria": "otra",
                "grave": severe is not None,
            }
        )
    return {"noticias": classified}


def simulated_position_review(dossier: str) -> dict[str, Any]:
    block = next(
        (b for b in extract_json_blocks(dossier) if isinstance(b, dict) and "ticker" in b), {}
    )
    return {
        "ticker": block.get("ticker", "?"),
        "accion": "MANTENER",
        "motivo": "IA simulada: el stop y los objetivos vigentes siguen mandando.",
        "noticia_grave_confirmada": False,
        "nuevo_stop": None,
        "confianza": 0.5,
    }


_SIMULATED_TASKS: dict[str, Callable[[str], dict[str, Any]]] = {
    TASK_ANALYST: simulated_analyst,
    TASK_NEWS: simulated_news,
    TASK_POSITION_REVIEW: simulated_position_review,
    TASK_LESSONS: lambda dossier: {"hipotesis": []},
}


@dataclass
class SimulatedAI:
    """A deterministic AI for tests and replay.

    ``script`` forces what the next calls return (a fault, ``"ok"`` or a ready
    response); once it is used up every call answers normally. ``mutate`` edits a
    normal answer before it is returned, to simulate an AI that makes things up.
    """

    script: Sequence[Fault | Literal["ok"] | AIResponse] = ()
    mutate: Mutation | None = None
    cost_usd: float = 0.0
    calls: list[AIRequest] = field(default_factory=list)

    def run(self, request: AIRequest) -> AIResponse:
        step = self.script[len(self.calls)] if len(self.calls) < len(self.script) else "ok"
        self.calls.append(request)
        if isinstance(step, AIResponse):
            return step
        if step == "timeout":
            return AIResponse("timeout", error=f"sin respuesta en {request.timeout_s} s")
        if step == "error":
            return AIResponse("error", raw="not json", error="la respuesta no es JSON")
        if step == "no_quota":
            return AIResponse(
                "error", error="Claude Code devolvió un error: usage limit", quota_exhausted=True
            )
        if step == "invalid":
            output: Any = {"decisiones": "esto no cumple el esquema"}
        else:
            handler = _SIMULATED_TASKS.get(request.task)
            if handler is None:
                return AIResponse("error", error=f"la IA simulada no conoce '{request.task}'")
            output = handler(request.dossier)
            if self.mutate is not None:
                output = self.mutate(output, request)
        return AIResponse(
            "ok",
            raw=json.dumps(output, ensure_ascii=False),
            structured_output=output,
            usage={
                "usage": {"input_tokens": len(request.dossier) // 4, "output_tokens": 0},
                "model_usage": {"simulated": {}},
                "num_turns": 1,
            },
            cost_usd=self.cost_usd,
            duration_ms=0,
            session_id=f"simulated-{len(self.calls)}",
        )
