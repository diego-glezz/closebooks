---
name: new-matching-rule
description: Flujo para mejorar el motor de conciliación closebooks - ver dónde se pierden puntos con `closebooks report`, inspeccionar fallos con `closebooks diff`, formular una regla generalizable, implementarla con test, medir en la ventana DEV de TRAIN (2024-H2) y confirmar en HOLDOUT (2025-H1), vigilando la precisión por regla y el determinismo. Úsala siempre que se pida "mejorar el score", "arreglar el segmento X", "casar Y" o "añadir una regla".
---

# Añadir o ajustar una regla de casación

## 1. Dónde están los puntos
`uv run closebooks run train --start 2024-07-01 --end 2024-12-31` (unos 6 min) y luego
`uv run closebooks report --start 2024-07-01 --end 2024-12-31`. El informe da tres tablas:
- **lost lines by segment**: líneas perdidas = n × (1 − acc). Prioriza por aquí.
- **exception classes**: F1 de cada clase. **Cada clase pesa 1/10 del 20 %**, así que subir una clase rara de 0,3
  a 0,8 vale ≈ 1 punto de score.
- **pair precision by rule**: una regla con precisión de pareja < ~0,85 suele restar más de lo que suma (además
  consume partidas que luego necesita otro cobro → errores en cadena).

## 2. Ver los fallos
`uv run closebooks diff --start 2024-07-01 --end 2024-12-31 --segment <seg> --rule <regla> --limit 10`.
Causas típicas (todas vistas en TRAIN):
- tercero mal resuelto (nombres truncados, sufijos legales, varios clientes con el mismo nombre) → `book.Parties`
- número no encontrado o encontrado de otro cliente (sufijos cortos, importes en el texto) → `text.py`, filtro de
  pagador en `_by_reference`
- importe o excepción mal → `rules.settle`, `deduction_kind`
- lote o aviso sin enlazar o mal enlazado → `_link_details`, `_from_advice`, `_link_confirming`
- partidas "muertas" (facturas de 2021 abiertas para siempre) → limitar búsquedas a partidas recientes

## 3. Regla
Escríbela en una frase citando POLICIES.md o un patrón medido en TRAIN. Ejemplos que funcionaron esta noche:
- deducción del aviso "DISCOUNT" → SHORT_DISCOUNT si va en plazo; "RET" → SHORT_WITHHOLDING
- confirming: un cargo por vencimiento (siguiente día hábil del último vencimiento de la transferencia)
- pagos de varias facturas sin referencia: subconjunto **único** de partidas recientes, con como mucho 5
  elementos (precisión medida: 0,99 con 2, 0,72 con 5 y 0,23 con 10 o más)
- planes de pago del PDF (plazos, importe a pagar tras retención o anticipo) → PARTIAL

## 4. Implementar
Test primero en `tests/test_bankrec_rules.py`. Regla dentro de la estrategia correcta del motor con un `rule="..."`
propio. Nada de `set` sin `sorted()`.

## 5. Medir
1. DEV (2024-H2): score + `report`. Si baja la precisión de alguna regla, investiga antes de seguir.
2. HOLDOUT (2025-H1), solo para confirmar: tiene que subir lo mismo. Si sube DEV y no HOLDOUT → sobreajuste.
3. `uv run pytest` y `sample` como humo. Si sube el umbral de regresión, actualízalo.
4. `PYTHONHASHSEED=1` y `=2` tienen que dar el mismo score.
