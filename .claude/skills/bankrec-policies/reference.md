# Casos resueltos (de https://hackathon.usetalky.com/ejemplos, datos de TRAIN)

Formato: segmento → qué se ve en el banco → respuesta → trampa.

| Segmento | Banco | Respuesta | Trampa |
|---|---|---|---|
| ar_1to1 | "PAGO FRA. FV25-0400572", importe exacto | 1 fila, sin excepción | buscar el item_id en el texto (el banco solo trae el nº impreso) |
| ar_ref_noise | solo nombre del ordenante ("VILLAMAR D…"), importe exacto | la única abierta de ese tercero con ese importe | quedarse sin respuesta por no haber número |
| ar_same_amount_fifo | varias abiertas con el mismo importe | la más antigua (FIFO por fecha de factura) | elegir al azar o repartir |
| ar_1toN_remittance | "AVISO 8826032134" | facturas del aviso de remesa, una fila por factura | buscar combinaciones que sumen (hay muchas) |
| ar_1toN_credit_note | factura − abono | factura completa + abono (positivo), sin excepción | marcar PARTIAL y olvidar el abono |
| ar_1toN_text | "FV22-0519411 0516064" (resto sin prefijo, texto partido en registros de 38) | todas las citadas | leer solo el primer número |
| ar_instalment | "PLAZO 2/3 FRA …" | importe cobrado, PARTIAL (el último plazo, sin excepción) | tratarlo como pago de menos |
| ar_partial | cita la factura y paga una parte | importe cobrado, PARTIAL | poner el total en amount |
| ar_short_discount | pago dentro del plazo de descuento | total de la factura, SHORT_DISCOUNT | amount = cobrado + PARTIAL |
| ar_short_bank_charges | "TRANSFER. EN DIV." con merma pequeña | total, SHORT_BANK_CHARGES | confundir con un parcial |
| ar_short_withholding | cliente extranjero (MX/BR/MA) | totales, SHORT_WITHHOLDING en las facturas afectadas | — |
| ar_short_dispute | remesa con deducción con motivo (en el aviso) | esa factura: lo pagado + SHORT_DISPUTE; el resto, sin excepción | aplicar el descuento a otra factura |
| ar_overpayment | paga algo más de lo debido | importe de la factura, OVERPAYMENT | amount = cobrado |
| ar_duplicate_payment | segundo cobro de una factura ya cobrada | misma factura, DUPLICATE_PAYMENT | buscarle otra factura parecida |
| ar_fx | USD cobrado en una cuenta EUR ("Importe original 1610,69 USD") | amount en USD, FX_DIFFERENCE | escribir el importe en EUR |
| ar_third_party_parent | paga la matriz | facturas de la filial | buscar solo del ordenante |
| ar_third_party_agency | "RECOBROS … EXPTE …" | factura del deudor citado | descartar porque el ordenante no es cliente |
| ar_third_party_confirming | "ORD. PRD DEPURACION … FRA 0502021" | factura del ordenante real | tratar al banco como cliente |
| ar_dd_returned | "DEVOL.RECIBOS … MANDATO VTM-ES-…" (negativo) | factura del recibo, RETURNED | clasificarlo como comisión |
| ar_cheque_returned | "RETURNED ITEM CHK … NSF" | factura del cheque, RETURNED | buscar una factura negativa |
| ar_dd_batch | "ABONO REMESA SEPA CORE 0323…" | todas las facturas del fichero de la remesa | casar el total con una sola factura |
| ar_card_settlement | "LIQUIDACION REMESA DE COMERCIOS" (neto) | pedidos por su **bruto** | usar el neto |
| ar_retention | PDF: retención del 10 %; paga el 90 % | importe pagado, PARTIAL | marcarlo como disputa |
| ar_advance_deduction | PDF: "a deducir anticipo PF…" | neto pagado, PARTIAL | buscar un abono que no existe |
| ar_replaced_invoice | cita una factura anulada | la factura sustituta (PDF "anula y sustituye") | casar con la anulada |
| ar_new_iban | IBAN desconocido; un PDF anuncia el cambio | factura del cliente | descartarlo por IBAN desconocido |
| ap_payment_single | "PAGO TRANSFERENCIAS …" con el nº del proveedor | API, sin excepción | usar nuestro nº de pedido |
| ap_confirming | "PAGO CONFIRMING REMESA CF…" | una fila por factura del vencimiento | casarlo con un solo proveedor |
| ap_factored | "TRANSFERENCIA A NORTE PAGOS CONFIRMING EFC" | facturas cedidas a ese factor (PDF) | no reconocer al factor |
| ap_direct_debit | recibo del proveedor | su factura | clasificarlo como gasto |
| nonitem_* | comisión, cash pool, nóminas, impuestos | 1 fila con categoría | forzar una factura |
| unidentified(_lookalike) | ingreso en efectivo, o el mismo importe que una factura ajena | UNIDENTIFIED + UNIDENTIFIED | casar solo por importe |

En `sample` aparecen 64 segmentos; `closebooks run sample` muestra la exactitud de cada uno.
