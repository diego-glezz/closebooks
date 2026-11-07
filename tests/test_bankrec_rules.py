"""Unit tests for text keys, advice parsing and the POLICIES exception rules (no data needed)."""

from __future__ import annotations

import pandas as pd
import pytest

from closebooks.bankrec.book import Book
from closebooks.bankrec.docs import Advice, _parse_csv_rows, _parse_txt, parse_amount, parse_clauses
from closebooks.bankrec.rules import (
    FX,
    RETURN_RX,
    Ctx,
    category_by_text,
    duplicate_refund_category,
    settle,
)
from closebooks.bankrec.text import number_keys, service_month, text_tokens

TERMS = {"D2N30": (2.0, 10), "T30": (0.0, 0)}


def make_book(*items: tuple) -> Book:
    rows = []
    for iid, number, total, issue, *rest in items:
        terms = rest[0] if rest else "T30"
        ccy = rest[1] if len(rest) > 1 else "EUR"
        rows.append(
            {
                "item_id": iid,
                "kind": iid[:3],
                "side": iid[:2],
                "entity": "ES01",
                "number": number,
                "issue_date": issue,
                "due_date": issue,
                "party_id": "C1",
                "party_name": "Cliente Uno S.L.",
                "currency": ccy,
                "total_c": total,
                "open_c": total,
                "terms": terms,
                "po": "",
                "method": "",
                "related": "",
            }
        )
    return Book(pd.DataFrame(rows))


FX_RATES = FX(pd.DataFrame({"date": ["2025-01-01"], "currency": ["USD"], "rate": [1.10]}))


def ctx(cash: int, booking: str = "2025-06-10", **kw) -> Ctx:
    base = {
        "txn": "BT1",
        "booking": booking,
        "cash_c": cash,
        "ccy": "EUR",
        "orig_c": 0,
        "orig_ccy": "",
        "international": False,
        "payer_country": "ES",
    }
    base.update(kw)
    return Ctx(**base)


# ---------------------------------------------------------------- text


def test_number_keys_cover_noisy_references():
    keys = number_keys("RE250108497")
    assert {"RE250108497", "250108497", "108497", "0108497"} <= keys
    assert "211247" in number_keys("FV25-0211247")
    assert {"FV250211247", "211247"} <= text_tokens("TRANSF | FRAS 211247 FV25-0211247")


def test_service_month():
    assert service_month("MANT. MAY 25") == 5
    assert service_month("SERVICIOS MAYO 2025") == 5
    assert service_month("PAGO FRA 123456") is None


def test_return_regex_no_false_positive_on_transferencia():
    assert not RETURN_RX.search("TRANSFERENCIA SEPA | SEGURIDAD RIBAS")
    assert RETURN_RX.search("DEVOLUCION RECIBO AC04 REM 05250620996")
    assert RETURN_RX.search("RETURNED ITEM CHK 0269257 NSF")


def test_categories():
    assert category_by_text("TRASPASO CASH POOLING NIVELACION SALDO") == "CASH_POOL"
    assert category_by_text("ZBA SWEEP TO HEADER") == "CASH_POOL"
    assert category_by_text("PAG NOMINAS | NOMINAS 01/2022") == "PAYROLL"
    assert category_by_text("IMPUESTOS MOD 111 202110") == "TAX_PAYMENT"
    assert category_by_text("COMISION TRANSF. EXTERIOR") == "BANK_FEES"


def test_duplicate_payment_refund_category_depends_on_sign():
    assert duplicate_refund_category("REFUND DUPLICATE PAYMENT", receipt=False) == "CUSTOMER_REFUND"
    assert duplicate_refund_category("ABONO PAGO DOBLE", receipt=True) == "SUPPLIER_REFUND"
    assert duplicate_refund_category("TRANSF FERRETERIA GOMEZ FRA 1003", receipt=True) is None


def test_intercompany_loan_is_intercompany_not_loan_repayment():
    assert category_by_text("PRESTAMO INTRAGRUPO CUOTA") == "INTERCOMPANY"
    assert category_by_text("CUOTA PRESTAMO HIPOTECARIO") == "LOAN_REPAYMENT"


def test_sign_dependent_categories():
    """POLICIES §4: proforma receipt -> advance; duplicate refund debit -> customer refund."""
    pf = "SEPA-UEBERWEISUNG SVWZ+PROFORMA PF25-0123 RHEI9"
    assert category_by_text(pf, receipt=True) == "CUSTOMER_ADVANCE"
    assert category_by_text("ACH CREDIT X CCD RMR*IV*PF25-0123*PI*10.00", receipt=True) == (
        "CUSTOMER_ADVANCE"
    )
    assert category_by_text(pf) is None  # without the sign nothing is decided
    assert category_by_text(pf, receipt=False) is None  # a debit naming a proforma is not one
    dup = "ACH DEBIT LAKE HARMON POOL VENDOR PMT HPNB REFUND DUPLICATE PAYMENT 123"
    assert category_by_text(dup, receipt=False) == "CUSTOMER_REFUND"
    assert category_by_text(dup, receipt=True) is None
    assert category_by_text("SVWZ+ABONO PAGO DOBLE RHEI", receipt=True) == "SUPPLIER_REFUND"
    # "REEMBOLSO <nº>" may settle a supplier credit note: never a category by text alone
    assert category_by_text("SVWZ+REEMBOLSO FV-25-123 RHEI", receipt=True) is None


# ---------------------------------------------------------------- documents


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("11.302,46", 1130246),
        ("1,295.52", 129552),
        ("3 735,43", 373543),
        ("-21,16", -2116),
        ("3691.71", 369171),
    ],
)
def test_parse_amount(raw, cents):
    assert parse_amount(raw) == cents


def test_parse_email_advice_with_deductions():
    adv = Advice(file="x.txt", entity="ES01", payer="P", received="2025-06-09")
    _parse_txt(
        "Subject: Aviso de pago PAY-2025-001442\n\n"
        "  FV25-0404327       28/02/25       11.302,46  -2.260,49 DAMAGED GOODS\n"
        "  FV25-0404194       26/02/25          725,69\n"
        "  AB25-0400348       03/06/25         -386,16\n",
        adv,
    )
    assert adv.ref == "PAY-2025-001442"
    assert [ln.number for ln in adv.lines] == ["FV25-0404327", "FV25-0404194", "AB25-0400348"]
    assert adv.lines[0].paid == 1130246 - 226049 and adv.lines[0].reason == "DAMAGED GOODS"


def test_parse_csv_advice_semicolon_header():
    adv = Advice(file="x.csv", entity="ES01", payer="P", received="")
    _parse_csv_rows(
        [
            ["payment_ref", "REM/2025/0478", "payment_date", "2025-06-02"],
            ["document", "doc_date", "gross", "discount_or_deduction", "reason", "paid"],
            ["RE240121310", "16/12/2024", "1120.27", "", "", "1120.27"],
            ["TOTAL", "", "", "", "", "1120.27"],
        ],
        adv,
    )
    assert adv.ref == "REM/2025/0478" and len(adv.lines) == 1 and adv.lines[0].paid == 112027


def test_parse_xlsx_advice_with_voucher_layout_from_september_2025():
    # the key account puts ITS voucher first; our invoice number (no prefix) is in "Invoice"
    adv = Advice(file="x.xlsx", entity="AU01", payer="P", received="")
    _parse_csv_rows(
        [
            ["Pool & Spa Sol Pty Ltd", "", "", "", "", "", ""],
            ["Payment advice", "2057001209", "2025-09-01", "", "", "", ""],
            ["Voucher", "Invoice", "PO", "Inv. date", "Amount", "Deduction", "Net paid"],
            ["VCHR65740934", "320871", "4517600404", "2024-09-13", "1044.09", "0", "1044.09"],
            [
                "VCHR24305374",
                "310102",
                "4573929383",
                "2025-04-18",
                "11793.4",
                "-589.67",
                "11203.73",
            ],
        ],
        adv,
    )
    assert adv.ref == "2057001209"
    assert [ln.number for ln in adv.lines] == ["320871", "310102"]
    assert adv.lines[1].gross_c == 1179340 and adv.lines[1].deduction_c == -58967
    assert adv.paid_total == 104409 + 1120373


def test_parse_clauses():
    c = parse_clauses("Esta factura anula y sustituye a la factura FV24-0111238 de 13/06/2024.")
    assert c.replaces == "FV24-0111238"
    assert parse_clauses("Retention 5% (193,55 EUR) withheld until final acceptance").retention
    c = parse_clauses("Cuota de mantenimiento y servicio tecnico correspondiente a mayo 2025.")
    assert (c.service_year, c.service_month) == (2025, 5)


# ---------------------------------------------------------------- POLICIES §2-3


def test_exact_payment_no_exception():
    b = make_book(("ARI-2025-0000001", "FV25-1", 121000, "2025-05-01"))
    assert settle(b, ["ARI-2025-0000001"], ctx(121000), FX_RATES, TERMS)[0].exception == ""


def test_discount_within_terms_clears_full_amount():
    b = make_book(("ARI-2025-0000001", "FV25-1", 100000, "2025-06-01", "D2N30"))
    a = settle(b, ["ARI-2025-0000001"], ctx(98000, "2025-06-12"), FX_RATES, TERMS)
    assert (a[0].amount_c, a[0].exception) == (100000, "SHORT_DISCOUNT")


def test_discount_after_deadline_is_partial():
    b = make_book(("ARI-2025-0000001", "FV25-1", 100000, "2025-05-01", "D2N30"))
    a = settle(b, ["ARI-2025-0000001"], ctx(98000, "2025-06-12"), FX_RATES, TERMS)
    assert (a[0].amount_c, a[0].exception) == (98000, "PARTIAL")


def test_small_overpayment_and_big_excess():
    b = make_book(("ARI-2025-0000001", "FV25-1", 580925, "2025-05-01"))
    a = settle(b, ["ARI-2025-0000001"], ctx(585925), FX_RATES, TERMS)
    assert (a[0].amount_c, a[0].exception) == (580925, "OVERPAYMENT")
    assert settle(b, ["ARI-2025-0000001"], ctx(1500000), FX_RATES, TERMS) is None


def test_credit_note_netted_is_positive_without_exception():
    b = make_book(
        ("ARI-2025-0000001", "FV24-1", 124734, "2025-04-01"),
        ("ARC-2025-0000002", "AB24-1", 18709, "2025-04-02"),
    )
    a = settle(b, ["ARI-2025-0000001", "ARC-2025-0000002"], ctx(106025), FX_RATES, TERMS)
    assert {(x.item[:3], x.amount_c, x.exception) for x in a} == {
        ("ARI", 124734, ""),
        ("ARC", 18709, ""),
    }


def test_fx_invoice_paid_into_eur_account():
    b = make_book(("ARI-2025-0000001", "FV21-1", 161069, "2025-05-01", "T30", "USD"))
    a = settle(b, ["ARI-2025-0000001"], ctx(141380, orig_c=161069, orig_ccy="USD"), FX_RATES, TERMS)
    assert (a[0].amount_c, a[0].exception) == (161069, "FX_DIFFERENCE")


def test_international_bank_charges():
    b = make_book(("ARI-2025-0000001", "FV21-1", 191944, "2025-05-01"))
    a = settle(b, ["ARI-2025-0000001"], ctx(189444, international=True), FX_RATES, TERMS)
    assert (a[0].amount_c, a[0].exception) == (191944, "SHORT_BANK_CHARGES")


def test_bank_charges_never_span_several_invoices():
    b = make_book(
        ("ARI-2025-0000001", "FV21-1", 100000, "2025-05-01"),
        ("ARI-2025-0000002", "FV21-2", 100000, "2025-05-02"),
    )
    a = settle(
        b,
        ["ARI-2025-0000001", "ARI-2025-0000002"],
        ctx(198000, international=True),
        FX_RATES,
        TERMS,
    )
    assert "SHORT_BANK_CHARGES" not in {x.exception for x in a}


def test_bank_charges_must_follow_learned_fee_schedule():
    b = make_book(("ARI-2025-0000001", "FV21-1", 191944, "2025-05-01"))
    ids = ["ARI-2025-0000001"]
    on = ctx(189444, international=True, fees={"EUR": [1500, 2500]})
    off = ctx(189444 + 500, international=True, fees={"EUR": [1500, 2500]})
    assert settle(b, ids, on, FX_RATES, TERMS)[0].exception == "SHORT_BANK_CHARGES"
    assert settle(b, ids, off, FX_RATES, TERMS)[0].exception != "SHORT_BANK_CHARGES"
    # a currency without learned schedule keeps the generic rule
    other = ctx(189444, international=True, fees={"USD": [1185]})
    assert settle(b, ids, other, FX_RATES, TERMS)[0].exception == "SHORT_BANK_CHARGES"


def test_bank_charges_non_eur_fee_on_schedule_needs_no_international_hint():
    b = make_book(("ARI-2025-0000001", "FV21-1", 191944, "2025-05-01", "T30", "USD"))
    ids = ["ARI-2025-0000001"]
    on = ctx(191944 - 1185, ccy="USD", fees={"USD": [1185]})
    off = ctx(191944 - 1186 * 3, ccy="USD", fees={"USD": [1185]})
    assert settle(b, ids, on, FX_RATES, TERMS)[0].exception == "SHORT_BANK_CHARGES"
    assert settle(b, ids, off, FX_RATES, TERMS)[0].exception != "SHORT_BANK_CHARGES"
    # the same fee in EUR is round and still needs the international hint
    e = make_book(("ARI-2025-0000002", "FV21-2", 191944, "2025-05-01"))
    eur = ctx(191944 - 1500, fees={"EUR": [1500]})
    assert (
        settle(e, ["ARI-2025-0000002"], eur, FX_RATES, TERMS)[0].exception != "SHORT_BANK_CHARGES"
    )


def test_fx_line_replaces_short_discount_with_fx_difference():
    # USD invoice paid into an EUR account with a 2 % early-payment discount: POLICIES §3 says
    # the fully cleared row carries FX_DIFFERENCE instead of SHORT_DISCOUNT
    b = make_book(("ARI-2025-0000001", "FV21-1", 100000, "2025-06-01", "D2N30", "USD"))
    c = ctx(90000, orig_c=98000, orig_ccy="USD")
    a = settle(b, ["ARI-2025-0000001"], c, FX_RATES, TERMS)
    assert (a[0].amount_c, a[0].exception) == (100000, "FX_DIFFERENCE")
    # same cash in the invoice currency stays SHORT_DISCOUNT
    b2 = make_book(("ARI-2025-0000002", "FV21-2", 100000, "2025-06-01", "D2N30"))
    a2 = settle(b2, ["ARI-2025-0000002"], ctx(98000), FX_RATES, TERMS)
    assert a2[0].exception == "SHORT_DISCOUNT"


def test_domestic_payer_never_pays_intermediary_bank_charges():
    b = make_book(("ARI-2025-0000001", "FV21-1", 191944, "2025-05-01", "T30", "USD"))
    ids = ["ARI-2025-0000001"]
    fees = {"USD": [1185]}
    foreign = ctx(191944 - 1185, ccy="USD", fees=fees)
    home = ctx(191944 - 1185, ccy="USD", fees=fees, domestic=True)
    assert settle(b, ids, foreign, FX_RATES, TERMS)[0].exception == "SHORT_BANK_CHARGES"
    assert settle(b, ids, home, FX_RATES, TERMS)[0].exception != "SHORT_BANK_CHARGES"


def test_partial_payment_keeps_cash_amount():
    b = make_book(("ARI-2025-0000001", "FV22-1", 82251, "2025-05-01"))
    a = settle(b, ["ARI-2025-0000001"], ctx(20562), FX_RATES, TERMS)
    assert (a[0].amount_c, a[0].exception) == (20562, "PARTIAL")


# ---------------------------------------------------------------- added after TRAIN analysis


def test_unique_subset_accepts_only_unambiguous_solutions():
    from closebooks.bankrec.rules import unique_subset

    vals = [254199, 2824151, 1056989, 70269, 1295305]
    assert unique_subset(vals, 254199 + 2824151) == [0, 1]
    assert unique_subset([100, 100, 200], 200) is None  # three ways to make 200
    assert unique_subset([500, 700], 999) is None


def test_deduction_kind():
    from closebooks.bankrec.rules import deduction_kind

    assert deduction_kind("DISCOUNT") == "discount"
    assert deduction_kind("RET") == "withholding"
    assert deduction_kind("DAMAGED GOODS") == "dispute"
    assert deduction_kind("PENAL. RETRASO") == "dispute"


def test_parse_xlsx_advice_layout_of_2025_09():
    """Voucher | Invoice | PO | Inv. date | Amount | Deduction | Net paid (no reason column)."""
    adv = Advice(file="x.xlsx", entity="AU01", payer="P", received="")
    _parse_csv_rows(
        [
            ["Pool & Spa Sol Pty Ltd", "", "", "", "", "", ""],
            ["Payment advice", "2057001209", "2025-09-01", "", "", "", ""],
            ["Voucher", "Invoice", "PO", "Inv. date", "Amount", "Deduction", "Net paid"],
            ["VCHR65740934", "320871", "4517600404", "2024-09-13", "1044.09", "0", "1044.09"],
            ["VCHR04037863", "306817", "4554233277", "2025-03-10", "4943.81", "-98.88", "4844.93"],
        ],
        adv,
    )
    assert adv.ref == "2057001209" and adv.pay_date == "2025-09-01"
    assert [ln.number for ln in adv.lines] == ["320871", "306817"]  # the invoice, not the voucher
    assert adv.lines[0].gross_c == 104409 and adv.lines[0].paid == 104409
    assert adv.lines[1].deduction_c == -9888 and adv.lines[1].paid == 484493
    assert adv.lines[1].reason == ""


def test_parse_pdf_advice_deduction_and_reason_on_one_line():
    from closebooks.bankrec.docs import _parse_pdf_text

    adv = Advice(file="x.pdf", entity="DE01", payer="P", received="")
    _parse_pdf_text(
        "REMITTANCE ADVICE\nDocument\nDate\nGross\nDeduction\nPaid\n"
        "RE250106321\n04/04/2025\n4,371.12\n4,371.12\n"
        "RE250106102\n02/04/2025\n2,422.74\n-121.13 PENAL. RETRASO\n2,301.61\n"
        "RE250106165\n03/04/2025\n2,708.15\n2,708.15\n",
        adv,
    )
    assert [ln.number for ln in adv.lines] == ["RE250106321", "RE250106102", "RE250106165"]
    ln = adv.lines[1]
    assert (ln.gross_c, ln.deduction_c, ln.paid_c) == (242274, -12113, 230161)
    assert ln.reason == "PENAL. RETRASO"
    assert adv.lines[2].paid == 270815  # the table keeps being read after the deduction


def test_deduction_without_reason_is_classified_by_amount():
    from closebooks.bankrec.rules import deduction_kind_by_amount

    book = make_book(("ARI-2025-0000001", "FV25-1", 100000, "2025-05-20", "D2N30"))
    it = book.items["ARI-2025-0000001"]
    assert deduction_kind_by_amount(it, -2000, TERMS, "ES") == "discount"  # 2 % of the total
    assert deduction_kind_by_amount(it, -1500, TERMS, "ES") == "dispute"
    assert deduction_kind_by_amount(it, -1500, TERMS, "MX") == "withholding"  # 1.5 %
    assert deduction_kind_by_amount(it, -1500, TERMS, "BR") == "withholding"
    plain = make_book(("ARI-2025-0000002", "FV25-2", 100000, "2025-05-20", "T30"))
    assert (
        deduction_kind_by_amount(plain.items["ARI-2025-0000002"], -2000, TERMS, "ES") == "dispute"
    )


def test_returned_never_reopens_more_than_the_document():
    book = make_book(("ARI-2025-0000001", "FV25-1", 100000, "2025-05-20"))
    iid = "ARI-2025-0000001"
    # window that starts after the original payment: the item is still open for its total
    book.apply("BT-RET", iid, 100000, "RETURNED")
    assert book.open[iid] == 100000
    book.apply("BT-PAY", iid, 100000, "")
    assert book.open[iid] == 0
    book.apply("BT-RET2", iid, 100000, "RETURNED")  # with history it does re-open the amount
    assert book.open[iid] == 100000


def test_confirming_debit_set_needs_exact_amount_and_supplier_count():
    from closebooks.bankrec.engine import _fix_set

    amt = {"a": 100, "b": 250, "c": 400, "d": 75, "e": 999}
    assert _fix_set(["a", "b", "c"], [], amt, 750, 3) == ["a", "b", "c"]
    assert _fix_set(["a", "b", "c"], [], amt, 750, 2) is None  # amount right, count wrong
    # one overdue transfer belongs to a later debit: take it out
    assert _fix_set(["a", "b", "c", "e"], [], amt, 750, 3) == ["a", "b", "c"]
    # one transfer due in the next days is paid already: add it
    assert _fix_set(["a", "b"], ["d", "e"], amt, 425, 3) == ["a", "b", "d"]
    # two different ways to fix it: refuse
    amb = {"a": 100, "b": 100, "c": 300}
    assert _fix_set(["a", "b", "c"], [], amb, 400, 2) is None


def test_pdf_payment_plans():
    c = parse_clauses(
        "Forma de pago: 3 vencimientos a 60/90/120 dias (642,98 / 642,98 / 642,99 EUR)."
    )
    assert c.instalments == [64298, 64298, 64299]
    c = parse_clauses("Payment terms: 2 instalments at 30/60 days (38,871.10 / 38,871.11 USD).")
    assert c.instalments == [3887110, 3887111]
    c = parse_clauses("Retention 5% (193,55 EUR). Amount payable at due date: 3.677,57 EUR.")
    assert c.payable_c == 367757
    c = parse_clauses(
        "A deducir anticipo PF24-0100005 de 282,92 EUR. Importe neto a pagar 660,15 EUR."
    )
    assert c.payable_c == 66015


def test_amounts_in_text_are_not_references():
    toks = text_tokens("ACH CREDIT CCD PAYMENT ID:8615117246 RMR*IV*BACKYARD*PI*1013.45")
    assert "101345" not in toks and "8615117246" in toks
    assert "394278" not in text_tokens("PAGO FRA FV24-0322584 IMPORTE 3.942,78 EUR")


def test_name_matching_uses_same_normalization_on_both_sides():
    from closebooks.bankrec.book import Parties

    cust = pd.DataFrame(
        [
            {
                "customer_id": "C1",
                "entity": "US01",
                "name": "Pool & Spa King e Hijos Inc.",
                "trade_name": "",
                "country": "US",
                "iban": "",
                "sepa_mandate_id": "",
                "parent_customer_id": "",
                "group_id": "",
            }
        ]
    )
    sup = pd.DataFrame(columns=["supplier_id", "entity", "name", "country", "iban"])
    p = Parties(cust, sup, {})
    assert p.by_name("ACH CREDIT POOL & SPA KING E HIJOS CCD", {"US01"})[0][0] == "C1"


# ---------------------------------------------------------------- role B: FIFO subset


def test_subset_solutions_finds_every_subset_within_tolerance():
    from closebooks.bankrec.rules import subset_solutions

    values = [100, 250, 300, 50, 400]
    assert sorted(subset_solutions(values, 450, tol=0)) == [[0, 2, 3], [3, 4]]
    assert subset_solutions(values, 10_000) == []
    assert subset_solutions([1] * 14, 7, tol=0, limit=10) is None  # 3432 solutions: too generic


def test_fifo_pick_prefers_the_prefix_and_abstains_on_ties():
    from closebooks.bankrec.rules import fifo_pick

    # items are in FIFO order: [0, 1, 2] is a prefix (0 holes), [0, 2, 4] leaves 2 older unpaid
    assert fifo_pick([[0, 2, 4], [0, 1, 2]], max_holes=2) == [0, 1, 2]
    assert fifo_pick([[0, 2, 3], [0, 1, 3]], max_holes=2) is None  # 1 hole each: tie, abstain
    assert fifo_pick([[0, 2, 3], [0, 1, 4]], max_holes=2) == [0, 2, 3]  # 1 hole beats 2
    assert fifo_pick([[2, 5]], max_holes=2) is None  # 4 holes: too far from FIFO


def test_flags_defaults():
    from closebooks.bankrec.variants import Flags

    f = Flags()
    assert f.two_pass_party and f.ref_cn_subset and f.ref_po  # ON after measuring them
    assert not any((f.ref_corroborate, f.ref_trust_other, f.complete_near, f.trace_ref))
    assert not any((f.iban_learn, f.name_fuzzy, f.ref_fuzzy, f.fifo_subset))
    assert (f.exact_recent_days, f.stale_days) == (0, 0)
    assert (f.subset_recent_days, f.subset_cap, f.subset_max_items) == (240, 22, 5)


# ---------------------------------------------------------------- role-B switches


def _fake_engine(book: Book):
    from types import SimpleNamespace

    from closebooks.bankrec.engine import Engine

    fake = SimpleNamespace(book=book, fx=FX_RATES, terms=TERMS)
    fake._signed = lambda i: Engine._signed(fake, i)
    return Engine, fake


def test_cited_invoices_paid_net_of_a_subset_of_credit_notes():
    """ref_cn_subset: the shortfall is exactly ONE subset of the party's open credit notes."""
    b = make_book(
        ("ARI-2025-0000001", "FV25-1", 100000, "2025-04-01"),
        ("ARI-2025-0000002", "FV25-2", 50000, "2025-04-03"),
        ("ARC-2025-0000003", "AB25-1", 7000, "2025-04-05"),
        ("ARC-2025-0000004", "AB25-2", 2500, "2025-04-06"),
        ("ARC-2025-0000005", "AB25-3", 900, "2025-04-07"),
    )
    engine, fake = _fake_engine(b)
    cns = ["ARC-2025-0000003", "ARC-2025-0000004", "ARC-2025-0000005"]
    inv = ["ARI-2025-0000001", "ARI-2025-0000002"]
    got = engine._net_of_credits(fake, inv, cns, ctx(150000 - 7000 - 900))
    assert {(a.item, a.amount_c, a.exception) for a in got} == {
        ("ARI-2025-0000001", 100000, ""),
        ("ARI-2025-0000002", 50000, ""),
        ("ARC-2025-0000003", 7000, ""),
        ("ARC-2025-0000005", 900, ""),
    }
    # no subset of the credit notes explains the shortfall: leave it to ref_partial
    assert engine._net_of_credits(fake, inv, cns, ctx(150000 - 1234)) is None
    # cash above the cited invoices: not this rule
    assert engine._net_of_credits(fake, inv, cns, ctx(150100)) is None


def test_pdf_clauses_never_touch_invoices_issued_after_the_pdf(monkeypatch):
    """pdf_dated: a 2024 PDF must not reach this year's invoice through the noisy keys."""
    from closebooks.bankrec import engine as eng

    b = make_book(("ARI-2025-0000001", "RE250104461", 1000, "2025-06-26"))
    _, fake = _fake_engine(b)
    monkeypatch.setattr(eng.FLAGS, "pdf_dated", False)
    assert eng.Engine._pdf_items(fake, "RE240104461", "AR", "ES01", "2024-03-01") == [
        "ARI-2025-0000001"
    ]
    monkeypatch.setattr(eng.FLAGS, "pdf_dated", True)
    assert eng.Engine._pdf_items(fake, "RE240104461", "AR", "ES01", "2024-03-01") == []
    assert eng.Engine._pdf_items(fake, "RE250104461", "AR", "ES01", "2025-07-02") == [
        "ARI-2025-0000001"
    ]


def test_weak_citations_are_accepted_only_when_they_agree_on_one_party():
    """ref_corroborate: two short suffixes of the same customer are no coincidence; one suffix of
    each of two customers is."""
    b = make_book(
        ("ARI-2025-0000001", "FV25-0516064", 40000, "2025-04-01"),
        ("ARI-2025-0000002", "FV25-0519411", 25000, "2025-04-03"),
        ("ARI-2025-0000003", "FV25-0777777", 31000, "2025-04-05"),
    )
    b.items["ARI-2025-0000003"].party = "C2"
    engine, fake = _fake_engine(b)
    one, two, other = "ARI-2025-0000001", "ARI-2025-0000002", "ARI-2025-0000003"
    # C1 has two cited documents, C2 only one: C1 is corroborated
    assert engine._corroborated(fake, [one, two, other], set(), ctx(65000)) == [one, two]
    # one weak suffix per customer and no amount agreement: nothing is corroborated
    assert engine._corroborated(fake, [one, other], set(), ctx(99999)) == []
    # a single weak suffix whose amount IS the cash is enough
    assert engine._corroborated(fake, [one, other], set(), ctx(31000)) == [other]
    # strong citations are not part of the vote
    assert engine._corroborated(fake, [one, two], {one, two}, ctx(65000)) == []


def test_cancelling_credit_note_and_its_invoice_are_never_settled():
    from types import SimpleNamespace

    from closebooks.bankrec.engine import Engine

    def row(iid, number, related="", reason=""):
        return {
            "item_id": iid, "kind": iid[:3], "side": "AR", "entity": "ES01", "number": number,
            "issue_date": "2025-05-01", "due_date": "2025-05-01", "party_id": "C1",
            "party_name": "C", "currency": "EUR", "total_c": 1000, "open_c": 1000, "terms": "",
            "po": "", "method": "", "related": related, "reason": reason,
        }  # fmt: skip

    book = Book(
        pd.DataFrame(
            [
                row("ARI-2025-0000001", "FV1"),
                row("ARC-2025-0000001", "NC1", "FV1", "Anulación de factura"),
                row("ARI-2025-0000002", "FV2"),
                row("ARC-2025-0000002", "NC2", "FV2", "Devolución / rectificación"),
            ]
        )
    )
    fake = SimpleNamespace(book=book)
    fake._carried = Engine._carried.__get__(fake)
    Engine._cancel_by_reason(fake)
    assert book.cancelled == {"ARI-2025-0000001", "ARC-2025-0000001"}  # a return stays open


def test_aggregated_run_total_converts_foreign_documents_to_the_account_currency():
    # an EUR account pays a bulk file with USD documents: the debit is the EUR total with the
    # USD documents converted at the reference rate (1.10 USD per EUR here)
    from closebooks.bankrec.engine import _run_total_in

    rows = [
        {"doc_c": 50000, "currency": "EUR"},
        {"doc_c": 110000, "currency": "USD"},
        {"doc_c": -11000, "currency": "USD"},  # credit note netted
    ]
    assert _run_total_in(rows, "EUR", "2025-03-03", FX_RATES) == 50000 + 100000 - 10000


def test_final_masters_are_united_with_the_previous_ones(tmp_path):
    # masters_final (state at 2026-06-30) must not drop what masters/ knew: a customer that
    # left, the old IBAN of one that changed bank (POLICIES §5.11: both accounts are valid)
    from closebooks.bankrec.data import load_masters

    files = {
        "categories.csv": ("category,description,gl_account", ["BANK_FEES,x,1"]),
        "fx_rates.csv": ("date,currency,units_per_eur,source", ["2025-12-31,USD,1.10,ECB"]),
        "suppliers.csv": ("supplier_id,entity,name,iban", ["S1,ES01,Sup,ES00"]),
        "bank_accounts.csv": ("account_id,entity,currency,iban", ["A1,ES01,EUR,ES11"]),
        "entities.csv": ("entity,country,functional_currency", ["ES01,ES,EUR"]),
        "payment_terms.csv": ("code,days,early_payment_discount_pct,discount_days", ["T30,30,0,0"]),
    }
    for folder, customers, extra in (
        ("masters", ["C1,ES01,Old Name,ES99OLD", "C2,ES01,Gone,ES98"], None),
        ("final/masters_final", ["C1,ES01,New Name,ES97NEW", "C3,ZA01,New,ZA01"], "ZA01,ZA,ZAR"),
    ):
        d = tmp_path / folder
        d.mkdir(parents=True)
        (d / "customers.csv").write_text(
            "customer_id,entity,name,iban\n" + "\n".join(customers) + "\n", encoding="utf-8"
        )
        for name, (head, rows) in files.items():
            rows = rows + ([extra] if name == "entities.csv" and extra else [])
            (d / name).write_text(head + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    final = load_masters(tmp_path, "final")
    cust = final.customers.set_index("customer_id")
    assert sorted(cust.index) == ["C1", "C2", "C3"]  # nobody is lost, ZA01 is added
    assert cust.loc["C1", "name"] == "New Name"  # the final version wins on the same key
    assert sorted(final.entities["entity"]) == ["ES01", "ZA01"]
    assert len(load_masters(tmp_path, "live").customers) == 2  # other phases: masters/ only
