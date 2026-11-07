---
name: leaderboard-loop
description: Disciplina de envíos al leaderboard de Talky - límites oficiales (1 envío cada 10 s y 100.000 al día por fase, pedir URL ya gasta hueco, consultar cada ≥ 5 s), validación previa con el evaluador oficial, registro de cada envío, iterar con TRAIN como juez y el protocolo del FINAL de un solo tiro (18:00 datos, 19:30 cierre). Úsala antes de cualquier envío, al analizar una puntuación de LIVE o al preparar el FINAL.
---

# Envíos: LIVE para aprender, FINAL de un solo tiro

## Reglas que no se negocian
- **Límites** (https://hackathon.usetalky.com/reglas): 1 envío cada 10 s y 100.000 al día **por fase**; archivo
  ≤ 60 MB y ≤ 3 M filas. **Pedir la URL de subida ya consume el hueco**, aunque luego no se suba el archivo.
- **Consultar como mucho cada 5 s**: toda la sala comparte IP. `closebooks submit` ya lo respeta.
- **LIVE**: cuenta la **mejor** puntuación; a igualdad, gana quien la consiguió antes. Enviar no se raciona, pero LIVE
  no decide nada: un cambio entra por TRAIN (DEV + HOLDOUT), no porque suba en LIVE.
- **FINAL**: un solo envío; cuenta el primer fichero válido y no se puede repetir. Un fichero con errores de formato
  no gasta el envío. Empate → gana el envío FINAL más temprano. Sin envío FINAL no se entra en la clasificación.
- El token solo vive en `HK_TOKEN` (`.env`). Nunca en el código, en el chat ni en un commit.

## Antes de cada envío LIVE
1. `uv run pytest` en verde (incluye la regresión de sample).
2. `uv run closebooks run train` (o `sample` si vamos con prisa): el score local no baja respecto al último envío.
3. `uv run closebooks run live`: valida el fichero (sin errores y con el 100 % de movimientos cubiertos).
4. `uv run closebooks submit out/live/submission.csv.gz --phase LIVE --note "<qué cambió>" --dry-run`, y luego
   sin `--dry-run`. Cada envío queda en `out/submissions/<hora UTC>_<FASE>/`: el fichero exacto enviado, su
   `explain.csv.gz`, todas las respuestas del servidor (`responses.jsonl`), `final.json` y `meta.json`; y una fila en
   `docs/SUBMISSIONS.md`. Si se corta la red esperando la nota, `closebooks fetch <sub_id>` la recupera sin gastar hueco.
5. **Un cambio por envío** siempre que se pueda. La puntuación LIVE trae el desglose por métrica y segmento: compáralo
   con TRAIN. Si un segmento va mucho peor en LIVE que en TRAIN → drift (skill `dataset-onboarding`).

## TRAIN es el juez, LIVE el termómetro
TRAIN tiene golden para cuatro años y el mismo evaluador. LIVE solo da un número por envío y no se puede depurar
contra él. Itera en local con `sample` (segundos) y `train` (minutos); usa LIVE para confirmar y para detectar drift.

## Protocolo FINAL (18:00 → 19:30)
- **17:30 — congelar el código.** Etiqueta `pre-final`. Solo arreglos de robustez.
- **18:00** — descarga `final` y verifica el SHA (skill `dataset-onboarding`). Revisa `final/masters_final/`.
- `uv run closebooks run final` → tiempos, distribución de `rule`, validación OK y 100 % de movimientos cubiertos.
- Revisión rápida: ¿hay líneas `ZA01`/ZAR? ¿Cuántas acaban en `unidentified` frente a LIVE? ¿Hay `error:` en `rule`?
- `uv run closebooks submit out/final/submission.csv.gz --phase FINAL --note "final"` (pide escribir FINAL).
- **Objetivo: enviar antes de las 19:00.** Margen para un fichero rechazado. Cierre a las 19:30.
- **19:45**: entrega del proyecto (vídeo ≤ 5 min, repo de GitHub accesible para el jurado, descripción).
