# Análisis del rol B (cobros por referencia y tercero): 7 nov 2026, ~14:45

Rama `rol-b-cobros-referencia-tercero` (60ed3d7 + cambios sin commitear de otra sesión: flags `pdf_exact`,
`pdf_dated`, `ref_cn_subset`). Todo medido con el evaluador oficial. Scripts de análisis en `out/rolb/` (no se
commitean). Ventanas: DEV = TRAIN 2024-07-01..12-31, HOLDOUT = TRAIN 2025-01-01..06-30.

**En 6 líneas**
1. El último envío **no subió: bajó** de 82,23 (986990d) a **81,95** (b71f626, rama `a-contexto-previo`, no está en main).
2. Ganó lo que se esperaba (plazos +761 líneas, retenciones +244, confirming +50) pero perdió más: los PDFs de TRAIN
   con "anula y sustituye" casan por **clave ruidosa** con facturas de LIVE y las marcan como canceladas
   (~4.600 partidas). Explica 2.854 de las 5.395 partidas que desaparecen del CSV.
3. Esta rama con los flags apagados da en LIVE un CSV **idéntico byte a byte** (descomprimido) al del 82,23. No se
   envía: el preflight no lo para (compara el `.gz`, cuya cabecera cambia), pero es gastar un hueco para nada.
4. Los flags de Adrián no mueven DEV (−0,24 … +0,07). No vale la pena lanzar el algoritmo genético.
5. **La palanca grande del rol B es otra:** el 90 % de lo que se pierde en `ar_ref_noise` son cobros sin referencia
   de **muchas facturas** (mediana 6) de un tercero ya identificado. Un subconjunto exacto **único** sobre todas las
   facturas recientes del tercero (sin el tope de 22 partidas y 5 elementos) acierta en el 71 % de los casos, sin
   ningún error en la muestra. Estimado: **+0,6 a +1,6 pts**.
6. Hay un **no determinismo** en `learn.py` (empates de categoría según el orden de filas de DuckDB): el modelo
   construido en este worktree difiere del de `claude-hackathon`. Afecta al FINAL (modelo nuevo, dos máquinas).

---

## 1. Por qué cambió el último envío (82,23 → 81,95)

| | 986990d (10:29) | b71f626 (12:28) | Δ |
|---|---|---|---|
| Score | 82,23 | **81,95** | −0,28 |
| Allocation F1 | 0,8449 | 0,8413 | −0,0036 |
| Precisión | 0,9926 | 0,9915 | −0,0011 |
| Recall | 0,7354 | 0,7306 | −0,0048 |
| Txn accuracy | 0,9043 | 0,8999 | −0,0044 |
| Exception macro-F1 | 0,6430 | 0,6442 | +0,0012 |
| Amount-weighted acc. | 0,8719 | 0,8491 | −0,0228 |
| Filas del CSV | 594.230 | 591.007 | −3.223 |

**Commits.** `git diff 986990d b71f626 --stat`: `data.py` (+76), `docs.py` (+23), `tests/test_prior_context.py`
(nuevo), `docs/SUBMISSIONS.md`. El único cambio de comportamiento es `data._with_prior_context`: LIVE carga de
`train/` las remesas SEPA, tarjetas y cheques de 62 días antes, los payment runs de 120 días, los avisos de 40 días y
los **PDFs de 550 días** (`docs.pdf_texts`/`load_advices` aprenden a leer de otra carpeta).

**Segmentos (n × Δacc = líneas ganadas o perdidas), de los 60 que devuelve el servidor:**

| Suben | n | acc | líneas | Causa (regla en explain) |
|---|---|---|---|---|
| ar_instalment | 4.700 | 0,754 → 0,916 | **+761** | `party_plan` 821 → 1.612 (+791): planes de plazos de PDFs de TRAIN (511 venían de `unidentified`, 269 de `category_learned_fallback`) |
| ar_retention | 1.000 | 0,709 → 0,953 | +244 | importes a pagar tras retención de PDFs de TRAIN (`party_plan`) |
| ap_confirming | 850 | 0,558 → 0,617 | +50 | 50 líneas `default_debit` → `link_run`: remesas de confirming de TRAIN |

| Bajan | n | acc | líneas | Causa |
|---|---|---|---|---|
| ar_ref_noise | 42.000 | 0,872 → 0,862 | −420 | `ref_set` → `unidentified`/`category_learned_fallback` (410 líneas): la factura citada está cancelada |
| ar_dd_batch | 1.600 | 0,955 → 0,768 | −299 | 316 líneas siguen en `link_dd` pero pierden 733 partidas y ganan 356 |
| ar_1toN_text | 6.500 | 0,850 → 0,818 | −208 | igual que ar_ref_noise |
| ar_1toN_remittance | 1.200 | 0,887 → 0,729 | −190 | 551 líneas `link_advice` pierden 956 partidas y no ganan ninguna |
| ar_1to1 | 19.000 | 0,997 → 0,989 | −152 | ídem |
| ar_short_dispute | 1.100 | 0,492 → 0,372 | −132 | ídem (avisos) |
| ar_same_amount_fifo / _month | 6.400 / 4.400 | −0,018 / −0,020 | −115 / −88 | `party_amount` → `unidentified` |
| ar_card_settlement | 1.300 | 1,000 → 0,919 | −105 | 102 líneas `link_card` pierden 171 partidas y no ganan ninguna |
| ar_1toN_credit_note | 3.300 | 0,481 → 0,459 | −73 | — |
| ar_cheque_deposit | 2.000 | 0,701 → 0,680 | −42 | 63 líneas `link_cheque` pierden 63 partidas |
| 17 segmentos más | | | −1 a −22 | |

`explain.csv.gz` de los dos envíos (guardados en `out/submissions/<hora>_LIVE/`): **2.301 líneas cambian de regla y
3.516 cambian de salida** (1.268 con la misma regla: link_advice 551, link_dd 316, party_amount 196, link_card 102,
link_cheque 63).

**Causa de las bajadas (verificada).** En los enlaces de lote la partida desaparece sin sustituta: no es que el lote se
enlace mal, es que la partida ya no está disponible. `engine._load_pdf_clauses` (`engine.py:109-145`) busca el número
del PDF y el de "anula y sustituye" con `book.find_number`, que, si no hay número exacto, prueba **claves ruidosas**
(`text.number_keys`: sin prefijo, sin año, últimos 6-7 dígitos). En LIVE las facturas de TRAIN ya cerradas no están
en el libro, así que el PDF de `RE24-0108497` cae en la `RE25-0108497` de LIVE. Recontado con
`out/rolb/verify_pdf.py` (PDFs de factura/abono AR de `train/` en la ventana de 550 días, 34.097, todos con texto en
caché):
- 4.597 partidas de LIVE quedan marcadas como **canceladas** (2.096 con el PDF nuevo exacto y el sustituido ruidoso;
  2.501 con los dos ruidosos);
- 1.424 reciben un **plan de plazos ajeno** y 679 un mes de servicio ajeno;
- de las 5.395 partidas que salen del CSV, **2.854 están entre las canceladas** y 2 entre las de plan ajeno.
  Las otras 2.541 no las puedo atribuir una a una: son compatibles con efectos en cadena (una partida cancelada
  hace que otro cobro elija otra), pero no lo he demostrado.

El macro-F1 de excepciones sube un poco (+0,0012), probablemente por los PARTIAL de los plazos; no lo he desglosado.

**Qué hacer con b71f626** (dueños A y D): no fusionar tal cual. Aplicar a las filas de fases anteriores (`_src`
no vacío) solo el número **exacto** (el `pdf_exact` de esta rama, pero limitado a esas filas, para que DEV no cambie).
`pdf_exact` en todas las filas **resta** en DEV (85,99 → 85,96) y HOLDOUT (85,68 → 85,66): pierde 185 y 171 parejas
correctas de `party_subset`. Se mide en LIVE: tiene que mantener ar_instalment ≈ 0,92 y devolver ar_dd_batch,
ar_card_settlement y ar_1toN_remittance a los valores del primer envío. Como comprobación local antes de enviar:
el número de partidas canceladas en LIVE debe ser ≈ el del motor base.

---

## 2. Estado del envío de esta rama

| Paso | Resultado |
|---|---|
| `uv run pytest -q` | verde: 30 passed, 1 skipped (la regresión de sample se salta porque `./data` no existe en el worktree) |
| `closebooks run live --data …` | validación OK, 594.230 filas, **203.083/203.083 líneas**; 8 m 32 s (load 25 s · setup 154 s · match 322 s, con otros 5 procesos en paralelo) |
| `submit … --dry-run` | `preflight OK: 594230 rows, 203083 bank lines, sha 5505ddd7bbbf` |
| ¿Igual a un envío anterior? | **Sí.** El CSV descomprimido (sha256 `5a27e61b…`) y el explain son idénticos byte a byte a los del envío 986990d (82,23). El `.gz` tiene otro sha por la cabecera gzip, y además el log del preflight es por carpeta (`out/submissions` relativo): en este worktree está vacío |

**No enviado.** Daría 82,23 otra vez. Si aun así se quiere (por ejemplo, para registrar la rama), el comando es:
`uv run closebooks submit out/live/submission.csv.gz --phase LIVE --data C:/claude-hackathon/data/challenge --note "rama B, flags apagados (60ed3d7)"`.

**Por qué el setup de DEV tardó 278 s y no ~76 s.** El worktree se creó sin `out/`: esa ejecución tuvo que
extraer el texto de todos los PDFs (la caché `out/cache/pdf_text_train_2024-07_2024-12.json` no existía) y construir
el modelo de categorías (`out/models/categories_2024-07-01.json`, escrito a las 13:25, durante la ejecución). Las
cachés se copiaron a las 13:46. Arreglo: enlazar `out/cache` y `out/models` a `C:/claude-hackathon/out/` como hace
`closebooks-a` (`out/cache -> /c/claude-hackathon/out/cache`).

**Dos fallos de A que he visto aquí:**
- `submit.preflight` (`submit.py:107-112`) compara el sha256 del `.gz`. Dos `run live` iguales dan `.gz` distintos,
  así que la guarda contra reenvíos no funciona. Debe comparar el sha del CSV descomprimido, y el log debería ser
  único (no `out/submissions` relativo a cada worktree).
- **No determinismo en `learn.py:42-70`**: la consulta DuckDB no tiene `ORDER BY` y `Counter.most_common(1)` desempata
  por orden de inserción. El `categories_2024-07-01.json` de este worktree y el de `claude-hackathon` difieren en
  empates CUSTOMER_ADVANCE/UNIDENTIFIED (p. ej. `+|NTRF||TT IN COX HYDRO CO LTD`). Con la caché no se nota, pero el
  FINAL construye un modelo nuevo (y el protocolo pide el mismo sha en dos máquinas). Arreglo de una línea:
  `top, top_n = min(cats.items(), key=lambda kv: (-kv[1], kv[0]))`. Medir: sample y DEV con la caché borrada dos
  veces, CSV idéntico.

---

## 3. Cómo subir más

### 3a. Los interruptores de Adrián, uno a uno (DEV; HOLDOUT cuando lo hay)

`scripts/exp_b.py`, evaluador oficial, precisión de pareja por regla. "prec. marginal" = parejas correctas
añadidas / parejas añadidas.

| Flag | DEV | HOLDOUT | Qué hace en la práctica | Veredicto |
|---|---|---|---|---|
| (base) | 85,99 | 85,68 | | |
| `iban_learn` | 86,00 | — | +3 parejas `ref_set` | nulo |
| `name_fuzzy` | 85,99 | — | ningún cambio | nulo |
| `ref_fuzzy` | 85,99 | — | `ref_fuzzy` 577 parejas (prec. 0,998), pero se las quita a `party_amount` (−511), `party_discount` (−45)… | nulo: mismas parejas, otra regla |
| `two_pass_party` | 86,04 | 85,70 | DEV: `party_subset` −213 (prec. sube 0,932 → 0,944). HOLDOUT: `party_amount` +147 parejas con **prec. marginal 0,55**, `party_subset` −264 (0,34) | no entra: +0,05/+0,02, por debajo del ruido mensual y con altas de baja precisión |
| `subset_max_items=8` (otra sesión) | 85,98 | — | `party_subset` +5.292 parejas con **prec. marginal 0,49**; P 0,9930 → 0,9883 | no |
| `subset_max_items=22` (otra sesión) | 85,75 | — | P 0,9814 | no |
| `ref_cn_subset` | 86,06 (+0,07) | **85,75 (+0,07)** | `ref_set_cn` DEV +1.364 parejas (prec. 0,92), HOLDOUT +1.373 (prec. 0,97); ar_1toN_credit_note +180 / +184 líneas; ar_instalment −24 / −4; los 12 meses suben | **entra** |
| `pdf_exact` | 85,96 | 85,66 | ver §1 | no en todas las filas; sí en las de fases anteriores |
| `pdf_dated` (claves difusas sí, pero nunca hacia partidas emitidas después de la fecha del PDF) | 85,98 | 85,67 | DEV/HOLDOUT casi neutro (−0,01). En LIVE con el código de b71f626 quita **3.548 de las 4.346** anulaciones de PDFs de TRAIN (quedan 798, cerca de los 754 "anula y sustituye" de A) y recupera las 4 partidas de ejemplo de §1. Test: `test_pdf_clauses_never_touch_invoices_issued_after_the_pdf` | arreglo candidato para b71f626; el juez es un envío LIVE (DEV no tiene la colisión entre fases) |

**Algoritmo genético:** no lo he lanzado. `ga_b.py --workers 8 --generations 3 --pop 16` son 48 evaluaciones de
13-19 min cada una (con 5-6 procesos a la vez; cada uno ocupa 3-4 GB y la máquina tiene 32 GB): **≈ 2 h solo DEV**,
más HOLDOUT. Con los flags midiendo ±0,05, no compensa.

### 3b. Dónde se pierden las líneas del rol B (DEV, `out/rolb/seg_fail.py`)

| Segmento | n | perdidas | de ellas "no asignamos nada" (`unidentified` + `category_learned_fallback`) | parejas erróneas |
|---|---|---|---|---|
| ar_ref_noise | 41.255 | ~5.460 | **5.205** | ~180 |
| ar_1toN_credit_note | 3.190 | ~1.650 | 1.368 | ~230 (sobre todo `ref_partial` que deja fuera el abono) |
| ar_n_to_m | 1.864 | ~1.340 | 1.011 | ~140 + 92 con excepción distinta |
| ar_partial | 3.775 | ~1.150 | 1.080 | ~15 + 64 con excepción distinta |
| ar_1toN_text | 6.221 | ~1.010 | 994 | ~16 |

El problema es de **recall**, no de precisión: casi todo lo perdido son cobros que el motor no se atreve a asignar.

**Patrón de ar_ref_noise** (`out/rolb/miss_examples.py`, 5.252 líneas perdidas): 4.724 (90 %) pagan **varias
facturas** (mediana 6, p75 9, máx. 43); en 5.019 (96 %) el nombre del tercero está en el texto o en
`counterparty_name`; solo 773 citan algún número de factura. Ejemplo típico: `DIRECT CREDIT WILSON POOL & SPA PT`,
50.345,53 AUD = 16 facturas del mismo cliente emitidas entre enero y abril. El golden no es "todas las abiertas" (1 de
4.724) ni "todas las vencidas" (6): elige un subconjunto, y el tercero tiene de media 37 facturas abiertas más.

**Por qué no lo cogemos hoy.** `_unique_subset_alloc` (`engine.py:1168-1195`) se queda con las **22 facturas más
recientes** del tercero (`subset_cap`) y acepta como mucho **5** (`subset_max_items`). Al recortar a 22, un
subconjunto "único" dentro del recorte puede no serlo en el total: por eso subir `subset_max_items` a 8 hunde la
precisión (0,49). El fallo está en el recorte, no en el tamaño.

**Medido con el estado abierto "perfecto" (golden) sobre 400 líneas perdidas de varias facturas**
(`out/rolb/subset_pool.py`, contando con programación dinámica en céntimos cuántos subconjuntos dan el importe):

| Conjunto de facturas del tercero | golden dentro | único y = golden | único y ≠ golden | ambiguo | sin solución |
|---|---|---|---|---|---|
| todas las abiertas | 400 | 116 | 0 | 284 | 0 |
| vencidas (due ≤ fecha) | 334 | 173 | 3 | 192 | 32 |
| due ≤ fecha + 15 | 400 | 164 | 0 | 236 | 0 |
| emitidas ≥ fecha − 240 | 380 | 222 | 2 | 168 | 8 |
| **emitidas ≥ fecha − 240 y due ≤ fecha + 15** | 380 | **284 (71 %)** | **0** | 104 | 12 |

SEGMENT_RESULTS_PLACEHOLDER

### 3c. Lo que dice el análisis del rol A, con dueño

- **A2, avisos xlsx con cabecera nueva (D, con C para las deducciones sin motivo).** 178 avisos y 30.225 filas vacías
  en LIVE desde septiembre. Encaja con lo que veo: `ar_1toN_remittance` 0,887 y `ar_short_dispute` 0,49 en LIVE frente
  a DEV. Es lo de más impacto de A (+0,9 a +2,5) y en el FINAL pesa más. No se puede medir en DEV (el formato no existe
  en TRAIN): DEV y sample deben salir idénticos y el juez es un envío LIVE.
- **A1, contexto entre fases (A + D).** Ya está hecho en `a-contexto-previo` y **resta** por lo de §1. Con el
  número exacto para las filas de fases anteriores debería quedarse con lo bueno (+761 plazos, +244 retenciones, +50
  confirming ≈ +1.050 líneas ≈ +0,3 a +0,5 pts) sin lo malo.
- **A3, DEV estricto (A).** De acuerdo: hoy no podemos medir en DEV nada de lo de §1 porque DEV no tiene el mismo
  contexto que LIVE. Además: con el estricto, la regla del rol B de 3b se puede medir sin el sesgo de los avisos tardíos.
- **A6, historial de cerradas para DUPLICATE/RETURNED (A + C)** y **A8, Moncayo/ZA01 (A + D):** no son del rol B.
- **Riesgo para B que señala A:** `subset_recent_days=240` en el FINAL ("en 2026 se paga más tarde", POLICIES §6).
  La propuesta 1 usa la misma ventana; habría que medir en HOLDOUT con 240 y 365.

---

## 4. Prioridades: (impacto × probabilidad) / esfuerzo

| # | Mejora | Impacto estimado | Prob. | Esfuerzo | Dueño | Cómo se mide |
|---|---|---|---|---|---|---|
| 1 | **Avisos xlsx con cabecera nueva** (A2) | +0,9 a +2,5 LIVE, más en FINAL | 0,8 | 1-1,5 h | D (+C) | DEV/sample idénticos; envío LIVE: ar_1toN_remittance, ar_short_dispute, R |
| 2 | **Subconjunto exacto único sobre todas las facturas recientes del tercero** (programación dinámica, sin tope de 22/5) | +0,6 a +1,6 DEV y LIVE (ver abajo) | 0,6 | 2-3 h | **B** | flag `subset_dp`; DEV + HOLDOUT; precisión de la regla nueva ≥ 0,95; 6 meses sin bajada |
| 3 | **Contexto entre fases con número exacto en los PDFs previos** (arreglar b71f626) | +0,3 a +0,5 sobre 82,23 (recupera −0,28 y deja +1.050 líneas) | 0,7 | 0,5 h | A + D | DEV idéntico (por construcción); envío LIVE: ar_instalment ≈ 0,92 y ar_dd_batch ≈ 0,95 |
| 4 | **Determinismo de `learn.py`** + preflight con sha descomprimido y log único | riesgo de FINAL (dos máquinas, modelo nuevo) | — | 0,5 h | A | borrar `out/models`, dos `run sample`/DEV, sha idéntico |
| 5 | `ref_cn_subset` (abonos netos en referencias citadas) | +0,07 DEV y +0,07 HOLDOUT, medido | 0,9 | hecho, con test | B | ya medido: `ref_set_cn` 0,92 / 0,97; activarlo por defecto y comprobar que sample no baja |

Descartado por medición: `two_pass_party`, `subset_max_items` 8/22, `iban_learn`, `name_fuzzy`, `ref_fuzzy`,
`pdf_exact` global y el algoritmo genético.

**Cálculo del impacto de la #2.** En DEV hay 4.724 líneas de ar_ref_noise perdidas con varias facturas; el 71 % se
resolvería con el estado abierto perfecto. Suponiendo que el estado real del libro (con errores previos) deja un 50-70
% de eso: 1.700-2.400 líneas × 7,4 parejas ≈ 12.000-17.500 parejas de ~716.500 → recall +1,7 a +2,4 puntos → F1
+0,011 a +0,015 (×50 = +0,5 a +0,75) y txn +0,9 a +1,2 % (×30 = +0,26 a +0,37), **≈ +0,8 a +1,1 pts**, más lo
de otros segmentos. LIVE tiene el mismo segmento (42.000 líneas, 0,872). **Diseño:** en `_by_party` paso (d), solo
para el tercero mejor clasificado y si el nombre casa; conjunto = facturas abiertas del tercero (y familia)
emitidas en los últimos 240 días con vencimiento ≤ fecha + 15; contar soluciones exactas (±1 céntimo) con dos
bitsets de Python (`r1 |= r1 << a`, `r2 |= (r2 << a) | (r1 & (r1 << a))`), ~10-20 ms por línea; aceptar solo si hay
exactamente una y tiene ≥ 2 facturas; reconstruir hacia atrás con los bitsets por prefijo. Determinista (todo con
`sorted`). Riesgo: el estado abierto del motor no es el del golden; por eso la puerta es la precisión de la regla en
DEV y HOLDOUT, no la muestra.
