---
description: Descarga o verifica una fase nueva, la pone en marcha y busca drift (sigue la skill dataset-onboarding)
argument-hint: "[fase]"
---

Fase: `$1` (si está vacía, `live`). Sigue la skill `dataset-onboarding`:
1. Comprueba que `data/challenge/<fase>/` existe y que el SHA del paquete descargado es el oficial.
2. Lanza el subagente `data-scout` sobre la fase para detectar formatos o columnas nuevos.
3. `uv run closebooks run <fase>` y revisa tiempos, distribución de reglas y validación.
4. Compara la distribución de reglas con la de `train`/`sample`: lo que se dispare es drift. Propón arreglos.
