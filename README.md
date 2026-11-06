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
Datos (no se versionan): descarga los paquetes de https://hackathon.usetalky.com/datos, verifica el SHA-256 y
extráelos en `data/challenge/` (ver la skill `dataset-onboarding`).

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
`/cleanup`, `/handoff` · hook de ruff en `.claude/settings.json`. Organización del día: `docs/PLAYBOOK.md`.
