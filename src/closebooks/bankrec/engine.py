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
from collections.abc import Callable
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
    deduction_kind_by_amount,
    discount_c,
    duplicate_refund_category,
    settle,
    unique_subset,
)
from closebooks.bankrec.text import alnum, fold, service_month, text_tokens

AP_FACTORY = {"ES00": {"ES00", "ES01", "ES02", "PT01"}}
# unplanned payments on account are always one of these shares of the invoice total (TRAIN)
PARTIAL_SHARES = (0.25, 0.3, 0.4, 0.5, 0.6, 0.7)
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
        self,
        ph: Phase,
        cache_dir: Path | None = None,
        category_model: Path | None = None,
        on_step: Callable[[str], None] | None = None,
    ):
        """`on_step(msg)` is called when each slow setup step starts (progress display only)."""
        step = on_step or (lambda _msg: None)
        self.ph = ph
        step("libro de partidas y terceros")
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
        self.by_txn: dict[str, list[tuple[str, int]]] = {}  # bank line -> [(item, cents)]
        self.month_hint: dict[str, tuple[int, int]] = {}  # item -> (service year, month)
        self.plan: dict[str, list[int]] = {}  # item -> planned partial amounts (PDF)
        step(f"cláusulas de {len(ph.pdf_index):,} PDFs".replace(",", "."))
        self._load_pdf_clauses()
        step("enlazar remesas, tarjetas, cheques, payment runs y avisos")
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
            ents = {r["entity"]}
            # A PDF of an earlier phase (LIVE/FINAL context) only describes items carried over
            # from that phase: exact number, issued no later than the PDF itself. The noisy-key
            # fallback of find_number would otherwise attach its clauses to look-alike numbers of
            # THIS phase (measured on LIVE: 1,849 items wrongly cancelled as "replaced").
            prior = bool(r.get("_src"))
            doc_date = str(r.get("document_date", ""))[:10]
            ids = (
                self._carried(r["document_number"], side, ents, doc_date)
                if prior
                else self.book.find_number(r["document_number"], side=side, entities=ents)
            )
            if not ids:
                continue
            new = ids[0]
            if c.replaces:
                olds = (
                    self._carried(c.replaces, side, ents, doc_date)
                    if prior
                    else self.book.find_number(c.replaces, side=side, entities=ents)
                )
                for old in olds:
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

    def _carried(self, number: str, side: str, entities: set[str], before: str) -> list[str]:
        """Items with exactly this printed number (no noisy keys), issued on or before `before`."""
        return [
            i
            for i in self.book.by_number.get(alnum(number), [])
            if self.book.items[i].side == side
            and self.book.items[i].entity in entities
            and (not before or self.book.items[i].issue <= before)
        ]

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
            amt = abs(int(row["doc_c"]))
            ids: list[str] = []
            for p in sup or [None]:  # every supplier with that IBAN (IBANs can repeat)
                ids += [
                    i
                    for i in self.book.find_number(row["document_number"], party=p, side="AP")
                    if i not in ids
                ]
            # the maturity is set by the invoices of the transfer: a credit note (its due is its
            # issue date) or an approximate number match must not move it
            invoices = [i for i in ids if not self.book.items[i].is_credit]
            exact = [i for i in invoices if abs(self.book.items[i].total - amt) <= 1]
            pick = exact or invoices[:1]
            if pick:
                due_by_number[(row["beneficiary_iban"], row["document_number"])] = self.book.items[
                    pick[0]
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
            or "/CHGS/" in text  # intermediary bank charges quoted by the bank
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
            fees=self.cats.fees,
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
        for pid, _n in self.parties.by_name(joined, scope)[:8]:
            if pid not in out:
                out.append(pid)
        return out

    def _sides(self, receipt: bool) -> tuple[str, ...]:
        # receipts settle AR invoices (or AP credit notes refunded); payments the opposite
        return ("ARI", "ARC", "APC") if receipt else ("API", "APC", "ARC")

    def _finish(self, res: Result) -> Result:
        for a in res.allocs:
            self.book.apply(res.txn, a.item, a.amount_c, a.exception)
        if res.allocs and not any(a.exception == "RETURNED" for a in res.allocs):
            self.by_txn[res.txn] = [(a.item, a.amount_c) for a in res.allocs]
        return res

    def _reversal(self, r: dict, ctx: Ctx) -> Result | None:
        """A credit that brings back one of OUR transfers (rejected by the beneficiary bank):
        POLICIES §3 RETURNED, every item of the returned operation gets a row with the amount of
        its original allocation. The original is the earlier debit for exactly this amount that
        shares a reference token (payment-run id / end-to-end id) with the return."""
        txn = r["bank_txn_id"]
        blob = " ".join(
            str(r[c]) for c in ("text", "ref1", "ref2", "end_to_end_id", "remittance_info")
        )
        toks = {alnum(w) for w in fold(blob).replace("|", " ").replace("/", " ").split()}
        toks |= {alnum(p.split()[0]) for p in fold(blob).split("/REF") if p.split()}
        found: dict[str, list[tuple[str, int]]] = {}
        for tok in sorted(t for t in toks if len(t) >= 6 and any(ch.isdigit() for ch in t)):
            for i in self.tok_idx.get(tok, ()):
                o = self.rows[i]
                if (
                    o["bank_txn_id"] < txn
                    and o["account_id"] == r["account_id"]
                    and int(o["amount_c"]) == -ctx.cash_c
                    and self.by_txn.get(o["bank_txn_id"])
                ):
                    found[o["bank_txn_id"]] = self.by_txn[o["bank_txn_id"]]
        if len(found) != 1:  # none, or ambiguous: do not guess
            return None
        allocs = next(iter(found.values()))
        return Result(txn, [Alloc(i, c, "RETURNED") for i, c in allocs], rule="returned_transfer")

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
        dup_refund = duplicate_refund_category(text, receipt)
        if dup_refund:
            return Result(txn, category=dup_refund, rule="duplicate_refund")
        learned = self.cats.strong(r)
        if learned:
            exc = "UNIDENTIFIED" if learned == "UNIDENTIFIED" else ""
            return Result(txn, category=learned, exception=exc, rule="category_learned")
        cat = category_by_text(text, receipt)
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
            res = self._reversal(r, ctx) if receipt else None
            res = res or self._returned(r, ctx, parties, receipt)
            if res:
                return self._finish(res)
        res = self._by_reference(r, ctx, text, parties, scope, receipt)
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
            tied = [p for p, score in ps if score == ps[0][1]] if ps else []
            if len(tied) > 1:
                # several customers match the name equally well: take the document only if
                # exactly one of them has an open item for exactly this amount
                hits = [
                    iid
                    for p in tied
                    for iid in self.book.open_items(p, booking, side)
                    if abs(self.book.open[iid] - amt) <= 1
                ]
                return hits[0] if len(hits) == 1 else None
            party = tied[0] if tied else None
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
        payer_country = self.parties.country.get(ps[0][0], "") if ps else ""
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
            kind = ""
            if ln.deduction_c:
                kind = (
                    deduction_kind(ln.reason)
                    if ln.reason.strip()
                    else deduction_kind_by_amount(it, ln.deduction_c, self.terms, payer_country)
                )
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
        for p in parties[:3]:
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
            if not own and len(parties) > 3:
                # homonyms tie on the name score and the id decides: a FULL printed number that
                # belongs to one of the other name matches identifies the payer
                ext: set[str] = set()
                for p in parties[3:8]:
                    ext |= self.parties.family(p)
                own = [i for i in ids if i in strong and self.book.items[i].party in ext]
                if own:
                    fam |= ext
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
        for combo, uncited in ((open_inv + credits, False), (open_inv + party_credits, True)):
            res = settle(
                self.book, list(dict.fromkeys(combo)), ctx, self.fx, self.terms, allow_partial=False
            )
            # POLICIES §3: OVERPAYMENT is an excess over ONE invoice; with credit notes we were
            # not told about it is only a guess that the cash includes them
            if res and uncited and any(a.exception == "OVERPAYMENT" for a in res):
                continue
            if res:
                return Result(r["bank_txn_id"], res, rule="ref_set")
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

    def _by_party(
        self, r: dict, ctx: Ctx, text: str, parties: list[str], receipt: bool
    ) -> Result | None:
        if not parties:
            return None
        side = "AR" if receipt else "AP"
        kinds = self._sides(receipt)
        month = service_month(text)
        for rank, p in enumerate(parties[:8]):
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
                if cn:
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
            # (b1) discount taken AFTER the deadline on a single invoice: not accepted, the
            # difference stays open -> PARTIAL with the cash applied (POLICIES §3)
            if rank == 0:
                late = []
                for i in inv:
                    d, within = discount_c(self.book.items[i], self.terms, ctx.booking)
                    if (
                        d
                        and not within
                        and self.book.items[i].ccy == ctx.ccy
                        and abs(self.book.open[i] - d - ctx.cash_c) <= 1
                    ):
                        late.append(i)
                if len(late) == 1:
                    return Result(
                        r["bank_txn_id"],
                        [Alloc(late[0], ctx.cash_c, "PARTIAL")],
                        rule="party_discount_late",
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
            # (b3) payment on account without reference: a round share (25-70 %, pattern measured
            # on TRAIN) of ONE untouched invoice of the likeliest payer; PARTIAL with the cash
            if rank == 0:
                # the customer may be paying the invoice NET of an open credit note of its own
                # (the golden then clears both): that explanation comes first
                credits_open = [self.book.open[c] for c in open_ids if self.book.items[c].is_credit]
                share = [
                    i
                    for i in inv
                    if self.book.open[i] == self.book.items[i].total
                    and self.book.items[i].ccy == ctx.ccy
                    and any(
                        abs(round(self.book.items[i].total * f) - ctx.cash_c) <= 1
                        for f in PARTIAL_SHARES
                    )
                    and not any(abs(self.book.open[i] - c - ctx.cash_c) <= 2 for c in credits_open)
                ]
                if len(share) == 1:
                    return Result(
                        r["bank_txn_id"],
                        [Alloc(share[0], ctx.cash_c, "PARTIAL")],
                        rule="party_partial_share",
                    )
            # (c) the customer pays its oldest open items (FIFO by due date, then issue date)
            for target, tol in self._targets(ctx):
                extra = self._complete(p, ctx, set(), target, side, subsets=False, tol=tol)
                if extra:
                    res = settle(self.book, extra, ctx, self.fx, self.terms, allow_partial=False)
                    if res:
                        return Result(r["bank_txn_id"], res, rule="party_fifo")
            if rank >= 4:
                continue  # combinatorial searches only for the most likely payers
            # (d) a payment of several invoices without reference: the UNIQUE subset of the
            # payer's recent open items (credit notes included) that adds up to the cash
            res = self._unique_subset_alloc(open_ids, ctx)
            if res:
                return Result(r["bank_txn_id"], res, rule="party_subset")
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
        recent_days: int = 240,
        cap: int = 22,
        max_items: int = 5,
    ) -> list[Alloc] | None:
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

    def run(
        self, progress: Callable[[int, int], None] | None = None, every: int = 1000
    ) -> list[Result]:
        """Process every bank line in booking order. `progress(done, total)` is called every
        `every` lines and at the end (display only: it does not change any result)."""
        out = []
        total = len(self.rows)
        for n, r in enumerate(self.rows, start=1):
            if progress and n % every == 0:
                progress(n, total)
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
        if progress:
            progress(total, total)
        return out
