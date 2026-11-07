# closebooks — conciliación bancaria de Grupo Valtermia (Talky × Anthropic, Madrid, 7 nov 2026)

Para cada movimiento bancario hay que decir **qué partidas abiertas liquida y por qué importe**. Si no liquida ninguna,
se indica su **categoría**, y en todos los casos la **excepción** cuando el dinero no cuadra. Se entrega un CSV y lo
puntúa el evaluador oficial:

`Score = 100 × (0,5·Allocation F1 + 0,3·Transaction accuracy + 0,2·Exception macro-F1)`, con tolerancia
max(0,01; 0,5 %). Referencias: ingenuo ≈19, heurística buena ≈61. **Nuestro motor: 85,99 en DEV (TRAIN 2024-H2)
y 85,68 en HOLDOUT (TRAIN 2025-H1)**, ventanas que reproducen LIVE; 84,55 en `sample` (sin historial).

Fuentes de verdad: `data/challenge/POLICIES.md` (reglas que sigue el golden **al pie de la letra**),
`FORMAT.md`, `DATA_DICTIONARY.md` y los 36 ejemplos (skill `bankrec-policies`).

## Comandos

```bash
uv sync && uv run pytest                      # 28 tests (+1 regresión que exige ≥84 en sample)
uv run closebooks info                        # qué fases hay descargadas
uv run closebooks run sample                  # motor + CSV + score oficial (~10 s, infravalora)
uv run closebooks run train --start 2024-07-01 --end 2024-12-31   # DEV: ventana tipo LIVE (~6 min)
uv run closebooks run train --start 2025-01-01 --end 2025-06-30   # HOLDOUT: confirmar, no iterar
uv run closebooks run live                    # genera out/live/submission.csv.gz y lo VALIDA (~7 min)
uv run closebooks report --start 2024-07-01 --end 2024-12-31   # dónde se pierden puntos
uv run closebooks diff --start 2024-07-01 --end 2024-12-31 --segment ar_partial --rule party_amount
uv run closebooks submit out/live/submission.csv.gz --phase LIVE --note "..." --dry-run
uv run closebooks history
uv run closebooks fetch <sub_id>               # vuelve a pedir el resultado de un envío (no gasta hueco)
```

## Arquitectura (`src/closebooks/bankrec/`)

```
data.py    carga una fase (sample/train/live/final) + masters; importes en céntimos (int);
           load_window(): ventana de TRAIN con saldos abiertos al inicio rehechos desde el golden
learn.py   categorías de líneas sin partida aprendidas de TRAIN (solo fechas ANTERIORES a lo evaluado)
text.py    normalización, claves de nº de documento (sin prefijo, sin ceros, sufijos), mes de servicio
book.py    Book: partidas abiertas con saldo vivo, índices por clave/nº/tercero; Parties: IBAN/mandato/nombre
docs.py    avisos de remesa (csv/txt/xlsx/pdf) y cláusulas de PDFs (sustituye, plazos, retención, cedido…)
rules.py   POLICIES §2-4: settle() reparte el cobro y decide excepción; FX; categorías por texto
engine.py  pasada cronológica: ficheros de detalle → categoría → devolución → referencias → tercero → fallback
output.py  CSV de entrega, explain.csv.gz (regla por línea), score/validate con el evaluador OFICIAL
submit.py  envío a la API real (upload-url → S3 → score → poll 5 s), con validación previa y log
```
El evaluador oficial vive en `data/challenge/starter/bankrec_score.py`: **no lo copies ni lo modifiques**.

## Reglas del proyecto

- **Importes en céntimos (`int`)**; `amount` del CSV en la **divisa de la partida**, siempre positivo (los abonos también).
- **Determinismo**: nunca iterar un `set` sin `sorted()`. Mismo input → mismo CSV, porque el FINAL es un solo tiro.
  Hay que comprobarlo con `PYTHONHASHSEED=1` y `=2`: el score tiene que coincidir.
- **Cada estrategia del motor deja `rule`** (va a `explain.csv.gz`). Sin eso no se puede depurar ni explicarlo al jurado.
- **Precisión > forzar**: un par erróneo resta en precisión Y en la exactitud de la transacción. Ante la duda,
  `UNIDENTIFIED` (cobros) o la categoría (cargos).
- **No sobreajustar**: nada de ids, nombres ni importes concretos. Cada regla debe poder justificarse con una frase de
  POLICIES.md o con un patrón que se repita en TRAIN.
- **Medir siempre con el evaluador oficial.** Iterar en la ventana DEV (2024-H2) y confirmar en HOLDOUT
  (2025-H1) antes de enviar a LIVE. `sample` solo como humo rápido: no tiene historial y engaña.
  Una regla que sube DEV y baja HOLDOUT no entra. Mide también la precisión por regla (una regla con
  precisión de pareja < ~0,85 suele restar).
- Hay drift en LIVE/FINAL (POLICIES §6): el banco Moncayo pasa a camt.053 el 2025-10-01, cambia el formato de los
  avisos de un cliente grande, llega una filial nueva ZA01 (ZAR, "KWI…") en FINAL y hay `final/masters_final/`.
- Nunca leer ni imprimir `.env` ni el token. `data/` y `out/` no se commitean.

## Cómo trabajar

Fallo concreto → `closebooks diff --segment X` → hipótesis → regla en `engine.py`/`rules.py` → test en
`tests/test_bankrec_rules.py` → `closebooks run sample` + `train` → si mejora sin regresiones, commit. Skills:
`bankrec-policies` (dominio), `new-matching-rule` (flujo), `dataset-onboarding` (datos nuevos o drift) y
`leaderboard-loop` (envíos y FINAL).
