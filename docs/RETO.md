# El reto en 5 minutos

## Objetivo
Automatizar la **conciliación bancaria** de Grupo Valtermia, una multinacional ficticia con 17 sociedades, 13 bancos y
12 divisas. Es lo que tesorería hace a mano cada día: mirar cada movimiento del banco y decidir **a qué corresponde**.

Gana el equipo con **más nota en el FINAL**: un único envío con datos nuevos que se publican a las 18:00.

## Qué nos dan
Para cada fase (TRAIN, LIVE y FINAL):

- **Movimientos bancarios** (`bank_transactions`): fecha, cuenta, importe, divisa y un texto bancario sucio, del estilo
  "TRANSF FERRETERIA GOM FRA 4567…". Para LIVE son unos 203.000.
- **Partidas abiertas**: facturas a clientes (`ARI-`), de proveedores (`API-`) y abonos (`ARC-`/`APC-`), con su saldo
  pendiente al inicio. Casi un millón.
- **Información de apoyo**:
  - avisos de pago de clientes ("os pago estas 300 facturas"), en CSV, TXT, Excel o PDF;
  - remesas de recibos domiciliados;
  - liquidaciones de tarjetas y cheques;
  - órdenes de pago a proveedores;
  - **PDFs de facturas**, con cláusulas como "pago 2 de 3", "sustituye a la factura X" o "retención del 5 %".
- **Datos maestros**: clientes, cuentas bancarias, sociedades, tipos de cambio y la lista de categorías.
- **Las reglas**: `data/challenge/POLICIES.md` explica cómo se decide cada caso, y la solución correcta las sigue al
  pie de la letra.
- **Solo en TRAIN**, 4 años con **las respuestas correctas** (`golden_allocations`) y el **evaluador oficial**, para
  medirnos en casa.

| Fase | Fechas | Qué es |
|---|---|---|
| TRAIN | 2021-07 → 2025-06 | Datos + respuestas correctas, para aprender y medirse |
| LIVE | 2025-07 → 2025-12 | Sin respuestas; ranking en directo (1 envío cada 3 min, 20 al día) |
| FINAL | 2026-01 → 2026-06 | Sale a las 18:00. **Un solo envío**, cierre 19:30, resultado a las 20:30 |

En LIVE y FINAL hay **cambios a propósito** (drift): un banco cambia de formato, un cliente cambia sus avisos de pago y
aparece una filial nueva en Sudáfrica (ZAR).

## Qué tenemos que devolver
**Un CSV** con al menos una fila por cada movimiento bancario (formato completo en `data/challenge/FORMAT.md`):

```
bank_txn_id,item_id,amount,category,exception
BT-…1234,ARI-2025-0456789,1210.00,,              ← este cobro paga esta factura, 1.210 €
BT-…1234,ARI-2025-0456790,726.00,,               ← …y también esta, 726 €
BT-…1240,,,BANK_FEES,                            ← no paga facturas: es una comisión
BT-…1300,ARI-2025-0400001,980.00,,SHORT_DISCOUNT ← paga, pero descontando pronto pago
BT-…1301,,,UNIDENTIFIED,UNIDENTIFIED             ← no sabemos de quién es
```

**Nota = 100 × (0,5 · F1 de facturas emparejadas + 0,3 · movimientos completamente bien + 0,2 · macro-F1 de
excepciones)**, con un margen de max(0,01; 0,5 %) en los importes.

Emparejar mal resta dos veces (en precisión y en el movimiento), así que **ante la duda es mejor no emparejar**.

## Qué tiene que hacer nuestro algoritmo
Para cada movimiento, responder tres preguntas:

1. **¿Qué partidas abiertas liquida, y cuánto de cada una?** Puede ser una factura, 300, o una parte de una.
2. **Si no liquida ninguna, ¿qué es?** Comisión, nómina, impuesto, préstamo, traspaso interno…
3. **¿Cuadra el dinero?** Si no, marcar la excepción: `PARTIAL`, `OVERPAYMENT`, `SHORT_DISCOUNT`,
   `SHORT_BANK_CHARGES`, `SHORT_WITHHOLDING`, `SHORT_DISPUTE`, `FX_DIFFERENCE`, `DUPLICATE_PAYMENT`, `RETURNED` o
   `UNIDENTIFIED`.

## Cómo lo hace nuestro motor (`closebooks`)
- **Recorre los movimientos en orden cronológico** con un "libro" de facturas abiertas que lleva el saldo vivo. Cuando
  un cobro paga parte de una factura, el saldo baja, y el siguiente cobro ve lo que queda (`bankrec/book.py`).
- Para cada movimiento prueba **una cascada de estrategias, de la más segura a la menos** (`bankrec/engine.py`):
  1. **Ficheros de detalle:** si el apunte corresponde a una remesa, un aviso de pago, un lote de tarjetas o una orden
     de pagos, el propio fichero dice qué facturas incluye.
  2. **Categoría:** comisiones, nóminas, impuestos…, a partir del texto y de lo aprendido en TRAIN.
  3. **Devoluciones** de recibos.
  4. **Números de factura en el texto**, aunque estén cortados o les falten ceros.
  5. **Quién paga más el importe:** identificar al cliente por IBAN, mandato o nombre, y buscar qué facturas suyas
     suman ese importe (la más antigua primero, con descuentos, combinaciones…).
  6. **Si nada encaja, `UNIDENTIFIED`**, mejor que inventarse una pareja.
- Una vez elegidas las facturas, **`bankrec/rules.py` aplica POLICIES** para decidir el importe de cada una y la
  excepción.
- **Cada línea guarda qué regla la decidió** (`out/<fase>/explain.csv.gz`), para poder depurarlo y explicárselo al
  jurado.

| Referencia | Nota |
|---|---|
| Emparejamiento ingenuo (starter) | ≈ 19 |
| Heurística buena | ≈ 61 |
| **closebooks**, ventana DEV (TRAIN 2024-H2) | **85,99** |
| **closebooks**, ventana HOLDOUT (TRAIN 2025-H1) | **85,68** |

Siguiente lectura: `README.md` (instalación y comandos), `docs/PLAYBOOK.md` (roles y horario) y `CLAUDE.md` (reglas
del proyecto).
