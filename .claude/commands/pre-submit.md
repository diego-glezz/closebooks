---
description: Checklist y envío a LIVE o FINAL (tests, ejecución, validación oficial, registro)
argument-hint: "[LIVE|FINAL] [nota]"
---

Fase: `$1` (si está vacía, LIVE). Nota del envío: `$2`. Sigue la skill `leaderboard-loop`.

1. `git status`: commitea lo pendiente con un mensaje claro y anota el hash.
2. `uv run pytest -q` en verde.
3. Si es LIVE: `uv run closebooks run train` (o sample) y compara con el último envío en `docs/SUBMISSIONS.md`.
4. `uv run closebooks run <live|final>`: validación OK y 100 % de movimientos cubiertos. Revisa las reglas `error:`.
5. `uv run closebooks submit out/<fase>/submission.csv.gz --phase <FASE> --note "<nota>" --dry-run`.
6. Dime el comando exacto para enviar **sin** `--dry-run` y espera a que lo confirmemos. Si es FINAL, recuerda que
   es un solo tiro.
