# Playbook — sábado 7 nov, La Nave

## Horario oficial
| Hora | Hito |
|---|---|
| 09:00 | Acreditación y café |
| 09:30 | Presentación del reto |
| **10:00** | **Arranca LIVE**: clasificación en directo (1 envío cada 10 s por fase; cuenta la mejor nota) |
| 14:00 | Comida |
| **18:00** | **Se publican los datos FINAL**: UN solo envío puntuado |
| **19:30** | **Cierre de envíos** |
| **19:45** | **Entrega del proyecto**: vídeo ≤ 5 min, repo de GitHub y descripción |
| 20:30 | Clasificación final |

## Estado de partida
Motor `closebooks`: **85,99 en DEV y 85,68 en HOLDOUT** (ventanas de TRAIN que reproducen LIVE) con el evaluador
oficial; la heurística de referencia ronda 61. El LIVE validado (100 % de líneas) está listo en
`out/live/submission.csv.gz`. Iterar en DEV y confirmar en HOLDOUT (skill `new-matching-rule`).

## Roles (cada uno en su rama/worktree, merge a `main` cada 1-2 h)
| Rol | Dueño de | Segmentos / métricas |
|---|---|---|
| **A. Datos, ejecución y envíos** | `data.py`, `output.py`, `submit.py`, `cli.py`; LIVE/FINAL; rendimiento; protocolo FINAL | que todo corra < 20 min, validación y drift |
| **B. Cobros por referencia y tercero** | `text.py`, `book.Parties`, `engine._by_reference/_by_party` | ar_ref_noise, ar_1toN_*, ar_same_amount_*, terceros (matriz, agencia, confirming), ar_new_iban |
| **C. Excepciones e importes** | `rules.settle`, `engine._returned`, plazos/retención/anticipo | PARTIAL, SHORT_*, OVERPAYMENT, DUPLICATE, RETURNED, FX (**macro-F1**) |
| **D. Lotes, proveedores, PDFs y categorías** | `docs.py`, `engine._link_details/_from_link/_from_advice`, `rules.category_by_text` | remesas, avisos, payment runs, confirming, factoring, nonitem_*, UNIDENTIFIED |

**Prompt de arranque para cada uno** (en Claude Code, en su worktree):
> Lee CLAUDE.md y la skill bankrec-policies. Mi rol es <X>. Con `closebooks report` y `closebooks diff` sobre la
> ventana DEV (2024-07-01 a 2024-12-31), propón las 3 reglas con más impacto en mis segmentos. No implementes hasta que te dé el OK;
> después sigue la skill new-matching-rule.

## Plan del día
- **09:00-10:00**: `git pull`, `uv sync`, `uv run pytest`, `closebooks run train --start 2024-07-01 --end 2024-12-31`
  y `closebooks report` (dónde está cada punto). A pone
  `HK_TOKEN` en el `.env` de todos.
- **10:00**: A lanza `closebooks run live` + el primer envío LIVE con el motor tal cual. Es el primer dato real.
- **10:00-17:30**: ciclos de mejora. Cada mejora se mide en train → merge → A envía a LIVE con `--note`.
  `docs/SUBMISSIONS.md` se rellena solo.
- **17:30**: congelar. `git tag pre-final`. Solo robustez. Ensayo: `closebooks run live` cronometrado.
- **18:00**: protocolo FINAL (skill `leaderboard-loop`). **Enviar antes de las 19:00.**
- **18:00-19:40, en paralelo, D (+ quien quede libre)**: vídeo y descripción (ver abajo). El repo tiene que ser
  visible para el jurado.

## Entregable para el jurado (19:45)
- **Vídeo ≤ 5 min a 720p**: problema → idea clave (libro de partidas cronológico con saldo vivo + cascada de
  estrategias explicables + POLICIES como código) → demo (`closebooks run`, `diff`, `explain.csv.gz`) → resultados
  (sample, train, LIVE) → cómo usamos Claude Code (skills, subagentes, hooks).
- **Descripción**: ideas clave, stack (Python, pandas, DuckDB, pypdf; Claude Code con Opus), qué haríamos después y
  miembros del equipo.
- **Repo**: README con cómo reproducir. Nada de datos ni tokens.

## Presupuesto de IA
El motor es determinista y no llama a ningún LLM. Si se añade un agente LLM (para PDFs o líneas ambiguas), usa
`llm/client.py` (con caché en disco), Haiku para volumen y solo sobre residuos (< 1 % de líneas).
