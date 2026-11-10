# VÉRTICE

Asesor de compra de acciones de EE. UU. (solo largo). Cada día hábil propone al menos 5 ideas con sus razones, registra cuáles compraste y te avisa cuándo vender. **No se conecta a ningún broker ni envía órdenes**: tú compras y vendes a mano y lo confirmas en la interfaz.

Es un experimento educativo y medible, no asesoría financiera. La especificación completa está en [`ROADMAP.md`](ROADMAP.md) y las reglas no negociables en [`CLAUDE.md`](CLAUDE.md).

## Estado

| Fase | Contenido | Estado |
| --- | --- | --- |
| 0 | Cimientos: configuración, base de datos, reloj, calendario NYSE, logging | Terminada |
| 1 | Colectores de datos | Pendiente |
| 2 | Motor de datos e indicadores | Pendiente |
| 3 | Filtro, controles y modo replay | Pendiente |
| 4 | Analista IA | Pendiente |
| 5 | Riesgo, Plan del día y confirmaciones | Pendiente |
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

`make` sin argumentos lista todos los comandos.

## Dónde está cada cosa

- `config/config.yaml`: todos los parámetros. Un valor inválido detiene el arranque y dice qué campo corregir.
- `.env`: las API keys. Nunca se sube a git; `.env.example` lista los nombres.
- `backend/core/`: configuración, reloj (real o simulado), calendario NYSE y logging.
- `backend/db/`: tablas y migraciones. La base vive en `data/vertice.db`.
- `backend/tests/`: tests de cada pieza.
