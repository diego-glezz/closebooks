---
name: bankrec-policies
description: Reglas de casación de Grupo Valtermia que sigue el golden (POLICIES.md) resumidas para decidir partidas, importe y excepción de un movimiento bancario - tabla de excepciones (PARTIAL, SHORT_*, OVERPAYMENT, DUPLICATE_PAYMENT, RETURNED, FX_DIFFERENCE, UNIDENTIFIED), categorías sin partida, terceros que pagan, remesas, FIFO, facturas sustituidas, plazos/retenciones/anticipos y cómo puntúa el evaluador. Úsala siempre que haya que decidir o revisar cómo se concilia un tipo de movimiento o por qué el golden dice otra cosa.
---

# Reglas de conciliación (resumen operativo de POLICIES.md)

El original está en `data/challenge/POLICIES.md`: **ante cualquier duda, léelo**. Los casos resueltos están en
`reference.md`, en esta misma carpeta.

## Cómo puntúa (y qué implica)
- **Allocation F1 (50 %)**: cada par (movimiento, partida) con importe dentro de max(0,01; 0,5 %). Un par sobrante baja
  la precisión, y uno que falta, el recall.
- **Transaction accuracy (30 %)**: el conjunto completo del movimiento (partidas + importes + excepciones; o categoría
  + excepción) tiene que ser exacto.
- **Exception macro-F1 (20 %)**: la media del F1 de cada clase. Una clase rara (DUPLICATE_PAYMENT, SHORT_WITHHOLDING,
  SHORT_BANK_CHARGES) pesa lo mismo que PARTIAL, así que **acertar las raras vale mucho**.

## Importe de cada fila (§2)
| Caso | amount |
|---|---|
| Pago normal | saldo abierto de la partida |
| Abono compensado en un pago | importe completo del abono, **positivo**, sin excepción |
| SHORT_DISCOUNT / SHORT_BANK_CHARGES / SHORT_WITHHOLDING / FX_DIFFERENCE / OVERPAYMENT | **saldo abierto completo** |
| PARTIAL / SHORT_DISPUTE | lo **realmente cobrado** para esa partida |
| DUPLICATE_PAYMENT | lo pagado otra vez |
| RETURNED | lo que se reabre (la asignación original) |
Siempre en la **divisa de la partida**, aunque la cuenta sea de otra divisa.

## Excepciones (§3): árbol de decisión
1. ¿La partida ya estaba liquidada y se vuelve a pagar? → `DUPLICATE_PAYMENT` (lo lleva la línea posterior).
2. ¿Vuelve un cobro o pago (recibo SEPA devuelto, cheque impagado, contracargo, transferencia rechazada)? → `RETURNED`
   en **cada** partida de la operación devuelta.
3. ¿La divisa del banco ≠ la de la factura? → `FX_DIFFERENCE` en las facturas que se liquidan completas (sustituye a
   vacío, DISCOUNT, WITHHOLDING y BANK_CHARGES). PARTIAL y DISPUTE se mantienen; los abonos quedan vacíos.
4. ¿Se cobra de menos?
   - Hay descuento en las condiciones (`payment_terms.csv`), la fecha de **apunte** ≤ fecha factura + días + **3 de
     gracia** y cobro = total − % × **total** → `SHORT_DISCOUNT`. Fuera de plazo → `PARTIAL` (queda abierto).
   - Cliente de MX/BR/MA que retiene el 1,25 %, el 1,5 % o el 10 % del total → `SHORT_WITHHOLDING`.
   - Transferencia de fuera de SEPA con merma ≤ 60 EUR equivalentes **y** ≤ 2 % → `SHORT_BANK_CHARGES`.
   - Deducción **con motivo** (normalmente solo visible en el aviso de remesa) → `SHORT_DISPUTE` con importe = lo pagado.
   - En cualquier otro caso → `PARTIAL` (a cuenta, plazos salvo el último, primer cobro con retención de garantía,
     factura con anticipo a deducir).
5. ¿Se cobra de más sobre UNA factura? → `OVERPAYMENT` (el exceso queda a cuenta, sin fila extra).
6. Sin excepción: el **último** plazo, la liberación de una retención, el resto tras un parcial, la liquidación de una
   disputa, un recibo re-presentado y los abonos compensados.
7. Cobro sin partida posible → una fila con categoría `UNIDENTIFIED` y excepción `UNIDENTIFIED`. **No se fuerza**:
   hay cobros no identificados con el mismo importe que una factura ajena.

## Sin partida (§4): categorías
BANK_FEES (también comisiones internacionales, que suelen ir en un apunte aparte el mismo día), CASH_POOL (ambos
lados del barrido), INTERNAL_TRANSFER (misma sociedad), INTERCOMPANY (entre sociedades del grupo), FX_DEAL (las dos
patas), PAYROLL, SOCIAL_SECURITY, TAX_PAYMENT/TAX_REFUND, LOAN_*, LEASING, INSURANCE, CARD_EXPENSES, interés,
BANK_REVERSAL (el error **y** su anulación), CUSTOMER_ADVANCE (pago de proforma), CUSTOMER_REFUND (devolvemos un
duplicado), SUPPLIER_REFUND (nos devuelven un pago duplicado). Si el proveedor reembolsa **su abono**, no es categoría:
liquida el APC. Las liquidaciones de tarjeta **no** son comisiones: liquidan pedidos por su importe **bruto**.

## Cómo elegir las partidas (§5)
- **Paga un tercero**: la matriz (`parent_customer_id`/`group_id`), un banco de confirming/factor
  ("CONFIRMING … ORD. <cliente>") o una agencia de recobro ("EXPTE …") → se asignan las partidas del cliente pagado.
- **Fábrica de pagos**: ES00 paga facturas de proveedor de ES01, ES02 y PT01.
- **Lotes**: `direct_debit_batches/`, `card_settlements/`, `cheque_deposits/` y `payment_runs/` (por `end_to_end_id`
  en transfers; el total en los agregados; una cuenta por vencimiento en confirming).
- **Avisos de remesa**: pueden llegar días después; usan números sin prefijo o del cliente; un cliente grande cambia
  de formato en sept. 2025.
- **Referencias ruidosas**: sin prefijo ni ceros, con erratas o dígitos traspuestos, cortadas a 16/35/38, con el pedido
  del cliente, con una factura anulada o sin referencia (solo nombre o IBAN).
- **Mismo importe, varias abiertas**: FIFO por fecha de factura. Si el texto nombra un mes ("MANT. MAYO 25") → la
  factura de ese mes de servicio, que se emite a principios del mes siguiente.
- **"Anula y sustituye a X"** (PDF): un pago que cita X liquida la sustituta. X y su abono nunca se liquidan por banco.
- **Plazos, retención de garantía y anticipo**: solo lo dice el PDF de la factura.
- **IBAN nuevo**: lo comunica un PDF; IT01 cambió de cuenta de cobros el 2026-02-01 y las dos cuentas son válidas.

## Dónde está cada cosa en el código
`rules.settle()` (excepciones e importes) · `engine._by_reference` · `engine._by_party` · `engine._from_link`
(lotes) · `engine._from_advice` · `engine._returned` · `rules.category_by_text` · `docs.parse_clauses`.
