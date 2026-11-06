---
name: leaderboard-loop
description: Disciplina de envíos al leaderboard de Talky - límites (1 cada 3 min, 20 al día por fase, pedir URL ya gasta hueco), validación previa con el evaluador oficial, registro de cada envío, iterar con TRAIN como juez y el protocolo del FINAL de un solo tiro (18:00 datos, 19:30 cierre). Úsala antes de cualquier envío, al analizar una puntuación de LIVE o al preparar el FINAL.
---

# Envíos: LIVE para aprender, FINAL de un solo tiro

## Reglas que no se negocian
- **Límites**: 1 envío cada 3 min y 20 al día **por fase**. **Pedir la URL de subida ya consume el hueco.**
- **Consultar como mucho cada 5 s**: toda la sala comparte IP. `closebooks submit` ya lo respeta.
- **FINAL**: un solo envío puntuado; un fichero rechazado por formato no cuenta y se puede reintentar. Empate → gana
  el envío más temprano.
- El token solo vive en `HK_TOKEN` (`.env`). Nunca en el código, en el chat ni en un commit.

## Antes de cada envío LIVE
1. `uv run pytest` en verde (incluye la regresión de sample).
2. `uv run closebooks run train` (o `sample` si vamos con prisa): el score local no baja respecto al último envío.
3. `uv run closebooks run live`: valida el fichero (sin errores y con el 100 % de movimientos cubiertos).
4. `uv run closebooks submit out/live/submission.csv.gz --phase LIVE --note "<qué cambió>" --dry-run`, y luego
   sin `--dry-run`. Queda registrado en `out/submissions/` y en `docs/SUBMISSIONS.md`.
5. **Un cambio por envío** siempre que se pueda. La puntuación LIVE trae el desglose por métrica y segmento: compáralo
   con TRAIN. Si un segmento va mucho peor en LIVE que en TRAIN → drift (skill `dataset-onboarding`).

## TRAIN es el juez, LIVE el termómetro
TRAIN tiene golden para cuatro años y el mismo evaluador. LIVE solo da un número cada 3 min. Itera en local con
`sample` (segundos) y `train` (minutos); usa LIVE para confirmar.

## Protocolo FINAL (18:00 → 19:30)
- **17:30 — congelar el código.** Etiqueta `pre-final`. Solo arreglos de robustez.
- **18:00** — descarga `final` y verifica el SHA (skill `dataset-onboarding`). Revisa `final/masters_final/`.
- `uv run closebooks run final` → tiempos, distribución de `rule`, validación OK y 100 % de movimientos cubiertos.
- Revisión rápida: ¿hay líneas `ZA01`/ZAR? ¿Cuántas acaban en `unidentified` frente a LIVE? ¿Hay `error:` en `rule`?
- `uv run closebooks submit out/final/submission.csv.gz --phase FINAL --note "final"` (pide escribir FINAL).
- **Objetivo: enviar antes de las 19:00.** Margen para un fichero rechazado. Cierre a las 19:30.
- **19:45**: entrega del proyecto (vídeo ≤ 5 min, repo de GitHub accesible para el jurado, descripción).
