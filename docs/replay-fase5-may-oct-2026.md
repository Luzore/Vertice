# Replay de la Fase 5 (mayo a octubre de 2026)

Corrida del 13 de noviembre de 2026 con el motor de riesgo de la Fase 5: tamaño por convicción (sin IA, la mitad del riesgo base), racha de la cuenta y filtro de expectativa de +0.15R tras costos. Mismo periodo y mismos datos que el replay de la Fase 3, para poder compararlos.

| | Fase 3 | Fase 5 |
| --- | --- | --- |
| Rendimiento del sistema | -4.23% | +0.32% |
| SPY | +5.16% | +5.16% |
| Operaciones | 85 | 86 |
| Ganadoras | 40% | 48% |
| Promedio por operación | -0.10R | +0.03R |
| Profit factor | 0.76 | 1.05 |

Sigue sin demostrar una ventaja: el resultado queda 4.84 puntos por debajo de SPY y el promedio por operación es prácticamente cero.

---

# Replay 2026-05-01 a 2026-10-30

Veredictos por reglas, sin IA. Cada idea COMPRAR se siguió como sombra con las reglas de salida, sobre barras diarias y con comisiones, IVA y deslizamiento.

## Resultado contra SPY

|  | Sistema (sombras) | SPY |
| --- | --- | --- |
| Capital inicial | $5,000 | $5,000 |
| Capital final | $5,016 | $5,258 |
| Rendimiento | +0.32% | +5.16% |
| Caída máxima | 2.03% | 4.49% |

Diferencia contra SPY: -4.84 puntos porcentuales.

## Ideas

- Días hábiles: 127. Ideas listadas: 1116 (mínimo por día: 5).
- COMPRAR: 95 · VIGILAR: 386 · NO HOY: 635.
- Días sin ninguna idea en COMPRAR: 58.
- De las ideas COMPRAR: 86 entró, 5 no ejecutable, 4 no entró.
- Régimen: ALCISTA 61 días, NEUTRAL 61 días, RIESGO ALTO 5 días.

## Sombras (operaciones virtuales)

| Grupo | Operaciones | Ganadoras | Promedio | Profit factor | Resultado neto |
| --- | --- | --- | --- | --- | --- |
| Todas | 86 | 48% | +0.03R | 1.05 | $+15 |

Mejor: +2.22R · Peor: -1.23R · Suma: +2.88R.

### Por setup

| Setup | Operaciones | Ganadoras | Promedio | Profit factor | Resultado neto |
| --- | --- | --- | --- | --- | --- |
| CATALIZADOR | 1 | 0% | -1.04R | 0.00 | $-19 |
| PULLBACK | 4 | 25% | -0.58R | 0.28 | $-17 |
| REBOTE | 1 | 0% | -0.24R | 0.00 | $-2 |
| RUPTURA | 80 | 50% | +0.08R | 1.19 | $+53 |

### Por régimen al entrar

| Régimen | Operaciones | Ganadoras | Promedio | Profit factor | Resultado neto |
| --- | --- | --- | --- | --- | --- |
| ALCISTA | 38 | 34% | -0.12R | 0.67 | $-64 |
| NEUTRAL | 48 | 58% | +0.15R | 1.62 | $+79 |

### Motivo de salida

| Motivo | Operaciones |
| --- | --- |
| saltó el stop | 28 |
| stop de tiempo | 23 |
| se cumplió el horizonte | 21 |
| resultados: se vende antes del reporte | 5 |
| fin del periodo de prueba | 4 |
| abrió debajo del stop | 3 |
| alcanzó el objetivo 1 | 1 |
| alcanzó el objetivo 2 | 1 |

## Controles que más bloquearon

| Control | Ideas bloqueadas |
| --- | --- |
| C13 Recompensa vs riesgo | 340 |
| C15 Máximo de posiciones | 240 |
| C16 Concentración sectorial | 150 |
| C06 Spread | 97 |
| C04 Liquidez | 82 |
| C08 Resultados | 50 |
| C05 Tamaño de empresa | 35 |
| C10 Régimen | 21 |
| C14 Stop razonable | 19 |
| C12 Extensión | 6 |

## Tiempos

- Cargar los datos: 7.3 s.
- Por día: 10.5 s en promedio; el más lento, 13.6 s.

## Cómo leer este reporte

- **No demuestra una ventaja.** Sirve para probar que todas las piezas funcionan juntas.
- **El universo es el de hoy.** Solo incluye empresas que hoy cotizan y son líquidas, lo que favorece los resultados pasados.
- **Capitalización y sector son los actuales**, no los de cada fecha.
- **Sin noticias ni sorpresas de EPS históricas**: el componente de catalizador casi no puntúa y los controles de noticias no bloquean.
- **Barras diarias**: no se conoce el orden dentro del día. Si el stop y un objetivo se tocaron el mismo día, se asume que primero saltó el stop.
- **Motor de riesgo**: el tamaño usa la convicción (sin IA, la mitad del riesgo base), la racha de la cuenta y el filtro de expectativa de +0.15R tras costos. La pausa por caída de 10% no se simula, porque solo el usuario puede levantarla.
- **Veredictos por reglas**: falta el criterio de la IA, que solo se puede medir hacia adelante.
