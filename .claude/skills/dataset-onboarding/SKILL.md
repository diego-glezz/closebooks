---
name: dataset-onboarding
description: Descargar, verificar y poner en marcha una fase de datos del reto Valtermia (train-lite, train, live, final) - SHA-256, estructura de carpetas, primera ejecución, comprobaciones de cordura y drift (camt.053 de Moncayo, filial ZA01 con ZAR, masters_final, cambio de formato de avisos). Úsala cuando lleguen datos nuevos, cuando `closebooks run` falle o puntúe raro en una fase, o al preparar el FINAL de las 18:00.
---

# Datos nuevos: de la descarga al primer CSV válido

## Descarga (`https://hackathon.usetalky.com/datos`)
| Paquete | Tamaño | SHA-256 |
|---|---|---|
| bankrec-docs-and-masters.zip | 10 MB | 210b2404efd011af248a251db2bc0ee85a2ddaa1d56a7a0ca0b95ede18cc1071 |
| bankrec-train-lite.tar.gz | 460 MB | 71defe3aa552855daae9338d83234438f1de40f0d7b7ec7cdb56327b4c11df1e |
| bankrec-train.tar.gz | 657 MB | 163f51a818193f6310b6fba96e3830e4347c883b0b84bc070153f5018e3f06b8 |
| bankrec-live.tar.gz | 87 MB | eec490d7c605048d0d3a8400af97ee13c9fe49e68dfeb3d68546daf50d8af520 |
| bankrec-final.tar.gz | sale a las 18:00 | ver en la web |

URLs: `https://hackathon.usetalky.com/data/v1/<paquete>`. Se descargan a `data/challenge/_downloads/`, se verifica
el `sha256sum` y se extraen en `data/challenge/`, de modo que queden `masters/ sample/ train/ live/ final/ starter/`.
No dependas del wifi del recinto: descarga en casa.

## Primera ejecución
1. `uv run closebooks info`: tiene que listar la fase.
2. `uv run closebooks run <fase>`: carga, enlaza, casa, escribe `out/<fase>/submission.csv.gz` y, si no hay golden,
   lo **valida con el evaluador oficial** (sin errores y cubriendo el 100 % de los movimientos).
3. Tiempos: anota load, setup y match. Para el FINAL (220k líneas) la ejecución completa tiene que tardar **< 20 min**.
   La extracción de PDFs se cachea en `out/cache/pdf_text_<fase>.json` (el segundo run es más rápido).

## Comprobaciones de cordura
- `explain.csv.gz`: distribución de `rule`. Si `unidentified` o `default_debit` se disparan respecto a `train`, hay
  drift: un formato de texto nuevo, cuentas o clientes nuevos.
- LIVE/FINAL traen `open_items_at_start.csv.gz`: comprueba que se cargan (`len(ph.items)`) y que `open_c` < `total_c`
  en las parcialmente cobradas.
- Las fechas de `bank_transactions` tienen que caer dentro de la fase.

## Drift conocido (POLICIES §6)
- **Banco del Moncayo pasa de N43 a camt.053 el 2025-10-01**: textos distintos, `counterparty_*`, `end_to_end_id`.
  Revisa `closebooks diff` en líneas `ES01-MONC-*` posteriores a esa fecha.
- **Cambio de formato de avisos de un cliente grande en sept. 2025**: revisa `docs.load_advices` (líneas sin parsear).
- **FINAL: nueva filial ZA01** (banco nuevo, ZAR, numeración "KWI…", renombrada el 2026-04-01) y
  `final/masters_final/` (lo carga `data.load_masters` automáticamente). Comprueba que hay tipos ZAR en `fx_rates`.
- Clientes renombrados o que cambian de banco: el nombre impreso en la factura también se indexa (`Parties`).
