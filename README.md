# closebooks — conciliación bancaria a escala

Solución del equipo para el **Claude Code Hackathon "Close the Books"** (Talky × Anthropic, Madrid, 7 nov 2026):
conciliar 1,9 M de movimientos bancarios de Grupo Valtermia con sus facturas, explicar las diferencias y marcar lo
que no se puede identificar.

**Enfoque:** un libro de partidas abiertas con saldo vivo, procesado en orden cronológico, y una cascada de
estrategias explicables, de la más segura a la menos:
1. ficheros de detalle (remesas SEPA, tarjetas, cheques, payment runs, avisos de remesa)
2. categorías sin partida
3. devoluciones
4. números de documento citados (ruidosos)
5. pagador + importe (FIFO, descuento, subconjuntos)
6. no identificado

Las reglas de `POLICIES.md` están escritas como código en `rules.settle()`. Cada línea guarda la regla que la
decidió (`explain.csv.gz`).

| Conjunto | Score (evaluador oficial) |
|---|---|
| TRAIN 2024-H2 (ventana DEV, igual que LIVE) | 85,99 |
| TRAIN 2025-H1 (ventana HOLDOUT) | 85,68 |
| sample (2025-06, sin historial) | 84,55 |
| LIVE | pendiente de enviar |

## Puesta en marcha (Windows/macOS/Linux)
```bash
uv sync
uv run pytest
cp .env.example .env            # HK_TOKEN (token del equipo) y, opcional, ANTHROPIC_API_KEY
```
Datos (no se versionan). Un solo comando los descarga, verifica el SHA-256 y los extrae en `data/challenge/`:
```bash
uv run python scripts/setup_data.py                                   # desde hackathon.usetalky.com
uv run python scripts/setup_data.py --from <carpeta-compartida> --cache <carpeta-compartida>/closebooks-pdf-cache.zip
uv run closebooks info                                                # sample/train/live deben salir OK
```
`--cache` instala el texto ya extraído de los PDFs (`out/cache/`) y ahorra minutos en la primera ejecución.
A las 18:00: `--final-sha <sha publicado>` añade el FINAL.

```bash
uv run closebooks run sample                       # motor + score oficial
uv run closebooks diff --segment ar_partial        # qué fallamos y por qué
uv run closebooks run live                         # CSV de entrega validado
uv run closebooks submit out/live/submission.csv.gz --phase LIVE --note "..." --dry-run
```

## Claude Code
`CLAUDE.md` (reglas del proyecto) · skills en `.claude/skills/` (`bankrec-policies`, `new-matching-rule`,
`dataset-onboarding`, `leaderboard-loop`) · subagentes en `.claude/agents/` (`failure-analyst`, `code-reviewer`,
`test-writer`, `data-scout`, `reconciliation-auditor`) · comandos `/run-eval`, `/pre-submit`, `/explore-data`,
`/cleanup`, `/handoff` · hook de ruff en `.claude/settings.json`. Organización del día: `docs/PLAYBOOK.md`. El reto explicado en sencillo: `docs/RETO.md`.

## Estructura del repo

```
CLAUDE.md                  reglas del proyecto para Claude Code (léelo primero)
README.md                  este fichero
pyproject.toml, uv.lock    dependencias (uv sync)
.env.example               plantilla de .env: HK_TOKEN del equipo y, opcional, ANTHROPIC_API_KEY
scripts/setup_data.py      descarga/copia + SHA-256 + extracción de los datos del reto

src/closebooks/
  cli.py                   comandos: run, score, diff, report, submit, history, info
  submit.py                envío al leaderboard (validación previa, límites, log en docs/SUBMISSIONS.md)
  config.py, money.py      configuración desde .env; importes
  llm/client.py            cliente de Claude con caché en disco (el motor no lo usa por defecto)
  audit/trail.py           registro append-only de decisiones
  bankrec/                 EL MOTOR
    data.py                carga de fases (sample/train/live/final) y masters; importes en céntimos
    text.py                normalización de textos y claves de nº de documento
    book.py                libro de partidas abiertas con saldo vivo; terceros por IBAN/mandato/nombre
    docs.py                avisos de remesa (csv/txt/xlsx/pdf) y cláusulas de los PDFs
    rules.py               POLICIES como código: reparto del cobro y excepción
    learn.py               categorías de líneas sin partida aprendidas de TRAIN
    engine.py              pasada cronológica con la cascada de estrategias
    output.py              CSV de entrega, explain.csv.gz y evaluador oficial
    analysis.py            dónde se pierden puntos (por segmento, excepción y regla)

tests/                     reglas del motor, submit con red simulada y regresión (≥84 en sample)
docs/                      PLAYBOOK (roles y horario), PROMPTS, SUBMISSIONS (registro de envíos),
                           STATUS (estado por rama, /handoff), DATASET_NOTES
.claude/                   skills, subagentes, comandos y hook de ruff (ver arriba)
```

**No se versiona** (`.gitignore`): `data/challenge/` (datos del reto: se montan con `scripts/setup_data.py`),
`out/` (resultados, cachés y modelos; se regeneran con `closebooks run`), `.env` (token), `.venv/`.
