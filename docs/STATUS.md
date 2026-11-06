# Estado (actualizar con /handoff)

## main — noche del 6 de noviembre

### Cómo medimos
Ventanas de TRAIN que reproducen LIVE (`closebooks run train --start ... --end ...`): seis meses de movimientos,
con los saldos abiertos al inicio reconstruidos desde el golden. **DEV = 2024-H2** (para iterar) y
**HOLDOUT = 2025-H1** (solo para confirmar). `sample` infravalora: no tiene historial.

### Evolución (evaluador oficial)
| Versión | DEV 2024-H2 | HOLDOUT 2025-H1 |
|---|---|---|
| Línea base (motor de la tarde) | 76,24 | 76,03 |
| + avisos: DISCOUNT/RET/disputa, nombres, refs, ap_partial, duplicados | 81,88 | 81,81 |
| + devoluciones (mandato, cheque, tarjeta), retenciones brutas | 82,53 | — |
| + subconjunto único (≤ 5), confirming por vencimiento, planes de pago del PDF | 83,96 | — |
| + avisos estrictos, proveedor dominante, tolerancia FX de POLICIES | 84,13 | 83,81 |
| + duplicados por pagador, sobrepagos (+redondo / 1 % / 10 %) | 85,35 | — |
| + comisiones solo en transferencias transfronterizas fuera de SEPA | **85,99** | **85,68** |

### Dónde quedan los puntos (DEV)
- UNIDENTIFIED: ~15.000 líneas marcadas frente a 4.600 del golden. Son sobre todo cobros de varias facturas sin
  referencia que no conseguimos casar (`ar_ref_noise`, `ar_n_to_m`, `ar_1toN_credit_note`).
- FX_DIFFERENCE: recall 0,69 (cobros en otra divisa sin importe original).
- Clases raras (cada una pesa 1/10 del macro-F1): SHORT_BANK_CHARGES F1 0,53, DUPLICATE_PAYMENT 0,65, OVERPAYMENT 0,75.
- `ar_same_amount_fifo`: elegimos otra factura con el mismo importe (cadena de errores de saldo).

### Herramientas nuevas
`closebooks report` (dónde se pierden puntos), `closebooks diff --start --end --rule`, ventanas de evaluación y
modelo de categorías aprendido de TRAIN (`learn.py`).
