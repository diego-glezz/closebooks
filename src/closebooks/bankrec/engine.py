"""Reconciliation engine: one pass over the bank lines in booking order.

For each line, the first strategy that produces an explainable allocation wins:
  1. linked detail file (DD batch, card settlement, cheque deposit, payment run, remittance advice)
  2. clear non-item category (fees, cash pool, payroll, taxes...)
  3. return of a collection / payment (RETURNED)
  4. document numbers cited in the text (+ party filter, replaced invoices, duplicates)
  5. payer + amount (exact, discount, subset of open items, FX)
  6. fallback: category by text, receipts -> UNIDENTIFIED
Each result keeps the rule that produced it (`rule`) for debugging and the jury explanation.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from closebooks.bankrec.book import Book, Parties
from closebooks.bankrec.data import Phase, cents
from closebooks.bankrec.docs import Advice, load_advices, parse_clauses, pdf_texts
from closebooks.bankrec.learn import CategoryModel
from closebooks.bankrec.rules import (
    FX,
    RETURN_RX,
    SEPA,
    WITHHOLDING_COUNTRIES,
    WITHHOLDING_RATES,
    Alloc,
    Ctx,
    category_by_text,
    deduction_kind,
    discount_c,
    fifo_pick,
    settle,
    subset_solutions,
    unique_subset,
)
from closebooks.bankrec.text import alnum, fold, service_month, text_tokens
from closebooks.bankrec.variants import FLAGS

AP_FACTORY = {"ES00": {"ES00", "ES01", "ES02", "PT01"}}
_MANDATE = re.compile(r"VTM-?[A-Z]{2}-?\d{4,}")
_CHEQUE = re.compile(r"(?:CHK|CHQ|CHEQUE|CHECK)\.?\s*(?:N[O0]\.?\s*)?(\d{5,})")
_CARD = re.compile(r"\*{2,}(\d{4})")


@dataclass
class Result:
    txn: str
    allocs: list[Alloc] = field(default_factory=list)
    category: str = ""
    exception: str = ""
    rule: str = ""


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _within(day: str, ref: str, before: int, after: int) -> bool:
    try:
        d, r = _d(day), _d(ref)
    except ValueError:
        return False
    return r - timedelta(days=before) <= d <= r + timedelta(days=after)


def _overpay_pattern(excess: int, total: int) -> bool:
    """Excess of an overpayment as customers make it: a round amount or 1 % / 10 % of the bill."""
    if excess <= 0:
        return False
    if excess % 100 == 0 and excess // 100 in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        return True
    return any(abs(excess - round(total * pct)) <= 1 for pct in (0.01, 0.10))


class Engine:
    def __init__(
        self, ph: Phase, cache_dir: Path | None = None, category_model: Path | None = None
    ):
        self.ph = ph
        m = ph.masters
        self.book = Book(ph.items)
        self.parties = Parties(m.customers, m.suppliers, self.book.items)
        self.fx = FX(m.fx)
        self.terms = {
            r["code"]: (float(r["early_payment_discount_pct"] or 0), int(r["discount_days"] or 0))
            for r in m.terms.to_dict("records")
        }
        acc = m.accounts.set_index("account_id")
        self.acc_entity = acc["entity"].to_dict()
        self.acc_ccy = acc["currency"].to_dict()
        self.entity_country = dict(zip(m.entities["entity"], m.entities["country"], strict=True))
        self.own_ibans = {alnum(i): a for a, i in acc["iban"].to_dict().items() if i}
        self.cache_dir = cache_dir or Path("out/cache")
        self.cats = CategoryModel(category_model)
        self.linked: dict[str, tuple[str, object]] = {}
        self.month_hint: dict[str, tuple[int, int]] = {}  # item -> (service year, month)
        self.plan: dict[str, list[int]] = {}  # item -> planned partial amounts (PDF)
        self._load_pdf_clauses()
        self._link_details()

    # ================================================================ setup

    def _load_pdf_clauses(self) -> None:
        if self.ph.pdf_index.empty:
            return
        rows = self.ph.pdf_index.to_dict("records")
        texts = pdf_texts(
            self.ph.folder / "pdfs", rows, self.cache_dir / f"pdf_text_{self.ph.name}.json"
        )
        for r in rows:
            c = parse_clauses(texts.get(r["file"], ""))
            dtype = r["document_type"]
            if dtype == "bank_account_notice" and c.new_iban and r.get("party_id"):
                self.parties.add_iban(c.new_iban, r["party_id"])
                continue
            if dtype not in ("invoice", "supplier_invoice", "credit_note", "supplier_credit_note"):
                continue
            side = "AR" if dtype in ("invoice", "credit_note") else "AP"
            ids = self._pdf_items(r["document_number"], side, r["entity"], r["document_date"])
            if not ids:
                continue
            new = ids[0]
            if c.replaces:
                for old in self._pdf_items(c.replaces, side, r["entity"], r["document_date"]):
                    if old != new:
                        self.book.cancelled.add(old)
                        self.book.replaced_by[old] = new
            if c.instalments or c.payable_c:
                self.plan[new] = sorted(
                    set(c.instalments) | ({c.payable_c} if c.payable_c else set())
                )
            if c.service_month and c.service_year:
                self.month_hint[new] = (c.service_year, c.service_month)
        # credit notes cancelling a replaced invoice are never settled by bank
        for iid, it in self.book.items.items():
            if it.is_credit and it.related:
                for old in self.book.find_number(it.related, side=it.side, entities={it.entity}):
                    if old in self.book.cancelled:
                        self.book.cancelled.add(iid)

    def _pdf_items(self, number: str, side: str, entity: str, day: str = "") -> list[str]:
        """Items a PDF talks about. A PDF prints the full number, so with `pdf_exact` the noisy
        keys (no prefix, no year...) are not used: they let an old PDF ("RE24...") hit this year's
        invoice with the same sequence ("RE25...") and cancel it or give it a foreign plan."""
        if FLAGS.pdf_exact:
            return [
                i
                for i in self.book.by_number.get(alnum(number), [])
                if self.book.items[i].side == side and self.book.items[i].entity == entity
            ]
        ids = self.book.find_number(number, side=side, entities={entity})
        if FLAGS.pdf_dated and day:
            # a document cannot talk about an invoice issued after it (an old "RE24..." PDF
            # hitting this year's "RE25..." through the noisy keys)
            ids = [i for i in ids if self.book.items[i].issue[:10] <= day[:10]]
        return ids

    def _bank_indexes(self) -> None:
        b = self.ph.bank
        self.tok_idx: dict[str, list[int]] = defaultdict(list)
        self.amt_idx: dict[tuple[str, int], list[int]] = defaultdict(list)
        self.rows = b.to_dict("records")
        for i, r in enumerate(self.rows):
            blob = " ".join(
                str(r[c]) for c in ("text", "ref1", "ref2", "end_to_end_id", "remittance_info")
            )
            for tok in {alnum(w) for w in fold(blob).replace("|", " ").replace("/", " ").split()}:
                if len(tok) >= 6 and any(ch.isdigit() for ch in tok):
                    self.tok_idx[tok].append(i)
            e2e = alnum(r["end_to_end_id"])
            if e2e:
                self.tok_idx[e2e].append(i)
            # "/REF PR010025061208-0413" keeps the dash: index the alnum of slash-split parts too
            for part in fold(blob).split("/REF"):
                tok = alnum(part.split()[0]) if part.split() else ""
                if len(tok) >= 10:
                    self.tok_idx[tok].append(i)
            self.amt_idx[(r["account_id"], int(r["amount_c"]))].append(i)

    def _link_confirming(self, conf: pd.DataFrame) -> None:
        """Confirming: the bank debits, per maturity date, the transfers of the run that fall due
        that day. A transfer's maturity = latest due date of its documents, moved to the next
        business day. Link each (run, maturity) group to the debit citing the run id."""

        def next_business(d: str) -> str:
            day = _d(d)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            return day.isoformat()

        due_by_number: dict[tuple[str, str], str] = {}
        for _, row in conf.drop_duplicates(["beneficiary_iban", "document_number"]).iterrows():
            sup = sorted(self.parties.by_iban.get(alnum(row["beneficiary_iban"]), set()))
            ids = self.book.find_number(
                row["document_number"], party=sup[0] if sup else None, side="AP"
            )
            if ids:
                due_by_number[(row["beneficiary_iban"], row["document_number"])] = self.book.items[
                    ids[0]
                ].due
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for _e2e, g in conf.groupby("end_to_end_id"):
            dues = [
                due_by_number.get((r["beneficiary_iban"], r["document_number"]), "")
                for r in g.to_dict("records")
            ]
            dues = [d for d in dues if d]
            if not dues:
                continue
            groups[(g.iloc[0]["run_id"], next_business(max(dues)))] += g.to_dict("records")
        for (run_id, maturity), rows in sorted(groups.items()):
            amount = -sum({r["end_to_end_id"]: int(r["xfer_c"]) for r in rows}.values())
            cands = [
                i
                for i in self.tok_idx.get(alnum(run_id), [])
                if int(self.rows[i]["amount_c"]) == amount
            ]
            self._claim(cands, "run", rows, maturity, 4, 4)

    def _claim(
        self, idx: list[int], kind: str, payload: object, day: str, before: int, after: int
    ) -> bool:
        for i in idx:
            r = self.rows[i]
            t = r["bank_txn_id"]
            if t in self.linked or not _within(r["booking_date"], day, before, after):
                continue
            self.linked[t] = (kind, payload)
            return True
        return False

    def _link_details(self) -> None:
        self._bank_indexes()
        ph = self.ph
        # --- outgoing payment runs (AP)
        if not ph.runs.empty:
            runs = ph.runs.copy()
            runs["doc_c"] = cents(runs["document_amount"])
            runs["xfer_c"] = cents(runs["transfer_amount"])
            conf = runs[runs["run_type"] == "confirming"]
            if not conf.empty:
                self._link_confirming(conf)
                runs = runs[runs["run_type"] != "confirming"]
            for (rtype, _key), g in runs.groupby(
                [
                    runs["run_type"],
                    runs.apply(
                        lambda r: (
                            r["end_to_end_id"]
                            if r["run_type"] == "transfers"
                            else f"{r['run_id']}|{r['execution_date']}"
                        ),
                        axis=1,
                    ),
                ]
            ):
                g0 = g.iloc[0]
                amount = -(int(g0["xfer_c"]) if rtype == "transfers" else int(g["doc_c"].sum()))
                if rtype == "transfers" and not amount:
                    amount = -int(g["doc_c"].sum())
                acct, day = g0["debtor_account_id"], g0["execution_date"]
                payload = g.to_dict("records")
                tok = alnum(g0["end_to_end_id"] if rtype == "transfers" else g0["run_id"])
                cands = [
                    i for i in self.tok_idx.get(tok, []) if int(self.rows[i]["amount_c"]) == amount
                ]
                if not cands:
                    cands = self.amt_idx.get((acct, amount), [])
                    if rtype == "transfers":
                        pref = [
                            i
                            for i in cands
                            if alnum(self.rows[i]["ref1"])
                            and tok.startswith(alnum(self.rows[i]["ref1"]))
                        ]
                        cands = pref or cands
                if self._claim(cands, "run", payload, day, 2, 6) or rtype != "confirming":
                    continue
                # confirming: one debit per maturity date, possibly weeks after the run;
                # every debit citing the run id gets the run rows, filtered at match time
                for i in self.tok_idx.get(tok, []):
                    t = self.rows[i]["bank_txn_id"]
                    if t not in self.linked and int(self.rows[i]["amount_c"]) < 0:
                        conf = runs[runs["run_id"] == g0["run_id"]].to_dict("records")
                        self.linked[t] = ("confirming", conf)
        # --- direct-debit batches
        if not ph.dd.empty:
            dd = ph.dd.copy()
            dd["amt_c"] = cents(dd["amount"])
            for bid, g in dd.groupby("batch_id"):
                g0 = g.iloc[0]
                total = int(cents(pd.Series([g0["batch_total"]])).iloc[0]) or int(g["amt_c"].sum())
                cands = [
                    i
                    for i in self.tok_idx.get(alnum(bid), [])
                    if int(self.rows[i]["amount_c"]) == total
                ]
                cands = cands or self.amt_idx.get((g0["account_id"], total), [])
                self._claim(cands, "dd", g.to_dict("records"), g0["collection_date"], 1, 7)
        # --- card settlements (gross per order, bank gets net)
        if not ph.cards.empty:
            cd = ph.cards.copy()
            cd["net_c"] = cents(cd["net"])
            for _sid, g in cd.groupby("settlement_id"):
                g0 = g.iloc[0]
                total = int(g["net_c"].sum())
                cands = self.amt_idx.get((g0["account_id"], total), [])
                self._claim(cands, "card", g.to_dict("records"), g0["settlement_date"], 1, 5)
        # returns need to find what a cheque / a card payment paid
        self.cheque_idx: dict[str, dict] = {}
        if not ph.cheques.empty:
            for row in ph.cheques.to_dict("records"):
                self.cheque_idx[str(row["cheque_number"]).lstrip("0")] = row
        self.card_idx: dict[tuple[str, int], str] = {}
        if not ph.cards.empty:
            for row in ph.cards.to_dict("records"):
                key = (str(row["card"])[-4:], round(float(row["gross"]) * 100))
                self.card_idx[key] = row["order_number"]
        # --- cheque / lockbox deposits
        if not ph.cheques.empty:
            ch = ph.cheques.copy()
            ch["amt_c"] = cents(ch["amount"])
            for did, g in ch.groupby("deposit_id"):
                g0 = g.iloc[0]
                total = int(cents(pd.Series([g0["deposit_total"]])).iloc[0]) or int(
                    g["amt_c"].sum()
                )
                cands = [
                    i
                    for i in self.tok_idx.get(alnum(did), [])
                    if int(self.rows[i]["amount_c"]) == total
                ]
                cands = cands or self.amt_idx.get((g0["account_id"], total), [])
                self._claim(cands, "cheque", g.to_dict("records"), g0["deposit_date"], 1, 6)
        # --- remittance advices (customer payments of many invoices)
        if not ph.remit_index.empty:
            advices = load_advices(
                ph.folder / "remittance_advices", ph.remit_index.to_dict("records")
            )
            pos_by_amt: dict[int, list[int]] = defaultdict(list)
            for i, r in enumerate(self.rows):
                if int(r["amount_c"]) > 0:
                    pos_by_amt[int(r["amount_c"])].append(i)
            for adv in advices:
                if not adv.lines:
                    continue
                day = adv.pay_date or adv.received
                tok = alnum(adv.ref)
                cands = (
                    [i for i in self.tok_idx.get(tok, []) if int(self.rows[i]["amount_c"]) > 0]
                    if len(tok) >= 6
                    else []
                )
                if not cands:
                    payer = fold(adv.payer).split()
                    first = payer[0] if payer else "#"
                    for amt in sorted({adv.paid_total, adv.total_c or 0} - {0}):
                        cands += [
                            i
                            for i in pos_by_amt.get(amt, [])
                            if first
                            in fold(self.rows[i]["text"] + " " + self.rows[i]["counterparty_name"])
                        ]
                self._claim(cands, "advice", adv, day, 15, 15)

    # ================================================================ per line

    def _ctx(self, r: dict) -> Ctx:
        acc_ccy = r["currency"]
        orig_ccy = r["orig_currency"] or ""
        text = fold(r["text"])
        cp_iban = alnum(r["counterparty_account"])
        international = (
            bool(orig_ccy and orig_ccy != acc_ccy)
            or "EN DIV" in text
            or "TRANSFER. EN DIV" in text
            or (cp_iban[:2] not in SEPA if cp_iban else False)
            or r["bank_code"] in ("13",)
            or "SWIFT" in text
            or "INTL" in text
        )
        return Ctx(
            txn=r["bank_txn_id"],
            booking=r["booking_date"],
            cash_c=abs(int(r["amount_c"])),
            ccy=acc_ccy,
            orig_c=abs(int(r["orig_amount_c"] or 0)),
            orig_ccy=orig_ccy,
            international=international,
            payer_country="",
        )

    def _scope(self, r: dict, receipt: bool) -> set[str]:
        ent = r["entity"]
        scope = {ent}
        if not receipt and ent in AP_FACTORY:
            scope |= AP_FACTORY[ent]
        return scope

    def _parties_of(self, r: dict, scope: set[str]) -> list[str]:
        out: list[str] = []
        cp = alnum(r["counterparty_account"])
        if cp and cp in self.parties.by_iban:
            out += [
                p for p in sorted(self.parties.by_iban[cp]) if self.parties.entity.get(p) in scope
            ]
        blob = " ".join([r["text"], r["counterparty_name"], r["remittance_info"]])
        for m in _MANDATE.finditer(fold(blob)):  # "MREF+VTM-US-0010463", "RUM VTM-FR-0003277"
            pid = self.parties.by_mandate.get(alnum(m.group(0)))
            if pid and pid not in out:
                out.append(pid)
        for tok in fold(blob).replace("|", " ").split():
            a = alnum(tok)
            if len(a) >= 20 and a[:2].isalpha() and a in self.parties.by_iban:
                out += [
                    p
                    for p in sorted(self.parties.by_iban[a])
                    if self.parties.entity.get(p) in scope
                ]
        # N43 splits the text in 38-char fields: a name can be cut across " | "
        joined = blob.replace(" | ", "") + " " + blob.replace("|", " ")
        named = self.parties.by_name(joined, scope, min_len=FLAGS.name_min_len)[:8]
        if FLAGS.name_fuzzy and not named and not out:
            named = self.parties.fuzzy_names(joined, scope)[:3]
        for pid, _n in named:
            if pid not in out:
                out.append(pid)
        return out

    def _cp_ibans(self, r: dict) -> list[str]:
        """Accounts printed on the line: camt counterparty, or IBAN-looking words in the text."""
        out: list[str] = []
        cp = alnum(r["counterparty_account"])
        if cp:
            out.append(cp)
        blob = fold(" ".join([r["text"], r["remittance_info"]]))
        for tok in blob.replace("|", " ").split():
            a = alnum(tok)
            if len(a) >= 20 and a[:2].isalpha() and a[2:4].isdigit() and a not in out:
                out.append(a)
        return out

    def _learn_payer(self, r: dict, res: Result, parties: list[str]) -> None:
        """A receipt settled by a cited document teaches us the payer's account (iban_learn)."""
        if r["amount_c"] <= 0 or not res.allocs or res.rule not in (
            "ref_set",
            "ref_completed",
            "ref_subset",
        ):
            return
        owners = {self.book.items[a.item].party for a in res.allocs}
        if len(owners) != 1:
            return
        owner = next(iter(owners))
        for iban in self._cp_ibans(r):
            if iban not in self.own_ibans:
                self.parties.learn_iban(iban, owner)

    def _sides(self, receipt: bool) -> tuple[str, ...]:
        # receipts settle AR invoices (or AP credit notes refunded); payments the opposite
        return ("ARI", "ARC", "APC") if receipt else ("API", "APC", "ARC")

    def _finish(self, res: Result) -> Result:
        for a in res.allocs:
            self.book.apply(res.txn, a.item, a.amount_c, a.exception)
        return res

    def process(self, r: dict) -> Result:
        txn = r["bank_txn_id"]
        amount = int(r["amount_c"])
        receipt = amount > 0
        ctx = self._ctx(r)
        link = self.linked.get(txn)
        if link:
            res = self._from_link(r, ctx, *link)
            if res:
                return self._finish(res)
        text = " | ".join(
            x
            for x in (r["text"], r["ref1"], r["ref2"], r["remittance_info"], r["counterparty_name"])
            if x
        )
        ftext = fold(text)
        # own accounts on the other side -> treasury movement, never an item
        cp = alnum(r["counterparty_account"])
        if cp in self.own_ibans:
            other = self.acc_entity.get(self.own_ibans[cp])
            cat = category_by_text(text) or (
                "INTERNAL_TRANSFER" if other == r["entity"] else "INTERCOMPANY"
            )
            if cat in ("CASH_POOL", "INTERNAL_TRANSFER", "INTERCOMPANY", "FX_DEAL"):
                return Result(txn, category=cat, rule="own_account")
        learned = self.cats.strong(r)
        if learned:
            exc = "UNIDENTIFIED" if learned == "UNIDENTIFIED" else ""
            return Result(txn, category=learned, exception=exc, rule="category_learned")
        cat = category_by_text(text)
        if cat and cat != "BANK_FEES" and not RETURN_RX.search(ftext):
            return Result(txn, category=cat, rule="category_text")
        if cat == "BANK_FEES" and (
            "COMISION" in ftext[:20] or (not receipt and abs(amount) < 20000)
        ):
            return Result(txn, category=cat, rule="fees")
        scope = self._scope(r, receipt)
        parties = self._parties_of(r, scope)
        if parties:
            ctx.payer_country = self.parties.country.get(parties[0], "")
            # cross-border from outside SEPA (POLICIES §3): payer country outside SEPA and
            # different from the country of the entity that collects
            own = self.entity_country.get(r["entity"], "")
            if ctx.payer_country and ctx.payer_country not in SEPA and ctx.payer_country != own:
                ctx.international = True
        if RETURN_RX.search(ftext):
            res = self._returned(r, ctx, parties, receipt)
            if res:
                return self._finish(res)
        res = self._by_reference(r, ctx, text, parties, scope, receipt)
        if not res and FLAGS.ref_fuzzy and parties:
            res = self._by_fuzzy_reference(r, ctx, text, parties, scope, receipt)
        if res:
            if FLAGS.iban_learn:
                self._learn_payer(r, res, parties)
            return self._finish(res)
        if FLAGS.two_pass_party:
            res = self._by_party(r, ctx, text, parties, receipt, strong_only=True)
            if res:
                return self._finish(res)
        res = self._by_party(r, ctx, text, parties, receipt)
        if res:
            return self._finish(res)
        guess = self.cats.fallback(r)
        if guess:
            exc = "UNIDENTIFIED" if guess == "UNIDENTIFIED" else ""
            return Result(txn, category=guess, exception=exc, rule="category_learned_fallback")
        if cat:
            return Result(txn, category=cat, rule="category_fallback")
        if receipt:
            return Result(
                txn, category="UNIDENTIFIED", exception="UNIDENTIFIED", rule="unidentified"
            )
        return Result(txn, category="BANK_FEES", rule="default_debit")

    # ---------------------------------------------------------------- strategies

    def _from_link(self, r: dict, ctx: Ctx, kind: str, payload) -> Result | None:
        txn = r["bank_txn_id"]
        ent = r["entity"]
        allocs: list[Alloc] = []
        if kind in ("dd", "card", "cheque"):
            for row in payload:
                if kind == "card":
                    num, amt = row["order_number"], round(float(row["gross"]) * 100)
                    name = ""
                elif kind == "dd":
                    num, amt = row["remittance_info"], round(float(row["amount"]) * 100)
                    name = row["debtor_name"]
                else:
                    num, amt = row["stub_reference"], round(float(row["amount"]) * 100)
                    name = row["drawer"]
                iid = self._item_for(
                    num,
                    amt,
                    name,
                    ent,
                    "AR",
                    row.get("mandate_id", ""),
                    row.get("debtor_iban", ""),
                    r["booking_date"],
                )
                if iid:
                    allocs.append(Alloc(iid, amt, ""))
            return Result(txn, allocs, rule=f"link_{kind}") if allocs else None
        if kind == "confirming":
            res = self._from_link(r, ctx, "run", payload)
            if not res:
                return None
            # keep the documents of the maturity paid by this debit: same due date first,
            # else any subset of the run whose total is the cash
            by_due: dict[str, list[Alloc]] = defaultdict(list)
            for a in res.allocs:
                by_due[self.book.items[a.item].due].append(a)

            def net(al: list[Alloc]) -> int:
                return sum(
                    -a.amount_c if self.book.items[a.item].is_credit else a.amount_c for a in al
                )

            for due in sorted(by_due, key=lambda d: d != ctx.booking):
                if abs(net(by_due[due]) - ctx.cash_c) <= 1:
                    return Result(txn, by_due[due], rule="link_confirming")
            if abs(net(res.allocs) - ctx.cash_c) <= 1:
                return Result(txn, res.allocs, rule="link_confirming")
            open_allocs = [a for a in res.allocs if self.book.open[a.item] > 0]
            if abs(net(open_allocs) - ctx.cash_c) <= 1:
                return Result(txn, open_allocs, rule="link_confirming")
            return None
        if kind == "run":
            ents = AP_FACTORY.get(ent, {ent})
            cands: list[tuple[dict, int, list[str]]] = []
            for row in payload:
                amt = abs(round(float(row["document_amount"]) * 100))
                sup = sorted(self.parties.by_iban.get(alnum(row["beneficiary_iban"]), set()))
                ids: list[str] = []
                for p in sup or [None]:  # every supplier with that IBAN (IBANs can repeat)
                    ids += [
                        i
                        for i in self.book.find_number(
                            row["document_number"], party=p, side="AP", entities=ents
                        )
                        if i not in self.book.cancelled and i not in ids
                    ]
                if not ids and not sup:  # unknown beneficiary IBAN: any supplier's document
                    ids = self.book.find_number(row["document_number"], side="AP", entities=ents)
                if ids:
                    cands.append((row, amt, ids))
            # supplier numbers repeat across suppliers: prefer the supplier that owns most of
            # the documents of this transfer (decided on the exact-amount matches)
            votes = Counter(
                self.book.items[i].party
                for _r, a, ids in cands
                for i in ids
                if abs(self.book.items[i].total - a) <= 1
            )
            for _row, amt, ids in cands:
                ids.sort(
                    key=lambda i: (
                        -votes.get(self.book.items[i].party, 0),
                        self.book.open[i] == 0,
                        abs(self.book.items[i].total - amt),
                        i,
                    )
                )
                iid = ids[0]
                open_c = self.book.open[iid]
                total = self.book.items[iid].total
                if amt > max(total, open_c) + max(1, total // 200):
                    continue  # pays more than the whole document: it is another supplier's one
                if open_c == 0 and self.book.paid[iid] and abs(total - amt) <= 1:
                    exc = "DUPLICATE_PAYMENT"
                elif not self.book.items[iid].is_credit and amt < open_c - 1:
                    exc = "PARTIAL"  # the run pays part of the document; the rest stays open
                else:
                    exc = ""
                allocs.append(Alloc(iid, amt, exc))
            return Result(txn, allocs, rule="link_run") if allocs else None
        if kind == "advice":
            return self._from_advice(r, ctx, payload)
        return None

    def _item_for(
        self,
        num: str,
        amt: int,
        name: str,
        ent: str,
        side: str,
        mandate: str,
        iban: str,
        booking: str,
    ) -> str | None:
        party = self.parties.by_mandate.get(alnum(mandate)) if mandate else None
        if not party and iban:
            ps = [
                p
                for p in sorted(self.parties.by_iban.get(alnum(iban), set()))
                if self.parties.entity.get(p) == ent
            ]
            party = ps[0] if ps else None
        for tok in sorted(text_tokens(num)):
            ids = [
                i
                for i in self.book.lookup(tok)
                if self.book.items[i].entity == ent
                and self.book.items[i].side == side
                and i not in self.book.cancelled
            ]
            if party:
                ids = [i for i in ids if self.book.items[i].party == party] or ids
            if ids:
                ids.sort(key=lambda i: (self.book.open[i] == 0, abs(self.book.open[i] - amt)))
                return ids[0]
        if not party and name:
            ps = self.parties.by_name(name, {ent})
            party = ps[0][0] if ps else None
        if party:
            for iid in self.book.open_items(party, booking, side):
                if abs(self.book.open[iid] - amt) <= 1:
                    return iid
        return None

    def _from_advice(self, r: dict, ctx: Ctx, adv: Advice) -> Result | None:
        ent = r["entity"]
        ps = self.parties.by_name(adv.payer, {ent})
        fam: set[str] = set()
        for p, _ in ps[:2]:
            fam |= self.parties.family(p)
        allocs: list[Alloc] = []
        for ln in adv.lines:
            ids = [
                i
                for i in self.book.find_number(ln.number, entities={ent})
                if self.book.items[i].side == "AR"
            ]
            if fam:  # the payer is known: never take another customer's document
                ids = [i for i in ids if self.book.items[i].party in fam]
            ids = [self.book.replaced_by.get(i, i) for i in ids]
            # the advice prints the document amount: a candidate must agree with it
            gross = abs(ln.gross_c)
            ids = [
                i
                for i in ids
                if abs(self.book.items[i].total - gross) <= max(1, gross // 200)
                or abs(self.book.open[i] - gross) <= max(1, gross // 200)
            ]
            if not ids or (not fam and len({self.book.items[i].party for i in ids}) > 1):
                continue
            ids.sort(key=lambda i: (self.book.open[i] == 0, abs(self.book.items[i].total - gross)))
            iid = ids[0]
            it = self.book.items[iid]
            paid = abs(ln.paid)
            open_c = self.book.open[iid] or it.total
            kind = deduction_kind(ln.reason) if ln.deduction_c else ""
            if it.is_credit:
                allocs.append(Alloc(iid, open_c, ""))
            elif kind == "discount":
                # POLICIES §3: within terms -> fully cleared; taken late -> PARTIAL (stays open)
                _d, within = discount_c(it, self.terms, ctx.booking)
                allocs.append(
                    Alloc(iid, open_c, "SHORT_DISCOUNT") if within else Alloc(iid, paid, "PARTIAL")
                )
            elif kind == "withholding":
                allocs.append(Alloc(iid, open_c, "SHORT_WITHHOLDING"))
            elif kind == "dispute":
                allocs.append(Alloc(iid, paid, "SHORT_DISPUTE"))
            elif abs(paid - open_c) <= 1:
                allocs.append(Alloc(iid, open_c, ""))
            else:
                one = settle(
                    self.book,
                    [iid],
                    Ctx(
                        ctx.txn,
                        ctx.booking,
                        paid,
                        it.ccy,
                        0,
                        "",
                        ctx.international,
                        ctx.payer_country,
                    ),
                    self.fx,
                    self.terms,
                )
                allocs += one or [Alloc(iid, paid, "PARTIAL")]
        if not allocs:
            return None
        same_ccy = r["currency"] == self.book.items[allocs[0].item].ccy
        if same_ccy and abs(abs(adv.paid_total) - ctx.cash_c) > max(100, ctx.cash_c * 0.05):
            return None  # advice of another payment: don't trust it
        explained = any(
            a.exception in ("SHORT_DISPUTE", "SHORT_DISCOUNT", "SHORT_WITHHOLDING", "PARTIAL")
            for a in allocs
        )
        if r["currency"] != self.book.items[allocs[0].item].ccy:
            # FX line: fully cleared invoices carry FX_DIFFERENCE (replaces discount/withholding)
            return Result(
                r["bank_txn_id"],
                [
                    Alloc(
                        a.item,
                        a.amount_c,
                        a.exception
                        if a.exception in ("PARTIAL", "SHORT_DISPUTE")
                        or self.book.items[a.item].is_credit
                        else "FX_DIFFERENCE",
                    )
                    for a in allocs
                ],
                rule="link_advice_fx",
            )
        # cash differs from the advice (charges): re-explain with the whole set
        adv_cash = sum(
            -a.amount_c if self.book.items[a.item].is_credit else a.amount_c
            for a in allocs
            if a.exception not in ("SHORT_DISCOUNT", "SHORT_WITHHOLDING")
        )
        if abs(adv_cash - ctx.cash_c) > 1 and not explained:
            alt = settle(
                self.book, [a.item for a in allocs], ctx, self.fx, self.terms, allow_partial=False
            )
            if alt:
                allocs = alt
        return Result(r["bank_txn_id"], allocs, rule="link_advice")

    def _returned(self, r: dict, ctx: Ctx, parties: list[str], receipt: bool) -> Result | None:
        txn = r["bank_txn_id"]
        amt = ctx.cash_c
        side = "AR" if not receipt else "AP"
        ftext = fold(" ".join([r["text"], r["ref1"], r["ref2"], r["remittance_info"]]))
        direct: list[str] = []
        m = _CHEQUE.search(ftext)
        if m and m.group(1).lstrip("0") in self.cheque_idx:  # returned cheque -> what it paid
            row = self.cheque_idx[m.group(1).lstrip("0")]
            direct = self.book.find_number(
                row["stub_reference"] or "", side="AR", entities={r["entity"]}
            )
            if not direct and row.get("drawer"):
                ps = self.parties.by_name(row["drawer"], {r["entity"]})
                parties = [p for p, _ in ps[:3]] + parties
        m = _CARD.search(ftext)
        if not direct and m and (m.group(1), amt) in self.card_idx:  # card chargeback
            direct = self.book.find_number(
                self.card_idx[(m.group(1), amt)], side="AR", entities={r["entity"]}
            )
        direct = [
            i
            for i in direct
            if abs(self.book.items[i].total - amt) <= 1
            or amt in [a for _t, a in self.book.paid.get(i, [])]
        ]
        if direct:
            return Result(txn, [Alloc(direct[0], amt, "RETURNED")], rule="returned_doc")
        fam: set[str] = set()
        for p in parties[:2]:
            fam |= self.parties.family(p)
        best = None
        for p in sorted(fam):
            for iid in self.book.by_party.get(p, []):
                it = self.book.items[iid]
                if it.side != side or it.issue > ctx.booking:
                    continue
                paid = [a for _t, a in self.book.paid.get(iid, [])]
                if amt in paid or (not paid and abs(it.total - amt) <= 1):
                    score = (amt in paid, it.issue)
                    if best is None or score > best[0]:
                        best = (score, iid)
        if best:
            return Result(txn, [Alloc(best[1], amt, "RETURNED")], rule="returned")
        return None

    def _by_reference(
        self, r: dict, ctx: Ctx, text: str, parties: list[str], scope: set[str], receipt: bool
    ) -> Result | None:
        kinds = self._sides(receipt)
        fam: set[str] = set()
        for p in parties[: FLAGS.fam_parties]:
            fam |= self.parties.family(p)
        cands: dict[str, None] = {}
        strong: set[str] = set()
        for tok in sorted(text_tokens(text)):
            for iid in self.book.lookup(tok):
                it = self.book.items[iid]
                if it.kind in kinds and it.entity in scope and it.issue <= ctx.booking:
                    key = self.book.replaced_by.get(iid, iid)
                    cands[key] = None
                    # full printed number (or 8+ digits of it) vs a short suffix like '415289'
                    if len(tok) >= 8 or tok == alnum(it.number):
                        strong.add(key)
        if not cands:
            return None
        ids = [i for i in cands if i not in self.book.cancelled]
        if not fam:  # unknown payer: a short suffix alone is too weak unless the amount matches
            ids = [i for i in ids if i in strong or abs(self.book.open[i] - ctx.cash_c) <= 1]
        if fam:
            own = [i for i in ids if self.book.items[i].party in fam]
            # payer known but the reference points to someone else: only trust it when the
            # amount matches exactly (agencies / confirming banks quote the debtor's invoice)
            ids = own or [i for i in ids if abs(self.book.open[i] - ctx.cash_c) <= 1]
            if not ids:
                return None
        # main side first: receipts settle ARI, payments API
        main = "ARI" if receipt else "API"
        inv = [i for i in ids if self.book.items[i].kind == main]
        if not inv:
            # refund of a credit note: supplier refunds us (APC) / we pay out a rappel (ARC)
            refund = "APC" if receipt else "ARC"
            cn = [
                i
                for i in ids
                if self.book.items[i].kind == refund
                and abs((self.book.open[i] or self.book.items[i].total) - ctx.cash_c) <= 1
            ]
            if cn:
                amt = self.book.open[cn[0]] or self.book.items[cn[0]].total
                return Result(r["bank_txn_id"], [Alloc(cn[0], amt, "")], rule="ref_refund")
            return None
        if len({self.book.items[i].party for i in inv}) > 1 and not fam:
            # ambiguous party: keep the ones whose amounts can explain the cash
            exact = [i for i in inv if abs(self.book.open[i] - ctx.cash_c) <= 1]
            inv = exact or inv
        open_inv = [i for i in inv if self.book.open[i] > 0]
        if not open_inv:
            # a duplicate needs the first payment in our book (closed before the phase =
            # unknown history, never call it a duplicate)
            closed = [
                i
                for i in inv
                if abs(self.book.items[i].total - ctx.cash_c) <= 1
                and self.book.paid.get(i)
                and (not fam or self.book.items[i].party in fam)
            ]
            if closed:
                return Result(
                    r["bank_txn_id"],
                    [Alloc(closed[0], ctx.cash_c, "DUPLICATE_PAYMENT")],
                    rule="ref_duplicate",
                )
            return None
        # try: cited invoices; cited + party credit notes; subsets of cited invoices
        party = self.book.items[open_inv[0]].party
        credits = [i for i in ids if self.book.items[i].is_credit and self.book.open[i] > 0]
        party_credits = [
            i for i in self.book.open_items(party, ctx.booking) if self.book.items[i].is_credit
        ]
        for combo in (open_inv + credits, open_inv + party_credits):
            res = settle(
                self.book, list(dict.fromkeys(combo)), ctx, self.fx, self.terms, allow_partial=False
            )
            if res:
                return Result(r["bank_txn_id"], res, rule="ref_set")
        if FLAGS.ref_cn_subset:
            # cited invoices paid net of SOME of the party's open credit notes (POLICIES: credit
            # notes are netted): the shortfall must be the unique subset of those credit notes
            res = self._net_of_credits(open_inv + credits, party_credits, ctx)
            if res:
                return Result(r["bank_txn_id"], res, rule="ref_set_cn")
        # cash exceeds the cited invoices: the text was truncated, complete with the party's
        # other open items (oldest first, or a small subset)
        cited = sum(self._signed(i) for i in open_inv + credits)
        for target, tol in self._targets(ctx):
            if target <= cited + 1:
                continue
            extra = self._complete(
                party,
                ctx,
                set(open_inv + credits),
                target - cited,
                "AR" if receipt else "AP",
                tol=tol,
            )
            if extra is not None:
                res = settle(
                    self.book,
                    open_inv + credits + extra,
                    ctx,
                    self.fx,
                    self.terms,
                    allow_partial=False,
                )
                if res:
                    return Result(r["bank_txn_id"], res, rule="ref_completed")
        if 1 < len(open_inv) <= 12:
            for k in range(len(open_inv) - 1, 0, -1):
                for sub in itertools.combinations(open_inv, k):
                    res = settle(
                        self.book, list(sub), ctx, self.fx, self.terms, allow_partial=False
                    )
                    if res:
                        return Result(r["bank_txn_id"], res, rule="ref_subset")
        res = settle(self.book, open_inv, ctx, self.fx, self.terms, allow_partial=True)
        return Result(r["bank_txn_id"], res, rule="ref_partial") if res else None

    def _by_fuzzy_reference(
        self, r: dict, ctx: Ctx, text: str, parties: list[str], scope: set[str], receipt: bool
    ) -> Result | None:
        """A cited number that matches nothing, but is one wrong / transposed digit away from
        exactly one open invoice of the known payer whose amount explains the cash
        (POLICIES §5.5: typos and transposed digits)."""
        fam: set[str] = set()
        for p in parties[: FLAGS.fam_parties]:
            fam |= self.parties.family(p)
        kinds = self._sides(receipt)
        main = "ARI" if receipt else "API"
        found: dict[str, None] = {}
        for tok in sorted(text_tokens(text)):
            if not tok.isdigit() or not 6 <= len(tok) <= 10 or self.book.lookup(tok):
                continue
            for k in self._digit_neighbours(tok):
                for iid in self.book.lookup(k):
                    it = self.book.items[iid]
                    if (
                        it.kind == main
                        and it.kind in kinds
                        and it.entity in scope
                        and it.party in fam
                        and it.issue <= ctx.booking
                        and self.book.open[iid] > 0
                        and iid not in self.book.cancelled
                    ):
                        found[iid] = None
        if len(found) != 1:
            return None
        iid = next(iter(found))
        res = settle(self.book, [iid], ctx, self.fx, self.terms, allow_partial=False)
        if not res:
            return None
        return Result(r["bank_txn_id"], res, rule="ref_fuzzy")

    @staticmethod
    def _digit_neighbours(tok: str) -> list[str]:
        out: dict[str, None] = {}
        for i, ch in enumerate(tok):
            for d in "0123456789":
                if d != ch:
                    out[tok[:i] + d + tok[i + 1 :]] = None
            if i + 1 < len(tok) and tok[i] != tok[i + 1]:
                out[tok[:i] + tok[i + 1] + tok[i] + tok[i + 2 :]] = None
        return list(out)

    def _by_party(
        self,
        r: dict,
        ctx: Ctx,
        text: str,
        parties: list[str],
        receipt: bool,
        strong_only: bool = False,
    ) -> Result | None:
        if not parties:
            return None
        side = "AR" if receipt else "AP"
        kinds = self._sides(receipt)
        month = service_month(text)
        for rank, p in enumerate(parties[: FLAGS.party_rank_exact]):
            fam = self.parties.family(p)
            open_ids: list[str] = []
            for q in sorted(fam):
                open_ids += [
                    i
                    for i in self.book.open_items(q, ctx.booking)
                    if self.book.items[i].kind in kinds
                ]
            if not open_ids:
                continue
            if not any(self.book.items[i].side == side for i in open_ids):
                # only credit notes of the other side: a refund of exactly one of them
                cn = [i for i in open_ids if abs(self.book.open[i] - ctx.cash_c) <= 1]
                if cn and not strong_only:
                    return Result(
                        r["bank_txn_id"],
                        [Alloc(cn[0], self.book.open[cn[0]], "")],
                        rule="party_refund",
                    )
                continue
            open_ids = [i for i in open_ids if self.book.items[i].side == side]
            open_ids.sort(key=lambda i: (self.book.items[i].issue, self.book.items[i].number))
            inv = [i for i in open_ids if not self.book.items[i].is_credit]
            # (a) one open invoice with exactly this amount (FX-converted if needed)
            exact = [
                i
                for i in inv
                if (res := settle(self.book, [i], ctx, self.fx, self.terms, allow_partial=False))
                and all(a.exception in ("", "FX_DIFFERENCE") for a in res)
            ]
            if exact and FLAGS.exact_recent_days and len(exact) > 1:
                # same amount, several open: invoices open for ages are dead, the payer
                # settles a recent one (TRAIN: the golden one is never older than ~160 days)
                lo = (_d(ctx.booking) - timedelta(days=FLAGS.exact_recent_days)).isoformat()
                exact = [i for i in exact if self.book.items[i].issue >= lo] or exact
            if exact:
                pick = exact[0]
                if month and len(exact) > 1:
                    for i in exact:
                        hint = self.month_hint.get(i)
                        iss = self.book.items[i].issue
                        if (hint and hint[1] == month) or (
                            not hint and int(iss[5:7]) == month % 12 + 1
                        ):
                            pick = i
                            break
                res = settle(self.book, [pick], ctx, self.fx, self.terms, allow_partial=False)
                return Result(r["bank_txn_id"], res or [], rule="party_amount")
            # (b) discount within terms on a single invoice
            for i in inv:
                d, within = discount_c(self.book.items[i], self.terms, ctx.booking)
                if d and within and abs(self.book.open[i] - d - ctx.cash_c) <= 1:
                    return Result(
                        r["bank_txn_id"],
                        [Alloc(i, self.book.open[i], "SHORT_DISCOUNT")],
                        rule="party_discount",
                    )
            # (b2) one instalment / the payable amount after retention or advance (invoice PDF)
            for i in inv:
                if (
                    any(abs(a - ctx.cash_c) <= 1 for a in self.plan.get(i, ()))
                    and self.book.items[i].ccy == ctx.ccy
                ):
                    rest = self.book.open[i] - ctx.cash_c
                    return Result(
                        r["bank_txn_id"],
                        [Alloc(i, ctx.cash_c, "PARTIAL" if rest > 1 else "")],
                        rule="party_plan",
                    )
            if strong_only:
                continue
            # (c) the customer pays its oldest open items (FIFO by due date, then issue date)
            for target, tol in self._targets(ctx):
                extra = self._complete(p, ctx, set(), target, side, subsets=False, tol=tol)
                if extra:
                    res = settle(self.book, extra, ctx, self.fx, self.terms, allow_partial=False)
                    if res:
                        return Result(r["bank_txn_id"], res, rule="party_fifo")
            if rank >= FLAGS.party_rank_comb:
                continue  # combinatorial searches only for the most likely payers
            # (d) a payment of several invoices without reference: the UNIQUE subset of the
            # payer's recent open items (credit notes included) that adds up to the cash
            res = self._unique_subset_alloc(open_ids, ctx)
            if res:
                return Result(r["bank_txn_id"], res, rule="party_subset")
            if FLAGS.fifo_subset:
                res = self._fifo_subset_alloc(open_ids, ctx)
                if res:
                    return Result(r["bank_txn_id"], res, rule="party_fifo_subset")
            if rank > 0:
                continue  # a short payment is only credible from the most likely payer
            # (e0) nothing open matches but we already collected/paid one of the party's documents
            # for exactly this amount: the same document paid again (POLICIES §3 DUPLICATE)
            dup = [
                i
                for q in sorted(fam)
                for i in self.book.by_party.get(q, [])
                if self.book.open[i] == 0
                and self.book.paid.get(i)
                and self.book.items[i].side == side
                and not self.book.items[i].is_credit
                and abs(self.book.items[i].total - ctx.cash_c) <= 1
            ]
            if len(dup) == 1:
                return Result(
                    r["bank_txn_id"],
                    [Alloc(dup[0], ctx.cash_c, "DUPLICATE_PAYMENT")],
                    rule="party_duplicate",
                )
            # (e1) overpayment of one invoice: the excess is a round amount (1, 10, 100...) or
            # 1 % / 10 % of the invoice (pattern measured on TRAIN); only if exactly one fits
            if receipt and inv and all(self.book.items[i].ccy == ctx.ccy for i in inv):
                over = [
                    i
                    for i in inv
                    if _overpay_pattern(ctx.cash_c - self.book.open[i], self.book.items[i].total)
                ]
                if len(over) == 1:
                    i = over[0]
                    return Result(
                        r["bank_txn_id"],
                        [Alloc(i, self.book.open[i], "OVERPAYMENT")],
                        rule="party_overpayment",
                    )
            # (e) bank charges / withholding / discount on a single invoice
            for i in inv:
                res = settle(self.book, [i], ctx, self.fx, self.terms, allow_partial=False)
                if res and res[0].exception in (
                    "SHORT_BANK_CHARGES",
                    "SHORT_WITHHOLDING",
                    "SHORT_DISCOUNT",
                    "FX_DIFFERENCE",
                ):
                    return Result(r["bank_txn_id"], res, rule="party_short")
        return None

    # ================================================================ helpers

    def _targets(self, ctx: Ctx) -> list[tuple[int, int]]:
        """Cash to explain, plus the gross amount before withholding at source for payers in
        withholding countries (POLICIES §3: 1.25 / 1.5 / 10 % of the invoice total)."""
        out = [(ctx.cash_c, 1)]
        if ctx.payer_country in WITHHOLDING_COUNTRIES:
            out += [(round(ctx.cash_c / (1 - rate / 100)), 25) for rate in WITHHOLDING_RATES]
        return out

    def _unique_subset_alloc(
        self,
        open_ids: list[str],
        ctx: Ctx,
        recent_days: int | None = None,
        cap: int | None = None,
        max_items: int | None = None,
    ) -> list[Alloc] | None:
        recent_days = FLAGS.subset_recent_days if recent_days is None else recent_days
        cap = FLAGS.subset_cap if cap is None else cap
        max_items = FLAGS.subset_max_items if max_items is None else max_items
        lo = (_d(ctx.booking) - timedelta(days=recent_days)).isoformat()
        pool = [i for i in open_ids if self.book.items[i].issue >= lo]
        pool = sorted(pool, key=lambda i: (self.book.items[i].issue, i), reverse=True)[:cap]
        if len(pool) < 2:
            return None
        for target, tol in self._targets(ctx):
            idx = unique_subset([self._signed(i) for i in pool], target, tol)
            # measured on TRAIN 2024-H2: pair precision 0.99 (2 items) ... 0.72 (5) ... 0.23 (10+);
            # subsets with credit notes are less reliable
            has_cn = bool(idx) and any(self.book.items[pool[k]].is_credit for k in idx)
            if idx and 2 <= len(idx) <= (3 if has_cn else max_items):
                res = settle(
                    self.book, [pool[k] for k in idx], ctx, self.fx, self.terms, allow_partial=False
                )
                if res:
                    return res
        return None

    def _fifo_subset_alloc(self, open_ids: list[str], ctx: Ctx) -> list[Alloc] | None:
        """A payment of many invoices without reference (POLICIES §5.6: customers pay the oldest
        open invoices first). Pool = the payer's oldest open items; of all subsets adding up to
        the cash, the one closest to a FIFO prefix wins if it is unique."""
        pool = [i for i in open_ids if self.book.items[i].ccy == ctx.ccy]
        if FLAGS.stale_days:
            lo = (_d(ctx.booking) - timedelta(days=FLAGS.stale_days)).isoformat()
            pool = [i for i in pool if self.book.items[i].issue >= lo]
        pool = sorted(pool, key=lambda i: (self.book.items[i].issue, self.book.items[i].number, i))
        pool = pool[: FLAGS.fs_cap]
        if len(pool) < 2:
            return None
        values = [self._signed(i) for i in pool]
        for target, tol in self._targets(ctx):
            sols = subset_solutions(values, target, tol)
            if not sols:
                continue
            sols = [
                s
                for s in sols
                if len(s) >= 2 and any(not self.book.items[pool[k]].is_credit for k in s)
            ]
            pick = fifo_pick(sols, FLAGS.fs_max_holes) if sols else None
            if pick:
                res = settle(
                    self.book,
                    [pool[k] for k in pick],
                    ctx,
                    self.fx,
                    self.terms,
                    allow_partial=False,
                )
                if res:
                    return res
        return None

    def _net_of_credits(
        self, cited: list[str], credits: list[str], ctx: Ctx, cap: int = 16
    ) -> list[Alloc] | None:
        cited = list(dict.fromkeys(cited))
        pool = [i for i in credits if i not in cited]
        pool = sorted(pool, key=lambda i: (self.book.items[i].issue, i), reverse=True)[:cap]
        if not pool or any(self.book.items[i].ccy != ctx.ccy for i in cited + pool):
            return None
        short = sum(self._signed(i) for i in cited) - ctx.cash_c
        if short <= 1:
            return None
        idx = unique_subset([self.book.open[i] for i in pool], short, 1)
        if not idx:
            return None
        return settle(
            self.book, cited + [pool[k] for k in idx], ctx, self.fx, self.terms, allow_partial=False
        )

    def _signed(self, iid: str) -> int:
        o = self.book.open[iid]
        return -o if self.book.items[iid].is_credit else o

    def _complete(
        self,
        party: str,
        ctx: Ctx,
        exclude: set[str],
        target: int,
        side: str,
        subsets: bool = True,
        tol: int = 1,
    ) -> list[str] | None:
        """Open items of the party (and its family) that add up to `target` cents:
        FIFO prefixes (by due date and by issue date, with/without credit notes) first,
        then small subsets."""
        fam = self.parties.family(party)
        pool = [
            i
            for q in sorted(fam)
            for i in self.book.open_items(q, ctx.booking, side)
            if i not in exclude
        ]
        if FLAGS.stale_days:  # invoices open for ages are dead: nobody pays them any more
            lo = (_d(ctx.booking) - timedelta(days=FLAGS.stale_days)).isoformat()
            pool = [i for i in pool if self.book.items[i].issue >= lo]
        if not pool:
            return None
        invs = [i for i in pool if not self.book.items[i].is_credit]
        cns = [i for i in pool if self.book.items[i].is_credit]
        cn_total = sum(self.book.open[i] for i in cns)
        for key in (
            lambda i: (self.book.items[i].due, self.book.items[i].issue),
            lambda i: (self.book.items[i].issue, self.book.items[i].number),
        ):
            acc = 0
            chosen: list[str] = []
            for i in sorted(invs, key=key):
                acc += self.book.open[i]
                chosen.append(i)
                if abs(acc - target) <= tol:
                    return chosen
                if cns and abs(acc - cn_total - target) <= tol:
                    return chosen + cns
                if acc > target + cn_total + tol:
                    break
        if subsets:
            small = sorted(pool, key=lambda i: (self.book.items[i].issue,))[:25]
            for k in (1, 2, 3):
                for sub in itertools.combinations(small, k):
                    if abs(sum(self._signed(i) for i in sub) - target) <= tol:
                        return list(sub)
        return None

    # ================================================================ run

    def run(self) -> list[Result]:
        out = []
        for r in self.rows:
            try:
                out.append(self.process(r))
            except Exception as exc:  # never lose a line: fall back to a safe row
                receipt = int(r["amount_c"]) > 0
                out.append(
                    Result(
                        r["bank_txn_id"],
                        category="UNIDENTIFIED" if receipt else "BANK_FEES",
                        exception="UNIDENTIFIED" if receipt else "",
                        rule=f"error:{type(exc).__name__}",
                    )
                )
        return out
