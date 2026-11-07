# Análisis del rol A (datos, ejecución y envíos): 7 nov 2026

Autor: rol A. No toca código; todo lo medido sale de comandos de solo lectura sobre `data/challenge/` (ver §7).
Cifras de partida: LIVE 82,23 (F1 0,845 · P 0,993 · R 0,735 · txn 0,904 · exc 0,643), DEV 85,99, HOLDOUT 85,68.

**Las conclusiones, en 6 líneas**
1. Hay un **drift que DEV no puede ver** y que pesa más que todo lo demás: desde septiembre de 2025, **178 avisos xlsx
   con un layout nuevo salen vacíos** (30.225 filas de factura perdidas en LIVE). En el FINAL durará los 6 meses.
2. DEV es **más optimista que LIVE** por cómo se construye la ventana: carga PDFs de hasta 550 días atrás, remesas de
   confirming de 120 días atrás y avisos recibidos **40 días después** del final de la ventana. LIVE no ve nada de eso.
3. Ese contexto **sí existe en disco** (está en `train/`; para el FINAL, en `train/` y `live/`). Cargarlo de forma igual
   en todas las fases recupera parte de la caída en plazos, retenciones y anticipos.
4. El FINAL tiene riesgos de **crash** que se pueden quitar baratos: `masters_final` incompleto, caché de PDFs
   obsoleta, URL del FINAL distinta y un primer arranque sin caché (~6 min solo de extraer PDFs).
5. El rendimiento no es urgente (unos 7,5 min frente a un límite de 20). La mitad del *match* se va en
   `Parties.by_name` y la mayor parte del *setup* en `groupby + to_dict` y en avisos sin caché.
6. Determinismo: OK en sample (CSV idéntico byte a byte con `PYTHONHASHSEED=1` y `=2`).

---

## 1. Qué ve el motor en LIVE/FINAL frente a la ventana DEV

`load_phase` (LIVE/FINAL, `data.py:229-258`) lee **solo** `<fase>/…`. `load_window` (DEV/HOLDOUT, `data.py:264-347`)
recorta `train/` con márgenes:

| Fuente | DEV (`load_window`) | LIVE (`load_phase`) | ¿Existe en disco para LIVE? | Medido |
|---|---|---|---|---|
| PDFs de documentos | fecha ≥ inicio − 550 d (`data.py:326-330`) | solo `live/pdfs` (fechas 2025-07-01…12-31) | **sí**, en `train/pdfs` | 22.361 PDFs de TRAIN corresponden a partidas abiertas al inicio de LIVE |
| Avisos de cambio de cuenta (IBAN nuevo) | igual que los PDFs | solo los de la fase | sí: 633 en TRAIN (249 desde 2024) | `ar_new_iban` |
| Payment runs | ejecución ≥ inicio − 120 d (`data.py:341`) | solo `live/payment_runs` (desde 2025-07-10) | sí | **21 remesas de confirming** de TRAIN (abr-jun 2025, 4.167 documentos) se cargan en **54 cargos** de LIVE (jul-sep) |
| Avisos de remesa | recibidos en inicio ± 40 d (`data.py:342`) | recibidos dentro de la fase | antes: sí; después: **no** | DEV usa **132 avisos recibidos en enero de 2025 (5.095 filas)** para cobros de diciembre: LIVE/FINAL nunca los tendrán |
| Remesas SEPA, tarjetas, cheques | fecha ≥ inicio − 62 d | solo la fase | — | 0 lotes de finales de junio con apunte en julio: **no cruzan la frontera** |
| Partidas abiertas al inicio | rehechas desde el golden | `open_items_at_start` oficial | — | equivalente |
| Historial de pagos (DUPLICATE/RETURNED) | partidas cerradas antes del inicio: descartadas (`data.py:311`) | no están | sí (golden de TRAIN; para el FINAL, nuestro LIVE) | ~5 % de cada clase por ventana (§2, A6) |
| Modelo de categorías (`learn.py`) | entrenado con todo lo anterior a 2024-07-01 | anterior a 2025-07-01 (`cli.py:79`) | — | equivalente, salvo el drift de Moncayo (A8) |
| Masters | foto a 2025-12-31 (futura para DEV) | igual | — | sesgo menor y de signo incierto |

**Conclusión:** DEV sobrestima LIVE en lo que dependa de PDFs antiguos, de confirming y de avisos tardíos, y no ve
ningún drift. La caída de los segmentos que dependen de cláusulas de PDF (ar_instalment 0,94→0,75, ar_retention
0,95→0,71, ar_advance_deduction 0,52→0,35) encaja con lo primero; la de `ar_1toN_remittance` y `ar_short_dispute`,
con el drift de avisos (A2).

Nota sobre la cifra de 25.377: yo cuento 22.361 casando por `(entity, alnum(document_number))` contra
`open_items_at_start`. Sin filtrar por sociedad salen más, porque los números de proveedor se repiten. El orden de
magnitud es el mismo.

---

## 2. Opciones

Notación del impacto: puntos de score LIVE/FINAL. Para pasar de pares a puntos uso F1 = 2PR/(P+R) con
P = 0,993 y R = 0,735, o sea dF1/dR ≈ 0,66 y 1 % de recall ≈ +0,33 pts. 1.000 líneas bien resueltas ≈ +0,15 pts de
exactitud de transacción. LIVE ≈ 740.000 pares en el golden: lo estimo con DEV (716.500 pares en 196.970 líneas)
escalado a 203.083 líneas.

### A1. Contexto entre fases: PDFs, avisos de IBAN y confirming de las fases anteriores, cargados igual en todas
1. **Qué es:** que la carga de una fase añada los PDFs, avisos de cambio de cuenta y remesas de confirming de las
   fases anteriores que todavía importan (para LIVE, `train/`; para el FINAL, `train/` + `live/`), y que `load_window`
   use **la misma regla**, de modo que DEV, HOLDOUT, LIVE y FINAL vean lo mismo.
2. **Evidencia:**
   - `data.py:251` (LIVE, solo `<fase>/pdfs`) frente a `data.py:326-330` (DEV, 550 días atrás).
   - De los 22.361 PDFs de TRAIN de partidas abiertas en LIVE tengo texto en caché para 14.743, y en ellos hay
     **2.414 importes a pagar tras retención/anticipo, 1.274 planes de plazos, 754 "anula y sustituye", 126 meses de
     servicio** y 1.219 cesiones. Como referencia, `live/pdfs` aporta 1.308 planes y 965 importes a pagar: **LIVE ve
     aproximadamente la mitad de los planes que necesita.**
   - Confirming: `engine._link_confirming` (`engine.py:168`) solo enlaza remesas cargadas. Hay 54 cargos de LIVE que
     citan 21 remesas de TRAIN (4.167 documentos).
3. **Impacto estimado:** +0,3 a +0,7 pts en LIVE (central ≈ +0,45). Cálculo: tamaños de segmento de DEV × caída
   LIVE−DEV ⇒ instalment 4.728×0,19 ≈ 900, retention 940×0,24 ≈ 225, advance 266×0,17 ≈ 45, en total ≈ 1.170
   líneas (+0,17 pts de txn). A eso se suman ≈ 1.170 + 4.167 pares de confirming (+0,7 % de recall, +0,23 pts), y
   PARTIAL mejora algo en el macro-F1 (+0,05 a 0,1). Supone que LIVE tiene segmentos del tamaño de DEV y que se
   recupera la precisión de DEV. En el FINAL el efecto es igual o mayor: las partidas que arrastra el FINAL tienen
   los PDFs en `live/` y en `train/`, y POLICIES §6 avisa de que en 2026 se paga más tarde, así que habrá más
   partidas viejas abiertas.
4. **Esfuerzo:** 2-3 h. Hay que añadir una columna `path` en `pdf_index` (ruta relativa a `data/challenge`) y que
   `pdf_texts` y `_load_pdf_clauses` la usen; concatenar las remesas de confirming previas a `runs` y unificar la regla
   en `load_window`. Extraer el texto de los 7.618 PDFs que faltan en caché lleva unos 3-5 min una sola vez (§A10).
   **Riesgo:** medio. Un PDF de una partida ya cerrada no molesta (`find_number` solo encuentra partidas del libro),
   pero un "anula y sustituye" antiguo podría marcar como cancelada una partida que el golden sí cobra. Se ve en
   `ar_replaced_invoice`. No sobreajusta: aplica POLICIES §5.7-8 con documentos que nos dan.
5. **Cómo se mide:** (a) `run train` DEV y HOLDOUT con la regla nueva y con A3. El objetivo es que DEV "estricto"
   suba y que HOLDOUT no baje. (b) Un envío a LIVE con solo este cambio. Mirar ar_instalment, ar_retention,
   ar_advance_deduction, ar_new_iban, ar_replaced_invoice y ap_confirming en el desglose por segmento. Se espera
   que se acerquen a DEV.
6. **Coordinación:** D (`docs.pdf_texts` y `engine._load_pdf_clauses`): solo hace falta el cambio de interfaz (ruta
   por fila). Yo hago `data.py`. B o D, para `_link_confirming`: no cambia, solo recibe más filas.

### A2. Drift del layout de avisos xlsx (septiembre de 2025): detectar, corregir y medir
1. **Qué es:** el parser de xlsx no reconoce la cabecera nueva `Voucher | Invoice | PO | Inv. date | Amount |
   Deduction | Net paid` y devuelve un aviso sin líneas.
2. **Evidencia:**
   - `docs._parse_csv_rows` solo busca la cabecera en la primera columna, con `_NUM_HEADERS` (`docs.py:97-104`), y
     "Voucher" no está en la lista.
   - En LIVE, **178 avisos xlsx (todos desde ago/sep de 2025) salen vacíos, con 30.225 filas de factura** (mediana de
     149 por aviso, máximo 854). Llegan de 16 sociedades y hay 0 errores de parseo: son avisos vacíos, que no se
     notan si nadie los busca. El formato anterior (`Document | Date | Gross | Deduction | Reason | Paid`, 319
     avisos) sí se lee.
   - Deducciones: 24.477 filas sin deducción, 3.484 con exactamente el 1 % (pinta de pronto pago) y 2.099 por encima
     del 5 % (pinta de disputa). **No hay columna de motivo.**
   - El número de factura viene sin prefijo (`106180`) y además está el PO del cliente.
3. **Impacto estimado:** **+0,9 a +2,5 pts en LIVE.** Con solo las 24.477 filas limpias al 85 % de acierto, el recall
   pasa de 0,735 a 0,763 y el F1 de 0,845 a 0,863, es decir **+0,9**. Con las 30.225 bien, F1 0,871: **+1,3**. Las
   3,5k filas de descuento y las 2,1k de disputa pueden sumar en el macro-F1 de SHORT_DISCOUNT y SHORT_DISPUTE (de 0
   a +1, no lo puedo medir sin golden). **En el FINAL pesa más:** el layout nuevo cubre los 6 meses, unos 45 avisos al
   mes, así que del orden de 270 avisos y 45.000 filas (extrapolación lineal).
4. **Esfuerzo:** 1-1,5 h. Añadir la cabecera nueva y el mapeo `Invoice→number`, `Amount→gross`, `Net paid→paid`,
   `Deduction`; usar `PO` como clave de respaldo (`book.by_key["PO:…"]` ya existe). Decidir la excepción de las
   deducciones sin motivo: 1 % dentro de plazo ⇒ `discount_c` (SHORT_DISCOUNT o PARTIAL si es tarde), y el resto
   con la regla de disputa (C). **Riesgo:** bajo para los pares (el filtro de importe de `_from_advice`, `engine.py:656-662`,
   los protege) y medio para las excepciones. No sobreajusta: es un formato documentado (POLICIES §5.4) y no depende
   de ningún cliente concreto.
5. **Cómo se mide:** DEV no puede, porque el formato no existe en TRAIN. Ahí solo se comprueba que **no cambia nada**
   (CSV idéntico en sample y DEV). El juez es un envío a LIVE con solo este cambio: mirar ar_1toN_remittance (0,89 →
   ~0,96), ar_short_dispute, la R global y el macro-F1. Antes de enviar, el comando de A4 debe dar 0 avisos vacíos.
6. **Coordinación:** el parser es de **D** (`docs.py`) y las excepciones sin motivo, de **C** (`deduction_kind` y
   `rules`). Yo aporto la detección, el recuento por pagador, formato y mes, y el envío LIVE de medida. **Es lo que
   más puntos da de todo el análisis:** propongo que D lo coja ya.

### A3. DEV fiel a LIVE: ventana "estricta" para que el juez no engañe
1. **Qué es:** un modo `--strict` (o hacerlo por defecto) en `load_window`. Los avisos se cargan si se recibieron ≤
   fin, sin los 40 días de después; los lotes, solo de la ventana; y los PDFs y el confirming, con la regla común de A1
   y no con márgenes propios.
2. **Evidencia:**
   - `data.py:342` (`fwd=40`): 132 avisos recibidos después del fin, con 5.095 filas, se enlazan con cobros de
     diciembre en DEV y en LIVE serían imposibles.
   - `data.py:326-330` y `:341` son márgenes que LIVE no tiene.
   - Hoy DEV 85,99 frente a LIVE 82,23: casi 4 pts que no sabemos repartir.
3. **Impacto:** 0 pts directos. Calidad de decisión: DEV bajará de forma estimada ≈ 0,2-0,3 pts solo por los avisos
   tardíos (5.095 pares ≈ +0,7 % de recall en DEV), y más si se quitan los PDFs antiguos sin aplicar A1. Así
   sabremos qué parte del hueco DEV−LIVE es contexto que falta (A1), qué parte es drift (A2, A8) y qué parte es
   distribución. Evita perseguir reglas que suben DEV gracias a datos que LIVE no tiene.
4. **Esfuerzo:** 0,5 h + 2 ejecuciones de `run train` (12 min). **Riesgo:** nulo para el envío. Cambia la referencia
   de DEV, así que hay que avisar a B, C y D y apuntar en STATUS la nueva base estricta.
5. **Cómo se mide:** `run train` DEV y HOLDOUT, antes y después. El 85,99 pasa a ser "DEV laxo".
6. **Coordinación:** todos iteran contra DEV. Hay que anunciar el cambio de base en `docs/STATUS.md` y que las
   comparaciones sean estricto contra estricto.

### A4. Comando `closebooks drift <fase>`: detectar el drift antes de enviar
1. **Qué es:** un informe de solo lectura que en un minuto dice qué ha cambiado en una fase sin golden.
2. **Evidencia:** los dos drifts encontrados hoy solo se veían mirándolos a propósito.
   - **Avisos vacíos por pagador, formato y mes:** 178 xlsx desde sep. (A2).
   - **Cobertura del modelo aprendido por cuenta y mes:** las cuentas de Moncayo pasan de fine 0,14-0,20, coarse 0,92
     y strong 0,09-0,17 en jul-sep a **0,000 en las tres desde octubre** (6.583 líneas). Las demás cuentas siguen
     estables (fine ≈ 0,29, coarse ≈ 0,93).
   - Para el FINAL hay que vigilar sociedades, cuentas y divisas nuevas (ZA01/ZAR), que haya tipos de cambio hasta el
     fin de la fase, el formato de extracto por cuenta, las partidas arrastradas sin `party_name`/`terms` (las de
     ZA01 anteriores a la compra no estarán en ninguna tabla anterior: `data.py:218-221`), la distribución de `rule`
     frente al último LIVE y las líneas `error:`.
3. **Impacto:** habilitador. Convierte los 30 minutos de las 18:00 en una lista de comprobación de 2 minutos.
   Con los datos de hoy, sin él no habríamos visto A2.
4. **Esfuerzo:** 1-1,5 h (`cli.py` + `analysis.py`, reutilizando `load_advices` y `CategoryModel`). **Riesgo:** nulo,
   es solo lectura.
5. **Cómo se mide:** en LIVE tiene que señalar A2 y Moncayo y no dar falsos positivos en las demás cuentas. Lo ensayo
   sobre la ventana HOLDOUT: debería salir limpio.
6. **Coordinación:** ninguna. Es de mi área.

### A5. Robustez del FINAL: quitar las causas de crash a las 18:00
1. **Qué es:** un paquete de arreglos pequeños en mis ficheros.
2. **Evidencia y arreglo:**
   - `load_masters` (`data.py:81-98`): si existe `final/masters_final/`, lee **todo** de ahí. Si le falta un fichero
     (por ejemplo `categories.csv` o `payment_terms.csv`), da `FileNotFoundError` y no sale CSV. **Arreglo:** usar el
     fichero de `masters_final` si existe y, si no, el de `masters/`, y avisar.
   - El setup del motor no está protegido: `Engine.run` captura los errores por línea (`engine.py:1129-1144`), pero
     `Engine.__init__` (`_load_pdf_clauses`, `_link_details`) no. Una columna nueva o que falte en un lote del FINAL
     lo tira todo. **Arreglo:** que cada fuente (runs, dd, cards, cheques, advices, pdfs) degrade sin romper, que se
     registre en un `warnings.json` y que el CLI lo imprima. El motor sigue y sale un CSV válido.
   - Caché de PDFs con clave solo por nombre de fichero (`docs.py:228-243`, `pdf_text_<fase>.json`). Si ensayamos con
     un "final" falso y luego llega el real con nombres de PDF coincidentes, **se reutilizaría texto equivocado sin
     avisar**. **Arreglo:** que la clave incluya tamaño y fecha de modificación, o borrar `pdf_text_final.json` antes
     de las 18:00. Lo mismo para `out/models/categories_*.json` (`cli.py:83-86`), que se reutiliza sin comprobar con
     qué datos se entrenó.
   - `setup_data.py` construye la URL del FINAL como `BASE_URL + "bankrec-final.tar.gz"`, y la web dice "ver en la
     web". **Arreglo:** opción `--final-url` y `--from` con el fichero bajado a mano. Comprobar que el tar trae
     `final/` en la raíz: si trae otra carpeta, `closebooks info` no lo verá.
   - `submit.preflight` compara con 60 MB el tamaño **comprimido** (`submit.py:93-95`), y FORMAT.md dice **60 MB sin
     comprimir** y ≤ 3 M filas. Hoy no muerde: sample son 1,25 MB sin comprimir por 8,5k líneas, así que LIVE ≈ 25-30
     MB y el FINAL parecido. **Arreglo:** medir el tamaño descomprimido y las filas.
   - Guarda del FINAL (`submit.py:107-111`): solo bloquea si el log local tiene `scored`/`received`. Tras un
     `poll-error` o un timeout (`timeout=600`, el FINAL puede tardar) el estado queda `poll-error` o `queued` y
     **permitiría reenviar**. Además el log es por carpeta (`out/submissions` relativo), así que cada worktree tiene el
     suyo. **Arreglo:** en el FINAL, cualquier `sub_id` previo que no sea `invalid` bloquea el reenvío (se usa
     `closebooks fetch`), y **solo A envía, desde una única carpeta**.
3. **Impacto:** protege el envío entero. Le doy un 10-20 % de probabilidad (estimación cualitativa) a que algo de esta
   lista muerda a las 18:00 con datos nuevos, y el coste de cada incidente son de 10 a 30 min de un margen de 90.
4. **Esfuerzo:** 1,5 h en total, todo en `data.py`, `cli.py`, `submit.py` y `scripts/setup_data.py`. **Riesgo:**
   bajo. El CSV de sample y el de DEV deben salir idénticos byte a byte.
5. **Cómo se mide:** tests nuevos (masters_final parcial, fuente rota, guarda FINAL tras `poll-error`, tamaño
   descomprimido). Ensayo de un "final falso" (§4, 17:30): copiar `live/` a una raíz temporal como `final/` con un
   `masters_final` al que le falte un fichero, y comprobar que sale un CSV válido.
6. **Coordinación:** ninguna, salvo avisar a D de que `_link_details` irá envuelto.

### A6. Historial de partidas cerradas para DUPLICATE_PAYMENT y RETURNED en la frontera de fase
1. **Qué es:** cargar en el libro, con saldo 0 y su historial de pagos, las partidas cobradas en los últimos ~90 días
   de la fase anterior, para que un duplicado o una devolución en el primer trimestre encuentren el pago original.
2. **Evidencia:** `load_window` las descarta (`data.py:311`) y LIVE no las trae, porque `open_items_at_start` solo
   tiene abiertas. Además `engine.py:841-842` lo dice explícitamente: "closed before the phase = unknown history,
   never call it a duplicate". Medido con el golden: **DEV tiene 196 filas RETURNED y 31 DUPLICATE (≈5 % de cada
   clase) cuya partida se cerró antes del inicio. HOLDOUT tiene 218 y 39.** Todas caen en los primeros 2-3 meses.
3. **Impacto:** +0,1 a +0,15 pts. Son 2 clases del macro-F1 con +5 % de recall cada una (≈ +0,005 de macro, +0,1
   pts) y ≈ 230 líneas (+0,035).
4. **Esfuerzo:** 2 h. Para DEV y LIVE se rehace desde el golden de TRAIN, que es un dato que tenemos. Para el FINAL,
   desde **nuestro propio CSV de LIVE** (precisión de pares 0,993). **Riesgo:** medio, porque más partidas en el libro
   dan más candidatas a casación falsa. Solo deben entrar en `_returned` y en las ramas de duplicado, nunca en
   `open_items`.
5. **Cómo se mide:** DEV/HOLDOUT estrictos. F1 de RETURNED y DUPLICATE_PAYMENT en `closebooks report`, y precisión de
   las reglas `returned*` y `*_duplicate`.
6. **Coordinación:** C (`engine._returned` y ramas de duplicado). Yo hago la carga en `data.py`.

### A7. Rendimiento sin cambiar resultados
1. **Qué es:** cuatro cambios con el CSV idéntico byte a byte.
   - **(a) Caché de avisos parseados.** `load_advices` no tiene caché y re-lee 2.196 PDFs y 497 xlsx en cada
     ejecución: **20,9 s medidos en LIVE**.
   - **(b) `_link_details` vectorizado.** Un solo `to_dict` y agrupar por índices, en lugar de 66.527 llamadas
     `g.to_dict("records")` por grupo y un `runs.apply(axis=1)` sobre 205k filas (`engine.py:232-244`).
   - **(c) Índice de nombres.** `Parties.by_name` (`book.py:199`) compara el prefijo común con todos los nombres que
     empiezan por la misma palabra ("PISCINAS…"). Un índice por los 8 primeros caracteres o una lista ordenada con
     `bisect` devuelve los mismos candidatos.
   - **(d) Poda en `unique_subset`.** Cortar cuando la suma de positivos < objetivo, y memo por pool.
2. **Evidencia:** cProfile en LIVE (setup completo + 50.000 primeras líneas; con el profiler todo va ~2,5-3× más
   lento).
   - **Setup:** `_link_details` 77 % (`to_dict` por grupo 36 %, `load_advices` 27 %), `Book.__init__` 19 % (1,03 M
     partidas, `number_keys` 1,66 M llamadas) y `_bank_indexes` 8 %.
   - **Match:** `by_name` **58 %** (47 M llamadas a `_common_prefix`), `unique_subset` **24 %**, `_by_party` (resto)
     y `_by_reference` 5 %.
3. **Impacto:** 0 pts. Tiempo: estimo una ejecución de LIVE de ~7 min → ~4-4,5 min ((a) −20 s, (b) −25 a 40 s, (c)
   −50 % del match). **No es crítico:** el FINAL estimado ronda los 8-9 min sin caché de modelos y el límite son 20.
   Lo que sí es crítico es A10.
4. **Esfuerzo:** (a) 0,5 h y (b) 1 h, en mis ficheros o en zona de setup; (c) 1,5 h y (d) 1 h tocan B y C. **Riesgo:**
   bajo si se exige byte a byte. En (c) cuidado con el orden de los empates (`sorted` actual).
5. **Cómo se mide:** `run sample` y DEV con el CSV idéntico (sha256) y los tiempos `load · setup · match` del CLI.
6. **Coordinación:** (c) con B (dueño de `Parties`) y (d) con C (`rules.unique_subset`). Recomiendo hacer solo (a)
   y (b) hoy.

### A8. Moncayo en camt.053 y ZA01: que el modelo de categorías no se quede ciego
1. **Qué es:** normalizar la clave del modelo aprendido. En Moncayo camt, `bank_subcode` = código N43 común + propio
   (`17701` = `17`+`701`, `04532` = `04`+`532`), así que la clave puede rehacerse como en N43. Además, añadir un
   respaldo por "forma de texto sin bank_code" para bancos nuevos (ZA01).
2. **Evidencia:**
   - Cobertura 0,000 desde 2025-10 en las 4 cuentas `*-MONC-*` (A4).
   - `bank_code` pasa de `17` a `ACMT/MCOP/CHRG` y el texto de `TRANSFERENCIAS X | …` a `TRANSFERENCIA SEPA DE X`.
   - En TRAIN 2025-H1 el ~12 % de las líneas de Moncayo no lleva partida: BANK_FEES 819, UNIDENTIFIED 253 y unas 200
     de otras categorías. `category_by_text` recupera 464 BANK_FEES en camt, y los cargos sin regla acaban en
     `default_debit` = BANK_FEES, que es lo correcto la mayoría de las veces.
3. **Impacto:** pequeño en LIVE (≈ 150-250 líneas de 3 meses, unos **+0,03 a +0,05**). En el FINAL, Moncayo son 6
   meses (el doble). ZA01 no está cubierta por el modelo (banco y textos nuevos, quizá "SARS" para impuestos o "UIF"
   para seguridad social) y es ≈ la diferencia de 220k − 203k ≈ 15k líneas (estimación burda). Con ~10 % de líneas
   sin partida, unas 1.500 líneas ≈ +0,2 pts en juego.
4. **Esfuerzo:** 1 h (`learn.py`, la clave solo). **Riesgo:** bajo-medio: una clave más gruesa puede activar
   `strong()` en líneas que llevan partida. Mantener los umbrales.
5. **Cómo se mide:** DEV/HOLDOUT sin regresión (allí Moncayo es N43 y no debería cambiar nada). Luego un envío LIVE y
   mirar `nonitem_*` y `unidentified`. Para ZA01 solo hay validación cualitativa a las 18:00 con A4.
6. **Coordinación:** D (categorías). `learn.py` no tiene dueño en el PLAYBOOK: propongo que lo haga A con revisión de
   D.

### A9. Determinismo y protocolo de envío
1. **Qué es:** verificar el determinismo **en LIVE** y no solo en sample; fijar el entorno; dejar huella del CSV.
2. **Evidencia:**
   - Sample con `PYTHONHASHSEED=1` y `=2` da el mismo CSV byte a byte (84,55 en los dos).
   - En el código solo hay una iteración de `set` sin `sorted` (`engine.py:155`), y es inocua porque solo cambia el
     orden en que se insertan claves distintas. `ccys.pop()` (`rules.py:143`) tiene un único elemento.
   - El texto de los PDFs depende de la versión de `pypdf`: la caché compartida entre máquinas **fija** el resultado,
     pero una máquina sin caché con otra versión podría sacar otro texto.
3. **Impacto:** de riesgo. El FINAL es un solo tiro.
4. **Esfuerzo:** 0,5 h, más 2 ejecuciones de LIVE en el ensayo de las 17:30.
5. **Cómo se mide:**
   - `PYTHONHASHSEED=1` y `=2` en `run live` → `sha256sum` del CSV descomprimido idéntico.
   - `uv sync --frozen`.
   - El sha del CSV se apunta en `docs/SUBMISSIONS.md`.
   - El FINAL se corre en **dos máquinas** con el mismo commit y la misma caché, y el sha tiene que coincidir.
6. **Coordinación:** una segunda máquina (quien quede libre a las 18:00).

### A10. Caché de texto de PDFs precalculada y extracción en paralelo
1. **Qué es:** dejar extraído ahora todo el texto que el FINAL pueda necesitar (los PDFs de TRAIN de partidas
   abiertas al inicio de LIVE, más `live/pdfs`, que ya está) en una caché común con clave `fase/fichero`. A las 18:00,
   extraer `final/pdfs` con `multiprocessing`.
2. **Evidencia:**
   - pypdf ≈ 20-23 ms por página (perfil de sample: 133 páginas en 3,07 s; LIVE: 2.489 páginas en 51 s con
     profiler).
   - `live/pdfs` tiene 16.688 PDFs. Si `final/pdfs` es parecido, la **primera** ejecución del FINAL tardará ≈ 6 min
     más solo en extraer (estimación: 16,7k × ~1 página × 23 ms).
   - Las cachés actuales están **duplicadas por ventana**: `pdf_text_train_2024-07_2024-12.json` (41 MB) y
     `…2025-01_2025-06.json` (42 MB) solapan casi entero.
   - Con A1 harían falta además 7.618 PDFs de TRAIN que hoy no están en ninguna caché.
3. **Impacto:** tiempo y riesgo. Pasa a ≈ 1-2 min la extracción del FINAL y hace viable A1 sin gastar tiempo a las
   18:00.
4. **Esfuerzo:** 1 h, en `docs.pdf_texts` (de D, pero el rendimiento es mío). La precarga se puede lanzar ya en
   segundo plano. **Riesgo:** bajo, porque el texto es el mismo. La clave tiene que incluir tamaño y mtime (A5).
5. **Cómo se mide:** el tiempo de `setup` de LIVE sin caché local frente a con caché, y que el CSV salga idéntico.
6. **Coordinación:** D (`docs.py`).

### Descartadas o aplazadas (y por qué)
- **Reentrenar `learn.py` con nuestras predicciones de LIVE (autoaprendizaje):** sin golden, se aprenden también los
  errores. El caso real (Moncayo) se cubre mejor con A8.
- **Usar las notas por segmento de LIVE para ajustar parámetros:** solo 60 segmentos con n redondeado; sería
  sobreajustar al termómetro. LIVE sirve para confirmar cambios aislados (A1, A2), no para buscarlos.
- **Copiar lotes de fin de mes de la fase anterior (DD, tarjetas, cheques, runs agregados):** medido, ningún lote de
  finales de junio de 2025 tiene apunte en LIVE. No aporta.
- **Avisos de la fase anterior:** 6 avisos y 126 filas para LIVE. Entra gratis con A1, pero no justifica trabajo
  propio.

---

## 3. Tabla ordenada por (impacto × probabilidad) / esfuerzo

| # | Opción | Impacto (pts o riesgo) | Prob. | Esfuerzo | Prioridad | Dueño |
|---|---|---|---|---|---|---|
| A2 | Layout nuevo de avisos xlsx | **+0,9 a +2,5** LIVE, más en FINAL | 0,8 | 1-1,5 h | **≈ 1,3** | D (+C), detecta A |
| A5 | Robustez FINAL (masters, setup, caché, URL, guarda, tamaño) | evita un crash a las 18:00 (10-20 %) | — | 1,5 h | **muy alta (riesgo)** | A |
| A1 | Contexto entre fases (PDFs, IBAN, confirming) | +0,3 a +0,7 | 0,7 | 2-3 h | ≈ 0,15 | A + D |
| A3 | DEV estricto | 0 directo; mejor juez | 0,9 | 0,5 h + 12 min | alta (barata) | A |
| A4 | `closebooks drift` | habilitador 18:00 | 0,9 | 1-1,5 h | alta | A |
| A10 | Caché de PDFs común y en paralelo | −5 min en el FINAL; habilita A1 | 0,9 | 1 h | alta | A + D |
| A9 | Determinismo en LIVE, dos máquinas, `--frozen` | riesgo | — | 0,5 h | media-alta | A |
| A6 | Historial de cerradas (DUP/RET) | +0,1 a +0,15 | 0,5 | 2 h | ≈ 0,03 | A + C |
| A8 | Moncayo camt y ZA01 en el modelo | +0,03 a 0,05 LIVE; ~+0,2 en juego en FINAL | 0,5 | 1 h | ≈ 0,05 | A (+D) |
| A7 | Rendimiento (a+b; c y d opcionales) | 0 pts; −2,5 min | 0,9 | 1,5 h (a+b) | baja | A (+B, C) |

**Las 3 primeras que recomiendo:**
1. **A2 (que D lo coja ya, con medida mía en LIVE).** Es la que más puntos da con diferencia, la única que pesa más
   en el FINAL que en LIVE y la que se mide en un solo envío.
2. **A5 + A10 (yo, ahora).** No suben nota, pero sin ellas un fallo a las 18:00 puede costar el FINAL entero. A10
   además es requisito de A1.
3. **A1 + A3 juntas (yo con D).** Hacen que DEV mida lo que verá el FINAL y recuperan parte de los segmentos de PDF.

---

## 4. Plan horario hasta las 17:30 (supongo que arrancamos a las 12:30)

| Hora | A hace | En segundo plano / otros |
|---|---|---|
| 12:30-13:00 | Paso A2 a D con las cifras (178 avisos, layout, deducciones). A5: tamaño sin comprimir, guarda FINAL, `--final-url` | **Lanzo la extracción de PDFs de TRAIN de partidas abiertas en LIVE** (A10, ~5 min) |
| 13:00-14:00 | A3 (estricto) + A1 en `data.py`; acuerdo con D la columna `path` en `pdf_index` | `run train` DEV estricto **sin** A1 = nueva base (6 min) |
| 14:00-14:45 | Comida | `run train` DEV y HOLDOUT estrictos **con** A1 (12 min) |
| 14:45-15:30 | A4 (`closebooks drift`) y A5 restante (masters por fichero, setup degradable, clave de caché) + tests | D termina A2; `run sample` y DEV deben salir idénticos |
| 15:30-16:15 | Merge de A1 → `run live` → **envío LIVE "A1"**. Merge de A2 → `run live` → **envío LIVE "A2"** (uno por cambio) | Comparar segmentos con lo esperado (§2) |
| 16:15-17:00 | A7 (a)+(b) si sobra tiempo; si no, A8 (Moncayo). A6 solo si C tiene hueco | B, C y D hacen sus últimos merges antes de las 17:00 |
| 17:00-17:30 | Último `run live` de `main` + envío LIVE. **Ensayo del FINAL** (abajo). 17:30: `git tag pre-final` | Todos: solo arreglos de robustez |

**Ensayo de las 17:00-17:30** (sin gastar nada):
1. Raíz temporal `rehearsal/` con `masters/`, `train/` y `live/` enlazados, y `final/` = copia de `live/` con un
   `masters_final/` **al que le falta un fichero**. Con eso, `uv run closebooks run final --data rehearsal` debe dar
   un CSV válido y mostrar los avisos de A5.
2. Borrar `out/cache/pdf_text_final.json` y `out/models/*` del ensayo.
3. `PYTHONHASHSEED=1` y `=2` en `run live`, y el sha256 tiene que ser idéntico (A9).
4. Cronometrar `load · setup · match` sin la caché de avisos y apuntarlo.
5. `closebooks submit out/live/submission.csv.gz --phase FINAL --dry-run` contra el ensayo: tiene que pasar el
   preflight.

---

## 5. Protocolo del FINAL (18:00 → 19:30)

| Hora | Paso | Criterio para seguir |
|---|---|---|
| 17:55 | `git checkout pre-final`, `uv sync --frozen`, caché común de PDFs en su sitio; 2 máquinas (A y otra) en el mismo commit | `uv run pytest` en verde |
| 18:00 | Descarga (`setup_data.py --final-sha <sha> [--final-url …]`), SHA-256 OK, `closebooks info` → `final OK` | Si el wifi falla: USB o hotspot. No esperar más de 10 min sin plan B |
| 18:05 | `uv run closebooks drift final` (A4): ZA01, ZAR, `masters_final` completo, tipos de cambio hasta 2026-06-30, avisos vacíos por formato, cobertura del modelo por cuenta | Anotar lo que salga; **no se toca código salvo un fallo de carga** |
| 18:07 | En paralelo, en las 2 máquinas: `uv run closebooks run final` (extracción de PDFs en paralelo) | ~8-10 min |
| 18:20 | Validación OK, 100 % de líneas cubiertas, 0 reglas `error:`, distribución de `rule` comparable con LIVE (`unidentified` y `default_debit` ±30 %), sha256 igual en las dos máquinas | Si no coincide el sha: repetir en la máquina principal con `PYTHONHASHSEED=0` y averiguar por qué antes de enviar |
| 18:25 | `reconciliation-auditor` sobre una muestra (10 líneas por regla frecuente, todas las de ZA01) | Solo bloquea si hay un error sistemático evidente |
| 18:30-18:45 | **Ventana de hotfix (solo si el drift lo exige y está claro):** un cambio de normalización de datos, nunca una regla de casación nueva. Ejemplos: el layout de un aviso del FINAL, un formato de masters. Juez: **un envío a LIVE con el hotfix no puede bajar la nota de LIVE** + `run sample` ≥ 84 | Si a las 18:45 no está verificado, se descarta y se envía `pre-final` |
| ≤ 18:50 | `uv run closebooks submit out/final/submission.csv.gz --phase FINAL --note "final <commit>"`. Escribir `FINAL`. Si la conexión se corta mientras espera, usar `closebooks fetch <sub_id>` y **nunca reenviar** | Objetivo: enviar antes de las 19:00 (empate → gana el más temprano) |
| si `invalid` | Leer los errores (el fichero rechazado no gasta el envío), corregir el formato y volver a validar en local | Margen hasta las 19:30 |
| 19:00-19:45 | Apuntar en `docs/SUBMISSIONS.md` (sha, commit, tiempos). Ayudar con el vídeo y el repo | Entrega a las 19:45 |

Reglas fijas: envía **solo A**, desde una carpeta; el `HK_TOKEN` solo está en `.env`; un fichero de red de seguridad
(el CSV del FINAL de la máquina 2) queda listo por si la 1 falla; no se pide la URL de subida del FINAL hasta tener el
fichero definitivo.

---

## 6. Cosas sueltas de mi área
- `output.validate` y `score` leen `masters/categories.csv` aunque exista `masters_final`. Si el FINAL añadiera una
  categoría, nuestra validación local daría un falso error. Hay que comprobarlo con A4.
- Las partidas arrastradas sin atributos (`data.py:218-221`) quedan con `terms=""`, sin descuento posible. En el
  FINAL afecta a las de ZA01 anteriores a la compra. Lo detecta A4.
- POLICIES §5.11: **IT01 cambia su cuenta de cobros el 2026-02-01** y los clientes siguen pagando en las dos. Hay que
  comprobar a las 18:00 que las dos cuentas están en `masters_final/bank_accounts.csv` (afecta a `own_account` y a
  `acc_entity`).
- POLICIES §6: "en 2026 se paga más tarde". `_unique_subset_alloc(recent_days=240)` (`engine.py:1053`) puede dejar
  fuera partidas válidas en el FINAL. Se lo paso a B como riesgo y no lo toco a ciegas.
- `docs/SUBMISSIONS.md` se escribe en la carpeta desde la que se envía. Con worktrees, hay que mergearlo a `main` desde
  la de A.

---

## 7. Cómo se obtuvo cada cifra (reproducible, solo lectura)
- Rangos de fechas y recuentos de `live/` (lotes, avisos, PDFs, banco, `open_items_at_start`): `_read_csv` y
  `_concat_dir` de `data.py`.
- PDFs de TRAIN de partidas abiertas en LIVE: unión por `entity|alnum(document_number)`. Cláusulas: `parse_clauses`
  sobre la caché `pdf_text_train_2025-01_2025-06.json`, que cubre 14.743 de esos 22.361.
- Confirming entre fases: tokens `alnum` del texto de LIVE frente a los `run_id` de `train/payment_runs`.
- Lotes de fin de mes: misma cuenta e importe en LIVE ≤ 2025-07-15 para lotes de TRAIN ≥ 2025-06-20.
- Avisos: `load_advices` sobre `live/remittance_advices` (20,9 s), agrupados por pagador, formato y mes, y lectura de
  los xlsx con openpyxl.
- Moncayo: `learn.build(before=2025-07-01)` y cobertura de `shape`, `coarse` y `strong` por cuenta y mes.
- DUP/RET en la frontera: DuckDB sobre el golden de TRAIN (partidas con pago anterior al inicio y ya cerradas).
- Determinismo: `run sample` con `PYTHONHASHSEED=1` y `=2`, CSV comparado byte a byte.
- Perfil: cProfile de `Engine.__init__` en LIVE + `run()` sobre las 50.000 primeras líneas (salida en
  `out/prof_live.txt`).
