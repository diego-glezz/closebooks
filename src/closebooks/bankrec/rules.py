"""Policy rules (POLICIES.md): how cash vs open amounts becomes amounts + exceptions,
FX conversion, and categories for lines that settle no item.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from closebooks.bankrec.book import Book, Item
from closebooks.bankrec.text import fold

FULL_CLEAR = (
    "SHORT_DISCOUNT",
    "SHORT_BANK_CHARGES",
    "SHORT_WITHHOLDING",
    "FX_DIFFERENCE",
    "OVERPAYMENT",
)
WITHHOLDING_COUNTRIES = {"MX", "BR", "MA"}
WITHHOLDING_RATES = (1.25, 1.5, 10.0)
SEPA = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "GR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
    "IS",
    "LI",
    "NO",
    "CH",
    "GB",
    "MC",
    "SM",
    "AD",
    "VA",
}


@dataclass(slots=True)
class Alloc:
    item: str
    amount_c: int  # in item currency, positive
    exception: str = ""


class FX:
    def __init__(self, fx: pd.DataFrame):
        self.dates: dict[str, list[str]] = {}
        self.rates: dict[str, list[float]] = {}
        for ccy, g in fx.sort_values("date").groupby("currency"):
            self.dates[ccy] = g["date"].tolist()
            self.rates[ccy] = g["rate"].tolist()

    def per_eur(self, ccy: str, day: str) -> float | None:
        if ccy == "EUR":
            return 1.0
        ds = self.dates.get(ccy)
        if not ds:
            return None
        i = bisect.bisect_right(ds, day) - 1
        return self.rates[ccy][max(i, 0)]

    def convert(self, cents: int, src: str, dst: str, day: str) -> int | None:
        if src == dst:
            return cents
        a, b = self.per_eur(src, day), self.per_eur(dst, day)
        if a is None or b is None:
            return None
        return round(cents / a * b)


@dataclass
class Ctx:
    """What the exception rules need to know about the bank line."""

    txn: str
    booking: str
    cash_c: int  # |amount| in account currency
    ccy: str  # account currency
    orig_c: int  # instructed amount (0 if none)
    orig_ccy: str
    international: bool  # cross-border from outside SEPA (charges possible)
    payer_country: str


def _days(d: str) -> date:
    return date.fromisoformat(d[:10])


def discount_c(it: Item, terms: dict[str, tuple[float, int]], booking: str) -> tuple[int, bool]:
    """(discount cents, within terms?) for an invoice, 0 if its terms have no discount."""
    pct, days = terms.get(it.terms, (0.0, 0))
    if not pct or it.is_credit:
        return 0, False
    disc = round(it.total * pct / 100)
    within = _days(booking) <= _days(it.issue) + timedelta(days=days + 3)
    return disc, within


def settle(
    book: Book,
    ids: list[str],
    ctx: Ctx,
    fx: FX,
    terms: dict[str, tuple[float, int]],
    allow_partial: bool = True,
) -> list[Alloc] | None:
    """Split the cash of a bank line over a set of items following POLICIES §2-3.
    Returns None if the cash cannot be explained by these items (caller tries something else)."""
    if not ids:
        return None
    items = [book.items[i] for i in ids]
    ccys = {it.ccy for it in items}
    if len(ccys) != 1:
        return None
    iccy = ccys.pop()
    fx_line = iccy != ctx.ccy
    if fx_line:
        if ctx.orig_c and ctx.orig_ccy == iccy:
            cash = ctx.orig_c
        else:
            conv = fx.convert(ctx.cash_c, ctx.ccy, iccy, ctx.booking)
            if conv is None:
                return None
            cash = conv
    else:
        cash = ctx.cash_c
    signed = [(it, book.open[it.id] if book.open[it.id] > 0 else it.total) for it in items]
    total = sum(-o if it.is_credit else o for it, o in signed)
    invoices = [(it, o) for it, o in signed if not it.is_credit]
    credits = [(it, o) for it, o in signed if it.is_credit]
    diff = total - cash  # >0: paid less, <0: paid more
    tol = 1 + len(items) // 10

    def full(exc_inv: str = "") -> list[Alloc]:
        return [Alloc(it.id, o, exc_inv) for it, o in invoices] + [
            Alloc(it.id, o, "") for it, o in credits
        ]

    # FX (POLICIES §3): the payer pays the invoice amount; the bank converts at its own rate,
    # worse than the reference by 0.1-0.6 % (G10) or 0.4-1.5 % (emerging). With the instructed
    # amount in the item currency the match is exact; otherwise allow that spread only.
    if fx_line:
        if ctx.orig_c and ctx.orig_ccy == iccy:
            ok = abs(diff) <= max(3, 0.005 * total)
        else:
            ok = -max(3, 0.004 * total) <= diff <= max(3, 0.018 * total)
        if ok:
            return full("FX_DIFFERENCE")
    if abs(diff) <= tol:
        return full("")
    if diff < 0:
        # a real overpayment is a small excess over ONE invoice; a big excess means we are
        # missing items (the caller will look for them)
        small = -diff <= max(10000, 0.05 * total)
        if len(invoices) == 1 and not fx_line and small:
            return full("OVERPAYMENT")
        return None
    # paid less: discount within terms?
    discs = [(it, *discount_c(it, terms, ctx.booking)) for it, _ in invoices]
    d_in = sum(d for _, d, w in discs if d and w)
    d_all = sum(d for _, d, _ in discs if d)
    if d_in and abs(diff - d_in) <= tol + len(invoices):
        ok = {it.id for it, d, w in discs if d and w}
        return [Alloc(it.id, o, "SHORT_DISCOUNT" if it.id in ok else "") for it, o in invoices] + [
            Alloc(it.id, o, "") for it, o in credits
        ]
    late_discount = d_all and not d_in and abs(diff - d_all) <= tol + len(invoices)
    # withholding at source
    if ctx.payer_country in WITHHOLDING_COUNTRIES and not late_discount:
        for rate in WITHHOLDING_RATES:
            wh = sum(round(it.total * rate / 100) for it, _ in invoices)
            if abs(diff - wh) <= tol + len(invoices):
                return full("SHORT_WITHHOLDING")
    # intermediary bank charges: <= 60 EUR equivalent and <= 2 % of the item
    if ctx.international and len(invoices) >= 1 and not late_discount:
        eur = fx.convert(diff, iccy, "EUR", ctx.booking) or diff
        biggest = max(o for _, o in invoices)
        if eur <= 6000 and diff <= 0.02 * biggest:
            return full("SHORT_BANK_CHARGES")
    if not allow_partial:
        return None
    # PARTIAL: oldest items cleared in full, the remainder on the last one
    out: list[Alloc] = [Alloc(it.id, o, "") for it, o in credits]
    remaining = cash + sum(o for _, o in credits)
    inv_sorted = sorted(invoices, key=lambda x: (x[0].issue, x[0].number))
    for k, (it, o) in enumerate(inv_sorted):
        if remaining <= 0:
            break
        if k == len(inv_sorted) - 1 or remaining < o:
            out.append(Alloc(it.id, min(remaining, o), "PARTIAL" if remaining < o else ""))
            remaining -= min(remaining, o)
        else:
            out.append(Alloc(it.id, o, ""))
            remaining -= o
    return out


# ---------------------------------------------------------------- categories

_CATEGORY_RULES: list[tuple[str, str]] = [
    # (regex on folded text, category). Order matters: first match wins.
    (
        r"ANULACION OPERACION INDEBIDA|ANUL\.? OPER|RETROCESION APUNTE|REVERSAL OF ERRONEOUS|"
        r"CONTREPASSATION|STORNO FEHL",
        "BANK_REVERSAL",
    ),
    (
        r"CASH ?POOL|NIVELACION SALDO|BARRIDO|ZERO ?BALANC|\bZBA\b|SWEEP|CENTRALISATION TRESO|"
        r"CASH CONCENTRATION|TRASPASO PROPIO",
        "CASH_POOL",
    ),
    (r"NOMINA|PAYROLL|SALAIRES|GEHALT|LOHN|SALARIOS", "PAYROLL"),
    (
        r"SEG(?:URIDAD)?\.? ?SOCIAL|TGSS|URSSAF|SOZIALVERS|SOCIAL SECURITY|ZUS\b|INSS|IMSS",
        "SOCIAL_SECURITY",
    ),
    (
        r"DEVOLUCION (?:IVA|IMPUESTO)|DEV\.? ?(?:IVA|AEAT)|TAX REFUND|REMBOURSEMENT (?:TVA|IMPOT)|"
        r"STEUERERSTATTUNG",
        "TAX_REFUND",
    ),
    (
        r"IMPUESTOS|AEAT|MOD\.? ?\d{3}|HACIENDA|DGFIP|FINANZAMT|TAX PAYMENT|IMPOTS|TVA |IRS |SAT ",
        "TAX_PAYMENT",
    ),
    (
        # before the loans: "PRESTAMO INTRAGRUPO" is intercompany financing (POLICIES §4)
        r"INTERCOMPANY|INTRAGRUPO|FINANCIACION (?:GRUPO|IC)\b|INTRAGROUPE|KONZERN",
        "INTERCOMPANY",
    ),
    (r"LEASING|RENTING|CREDIT[- ]BAIL", "LEASING"),
    (
        r"DISPOSICION (?:PRESTAMO|POLIZA)|LOAN DRAWDOWN|DEBLOCAGE PRET|KREDITAUSZAHLUNG",
        "LOAN_DRAWDOWN",
    ),
    (r"PRESTAMO|AMORTIZACION|LOAN REPAYMENT|ECHEANCE PRET|DARLEHEN|CUOTA PREST", "LOAN_REPAYMENT"),
    (r"SEGURO|POLIZA|PRIMA |INSURANCE|ASSURANCE|VERSICHERUNG", "INSURANCE"),
    (
        r"COMPRAVENTA DIVISA|FX DEAL|FORWARD|SPOT |CHANGE A TERME|DEVISENGESCH|COMPRA DIVISA|"
        r"VENTA DIVISA",
        "FX_DEAL",
    ),
    (
        r"INTERESES? (?:A SU FAVOR|ACREEDORES)|INTEREST CREDIT|INTERETS CREDITEURS|HABENZINS",
        "INTEREST_INCOME",
    ),
    (r"INTERESES?|INTEREST|INTERETS|ZINSEN", "INTEREST_EXPENSE"),
    (
        r"TARJETA(?:S)? (?:EMPRESA|CREDITO)|LIQUIDACION TARJETA|CORPORATE CARD|CARTE AFFAIRES|"
        r"FIRMENKREDITKARTE",
        "CARD_EXPENSES",
    ),
    (
        r"COMISION|COMMISSION|FRAIS|GEBUEHR|GEBUHR|ENTGELT|CHARGES|FEE\b|MANTENIMIENTO CUENTA",
        "BANK_FEES",
    ),
    (
        r"TRASPASO|TRANSFER BETWEEN OWN|VIREMENT INTERNE|UMBUCHUNG|INTERNAL TRANSFER",
        "INTERNAL_TRANSFER",
    ),
]
_COMPILED = [(re.compile(p), c) for p, c in _CATEGORY_RULES]

# Categories that depend on the sign of the line (POLICIES §4). Each pattern holds 100 % of the
# labelled lines in TRAIN: a receipt that pays a proforma is a customer advance; a debit that
# refunds a duplicate payment is a customer refund; a receipt "pago doble" is a supplier refund.
# ("REEMBOLSO <nº>" is not here: it can settle a supplier credit note, an item.)
_RECEIPT_RULES = [
    (re.compile(r"PROFORMA|\bPF\d{2}-\d"), "CUSTOMER_ADVANCE"),
    (re.compile(r"PAGO DOBLE|DOBLE PAGO|PAGO DUPLICADO"), "SUPPLIER_REFUND"),
]
_DEBIT_RULES = [
    (
        re.compile(r"REFUND DUPLICATE|DUPLICATE PAYMENT REFUND|DEVOLUCION PAGO DUPLICADO|PAGO DOBLE"),
        "CUSTOMER_REFUND",
    ),
]


def category_by_text(text: str, receipt: bool | None = None) -> str | None:
    """Category of a line that settles no item, from its text. `receipt` (money in) enables the
    rules that depend on the sign of the line."""
    t = fold(text)
    if receipt is not None:
        for rx, cat in _RECEIPT_RULES if receipt else _DEBIT_RULES:
            if rx.search(t):
                return cat
    for rx, cat in _COMPILED:
        if rx.search(t):
            return cat
    return None


RETURN_RX = re.compile(
    r"DEVOL(?:UCION)?\.? ?(?:DE )?RECIBO|DEVOL\.RECIBOS|IMPAYE|REJET PRLV|RUECKLASTSCHRIFT|"
    r"RUCKLASTSCHRIFT|RETURNED ITEM|RETURN(?:ED)? (?:DD|DEBIT|CHEQUE|CHECK)|CHARGEBACK|"
    r"CONTRACARGO|\bNSF\b|CHEQUE IMPAGADO|CHQ IMPAYE|DEVOLUCION TRANSFERENCIA|RETOUR VIREMENT|"
    r"RETURNED TRANSFER|R-TRANSAC|\bAC0[1-6]\b|\bAM0[1-9]\b|\bMD0[1-7]\b|\bMS0[23]\b|\bSL01\b"
)


_DISCOUNT_RX = re.compile(r"DISCOUNT|DESCUENTO|DTO\b|PRONTO PAGO|SKONTO|ESCOMPTE|REMISE|DESCONTO")
_WITHHOLD_RX = re.compile(
    r"^RET\b|RETENCION|RETENCAO|WITHHOLDING|\bWHT\b|\bISR\b|RETENUE A LA SOURCE|"
    r"QUELLENSTEUER|\bIRRF\b"
)


def deduction_kind(reason: str) -> str:
    """Classify a remittance-advice deduction: discount | withholding | dispute."""
    t = fold(reason).strip()
    if not t:
        return "dispute"
    if _DISCOUNT_RX.search(t):
        return "discount"
    if _WITHHOLD_RX.search(t):
        return "withholding"
    return "dispute"


def deduction_kind_by_amount(
    it: Item, deduction_c: int, terms: dict[str, tuple[float, int]], payer_country: str
) -> str:
    """Classify a deduction that states no reason (the 2025-09 advice layout), by its amount:
    the early-payment discount of the invoice terms, a tax withheld at source (POLICIES §3), or
    else a dispute. The discount may be taken late: it is still a discount (-> PARTIAL)."""
    ded = abs(deduction_c)
    pct, _days_ = terms.get(it.terms, (0.0, 0))
    if pct and not it.is_credit and abs(ded - round(it.total * pct / 100)) <= 1:
        return "discount"
    if payer_country in WITHHOLDING_COUNTRIES and any(
        abs(ded - round(it.total * rate / 100)) <= 1 for rate in WITHHOLDING_RATES
    ):
        return "withholding"
    return "dispute"


def unique_subset(
    values: list[int], target: int, tol: int = 1, max_solutions: int = 2
) -> list[int] | None:
    """Indices of the ONLY non-empty subset of `values` whose sum is within `tol` of `target`
    (meet-in-the-middle, fine up to ~24 values). None if there is no solution or several:
    with cent-level amounts a unique exact subset is very strong evidence, an ambiguous one is not.
    """
    n = len(values)
    if n == 0:
        return None
    h = n // 2

    def sums(vals: list[int]) -> list[tuple[int, int]]:
        out = [(0, 0)]
        for i, v in enumerate(vals):
            out += [(s + v, m | (1 << i)) for s, m in out]
        return out

    right: dict[int, list[int]] = {}
    for s, m in sums(values[h:]):
        lst = right.setdefault(s, [])
        if len(lst) < max_solutions:
            lst.append(m)
    found: list[tuple[int, int]] = []
    for s, ml in sums(values[:h]):
        for t in range(target - s - tol, target - s + tol + 1):
            for mr in right.get(t, ()):
                if ml or mr:
                    found.append((ml, mr))
                    if len(found) >= max_solutions:
                        return None
    if len(found) != 1:
        return None
    ml, mr = found[0]
    return [i for i in range(h) if ml >> i & 1] + [h + i for i in range(n - h) if mr >> i & 1]
