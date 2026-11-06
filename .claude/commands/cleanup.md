---
description: Limpieza de código sin cambiar el comportamiento (el CSV de sample tiene que salir idéntico byte a byte)
argument-hint: "[ruta]"
---

Limpia `$1` (si está vacía, `src/closebooks/bankrec`) SIN cambiar el comportamiento.

1. Línea base: `uv run closebooks run sample` y guarda `sha256sum out/sample/submission.csv.gz`.
2. Arregla código muerto, duplicación real (≥ 2 usos), funciones de más de 80 líneas, tipos en firmas públicas y
   constantes mágicas convertidas en parámetros con nombre.
3. No toques los nombres de `rule`, el formato del CSV ni la lógica de `rules.settle` (salvo legibilidad).
4. `uv run ruff check src tests`, `uv run pytest -q`, `closebooks run sample`: el sha256 del CSV tiene que ser
   IDÉNTICO. Si cambia, revierte esa parte.
