---
description: Ejecuta el motor en una fase, puntúa con el evaluador oficial (sample/train) o valida (live/final) y compara con la ejecución anterior
argument-hint: "[fase]"
---

Fase: `$1` (si está vacía, `sample`).

1. `uv run pytest -q` — si falla, para y reporta.
2. Guarda el `out/<fase>/score.json` anterior (si existe) como línea base.
3. `uv run closebooks run <fase>`: muestra tiempos, distribución de reglas y SCORE o validación.
4. Compara con la línea base: score total, F1, txn_acc, macro-F1 y los 5 segmentos que más suben o bajan.
5. Resume en ≤ 8 líneas. Si hay regresión, propón lanzar el subagente `failure-analyst`.
