# VÉRTICE

Asesor de compra de acciones de EE. UU. (solo largo). Cada día hábil propone al menos 5 ideas con sus razones, registra cuáles compraste y te avisa cuándo vender. **No se conecta a ningún broker ni envía órdenes**: tú compras y vendes a mano y lo confirmas en la interfaz.

Es un experimento educativo y medible, no asesoría financiera. La especificación completa está en [`ROADMAP.md`](ROADMAP.md) y las reglas no negociables en [`CLAUDE.md`](CLAUDE.md).

## Estado

| Fase | Contenido | Estado |
| --- | --- | --- |
| 0 | Cimientos: configuración, base de datos, reloj, calendario NYSE, logging | Terminada |
| 1 | Colectores de datos: Alpaca, Finnhub, FRED, SEC, RSS, calendario económico | Terminada |
| 2 | Motor de datos e indicadores: universo, datos normalizados, 62 métricas, régimen | Terminada |
| 3 | Filtro, 22 controles, Plan del día por reglas y modo replay | Terminada |
| 4 | Analista IA: dossier, Claude Code en modo headless, validación, presupuesto y caché | Terminada |
| 5 | Motor de riesgo, Plan del día guardado, confirmaciones "La compré / No la compré", sombras y máquina de estados | Terminada |
| 6 | Vigilante y alertas | Pendiente |
| 7 | Dashboard | Pendiente |
| 8 | Aprendizaje y métricas | Pendiente |
| 9 | Despliegue y periodo en papel | Pendiente |

## Cómo correrlo en local

Requisitos: [uv](https://docs.astral.sh/uv/), `make` y Python 3.12 (uv lo consigue si falta).

```bash
cp .env.example .env
```

```bash
make install
```

```bash
make test
```

```bash
make dev
```

Con `make dev` la API queda en http://127.0.0.1:8000 y el chequeo de salud en http://127.0.0.1:8000/api/health. El sistema arranca aunque `.env` esté vacío.

Para probar las fuentes de datos con tus keys:

```bash
make check
```

Muestra el estado y la latencia de cada fuente. Las que no tienen key salen como "SIN CONFIGURAR" y no se consultan.

Para ver el plan que el sistema publicaría con los datos ya descargados:

```bash
make preview AI=simulated
```

`AI=none` usa solo reglas, `AI=simulated` pasa los finalistas por la IA simulada y `AI=real` consulta a Claude (gasta una consulta del presupuesto diario y necesita Claude Code instalado y con sesión iniciada).

Las tres corridas del día, a mano (el programador que las lanza a su hora llega con el vigilante):

```bash
make plan
```

```bash
make recheck
```

```bash
make close
```

`make plan` arma y guarda el Plan del día con tu cuenta real (efectivo y posiciones confirmadas). `make recheck` es la revisión de las 9:45: una compra que abrió arriba de su precio de "no perseguir" pasa a VIGILAR. `make close` cierra el día: las ideas que no contestaste se siguen como sombras y se registra tu capital.

Con `make dev` corriendo, el plan y las confirmaciones están en la API:

- `GET /api/plan`: el plan de hoy.
- `POST /api/ideas/{id}/bought` con precio y cantidad: "La compré".
- `POST /api/ideas/{id}/declined`: "No la compré".
- `POST /api/positions/{id}/sold`: "Vendí" (parcial o total).
- `GET /api/account`, `GET /api/positions`, `GET /api/status`.

El sistema nunca envía órdenes: solo registra lo que tú le dices que hiciste.

`make` sin argumentos lista todos los comandos.

## Dónde está cada cosa

- `config/config.yaml`: todos los parámetros. Un valor inválido detiene el arranque y dice qué campo corregir.
- `.env`: las API keys. Nunca se sube a git; `.env.example` lista los nombres.
- `backend/core/`: configuración, reloj (real o simulado), calendario NYSE y logging.
- `backend/collectors/`: un colector por fuente, con límite de llamadas, caché, reintentos y detección de datos viejos. Los límites de cada proveedor están en la sección `data` de `config.yaml`.
- `backend/engine/`: universo, motor de datos (sin datos del futuro), indicadores, régimen, filtro, controles, analista IA, motor de riesgo, Plan del día, confirmaciones, sombras y máquina de estados.
- `prompts/` y `schemas/`: las instrucciones que recibe la IA y el formato JSON que debe devolver.
- `runs/`: dossiers enviados a la IA, la carpeta vacía desde la que corre (`ai_sandbox/`) y los reportes de replay. No se sube a git.
- `backend/replay/`: corre días pasados con reloj simulado y compara el resultado con SPY.
- `backend/db/`: tablas y migraciones. La base vive en `data/vertice.db`.
- `data/raw/`: respuestas crudas de las fuentes, comprimidas, por 30 días.
- `backend/tests/`: tests de cada pieza.
