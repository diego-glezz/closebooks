# Notas del dataset (Valtermia)

## Descargado
- `bankrec-docs-and-masters.zip` (SHA verificado) → `data/challenge/{README,FORMAT,POLICIES,DATA_DICTIONARY}.md`,
  `masters/`, `sample/` (2025-06 con golden) y `starter/` (con el evaluador oficial).
- Pendiente: `train-lite` (460 MB), `live` (87 MB) y `final` (18:00 del sábado).

## Hallazgos en sample
- 8.499 movimientos, 36.574 filas golden, 64 segmentos. Formatos: n43 3.868, mt940 2.537, camt053 2.094.
- Las partidas de la muestra incluyen todas las del golden, pero **no hay historial anterior**: partidas ya cobradas
  antes de junio aparecen abiertas por su total. Afecta a FIFO, al resto de parciales y a disputas. En TRAIN se
  procesa desde 2021, y LIVE/FINAL traen `open_items_at_start`.
- Los ficheros de detalle (remesas, avisos) están casi todos en UTF-8; hay fallback a cp1252.
- Avisos de remesa: 4 formatos (csv con `,` o `;` y metadatos en la cabecera; txt tipo correo con "Subject: …
  <ref>"; xlsx con filas de título; pdf con un valor por línea). La referencia del aviso aparece en el texto del banco.
- Payment runs: `transfers` (1 cargo por `end_to_end_id`; N43 corta la ref a 16 caracteres; el texto trae
  "/REF <e2e completo>"), `confirming` (cargos por vencimiento, a menudo en un mes posterior al de la remesa).
- Cuentas propias del grupo en `bank_accounts.csv`: si la contraparte es una de ellas → INTERNAL_TRANSFER,
  INTERCOMPANY o CASH_POOL.

## Preguntas abiertas
- ¿Confirming: el cargo de cada vencimiento = facturas con ese `due_date`? (verificar en TRAIN)
- ¿Cuántas categorías se resuelven solo por texto frente a por código de banco? (aprender de TRAIN)
