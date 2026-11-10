# ROADMAP — Bot asesor de acciones con IA (para Claude Code)

Nov 4, 2026 · @Marco Saenz

## 1. Cómo usar este roadmap con Claude Code

Este documento es la especificación completa del sistema: Claude Code debe construirlo por fases, en orden, y detenerse al final de cada fase para que valides sus criterios de aceptación.

1. Exporta este doc a Markdown y guárdalo como `ROADMAP.md` en la raíz de una carpeta/repo vacío.
2. Abre Claude Code en esa carpeta y pega el prompt de la sección 20 ("Prompt inicial").
3. Lo primero que hará Claude Code es crear `CLAUDE.md` con los principios no negociables de la sección 3, para que los respete en todas las sesiones.
4. Trabaja una fase por sesión. Al cerrar cada fase, Claude Code te muestra los criterios de aceptación cumplidos y espera tu "OK fase N".
5. Antes de la Fase 1, consigue las API keys de la sección 5 y ponlas tú mismo en `.env`. Nunca las pegues en el chat ni en el código.

Convenciones del proyecto:

- Interfaz, informes, alertas y razonamientos de la IA: en español.
- Código, nombres de variables, tablas y commits: en inglés.
- Nombre de trabajo del sistema: **VÉRTICE** (cámbialo en `config.yaml`).
- **R** = riesgo inicial por acción (precio de entrada − stop). Ganar "+2R" significa ganar el doble de lo que arriesgaste.
- **Sombra** = una recomendación que no compraste; el sistema la sigue de forma virtual para medir si sus ideas funcionan.
- **Dossier** = el paquete de datos ordenados que recibe la IA sobre un candidato.

## 2. Objetivo y alcance

VÉRTICE es un asesor, no un bot que opera: cada día hábil entrega al menos 5 ideas de compra de acciones con sus razones, registra cuáles compraste y te avisa cuándo vender. Tú ejecutas todas las órdenes en tu casa de bolsa.

**Qué hace**

- Recoge datos de mercado, volatilidad, sentimiento, noticias y macro de APIs gratuitas.
- Los ordena (motor de datos), calcula \~45 métricas (indicadores), puntúa el universo (filtro), aplica 22 comprobaciones (controles), pide criterio a Claude (analista IA) y calcula cuánto comprar (motor de riesgo).
- Publica el **Plan del día** antes de la apertura con mínimo 5 ideas, cada una con: veredicto, zona de entrada, stop, objetivos, número de acciones, monto, razones, riesgos y qué invalidaría la idea.
- Te deja marcar **"La compré"** (precio y cantidad reales) o **"No la compré"**.
- Vigila tus posiciones abiertas en tiempo real y te manda **alertas de venta** (dashboard + Telegram) con el motivo y la urgencia. Tú marcas **"Vendí"** con el precio real.
- Aprende de sus aciertos y errores con hipótesis verificables, y mide su rendimiento contra el S&P 500.

**Qué NO hace**

- No se conecta a ningún broker ni envía órdenes.
- No vende en corto, no usa margen, opciones ni ETFs apalancados o inversos.
- No promete rentabilidad: es un experimento medible.

**Diferencias con el bot del video**

| Aspecto | Bot del video | VÉRTICE |
| --- | --- | --- |
| Activos | 16 criptomonedas | Acciones de EE.UU. (opcional: solo las listadas en el SIC de la BMV) |
| Dirección | Largo y corto | Solo largo |
| Ejecución | Automática vía API del exchange | Manual: tú compras y vendes, el sistema registra tu confirmación |
| Salida | El bot cierra solo | El sistema te alerta y tú vendes |
| Ideas por día | 3–11 operaciones | Mínimo 5 ideas rankeadas; compras solo las que pasen todo |
| Datos extra | Derivados, DEX, on-chain | Volumen relativo, calendario de resultados, filings de la SEC, amplitud de mercado |
| Evaluación | Comparado con Bitcoin al final | Comparado con SPY todos los días, más las ideas "sombra" |

**Decisiones por defecto** (todas cambiables en `config.yaml`)

- Mercado: NYSE y Nasdaq, acciones comunes; filtro SIC desactivado hasta que cargues la lista de emisoras del SIC.
- Horizonte: swing de 1 a 10 días hábiles. El modo intradía existe pero viene apagado, porque exige mirar la pantalla todo el día.
- Moneda base: USD, con equivalente en MXN solo informativo.
- Acciones enteras por defecto; fraccionarias si tu broker las permite.
- "Mínimo 5 ideas" significa que el Plan del día **siempre lista las 5 mejores** con su veredicto (COMPRAR, VIGILAR o NO HOY). Si el mercado está en modo de riesgo alto, puede haber 0 en COMPRAR, y el plan explica por qué. Forzar compras malas para llegar a 5 está prohibido.

## 3. Principios no negociables (van a CLAUDE.md)

Estas 15 reglas se copian tal cual a `CLAUDE.md`; cualquier cambio de código que las rompa es un bug, aunque "funcione".

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

## 4. Arquitectura, stack y estructura de carpetas

El sistema es un pipeline de 9 nodos que corre en un servidor (tu VPS o tu PC), guarda todo en una base de datos y empuja cada cambio en vivo al dashboard por WebSocket.

```text
1 · COLECTORES  [Mercado] [Volatilidad] [Sentimiento] [Noticias] [Macro] [Calendario] [Eventos]
                 Alpaca    ATR, VIX      noticias,VIX  Alpaca,RSS FRED   FinCalendar  SEC, Finnhub
                                              │
                                              ▼
                              2 · MOTOR DE DATOS (normaliza, descarta datos viejos)
                                              │
                                              ▼
 3 · Indicadores ──► 4 · Filtro ──► 5 · Controles ──► 6 · Analista IA (decide qué) ◄─────┐
   ~45 métricas      puntúa universo  22 comprobaciones          │                        │
                                                                 ▼                        │
 9 · Vigilante ◄── TÚ, en tu broker ◄── 8 · Plan del día ◄── 7 · Riesgo (decide cuánto)   │
 (dice cuándo  ──►  "La compré" /        ≥5 ideas y razones                               │
  vender)           "Vendí"                                                               │
      │  alerta de venta ──► tú vendes                                                    │
      ▼                                                                                   │
 APRENDIZAJE NOCTURNO: lecciones confirmadas (≥20 casos, confianza ≥0.60) ────────────────┘
```

Los datos bajan por los nodos 1 a 8 hasta tu decisión; el vigilante te avisa cuándo vender y el aprendizaje nocturno devuelve las lecciones confirmadas al analista IA.

**Stack**

| Capa | Tecnología | Por qué |
| --- | --- | --- |
| Backend | Python 3.12, FastAPI, asyncio, httpx, pydantic v2 | Ecosistema de finanzas y datos; tipado de esquemas |
| Cálculo | pandas, numpy; indicadores propios con tests (pandas-ta solo como referencia) | Control total y verificable de cada fórmula |
| Calendario de mercado | `exchange_calendars` (o `pandas_market_calendars`) | Feriados y medias sesiones de NYSE |
| Programación de tareas | APScheduler con zona `America/New_York` | Corridas premercado, vigilancia y cierre |
| Base de datos | SQLite en modo WAL + SQLAlchemy 2 + Alembic (migrable a Postgres) | Simple en un VPS; soporta `CHECK` y triggers |
| IA | Claude Code CLI en modo headless (`claude -p`) | Lo que pediste; salida JSON validada con esquema |
| Frontend | React + Vite + TypeScript + Tailwind | Rápido y fácil de mantener con Claude Code |
| Grafo de nodos | React Flow (`@xyflow/react`) | El diagrama "Cómo fluye la información" en vivo |
| Gráficas | TradingView Lightweight Charts (licencia Apache 2.0) | Velas, curva de capital, marcas de compra y venta |
| Tiempo real | WebSocket de FastAPI hacia el navegador | El dashboard se actualiza sin recargar |
| Alertas | Telegram Bot API | Gratis, llega al celular, admite botones |
| Despliegue | Docker Compose + Caddy (HTTPS) + contraseña de acceso | Un comando para levantar todo |

**Estructura de carpetas**

```text
vertice/
├── CLAUDE.md                 # reglas no negociables (sección 3)
├── ROADMAP.md                # este documento
├── config/config.yaml        # todos los parámetros (sección 19)
├── .env.example              # nombres de las keys, sin valores
├── docker-compose.yml
├── backend/
│   ├── app/main.py           # FastAPI + WebSocket
│   ├── app/api/              # status, plan, positions, confirmations, config, lessons, metrics
│   ├── core/                 # clock, market_calendar, rate_limiter, cache, settings, logging
│   ├── collectors/           # alpaca, finnhub, massive, fred, sec_edgar, rss_news, econ_calendar, fx
│   ├── engine/
│   │   ├── data_engine.py    # nodo 2: normaliza
│   │   ├── indicators.py     # nodo 3
│   │   ├── screener.py       # nodo 4: filtro
│   │   ├── controls.py       # nodo 5: 22 comprobaciones
│   │   ├── ai_analyst.py     # nodo 6: wrapper de Claude Code CLI
│   │   ├── risk.py           # nodo 7
│   │   ├── recommender.py    # nodo 8: Plan del día + confirmaciones
│   │   └── watcher.py        # nodo 9: vigilante y alertas de venta
│   ├── learning/             # lessons, calibration, nightly_review
│   ├── replay/               # reloj simulado y reproductor de días históricos
│   ├── notify/telegram.py
│   ├── db/models.py, db/migrations/
│   ├── scheduler.py
│   └── tests/
├── prompts/                  # analyst.md, position_review.md, news_classifier.md, lesson_writer.md
├── schemas/                  # JSON Schemas de salida de la IA
└── frontend/                 # React
```

## 5. Fuentes de datos y API keys

Todo el sistema puede correr con $0 en datos: la base es Alpaca (precios y noticias), Finnhub (noticias, resultados, fundamentales), FRED (macro) y SEC EDGAR (filings). El único costo fijo es tu plan de Claude. Límites verificados el 4 de noviembre de 2026; cámbialos en `config.yaml` si el proveedor los modifica.

| Capa | Fuente | Costo | Límite del plan gratis | Variable en `.env` | Uso en VÉRTICE | Respaldo |
| --- | --- | --- | --- | --- | --- | --- |
| Mercado (barras, quotes, tiempo real) | [Alpaca Market Data](https://docs.alpaca.markets/docs/about-market-data-api), cuenta paper | $0 (Basic) | 200 llamadas/min; tiempo real solo del exchange IEX; WebSocket de 30 símbolos | `ALPACA_API_KEY`, `ALPACA_API_SECRET` | Barras diarias e intradía del universo, quotes del vigilante | Massive, yfinance |
| Noticias por ticker | [Alpaca News API](https://alpaca.markets/docs/news-api) (Benzinga) | Incluida en la cuenta | REST + WebSocket; el límite depende del plan de datos | las mismas de Alpaca | Titulares en tiempo real e histórico desde 2015 | Finnhub, RSS |
| Noticias, resultados, fundamentales, insiders | [Finnhub](https://anythingmcp.com/marketplace/finnhub) | $0, uso personal | 60 llamadas/min; velas (candles) NO incluidas; noticias 1 año atrás; calendario de resultados 1 mes atrás | `FINNHUB_API_KEY` | Fechas de earnings, sorpresas de EPS, noticias, métricas básicas | — |
| Fin de día de respaldo | [Massive](https://veryfront.com/docs/cloud/integrations/polygon) (antes Polygon.io) | $0 (Basic) | 5 llamadas/min; solo fin de día; 2 años de historia | `MASSIVE_API_KEY` | Respaldo de barras diarias y datos de referencia | yfinance |
| Macro | [FRED](https://datasetiq.com/blog/api-access-economic-data-guide) (Fed de St. Louis) | $0 | 120 solicitudes/min | `FRED_API_KEY` | Tasas 2 y 10 años, curva, VIX diario, spread high-yield, dólar, USD/MXN | — |
| Calendario económico | [Finance Calendar API](https://www.financecalendar.com/?p=2927) | $0 con cuenta verificada | 250 solicitudes/día, 5,000/mes; exige un enlace visible a financecalendar.com en la UI | `FINCAL_API_KEY` | CPI, empleo, PIB, decisiones de tasas: horas de alto impacto | Lista estática de fechas FOMC en `config.yaml` |
| Filings | [SEC EDGAR](https://www.sec.gov/os/accessing-edgar-data) | $0, sin key | 10 solicitudes/s; User-Agent con nombre y email obligatorio | `SEC_USER_AGENT` | 8-K (eventos), Form 4 (compras de insiders), ofertas de acciones (dilución) | Finnhub |
| Titulares generales | Google News RSS y Yahoo Finance RSS | $0, no oficial | Sin límite publicado: máximo 1 consulta por ticker cada 15 min | — | Contexto macro y respaldo de noticias | — |
| Tipo de cambio (opcional) | API SIE de Banxico (tipo FIX) o FRED | $0 | Token gratis | `BANXICO_TOKEN` | Mostrar el capital en MXN | FRED |
| Respaldo de desarrollo | yfinance | $0, no oficial | Bloqueos 429 frecuentes; nunca como fuente principal | — | Pruebas locales y índices como ^VIX intradía | — |
| Alertas | Telegram Bot API | $0 | — | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alertas de compra y venta al celular | Email |
| IA | Claude Code CLI (`claude -p`) | Tu suscripción de Claude, o API key con pago por uso | Los límites diarios y semanales de tu plan | Login de Claude Code, o `ANTHROPIC_API_KEY` | Analista, revisor de posiciones, clasificador de noticias, escritor de lecciones | Modo "solo reglas" |

**Cómo conseguir cada key**

- [ ] Alpaca: crea una cuenta "paper only" con tu email (está abierta a residentes de cualquier país) y genera la key en el dashboard. No necesitas fondearla.
- [ ] Finnhub: registro gratis en finnhub.io; la key aparece en el dashboard.
- [ ] FRED: registro en fred.stlouisfed.org y solicitud de API key.
- [ ] Finance Calendar: cuenta verificada en financecalendar.com/api.
- [ ] SEC: no hay key; define `SEC_USER_AGENT="VERTICE tu_email@dominio.com"`.
- [ ] Telegram: crea un bot con @BotFather y obtén tu chat id.
- [ ] Claude Code: instálalo en el servidor e inicia sesión una vez (o define `ANTHROPIC_API_KEY`).
- [ ] Opcionales: Massive, Banxico.

**Descartadas a propósito**

- **Reddit API:** dejó de aceptar solicitudes nuevas de acceso público el 31 de octubre de 2026 y el acceso restante termina en marzo de 2027 ([fuente](https://bigideasdb.com/reddit-api-pricing)). No construyas nada que dependa de ella.
- **Forex Factory:** no tiene API oficial y sus términos prohíben reutilizar su calendario.
- **Alpha Vantage gratis:** 25 solicitudes al día, insuficiente.

**Presupuesto de llamadas (universo de \~500 acciones)**

- Barras diarias: Alpaca acepta varios símbolos por solicitud; el universo completo cabe en menos de 20 llamadas.
- Noticias y earnings de los \~30 finalistas: 1 minuto de Finnhub.
- Vigilante: WebSocket de Alpaca para hasta 30 símbolos (posiciones + lista de espera), sin consumir llamadas REST.
- Cada colector lleva su propio limitador (token bucket) configurado al 80% del límite del proveedor.

## 6. Nodos 1 y 2: colectores y motor de datos

Los colectores traen datos crudos de cada fuente y el motor de datos los convierte en un único "estado del mercado" limpio, fechado y sin datos del futuro.

### Nodo 1 — Colectores (una capa por tipo de dato)

| Capa (nombre en la UI) | Qué trae | Fuente principal | Frecuencia |
| --- | --- | --- | --- |
| Mercado | Barras diarias (2 años) del universo; barras de 5 min de finalistas y posiciones; quotes en vivo; SPY, QQQ, IWM y los 11 ETFs sectoriales (XLK, XLF, XLE, XLV, XLY, XLP, XLI, XLB, XLU, XLRE, XLC) | Alpaca | Diario premercado; 5 min en sesión; WebSocket para posiciones |
| Volatilidad | VIX diario; VIX intradía; la volatilidad de cada acción se calcula en el nodo 3 | FRED (`VIXCLS`), yfinance de respaldo | Diario + cada 15 min |
| Sentimiento | Puntaje de noticias por ticker (−1 a +1) y sentimiento del mercado (VIX, amplitud, nuevos máximos vs mínimos) | Clasificador propio sobre noticias + cálculos del universo | Con cada noticia nueva; cada 15 min |
| Noticias | Titulares y resúmenes por ticker y macro | Alpaca News (WebSocket), Finnhub, RSS | Tiempo real |
| Macro | Tasas 2 y 10 años, curva 10–2, fondos federales, spread high-yield, dólar, USD/MXN | FRED | Diario |
| Calendario | Eventos económicos de alto impacto (hora y país) y fechas FOMC | Finance Calendar + lista en config | Diario y lunes semanal |
| Eventos corporativos | Fechas de resultados, sorpresa de EPS, 8-K, Form 4, ofertas de acciones, splits | Finnhub, SEC EDGAR, Alpaca | Diario + cada 30 min en sesión |

**Universo.** Se arma cada lunes desde el endpoint de activos de Alpaca (acciones activas en NYSE y Nasdaq), y se reduce con los filtros duros del nodo 5 (precio, liquidez, capitalización). Si activas `universe.sic_only`, se cruza con `config/sic_universe.csv` (la lista de emisoras del SIC que tú cargues). Se excluyen ETFs apalancados o inversos, SPACs, warrants y OTC.

**Contrato de un colector.** Cada uno implementa `fetch(as_of) -> RawSnapshot` con: fuente, hora de consulta, latencia en ms, estado (ok, lento, error, viejo), payload crudo. Los snapshots crudos se guardan 30 días para el modo replay y para auditoría. La latencia alimenta los milisegundos que se ven en cada nodo del dashboard.

### Nodo 2 — Motor de datos (normaliza)

1. Convierte todo a esquemas pydantic: `Bar`, `Quote`, `NewsItem`, `MacroPoint`, `EconEvent`, `CorporateEvent`, `Filing`.
2. Pasa toda hora a UTC y normaliza tickers (por ejemplo `BRK.B` y `BRK-B` son el mismo).
3. Ajusta precios por splits y deduplica noticias (misma URL o mismo titular en 6 horas).
4. Marca datos viejos con umbrales por capa: quote > 2 min en sesión, barras diarias sin la última sesión, macro > 3 días.
5. Calcula un puntaje de calidad por ticker (0 a 1) según datos faltantes o viejos; con calidad < 0.8 el ticker no puede salir como COMPRAR.
6. **Punto en el tiempo:** solo entrega datos con hora ≤ al reloj del sistema. En replay, el reloj es simulado; así se evita usar información del futuro.
7. Produce un objeto `MarketState` por ciclo: tablas del universo + contexto de mercado + eventos + salud de cada fuente. Es la única entrada del nodo 3.

## 7. Nodo 3: indicadores (\~45 métricas)

Este nodo calcula, para cada acción del universo, métricas de tendencia, momentum, volumen, volatilidad, niveles, eventos y contexto; todas en código propio con test unitario contra valores conocidos.

| Grupo | Métricas (parámetros por defecto) | Para qué sirve |
| --- | --- | --- |
| Tendencia (8) | SMA 20/50/200; EMA 9/21; pendiente de la SMA50 (20 días); ADX(14) con +DI/−DI; precio vs VWAP (intradía) | ¿Va hacia arriba y con qué fuerza? |
| Momentum (8) | RSI(14); RSI(2); MACD (12,26,9) línea, señal e histograma; retorno 1d, 5d, 20d, 60d | ¿Acelera o se está agotando? |
| Fuerza relativa (3) | Retorno 20d y 60d vs SPY; retorno 20d vs su ETF sectorial | Preferir líderes, no rezagadas |
| Volumen (7) | Volumen relativo RVOL (hoy vs promedio 20d, y a la misma hora del día); volumen en dólares promedio 20d; pendiente de OBV; MFI(14); CMF(20); volumen de días alcistas vs bajistas (20d) | Confirmar que hay dinero real detrás del movimiento |
| Volatilidad (9) | ATR(14) y ATR% del precio; volatilidad realizada 10d y 20d anualizada; Bollinger %B y ancho; compresión (Bollinger dentro de Keltner); rango del día vs ATR; gap de apertura %; beta 60d vs SPY | Elegir las volátiles "aprovechables" y dimensionar stops |
| Niveles (5) | Máximo y mínimo de 20 días (Donchian); distancia al máximo de 52 semanas; soportes y resistencias por pivotes (últimos 60 días); distancia a la SMA20 en ATRs (extensión) | Entrada, stop y objetivos con base en niveles reales |
| Eventos (6) | Días a resultados; sorpresa del último EPS; sentimiento de noticias 24h y 72h; anomalía en número de noticias; compras de insiders 90d (Form 4); oferta de acciones reciente (dilución) | Detectar catalizadores y trampas |
| Contexto de mercado (8, comunes a todas) | Tendencia de SPY (vs SMA50 y SMA200); VIX nivel y cambio 5d; amplitud: % del universo sobre su SMA50; nuevos máximos vs mínimos de 52 semanas; tendencia del ETF sectorial; curva 10–2; cambio de la tasa a 10 años 5d; spread high-yield | Clasificar el régimen del día |

**Régimen de mercado** (una etiqueta por día, la usan filtro, controles, riesgo e IA):

- **ALCISTA:** SPY sobre SMA50 y SMA200, VIX < 20, amplitud > 55%.
- **NEUTRAL:** el resto de los casos.
- **RIESGO ALTO:** SPY bajo SMA200, o VIX > 28, o VIX sube > 25% en 5 días.

Los umbrales viven en `config.yaml`. La salida del nodo es una tabla `indicators(ticker, as_of, metric, value)` más una vista ancha por ticker que consume el filtro. Meta de rendimiento: 500 tickers en menos de 60 segundos.

## 8. Nodos 4 y 5: filtro y controles

El filtro puntúa todo el universo y se queda con los 25 mejores; los controles aplican 22 comprobaciones deterministas y solo los que pasan llegan a la IA.

### Nodo 4 — Filtro (puntúa el universo)

Cada acción recibe una etiqueta de **setup** y un **puntaje de 0 a 100** con su desglose visible en la UI.

**Setups (solo de compra):**

- **RUPTURA:** cierre en máximo de 20 días con RVOL ≥ 1.5 y tendencia alcista (precio > SMA50 > SMA200 o SMA50 subiendo). Suma si venía de una compresión de volatilidad.
- **PULLBACK:** tendencia alcista y retroceso a la SMA20/EMA21, o RSI(2) < 10, con volumen bajo en el retroceso.
- **CATALIZADOR:** gap > 3% por noticia positiva o resultados mejores a lo esperado, con RVOL > 2, que se sostiene sobre el VWAP.
- **REBOTE:** sobreventa extrema (RSI(2) < 5 y 3+ días a la baja) en una acción que sigue sobre su SMA200. Solo en régimen ALCISTA o NEUTRAL.

**Puntaje:**

| Componente | Peso | Cómo puntea al máximo |
| --- | --- | --- |
| Tendencia | 25 | Precio > SMA20 > SMA50 > SMA200, ADX > 20 y subiendo |
| Momentum y fuerza relativa | 20 | Retornos 20d/60d por encima de SPY y de su sector |
| Volumen | 20 | RVOL > 1.5 y acumulación (OBV/CMF positivos) |
| Volatilidad aprovechable | 15 | ATR% entre 2.5% y 8%; penaliza > 12% (movimientos demasiado violentos para un stop razonable) |
| Catalizador y sentimiento | 10 | Noticia positiva reciente o sorpresa de EPS positiva |
| Calidad del setup | 10 | Patrón limpio y un nivel de stop claro debajo |

Penalizaciones: extensión > 3 ATR sobre la SMA20 (−15), sentimiento negativo en 72h (−10), calidad de datos < 0.9 (−5). El parámetro `screener.volatility_preference` (0 a 1) sube o baja el peso de la volatilidad: con 1 favorece "las más volátiles" que pediste, siempre dentro de los límites de liquidez.

### Nodo 5 — Controles (22 comprobaciones)

Cada control devuelve `pass`, `fail` o `warn`, con su motivo en texto. Dos tipos: **B** = bloquea; **R** = reduce el tamaño a la mitad y se le avisa a la IA.

| ID | Comprobación | Regla por defecto | Tipo |
| --- | --- | --- | --- |
| C01 | Datos frescos | Quote < 2 min en sesión; calidad del ticker ≥ 0.8 | B |
| C02 | Día hábil | NYSE abierto hoy según calendario | B |
| C03 | Precio mínimo | ≥ $5 | B |
| C04 | Liquidez | Volumen en dólares promedio 20d ≥ $20M | B |
| C05 | Tamaño de empresa | Capitalización ≥ $1,000M | B |
| C06 | Spread | Spread estimado ≤ 0.30% | B |
| C07 | Disponible en tu mercado | Está en la lista SIC (solo si `sic_only` está activo) | B |
| C08 | Resultados | No reporta en los próximos 3 días hábiles (configurable) | B |
| C09 | Evento macro | Sin CPI, empleo o FOMC en las próximas 6 horas | R |
| C10 | Régimen | En RIESGO ALTO solo puntaje ≥ 80 y tamaño a la mitad | R |
| C11 | Gap | Gap de apertura ≤ 2 ATR (no perseguir) | B |
| C12 | Extensión | Precio ≤ 3 ATR sobre la SMA20 | B |
| C13 | Recompensa vs riesgo | Primer objetivo ≥ 1.5R | B |
| C14 | Stop razonable | Stop entre 0.8 y 3 ATR y debajo de un nivel técnico | B |
| C15 | Máximo de posiciones | Posiciones abiertas < límite según agresividad | B |
| C16 | Concentración sectorial | Máximo 2 posiciones y 40% del capital por sector | B |
| C17 | Correlación | Correlación 60d con cada posición abierta ≤ 0.80 | R |
| C18 | Efectivo | Alcanza para al menos 1 acción (o el mínimo fraccionario) | B |
| C19 | Enfriamiento | No recomprar un ticker que salió por stop en los últimos 5 días hábiles | B |
| C20 | Freno de pérdidas | Pérdida del día < 2% y de la semana < 5% del capital | B |
| C21 | Noticia grave | Sin investigación regulatoria, quiebra, oferta de acciones o rebaja fuerte en 72h | B |
| C22 | Situación especial | Sin suspensión de cotización ni fusión con precio fijado | B |

Los que pasan los controles bloqueantes (máximo 10, configurable) son los **finalistas** que verá la IA. Los que fallan quedan registrados con su motivo y aparecen en la UI como "descartados".

## 9. Nodo 6: analista IA con Claude Code CLI

La IA recibe un dossier ordenado de los finalistas y devuelve un JSON validado con esquema: qué comprar, qué vigilar, qué descartar y por qué. Nunca decide montos.

**Cuándo se llama a la IA (y nada más)**

| Tarea | Momento | Llamadas por día | Modelo por defecto |
| --- | --- | --- | --- |
| Ranking de finalistas (todos en una llamada) | Premercado, y una segunda pasada tras un dato macro de las 8:30 ET | 1–2 | sonnet |
| Revisión de una posición | Solo con disparador: noticia nueva, movimiento ±1.5 ATR, cerca de stop u objetivo, revisión de cierre | 0–15 | sonnet |
| Clasificación de noticias | En lotes de 20 titulares | 2–8 | haiku |
| Escritura de lecciones | Después del cierre | 1 | sonnet u opus |
| "Explícame esta idea" | Botón en la UI | bajo demanda | sonnet |

**Llamada headless.** El wrapper de Python ejecuta Claude Code como subproceso desde una carpeta vacía (`runs/ai_sandbox/`), para que no lea el código del proyecto, y le pasa el dossier por stdin:

```bash
# se ejecuta con cwd = runs/ai_sandbox/
cat ../dossier_premarket.md | claude -p \
  --model sonnet \
  --append-system-prompt-file ../../prompts/analyst.md \
  --output-format json \
  --json-schema "$(cat ../../schemas/analyst_decision.schema.json)" \
  --permission-mode dontAsk \
  --max-turns 3
```

- La respuesta validada llega en el campo `structured_output`; también trae `session_id`, `usage` y `total_cost_usd`, que se guardan por llamada.
- Con tu suscripción basta el login normal de Claude Code. Si usas API key, agrega `--bare` y define `ANTHROPIC_API_KEY` (el modo `--bare` no lee el login de la suscripción).
- Timeout de 180 s, un reintento; si vuelve a fallar, se activa el modo "solo reglas" para esa corrida.
- Búsqueda web apagada por defecto (`ai.allow_web_search: false`). Si la enciendes, solo se permite `--allowedTools "WebSearch"` en revisiones de posición disparadas por noticias.

**Contenido del dossier** (Markdown con bloques JSON):

1. Fecha, hora ET y Monterrey, régimen del día y por qué.
2. Contexto macro y eventos de hoy con hora.
3. Cartera: efectivo, posiciones abiertas con su R actual, sectores.
4. Lecciones en vigor (id + texto + ámbito). Solo las que aplican al contexto.
5. Por candidato: setup, puntaje con desglose, \~15 métricas clave, niveles válidos (soportes, resistencias, múltiplos de ATR), hasta 5 noticias resumidas con fecha, fuente y sentimiento, eventos próximos, controles en `warn` y el historial del sistema en ese setup (tasa de acierto y expectativa en R).

**Reglas del prompt `prompts/analyst.md`** (resumen):

- Eres analista de swing trading, solo compras. Usa únicamente los datos del dossier; si algo falta, decláralo en `datos_faltantes`.
- Cualquier stop, objetivo o zona de entrada debe salir de los niveles válidos del dossier.
- Da una probabilidad honesta de tocar el primer objetivo antes que el stop. Será medida (calibración).
- Piensa primero en qué puede salir mal. COMPRAR solo si la expectativa es positiva después de costos.
- Explica en español claro, para un estudiante de finanzas: tesis en 3 frases como máximo, razones en viñetas.
- Respeta las lecciones en vigor y di cuáles aplicaste.

**Esquema de salida** (`schemas/analyst_decision.schema.json`):

```json
{
  "lectura_mercado": "string",
  "decisiones": [
    {
      "ticker": "string",
      "veredicto": "COMPRAR | VIGILAR | DESCARTAR",
      "setup": "RUPTURA | PULLBACK | CATALIZADOR | REBOTE",
      "prob_objetivo_antes_que_stop": 0.0,
      "confianza": 0.0,
      "entrada": { "tipo": "limite | stop_limite", "zona_min": 0.0, "zona_max": 0.0, "no_perseguir_sobre": 0.0 },
      "disparador_vigilar": "string o null",
      "stop": 0.0,
      "objetivos": [0.0, 0.0],
      "horizonte_dias": 0,
      "tesis": "string",
      "razones": ["string"],
      "riesgos": ["string"],
      "invalidacion": "string",
      "lecciones_aplicadas": ["L-012"],
      "datos_faltantes": ["string"]
    }
  ]
}
```

**Validación después de la IA** (en código): tickers solo de la lista de finalistas; stop < entrada < objetivos; cada nivel a ≤ 0.5% de un nivel válido o se ajusta; probabilidades entre 0 y 1; si un candidato no viene en la respuesta, queda como DESCARTAR con motivo "sin respuesta de la IA".

**Presupuesto de IA**

- `ai.daily_call_budget` (por defecto 40). La cabecera de la UI muestra "consultas IA usadas / límite".
- 30% del presupuesto queda reservado para revisar posiciones. Al 90%, solo se revisan posiciones. Al 100%, estado **SIN CUOTA IA**: el Plan del día sale con veredictos de reglas, marcados como "sin IA", y el vigilante sigue funcionando.
- Caché: el mismo dossier (mismo hash) en menos de 30 minutos reutiliza la respuesta.
- Si usas API key, `ai.max_usd_per_day` corta las llamadas al llegar al monto.

**Advertencia sobre replay con IA real:** el modelo ya conoce lo que pasó en el mercado antes de su fecha de corte de entrenamiento. Un replay con IA real sobre esas fechas no mide nada. Usa el replay para probar la plomería con una IA simulada, y mide la ventaja real solo hacia adelante (papel).

## 10. Nodo 7: motor de riesgo

El motor de riesgo decide cuántas acciones comprar con una regla fija: arriesgar un porcentaje pequeño del capital por idea, medido desde la entrada hasta el stop. Tú le das el capital disponible; él reparte.

**Tamaño de cada posición**

```latex
\text{riesgo}_{\$} = \text{capital} \times \text{riesgo}_{\%} \times m_{\text{conviccion}} \times m_{\text{regimen}} \times m_{\text{racha}}
```

```latex
\text{acciones} = \left\lfloor \frac{\text{riesgo}_{\$}}{\text{entrada} - \text{stop}} \right\rfloor \quad \text{con} \quad \text{acciones} \times \text{entrada} \le \min(\text{max}_{\%} \times \text{capital},\ \text{efectivo})
```

- Capital = efectivo que tú capturas + valor de mercado de tus posiciones reales.
- m de convicción: 0.5 a 1.25 según la probabilidad de la IA ya calibrada.
- m de régimen: 0.5 en RIESGO ALTO; cada control tipo R aplica otro 0.5 (piso total 0.25).
- m de racha: 0.5 tras 3 pérdidas seguidas o con caída > 5% desde el máximo, hasta recuperar la mitad.
- Con acciones fraccionarias activas se redondea a 0.01 en vez de a enteros.

**Filtro de expectativa** (tras costos):

```latex
EV_R = p \times R_{\text{obj1}} - (1 - p) \times 1 - \text{costos}_R \ \ge\ 0.15
```

Donde p es la probabilidad calibrada y costos\_R = (comisión de compra y venta + IVA sobre comisión + deslizamiento estimado) / riesgo total en dólares. Pon la comisión real de tu casa de bolsa en `costs.*`.

**Perillas por agresividad** (el control deslizante 1–10 de la cabecera; valores intermedios se interpolan):

| Agresividad | Riesgo por idea (% del capital) | Máx. posiciones | Máx. por posición (% del capital) | Puntaje mínimo | Probabilidad IA mínima | Stop inicial (ATR) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 (prudente) | 0.25% | 3 | 15% | 80 | 0.60 | 1.5 |
| 3 | 0.50% | 4 | 20% | 75 | 0.55 | 1.75 |
| 5 (por defecto) | 0.75% | 5 | 25% | 70 | 0.50 | 2.0 |
| 7 | 1.00% | 6 | 30% | 65 | 0.47 | 2.0 |
| 10 (agresivo) | 1.50% | 8 | 35% | 55 | 0.42 | 2.5 |

**Reparto del efectivo y costo de oportunidad**

1. Las ideas COMPRAR se ordenan por EV\_R y se asigna efectivo en ese orden hasta agotar efectivo o límites.
2. Las que no alcanzan quedan como "COMPRAR si liberas efectivo".
3. Si una idea nueva supera en más de 0.5R la expectativa restante de tu posición más débil, se propone una **rotación** ("vende X para comprar Y"). La decisión es tuya.

**Frenos automáticos**

| Condición | Estado | Efecto |
| --- | --- | --- |
| Pérdida del día ≥ 2% del capital | SOLO VIGILAR | Sin compras nuevas hasta mañana; el vigilante sigue |
| Pérdida de la semana ≥ 5% | SOLO VIGILAR | Sin compras hasta que tú lo reactives y leas el resumen semanal |
| Caída ≥ 10% desde el máximo | PAUSA | Revisión obligatoria: métricas, lecciones y parámetros |

## 11. Nodo 8: Plan del día y confirmación manual

Este nodo reemplaza a la "ejecución" del bot original: publica el Plan del día, te pregunta si compraste y convierte tu respuesta en una posición real o en una sombra.

**Contenido del Plan del día**

- Siempre ≥ 5 ideas, ordenadas: primero COMPRAR (por EV\_R), luego VIGILAR, luego NO HOY (las mejores por puntaje que no pasaron, con el motivo exacto).
- Por idea: ticker y nombre, sector, setup, puntaje con desglose, veredicto, probabilidad, zona de entrada, precio de "no perseguir", stop, objetivos en $ y en R, acciones y monto sugeridos (y % del capital), riesgo en $, tesis, razones, riesgos, invalidación, lecciones aplicadas.
- Arriba del plan: lectura del mercado, régimen y eventos del día con hora.
- Se publica a las 9:00 ET y se revisa a las 9:45 ET: si el precio abrió sobre "no perseguir", la idea pasa a VIGILAR sola.

**Reglas de entrada que el plan te recuerda**

- Orden límite dentro de la zona; nunca a mercado.
- No entrar en los primeros 15 minutos de la sesión (`entry.skip_first_minutes: 15`).
- Si tu casa de bolsa lo permite, coloca también la orden stop en el precio indicado: es tu protección si no ves la alerta.
- Las ideas VIGILAR traen un disparador (por ejemplo, "cierre de vela de 5 min arriba de la resistencia con RVOL > 1.5"). Cuando se cumple, el vigilante te avisa y la idea pasa a COMPRAR con el tamaño recalculado.

**Ejemplo ilustrativo de mensaje de Telegram**

```text
VÉRTICE · Plan del día · mié 4 nov 2026 · Régimen NEUTRAL
COMPRAR  XYZ  (RUPTURA · puntaje 82 · prob. 0.56)
Entrada: límite 48.10–48.40 (no perseguir sobre 48.90)
Stop: 46.70 (−1R)   Objetivos: 51.05 (+1.8R) / 52.90 (+3R)
Tamaño: 24 acciones ≈ $1,158 (23% del capital) · riesgo $37
Por qué: rompe máximo de 20 días con 2.1x su volumen normal
Riesgo: reporta resultados en 6 días
[La compré] [No la compré] [Ver en el dashboard]
```

**Flujo de confirmación**

1. **"La compré"** abre un formulario: precio promedio, cantidad, hora (por defecto ahora) y comisión pagada (opcional). Se crea la posición REAL y se recalcula el R con tu precio real.
2. **"No la compré"** pide un motivo opcional (sin efectivo, no me convenció, llegué tarde, otro). La idea se sigue como SOMBRA con la entrada teórica, solo si el precio sí tocó la zona ese día; si no, queda como "no ejecutable".
3. **Sin respuesta al cierre:** se registra como SOMBRA automática y el resumen de la noche te lo señala.
4. Se permiten compras y ventas parciales, y corregir una confirmación; toda corrección queda auditada.

**Estados de una idea**

- `PROPOSED` → `BOUGHT` (posición real) | `DECLINED` → `SHADOW` | `EXPIRED` → `SHADOW` o `NOT_FILLABLE`
- Posición: `OPEN` → `SELL_ALERT` → `PARTIALLY_CLOSED` → `CLOSED`
- Las transiciones válidas viven en una máquina de estados con tests. Una transición inválida no se aplica: pone el estado del sistema en PRECAUCIÓN y te pide resolverlo en la UI.

## 12. Nodo 9: vigilante de posiciones y reglas de venta

El vigilante revisa tus posiciones en tiempo real y te dice cuándo vender con un motivo concreto; sus reglas son código, así que funciona aunque la IA esté sin cuota.

**Cómo vigila**

- En sesión: WebSocket de Alpaca (IEX) para posiciones abiertas y disparadores de VIGILAR, hasta 30 símbolos. Evalúa reglas como máximo cada 10 s por símbolo; las confirmaciones usan velas de 5 min.
- Fuera de sesión: noticias nocturnas y gap de premercado entre 8:00 y 9:30 ET.
- El precio de IEX es una aproximación del precio consolidado; cada alerta indica fuente y hora del precio.

**Reglas de salida** (en orden de prioridad; todas configurables en `exits.*`)

| # | Regla | Condición | Acción sugerida | Urgencia |
| --- | --- | --- | --- | --- |
| 1 | Stop | Último precio ≤ stop, o apertura con gap debajo del stop | Vender todo ya | ALTA |
| 2 | Noticia grave | Noticia tipo C21 sobre una posición abierta (la IA la confirma) | Vender todo | ALTA |
| 3 | Objetivo 1 | Precio ≥ objetivo 1 | Vender 50% y subir el stop al precio de entrada | MEDIA |
| 4 | Stop móvil | Después de +1.5R: stop = máximo desde la entrada − 2.5 ATR, si queda más arriba | "Sube tu stop a $X" | BAJA |
| 5 | Objetivo 2 | Precio ≥ objetivo 2 | Vender el resto | MEDIA |
| 6 | Resultados | Reporta hoy después del cierre o mañana antes de la apertura | Vender antes del cierre de hoy (salvo `exits.hold_through_earnings: true`) | MEDIA |
| 7 | Tesis invalidada | Se cumple la condición de invalidación del plan (código si es numérica, IA si es cualitativa) | Vender | MEDIA |
| 8 | Régimen | El mercado pasa a RIESGO ALTO y la posición va por debajo de +1R | Subir el stop a 1 ATR o vender 50% | MEDIA |
| 9 | Tiempo | 5 días hábiles sin llegar a +0.5R | Vender al cierre | BAJA |
| 10 | Horizonte | Se cumplió `horizonte_dias` | Vender | BAJA |
| 11 | Rotación | Hay una idea mucho mejor y no hay efectivo (sección 10) | Sugerir vender la posición más débil | BAJA |

Regla de oro: **el stop solo sube, nunca baja.** Ni la IA ni el código pueden aflojarlo.

**Contenido de una alerta de venta**

- Ticker, regla que se activó y explicación en una frase.
- Precio actual con fuente y hora; ganancia o pérdida en $, % y R.
- Acción sugerida, urgencia y qué pasa si no haces nada.
- Botones: \[Vendí\] \[Ya subí el stop\] \[Ignorar (motivo)\].

**Comportamiento**

- Una alerta por posición y regla hasta que se resuelva. Las ALTA se repiten cada 5 minutos, máximo 3 veces, con sonido en el dashboard.
- "Vendí" pide precio, cantidad y hora; cierra (o reduce) la posición y calcula R real y ganancia neta de comisiones.
- Si no respondes a una alerta ALTA, la posición real sigue ABIERTA con la marca "pendiente de tu confirmación". El sistema nunca supone que vendiste.
- Las SOMBRAS siguen las mismas reglas con ejecuciones virtuales: el stop se llena al precio del stop, o al de apertura si hubo gap.
- La IA revisa una posición solo con los disparadores de la sección 9 y responde MANTENER, VENDER o SUBIR STOP, con motivo.

## 13. Sistema de aprendizaje y calibración

El sistema aprende con hipótesis falsables: la IA propone, el código cuenta casos y la base de datos decide cuándo una lección puede influir. Es la parte "Lo que va aprendiendo / Lo que ha aprendido" de la interfaz del video, más el arreglo del bug que allí tuvieron (lecciones confirmadas que no se aplicaban).

**Ciclo de vida de una lección**

1. **Proponer (cada noche, IA).** El código arma un resumen del día: operaciones cerradas reales y sombra, decisiones con su resultado, ideas NO HOY que habrían llegado a +2R (oportunidades perdidas) y errores. El prompt `prompts/lesson_writer.md` devuelve de 0 a 3 hipótesis en JSON: tipo (patrón, error propio o razonamiento), enunciado, prueba falsable, ámbito y efecto propuesto.
2. **Registrar (código).** Se descartan duplicados de ámbito parecido y se guarda como SUPOSICIÓN. Máximo 3 nuevas por día.
3. **Evaluar (código, nunca la IA).** Cada noche cuenta los casos que caen en el ámbito y en cuántos se cumplió la prueba. Confianza = límite inferior de Wilson al 95% de esa proporción. Con 20 casos, eso exige al menos 17 aciertos para llegar a 0.60.
4. **Activar (base de datos).** Pasa a EN VIGOR solo con ≥ 20 casos, ≥ 10 de ellos posteriores a la fecha de la suposición (evita el sesgo de mirar atrás) y confianza ≥ 0.60. Lo impone un `CHECK` de la tabla.
5. **Aplicar.** Según su efecto: texto inyectado en el dossier (filtrado por ámbito), ajuste del peso de un componente del filtro (±5 puntos por lección, ±15 máximo por componente) o un control nuevo tipo R o B. Cada decisión guarda `lessons_applied`.
6. **Retirar.** Si la confianza de los últimos 20 casos baja de 0.50, pasa a RETIRADA (se conserva el historial). Tú puedes retirar o fijar una lección a mano.
7. **Límite:** máximo 15 lecciones en vigor a la vez.

**Ejemplo de prueba falsable** (estructura que la IA debe llenar):

```json
{
  "statement": "Las RUPTURAS en régimen RIESGO ALTO terminan en stop con más frecuencia que en objetivo.",
  "scope": { "setup": "RUPTURA", "regime": "RIESGO_ALTO" },
  "test": { "metric": "hit_stop_before_target", "holds_if": "== true" },
  "effect": { "type": "screener_weight", "component": "setup_quality", "delta": -5 },
  "min_cases": 20
}
```

**Restricción en la base de datos** (el mínimo no depende del código que escribe lecciones):

```sql
CREATE TABLE lessons (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('pattern','own_error','reasoning')),
  statement        TEXT NOT NULL,
  scope            TEXT NOT NULL,   -- JSON
  test             TEXT NOT NULL,   -- JSON
  effect           TEXT NOT NULL,   -- JSON
  status           TEXT NOT NULL CHECK (status IN ('hypothesis','active','refuted','retired')),
  cases_total      INTEGER NOT NULL DEFAULT 0,
  cases_forward    INTEGER NOT NULL DEFAULT 0,
  confidence       REAL NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 1),
  proposed_at      TEXT NOT NULL,
  activated_at     TEXT,
  CHECK (status <> 'active'
         OR (cases_total >= 20 AND cases_forward >= 10 AND confidence >= 0.60))
);
```

**Calibración de la IA**

- Cada idea guarda la probabilidad de la IA (p) y su resultado (1 si tocó el objetivo 1 antes que el stop; 0 si no). Cuentan reales y sombras.
- **Brier score** = promedio de (p − resultado)². Menor es mejor; decir siempre 0.5 da 0.25. Va en la cabecera como "Calibración".
- Diagrama de confiabilidad por tramos de p (0.3–0.4, 0.4–0.5, …) en la página de Rendimiento.
- Antes de 50 resultados, el riesgo usa una p encogida: p\_cal = 0.5 + 0.5 × (p − 0.5). Con ≥ 50 se ajusta una calibración isotónica y se re-entrena cada semana.

**Tests obligatorios de esta parte**

- Insertar una lección `active` con 19 casos falla en la base de datos.
- Una lección en vigor con ámbito que coincide aparece en el dossier y en `lessons_applied` de la decisión.
- Una lección retirada deja de aparecer al día siguiente.

## 14. Dashboard en vivo

El dashboard copia la idea del video (cabecera de métricas, flujo de nodos en vivo, lo que piensa la IA, lo aprendido) y agrega lo que necesitas tú: el Plan del día con botones, tus posiciones y las alertas de venta. Todo en español, tema oscuro y listo para compartir pantalla.

**Cabecera (siempre visible)**

| Elemento | Qué muestra |
| --- | --- |
| Nombre + estado | NORMAL, PRECAUCIÓN, SOLO VIGILAR, PAUSA, SIN CUOTA IA o DATOS CAÍDOS, con color |
| Modo | Interruptor "Solo vigilar" (no propone compras, solo gestiona posiciones) / "Recomendar" |
| Agresividad | Control deslizante 1–10 (de "prudente" a "agresivo"), aplica las perillas de la sección 10 |
| Capital | Efectivo + posiciones, en USD (y MXN al pasar el cursor) |
| Hoy | % del día, y vs SPY |
| Caída | Drawdown desde el máximo |
| Ideas | Seguidas / propuestas hoy |
| Consultas IA | Usadas / límite del día |
| Calibración | Brier score |
| Costo/op | Costo promedio por operación en R |
| Reloj | Hora de Nueva York y de Monterrey; estado del mercado (abierto, premercado, cerrado) |
| Ojo | Modo presentación: oculta montos en $ y deja %, R y razones |
| Parar | Pone el sistema en PAUSA (pide confirmación) |

**Paneles de la vista principal** (de arriba abajo)

1. **Banner de estado** en lenguaje simple: "Operando con normalidad: vigila el mercado y propone compras cuando una idea supera todos los controles." Si hay un problema, dice cuál y qué hacer.
2. **Alertas de venta** (solo si hay): tarjetas rojas o ámbar con botón \[Vendí\]. Siempre arriba de todo.
3. **Cómo fluye la información (EN VIVO)**: grafo de React Flow. A la izquierda las fuentes (Alpaca, Finnhub, FRED, SEC, RSS, calendario); luego las capas (Mercado, Volatilidad, Sentimiento, Noticias, Macro, Calendario, Eventos) con latencia en ms y hora de la última actualización; luego el Motor de datos; abajo el **recorrido de una decisión**: Indicadores (\~45 métricas, N tickers) → Filtro (N candidatos) → Controles (22 comprobaciones, N pasan) → Analista IA (decide qué) → Motor de riesgo (decide cuánto) → Plan del día (te avisa) → Posición (vigilando N). Las aristas se animan cuando pasan datos. Clic en un nodo abre un panel lateral con sus datos (tabla + JSON crudo).
4. **Plan del día**: una tarjeta por idea con todos los campos de la sección 11 y los botones \[La compré\] \[No la compré\]. Botón "Explícame" que pide a la IA una explicación más didáctica.
5. **Qué está pensando la IA**: la última lectura del mercado y la tesis por ticker; también el "por qué no" de lo descartado.
6. **Controles de seguridad**: rejilla candidato × 22 controles con pass, warn o fail y el motivo al pasar el cursor.
7. **Tus posiciones abiertas**: precio en vivo, ganancia en $, % y R, barra de distancia al stop y al objetivo, días en posición, stop vigente (con aviso "actualízalo en tu broker" si subió) y botón \[Vendí\].
8. **Curva de capital**: TradingView Lightweight Charts con tres líneas normalizadas a 100: tu cuenta real, el sistema (reales + sombras) y SPY. Marcas de compra en azul y ventas en verde o rojo.
9. **Operaciones cerradas**: hora, ticker, resultado en $ y R, motivo (saltó el stop, alcanzó objetivo, stop de tiempo, decisión de la IA, decisión tuya) y si fue real o sombra.
10. **Lo que va aprendiendo / Lo que ha aprendido**: dos columnas como en el video. Cada suposición muestra tipo, enunciado, origen, casos actuales vs 20 necesarios, confianza vs mínimo 0.60 y barra de progreso. Al pasar el cursor: "No influye en ninguna decisión. Para empezar a hacerlo necesita 20 casos observados y una confianza de al menos 0.60. Ese mínimo lo impone la base de datos." Cada lección en vigor muestra fecha de suposición, fecha de confirmación y en cuántas decisiones ha entrado.

**Otras páginas**

- **Gráfica por ticker**: velas diarias y de 5 min con líneas de entrada, stop y objetivos.
- **Rendimiento**: tasa de acierto, expectativa en R, profit factor, caída máxima, Brier y diagrama de confiabilidad, resultados por setup y por régimen, ideas seguidas vs no seguidas, todo vs SPY.
- **Historial**: todas las ideas con su dossier, respuesta de la IA y controles (auditoría).
- **Configuración**: capital y efectivo, agresividad, universo y lista SIC, horizonte, comisiones, notificaciones, presupuesto de IA, estado de cada API (sin mostrar las keys).
- **Registro**: eventos del sistema en orden, filtrables.
- Pie de página con el enlace de atribución a financecalendar.com (lo exige su licencia).

**Diseño**

- Tema oscuro azul marino, números en fuente monoespaciada, verde para ganancias, rojo para pérdidas, ámbar para suposiciones y advertencias.
- Responsive: en el celular se ven primero las alertas, el Plan del día y tus posiciones.
- Acceso con contraseña y HTTPS; nada del dashboard queda público.

## 15. Base de datos

Una sola base (SQLite en WAL, migraciones con Alembic) guarda datos, decisiones, confirmaciones y lecciones; todo lo que la UI muestra sale de aquí.

| Tabla | Contenido clave |
| --- | --- |
| `universe` | ticker, nombre, sector, bolsa, `sic_listed`, activo, actualizado |
| `raw_snapshots` | fuente, hora, latencia ms, estado, payload crudo (30 días) |
| `bars_daily`, `bars_intraday` | OHLCV ajustado por splits |
| `news` | tickers, publicado, fuente, titular, resumen, URL, sentimiento, categoría, `severe` |
| `macro_points`, `econ_events`, `corporate_events` | series de FRED, calendario económico, earnings, filings |
| `indicator_values` | ticker, as\_of, métrica, valor |
| `runs` | tipo (premarket, intraday, close, replay), inicio, fin, régimen, estadísticas |
| `candidates` | run, ticker, setup, puntaje, desglose (JSON) |
| `control_results` | candidato, control (C01–C22), resultado, motivo |
| `ai_calls` | tarea, modelo, hash del dossier, ruta del dossier, respuesta cruda, `structured_output`, uso, costo, duración, estado |
| `ideas` | veredicto, probabilidad cruda y calibrada, zona de entrada, stop, objetivos, acciones, monto, riesgo $, EV\_R, tesis, razones, riesgos, invalidación, `lessons_applied`, estado |
| `positions` | idea, `is_shadow`, ticker, cantidad abierta, precio promedio, stop vigente, objetivos, R inicial, estado, apertura, cierre |
| `fills` | posición, lado, precio, cantidad, hora, comisión, origen (usuario o virtual) |
| `confirmations` | qué confirmaste (compré, no compré, vendí, subí stop), datos y nota |
| `alerts` | posición, regla, urgencia, mensaje, creada, resuelta, cómo |
| `state_transitions` | entidad, id, de, a, hora, actor (sistema o usuario), motivo |
| `lessons`, `lesson_cases` | lecciones (sección 13) y cada caso evaluado (cumple sí/no, posterior sí/no) |
| `metrics_daily` | capital real, capital del sistema, cierre de SPY, P&L, caída, Brier |
| `system_status` | componente, estado, mensaje, actualizado (alimenta el banner) |

Reglas: claves foráneas activadas; las transiciones de estado solo se escriben a través de la máquina de estados; respaldo diario de la base a un archivo con fecha (se conservan 14).

## 16. Calendario diario de operación

El Plan del día sale a las 9:00 de Nueva York, que en invierno son las 8:00 en Monterrey. El scheduler se programa en `America/New_York` y la UI convierte. Monterrey no cambia de horario; Nueva York sí (horario de verano del 14 de marzo al 7 de noviembre de 2027).

| Hora NY (ET) | Monterrey, nov–mar | Monterrey, mar–nov | Tarea |
| --- | --- | --- | --- |
| 06:00 | 05:00 | 04:00 | Colectores nocturnos: noticias, filings, calendario, macro, barras del día anterior |
| 07:30 | 06:30 | 05:30 | Motor de datos, indicadores, régimen, filtro y controles |
| 08:30 | 07:30 | 06:30 | Si hay dato macro de alto impacto a esta hora (CPI, empleo), esperar y recalcular el contexto |
| 08:45 | 07:45 | 06:45 | Analista IA sobre los finalistas; motor de riesgo |
| 09:00 | 08:00 | 07:00 | Se publica el Plan del día (dashboard + Telegram) |
| 09:30 | 08:30 | 07:30 | Abre el mercado; el vigilante pasa a tiempo real |
| 09:45 | 08:45 | 07:45 | Revisión tras la apertura: gaps, "no perseguir", disparadores de VIGILAR |
| 09:45–15:30 | 08:45–14:30 | 07:45–13:30 | Vigilancia continua, noticias en tiempo real y alertas |
| 15:30 | 14:30 | 13:30 | Revisión de cierre: resultados de mañana, stops de tiempo, "vender antes del cierre" |
| 16:15 | 15:15 | 14:15 | Cierre: resultados del día, sombras, métricas, respaldo y confirmaciones pendientes |
| 17:00 | 16:00 | 15:00 | Revisión nocturna: lecciones, calibración y resumen por Telegram |
| Sábado 10:00 | 09:00 | 08:00 | Resumen semanal, recalibración y reconstrucción del universo |

Los feriados y las sesiones cortas (cierre a las 13:00 ET) salen del calendario de NYSE; el scheduler nunca corre el Plan del día en un día sin mercado.

## 17. Fases de construcción y criterios de aceptación

Son 10 fases; Claude Code no empieza una fase sin tu "OK" de la anterior, y el modo replay llega en la Fase 3 para que todo lo demás se pruebe en minutos.

1. **Fase 0 — Cimientos**
   - Entregables: repo, `CLAUDE.md`, `config.yaml` validado con pydantic, `.env.example`, modelos y migraciones de la base, reloj (`clock.now()` real o simulado), calendario NYSE, logging estructurado, `docker-compose.yml`, `make dev` y `make test`.
   - Acepta si: `make test` pasa; una lección `active` con 19 casos es rechazada por la base; `clock` convierte bien ET ↔ Monterrey en fechas de invierno y verano.
2. **Fase 1 — Colectores**
   - Entregables: un colector por fuente de la sección 5 con limitador, caché, reintentos y estado; comando `python -m collectors.check`.
   - Acepta si: el check muestra cada fuente OK con su latencia; un test simula un 429 y el colector espera y reintenta sin pasarse del límite; sin keys, el sistema arranca y marca esa fuente como "sin configurar".
3. **Fase 2 — Motor de datos e indicadores**
   - Entregables: esquemas normalizados, detección de datos viejos, `MarketState`, las \~45 métricas y el régimen.
   - Acepta si: cada indicador tiene test contra valores conocidos; 500 tickers se calculan en < 60 s; en modo replay ningún dato tiene hora posterior al reloj.
4. **Fase 3 — Filtro, controles y modo replay (sin IA)**
   - Entregables: puntaje con desglose, setups, los 22 controles, Plan del día con veredictos por reglas, comando `python -m replay --from 2026-05-01 --to 2026-10-30`.
   - Acepta si: un día de replay corre en < 30 s; cada control tiene un test que lo hace fallar y otro que lo hace pasar; el replay produce un reporte con ideas, sombras y resultado vs SPY.
5. **Fase 4 — Analista IA**
   - Entregables: constructor de dossier, wrapper de `claude -p` con esquema, validación posterior, presupuesto, caché, IA simulada para tests y replay, prompts en `prompts/`.
   - Acepta si: una respuesta inválida provoca un reintento y luego el modo "solo reglas"; el presupuesto corta en el límite; un stop inventado fuera de tolerancia se corrige o se descarta; costo y uso quedan en `ai_calls`.
6. **Fase 5 — Riesgo, Plan del día y confirmaciones**
   - Entregables: tamaño de posición, filtro de expectativa, reparto del efectivo, rotación, frenos automáticos, endpoints de "La compré / No la compré", sombras, máquina de estados.
   - Acepta si: con capital de $5,000 la suma de montos nunca supera el efectivo; ningún tamaño rompe los topes de agresividad; el plan siempre lista ≥ 5 ideas; una transición inválida pone PRECAUCIÓN.
7. **Fase 6 — Vigilante y alertas**
   - Entregables: WebSocket de precios, las 11 reglas de salida, alertas en Telegram con botones, flujo "Vendí", recordatorio de confirmaciones pendientes.
   - Acepta si: en replay, trayectorias de precio sintéticas disparan stop, objetivo, stop móvil y stop de tiempo en el momento correcto; no hay alertas duplicadas; tras reiniciar el servidor, el vigilante retoma las mismas posiciones sin perder estado.
8. **Fase 7 — Dashboard**
   - Entregables: todos los paneles y páginas de la sección 14, WebSocket en vivo, modo presentación, contraseña.
   - Acepta si: carga en < 2 s; un cambio en la base se ve en < 2 s sin recargar; en el celular se ven primero alertas, plan y posiciones; el modo presentación no muestra ningún monto en $.
9. **Fase 8 — Aprendizaje y métricas**
   - Entregables: revisión nocturna, evaluador de lecciones, calibración, página de Rendimiento con benchmark.
   - Acepta si: pasan los 3 tests obligatorios de la sección 13; el Brier y la curva vs SPY coinciden con un cálculo manual sobre datos de prueba.
10. **Fase 9 — Despliegue y periodo en papel**
    - Entregables: despliegue en VPS (Docker + Caddy + HTTPS), respaldos, alerta si el servidor se cae, al menos 4 semanas siguiendo las ideas sin dinero real.
    - **Puerta para usar dinero real** (todas): ≥ 50 ideas cerradas entre reales y sombras; expectativa ≥ +0.10R después de costos; caída máxima < 10%; Brier < 0.24; dos semanas sin incidentes de estado sin resolver; reporte honesto vs SPY. Aun así, empieza con un capital pequeño.

## 18. Errores del bot del video y cómo los evita VÉRTICE

El propio autor del video documentó seis problemas en su semana de prueba; cada uno tiene aquí una defensa concreta en el diseño.

| Lo que pasó en el video | Cómo lo evita VÉRTICE |
| --- | --- |
| Día 1: abrió una operación de Bitcoin, luego vio una posición abierta que "no recordaba" y se bloqueó 2 horas | Una sola fuente de verdad (la base), máquina de estados con tests y conciliación diaria. Una posición real solo existe si tú la confirmas. Una inconsistencia pone PRECAUCIÓN con un botón para resolverla, no un bloqueo mudo |
| Día 2: se quedó sin cuota diaria de Claude durante horas. Día 6: llegó al límite semanal y tuvo que comprar otra cuenta | La IA solo ve a los finalistas (máximo 10, en una sola llamada), hay presupuesto diario con reserva para posiciones, caché y modelo barato para noticias. Sin cuota, el sistema sigue con reglas y el vigilante no depende de la IA |
| Día 3: las lecciones aparecían como aprendidas pero no se aplicaban | Cada decisión guarda `lessons_applied`; hay un test de integración que lo verifica; la UI muestra en cuántas decisiones entró cada lección |
| Cada arreglo tardaba un día en comprobarse (3–4 operaciones diarias); fueron 1.5 semanas de prueba y error | Modo replay desde la Fase 3 y trayectorias sintéticas para el vigilante: un cambio se prueba en minutos |
| Ganó 13% en 7 días con 35 operaciones, pero Bitcoin subió 21.2% en el mismo periodo; en una simulación larga no era rentable | Benchmark diario contra SPY, sombras para medir el sistema sin tu filtro, puerta de ≥ 50 ideas cerradas antes de usar dinero real |
| Las primeras operaciones fueron pérdidas y la IA cometió errores al programar el bot | Ninguna lección cambia nada con menos de 20 casos; cada fase tiene tests y criterios de aceptación antes de avanzar |

La conclusión del propio video es la regla de evaluación de este proyecto: una semana buena no demuestra nada; lo que cuenta es la expectativa después de costos sobre muchas operaciones, comparada con simplemente comprar el índice.

## 19. Archivo de configuración de ejemplo

Todos los umbrales del documento viven en `config/config.yaml`; la UI de Configuración edita este archivo y guarda cada versión con fecha.

```yaml
system:
  name: VERTICE
  mode: recommend            # recommend | watch_only | paused
  aggressiveness: 5          # 1..10 (sección 10)
  timezone_market: America/New_York
  timezone_user: America/Monterrey

capital:
  base_currency: USD
  cash_usd: 5000             # lo capturas tú y lo actualizas al depositar o retirar
  show_mxn: true
  fractional_shares: false

costs:
  commission_pct: 0.25       # pon la de tu casa de bolsa
  vat_on_commission_pct: 16
  slippage_pct: 0.05

universe:
  exchanges: [NYSE, NASDAQ]
  sic_only: false
  sic_list_path: config/sic_universe.csv
  min_price: 5
  min_dollar_volume_20d: 20000000
  min_market_cap: 1000000000
  exclude: [leveraged_etf, inverse_etf, spac, warrant, otc]

screener:
  top_n_to_controls: 25
  max_finalists: 10
  volatility_preference: 0.7   # 0..1; 1 = favorece las más volátiles
  atr_pct_sweet_spot: [2.5, 8.0]

regime:
  vix_bullish_max: 20
  vix_high_risk_min: 28
  breadth_bullish_min: 55

plan:
  min_ideas_listed: 5
  publish_time_et: "09:00"
  recheck_time_et: "09:45"

entry:
  skip_first_minutes: 15
  order_type: limit

exits:
  target1_sell_pct: 50
  move_stop_to_breakeven_after_t1: true
  trailing_start_r: 1.5
  trailing_atr_mult: 2.5
  time_stop_days: 5
  time_stop_min_r: 0.5
  hold_through_earnings: false

controls:
  earnings_block_days: 3
  macro_event_block_hours: 6
  max_gap_atr: 2
  max_extension_atr: 3
  min_reward_r: 1.5
  max_sector_positions: 2
  max_sector_pct: 40
  max_correlation: 0.80
  cooldown_days_after_stop: 5
  daily_loss_stop_pct: 2
  weekly_loss_stop_pct: 5
  max_drawdown_pause_pct: 10

ai:
  enabled: true
  analyst_model: sonnet
  news_model: haiku
  lessons_model: sonnet
  daily_call_budget: 40
  reserve_for_positions_pct: 30
  max_usd_per_day: null      # solo si usas ANTHROPIC_API_KEY
  timeout_s: 180
  allow_web_search: false
  cache_minutes: 30

learning:
  min_cases: 20              # informativo: el mínimo real está en el CHECK de la base
  min_forward_cases: 10
  min_confidence: 0.60
  retire_below: 0.50
  max_active_lessons: 15
  max_new_per_day: 3

notify:
  telegram: true
  repeat_high_alerts_min: 5
  repeat_high_alerts_max: 3

fomc_dates: []              # cárgalas desde federalreserve.gov
```

## 20. Prompt inicial para pegar en Claude Code

Copia este texto completo en la primera sesión de Claude Code, abierta en la carpeta donde guardaste `ROADMAP.md`.

```text
Lee ROADMAP.md completo antes de escribir una sola línea de código. Es la especificación de VÉRTICE: un asesor de compra de acciones (solo largo) que NO ejecuta órdenes; yo compro y vendo a mano y confirmo en la UI.

Reglas de trabajo:
1. Crea primero CLAUDE.md con los 15 principios de la sección 3, copiados tal cual, más las convenciones de la sección 1.
2. Construye por fases, en el orden de la sección 17. Empieza SOLO por la Fase 0.
3. Al terminar cada fase: corre todos los tests, muéstrame cada criterio de aceptación con evidencia (salida de comandos), lista lo que quedó pendiente y DETENTE hasta que yo escriba "OK fase N".
4. Nunca escribas API keys en el código ni me las pidas en el chat: usa .env.example y yo lleno .env.
5. Si algo del roadmap es ambiguo o contradictorio, pregúntame antes de decidir. Si una API cambió sus límites o endpoints, dime qué encontraste y propón el ajuste en config.yaml.
6. La IA del sistema se invoca con `claude -p` en modo headless, salida JSON con --json-schema, desde runs/ai_sandbox/. En tests usa siempre la IA simulada.
7. Código y nombres en inglés; interfaz, mensajes y razonamientos en español.
8. Prioriza que funcione y sea verificable sobre que se vea bonito; el diseño del dashboard llega en la Fase 7.

Empieza: resume en 10 líneas lo que entendiste del sistema, propón el plan de la Fase 0 y espera mi OK.
```

Para las fases siguientes basta con: `Continúa con la Fase N del ROADMAP.md siguiendo CLAUDE.md.`

## 21. Advertencias

Trata a VÉRTICE como un experimento educativo sin ventaja demostrada hasta que sus propios números, en papel y contra SPY, digan lo contrario.

- **No es asesoría financiera.** Las ideas las genera un sistema automatizado y la decisión final es tuya.
- **El autor del video concluyó que su bot no era rentable a largo plazo** y que su buena semana fue suerte. Por eso este diseño mide todo contra el índice antes de arriesgar dinero.
- **Acciones volátiles = gaps.** Un stop no garantiza el precio: si la acción abre muy abajo, la venta se hace al precio de apertura. Esa es la razón del límite de riesgo por idea.
- **Ejecución manual = deslizamiento.** Habrá alertas que no podrás atender al momento. Usa órdenes stop en tu casa de bolsa cuando sea posible.
- **Datos gratuitos tienen límites:** el tiempo real de Alpaca es solo del exchange IEX y varios planes gratuitos son para uso personal. No conviertas esto en un servicio para terceros sin revisar licencias.
- **Cuota de Claude:** la automatización consume tu plan; revisa los límites de uso de tu suscripción antes de subir el presupuesto diario.
- **Impuestos y comisiones:** las ganancias por venta de acciones tienen efectos fiscales; tu casa de bolsa emite la constancia anual. Captura tus comisiones reales para que los resultados sean netos.
- **Privacidad:** el dashboard muestra tu capital y posiciones; mantenlo con contraseña y usa el modo presentación al compartir pantalla.
