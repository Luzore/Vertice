# CLAUDE.md — VÉRTICE

Especificación completa: `ROADMAP.md`. Este archivo contiene las reglas que se respetan en todas las sesiones.

## Principios no negociables (ROADMAP §3, copiados tal cual)

Cualquier cambio de código que rompa estas reglas es un bug, aunque "funcione".

1. **Cero ejecución.** No existe código que envíe órdenes a un broker. El sistema solo recomienda y registra.
2. **Solo largo.** Prohibido: venta en corto, margen, opciones, ETFs apalancados o inversos (se excluyen del universo por lista y por nombre).
3. **Los números los calcula el código.** La IA nunca inventa precios ni métricas. Todo número que devuelva (stop, objetivo, zona de entrada) se valida contra los datos; si se sale de tolerancia, se corrige al nivel válido más cercano o la idea se descarta.
4. **La IA decide qué; el código decide cuánto.** El tamaño de posición, los límites de exposición y la distancia mínima del stop son deterministas.
5. **Los controles vetan a la IA, nunca al revés.** Un candidato que falla un control bloqueante no llega a COMPRAR, diga lo que diga la IA.
6. **Los stops no dependen de la IA.** Si la IA está caída o sin cuota, el vigilante sigue disparando alertas de stop, objetivo y tiempo.
7. **Una sola fuente de verdad: la base de datos.** Una posición real solo existe si confirmaste la compra. Toda transición de estado se registra con hora y origen (sistema o usuario).
8. **Todo es auditable.** Cada decisión guarda su dossier completo, la respuesta cruda de la IA, el resultado de cada control y las lecciones aplicadas. Debe poder reproducirse después.
9. **Las lecciones solo influyen si la base de datos lo permite:** ≥20 casos (≥10 posteriores a la suposición) y confianza ≥0.60, impuesto con una restricción `CHECK`, no con código de aplicación.
10. **Todo se mide contra un benchmark** (SPY) y contra las recomendaciones "sombra".
11. **Tiempo:** se guarda en UTC; el calendario de mercado se rige por `America/New_York`; la UI muestra hora de Nueva York y de Monterrey (`America/Monterrey`).
12. **Secretos solo en `.env`** (en `.gitignore`). Nunca se loguean ni se muestran en la UI.
13. **Cada fuente de datos tiene** limitador de tasa, caché, reintentos con backoff y detección de datos viejos. Si cae una fuente crítica, el estado pasa a PRECAUCIÓN y la UI dice cuál y por qué.
14. **Tests por nodo.** Ninguna fase se da por terminada con tests en rojo.
15. **Modo replay desde el día uno.** Todo el pipeline corre sobre días históricos con un reloj simulado, para probar cambios en minutos y no esperar a que el mercado abra.

## Convenciones del proyecto (ROADMAP §1)

- Interfaz, informes, alertas y razonamientos de la IA: en español.
- Código, nombres de variables, tablas y commits: en inglés.
- Nombre de trabajo del sistema: **VÉRTICE** (cámbialo en `config.yaml`).
- **R** = riesgo inicial por acción (precio de entrada − stop). Ganar "+2R" significa ganar el doble de lo que arriesgaste.
- **Sombra** = una recomendación que no compraste; el sistema la sigue de forma virtual para medir si sus ideas funcionan.
- **Dossier** = el paquete de datos ordenados que recibe la IA sobre un candidato.

## Forma de trabajo (ROADMAP §17 y §20)

- Construir por fases, en el orden de la sección 17. No empezar una fase sin el "OK fase N" de la anterior.
- Al terminar cada fase: correr todos los tests, mostrar cada criterio de aceptación con evidencia (salida de comandos), listar lo pendiente y detenerse.
- Nunca escribir API keys en el código ni pedirlas en el chat: usar `.env.example`; el usuario llena `.env`.
- Si algo del roadmap es ambiguo o contradictorio, o una API cambió sus límites o endpoints, preguntar antes de decidir y proponer el ajuste en `config/config.yaml`.
- La IA del sistema se invoca con `claude -p` en modo headless, salida JSON con `--json-schema`, desde `runs/ai_sandbox/`. En tests se usa siempre la IA simulada.
- Priorizar que funcione y sea verificable sobre que se vea bonito; el diseño del dashboard llega en la Fase 7.

## Decisiones aprobadas que ajustan el ROADMAP

- **2026-11-10 · Calendario económico.** Finance Calendar cerró su plan gratuito. Las fechas de CPI, empleo y PIB salen del calendario de publicaciones de FRED, y las fechas FOMC de `fomc_dates` en `config/config.yaml`. No existe `FINCAL_API_KEY` y el enlace de atribución a financecalendar.com deja de ser obligatorio.

- **2026-11-10 · Conexiones en vivo (WebSocket).** En la Fase 1 las noticias y los precios se consultan por REST. El WebSocket de Alpaca (precios y noticias en tiempo real) se construye en la Fase 6 con el vigilante y se verifica en el VPS, porque el entorno de desarrollo en la nube no permite WebSocket.
- **2026-11-10 · Yahoo Finance.** Es solo un respaldo opcional del VIX intradía y nunca es fuente crítica: si falla, se usa el VIX diario de FRED.
- **2026-11-10 · Cliente HTTP.** Se usa `httpx2`, el sucesor de `httpx` que nombra el ROADMAP.
- **2026-11-10 · Respuestas crudas.** Se guardan 30 días como archivos comprimidos en `data/raw/`; la tabla `raw_snapshots` es su índice. Así los respaldos diarios de la base se mantienen pequeños.

- **2026-11-11 · Sentimiento de noticias sin IA.** Un clasificador por reglas (listas de palabras) puntúa los titulares de −1 a +1 y marca las noticias graves del control C21. Es el que funciona sin cuota de IA; el clasificador con IA de la Fase 4 lo refina.
- **2026-11-11 · Sector de cada empresa.** Sale del código SIC que publica la SEC, mapeado a los 11 ETFs sectoriales. Es una aproximación a la clasificación GICS de esos ETFs.
- **2026-11-11 · Universo y capitalización.** El universo se filtra por tipo de valor, precio y liquidez. La capitalización mínima (control C05) se revisa después, solo para los candidatos, porque cuesta una llamada a Finnhub por empresa.

## Comandos

Desde la raíz del repo (requiere `uv` y `make`):

- `make install`: instala las dependencias fijadas en `backend/uv.lock`.
- `make test`: corre todos los tests. Las advertencias cuentan como fallo.
- `make lint`: revisa estilo y errores comunes con ruff.
- `make migrate`: crea o actualiza la base de datos.
- `make dev`: levanta la API en http://127.0.0.1:8000 con recarga automática.
- `make check`: prueba cada fuente de datos con una llamada real y muestra estado y latencia.
- `make data`: descarga el universo, 2 años de barras diarias y los datos macro (tarda varios minutos).

Los módulos del backend se ejecutan desde `backend/` (por ejemplo `cd backend && uv run python -m collectors.check`).

## Nota del entorno de desarrollo en la nube

- El entorno en la nube no permite conexiones WebSocket ni puertos distintos de HTTPS: el streaming de Alpaca no se puede probar ahí.
- Yahoo Finance responde 429 desde este entorno; es esperado y no cuenta como fallo.
- El entorno de Claude Code en la nube guarda una copia del Secret de Alpaca y la pone en el header `APCA-API-SECRET-KEY` de toda llamada a `data.alpaca.markets`, por encima del valor de `.env`. Si se regenera la key de Alpaca hay que actualizar también esa copia; si no, ese host responde 401 aunque `.env` esté bien. En el VPS esto no aplica: ahí solo cuenta `.env`.
