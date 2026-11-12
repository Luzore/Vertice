# Replay 2026-05-01 a 2026-10-30

Veredictos por reglas, sin IA. Cada idea COMPRAR se siguió como sombra con las reglas de salida, sobre barras diarias y con comisiones, IVA y deslizamiento.

## Resultado contra SPY

|  | Sistema (sombras) | SPY |
| --- | --- | --- |
| Capital inicial | $5,000 | $5,000 |
| Capital final | $4,788 | $5,258 |
| Rendimiento | -4.23% | +5.16% |
| Caída máxima | 7.25% | 4.49% |

Diferencia contra SPY: -9.39 puntos porcentuales.

## Ideas

- Días hábiles: 127. Ideas listadas: 1093 (mínimo por día: 5).
- COMPRAR: 92 · VIGILAR: 366 · NO HOY: 635.
- Días sin ninguna idea en COMPRAR: 57.
- De las ideas COMPRAR: 85 entró, 4 no ejecutable, 3 no entró.
- Régimen: ALCISTA 61 días, NEUTRAL 61 días, RIESGO ALTO 5 días.

## Sombras (operaciones virtuales)

| Grupo | Operaciones | Ganadoras | Promedio | Profit factor | Resultado neto |
| --- | --- | --- | --- | --- | --- |
| Todas | 85 | 40% | -0.10R | 0.76 | $-214 |

Mejor: +2.15R · Peor: -1.12R · Suma: -8.27R.

### Por setup

| Setup | Operaciones | Ganadoras | Promedio | Profit factor | Resultado neto |
| --- | --- | --- | --- | --- | --- |
| PULLBACK | 4 | 50% | -0.04R | 0.92 | $-4 |
| REBOTE | 1 | 0% | -0.24R | 0.00 | $-8 |
| RUPTURA | 80 | 40% | -0.10R | 0.76 | $-202 |

### Por régimen al entrar

| Régimen | Operaciones | Ganadoras | Promedio | Profit factor | Resultado neto |
| --- | --- | --- | --- | --- | --- |
| ALCISTA | 42 | 40% | -0.13R | 0.66 | $-169 |
| NEUTRAL | 41 | 39% | -0.07R | 0.89 | $-45 |
| RIESGO ALTO | 2 | 50% | +0.02R | 1.72 | $+1 |

### Motivo de salida

| Motivo | Operaciones |
| --- | --- |
| saltó el stop | 30 |
| se cumplió el horizonte | 23 |
| stop de tiempo | 22 |
| resultados: se vende antes del reporte | 6 |
| fin del periodo de prueba | 3 |
| abrió debajo del stop | 1 |

## Controles que más bloquearon

| Control | Ideas bloqueadas |
| --- | --- |
| C13 Recompensa vs riesgo | 330 |
| C15 Máximo de posiciones | 255 |
| C16 Concentración sectorial | 172 |
| C06 Spread | 89 |
| C04 Liquidez | 78 |
| C08 Resultados | 51 |
| C05 Tamaño de empresa | 35 |
| C14 Stop razonable | 21 |
| C10 Régimen | 21 |
| C12 Extensión | 4 |

## Tiempos

- Cargar los datos: 6.7 s.
- Por día: 9.2 s en promedio; el más lento, 10.5 s.

## Cómo leer este reporte

- **No demuestra una ventaja.** Sirve para probar que todas las piezas funcionan juntas.
- **El universo es el de hoy.** Solo incluye empresas que hoy cotizan y son líquidas, lo que favorece los resultados pasados.
- **Capitalización y sector son los actuales**, no los de cada fecha.
- **Sin noticias ni sorpresas de EPS históricas**: el componente de catalizador casi no puntúa y los controles de noticias no bloquean.
- **Barras diarias**: no se conoce el orden dentro del día. Si el stop y un objetivo se tocaron el mismo día, se asume que primero saltó el stop.
- **Veredictos por reglas**: falta el criterio de la IA (Fase 4) y el filtro de expectativa (Fase 5).
