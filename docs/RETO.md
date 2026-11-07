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
| LIVE | 2025-07 → 2025-12 | Sin respuestas; ranking en directo (1 envío cada 10 s; cuenta la mejor nota) |
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

## Mini ejemplo (datos inventados)

### Libro de partidas abiertas al empezar
| Factura | Cliente | Importe | Condiciones |
|---|---|---|---|
| ARI-…001 (nº F-1001) | Ferretería Gómez | 1.210,00 € | 2 % de descuento si paga en 10 días (emitida el 01/03) |
| ARI-…002 (nº F-1002) | Ferretería Gómez | 500,00 € | — |
| ARI-…003 (nº F-1003) | Riegos Díaz | 2.000,00 € | — |

El motor conoce el IBAN de Ferretería Gómez por los datos maestros.

### Los movimientos, en orden de fecha

**① 05/03 · −27,50 € · "COMISION MANTENIMIENTO CUENTA"**
- El modelo aprendido y el texto dicen claramente que es una comisión.
- → **`BANK_FEES`**, sin factura. Regla `fees`.

**② 08/03 · +1.185,80 € · "TRANSF FERRETERIA GOMEZ"** (sin número de factura)
- Pagador: el IBAN del ordenante es el de Ferretería Gómez.
- Referencia: en el texto no hay número de factura, así que se pasa al siguiente paso.
- Tercero + importe:
  - ¿Hay una factura de Gómez de 1.185,80 € exactos? No.
  - ¿Descuento por pronto pago? 1.210 − 2 % = **1.185,80**, dentro de los 10 días. ✔
- `settle` → **ARI-…001 por 1.210,00 € con `SHORT_DISCOUNT`**. El importe es el de la factura completa, porque queda
  saldada. Regla `party_discount`.
- 📒 Libro: ARI-…001 pasa a **0 €**.

**③ 12/03 · +1.000,00 € · "TRF RIEGOS DIAZ FRA 1003"**
- Referencia: "1003" coincide con el nº de **ARI-…003**, de Riegos Díaz.
- `settle`: la factura vale 2.000 y paga 1.000. No es descuento, ni retención, ni comisión.
- → **ARI-…003 por 1.000,00 € con `PARTIAL`**. Regla `ref_partial`.
- 📒 Libro: ARI-…003 pasa de 2.000 € a **1.000 € pendientes**.

**④ 20/03 · +500,00 € · "TRANSF FERRETERIA GOMEZ"**
- Pagador: Gómez, por el IBAN. Sin referencia.
- Tercero + importe: ¿hay una factura **abierta** de Gómez de 500 €? Sí, ARI-…002.
- La ARI-…001 ya no es candidata porque el libro la tiene a 0 desde el movimiento ②.
- → **ARI-…002 por 500,00 €**, sin excepción. Regla `party_amount`.

**⑤ 25/03 · +1.000,00 € · "TRF RIEGOS DIAZ FRA 1003"**
- Referencia: otra vez ARI-…003. **El libro dice que solo quedan 1.000 €**, no 2.000.
- `settle`: 1.000 pagados contra 1.000 pendientes, así que cuadra.
- → **ARI-…003 por 1.000,00 €**, sin excepción. La factura queda saldada. Regla `ref_completed`.
- Sin el saldo vivo, el motor habría visto 2.000 € pendientes y habría marcado otro `PARTIAL` por error.

**⑥ 28/03 · +350,00 € · "INGRESO"**
- No hay IBAN conocido, ni nombre, ni número de factura, y el modelo de categorías no lo reconoce.
- Podría casarlo con cualquier factura de 350 € de cualquier cliente, pero sería adivinar.
- → **`UNIDENTIFIED`**. Regla `unidentified`.

### Lo que entregamos
```
bank_txn_id,item_id,amount,category,exception
BT-1,,,BANK_FEES,
BT-2,ARI-…001,1210.00,,SHORT_DISCOUNT
BT-3,ARI-…003,1000.00,,PARTIAL
BT-4,ARI-…002,500.00,,
BT-5,ARI-…003,1000.00,,
BT-6,,,UNIDENTIFIED,UNIDENTIFIED
```

En `explain.csv.gz` queda la regla de cada línea (`fees`, `party_discount`, `ref_partial`, `party_amount`,
`ref_completed`, `unidentified`). Si el evaluador dice que la línea 3 está mal, sabemos qué estrategia revisar.

### Las 3 ideas que enseña
1. **Cascada:** primero lo más seguro (categoría clara, referencia) y después lo más deductivo (quién paga y cuánto).
2. **Saldo vivo:** el movimiento ⑤ solo sale bien porque el ③ ya había bajado el saldo de la factura.
3. **Ante la duda, no emparejar:** el ⑥ se queda como no identificado. Un emparejamiento malo resta más que admitir que
   no se sabe.

Siguiente lectura: `README.md` (instalación y comandos), `docs/PLAYBOOK.md` (roles y horario) y `CLAUDE.md` (reglas
del proyecto).
