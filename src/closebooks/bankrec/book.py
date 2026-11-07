"""Open-items ledger + indexes (by reference key, by party) + party resolution.

The book is processed chronologically: every allocation reduces the open amount, returns
re-open it, and a payment to an item that is already settled is a DUPLICATE_PAYMENT.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from closebooks.bankrec.text import LEGAL_SUFFIXES, alnum, fold, name_key, number_keys, words


@dataclass(slots=True)
class Item:
    id: str
    kind: str  # ARI ARC API APC
    side: str  # AR AP
    entity: str
    number: str
    issue: str
    due: str
    party: str
    pname: str
    ccy: str
    total: int  # cents, positive
    terms: str
    po: str
    method: str
    related: str

    @property
    def is_credit(self) -> bool:
        return self.kind in ("ARC", "APC")


class Book:
    def __init__(self, items: pd.DataFrame):
        self.items: dict[str, Item] = {}
        self.open: dict[str, int] = {}
        self.paid: dict[str, list[tuple[str, int]]] = defaultdict(list)  # item -> [(txn, cents)]
        self.by_key: dict[str, list[str]] = defaultdict(list)
        self.by_number: dict[str, list[str]] = defaultdict(list)  # alnum(number) exact
        self.by_party: dict[str, list[str]] = defaultdict(list)
        self.cancelled: set[str] = set()  # replaced invoices + their cancelling credit notes
        self.replaced_by: dict[str, str] = {}  # old number alnum -> new item id
        for r in items.itertuples(index=False):
            it = Item(
                r.item_id,
                r.kind,
                r.side,
                r.entity,
                r.number,
                r.issue_date,
                r.due_date,
                r.party_id,
                r.party_name,
                r.currency,
                int(r.total_c),
                r.terms,
                r.po,
                r.method,
                r.related,
            )
            self.items[it.id] = it
            self.open[it.id] = int(r.open_c)
            for k in number_keys(it.number):
                self.by_key[k].append(it.id)
            self.by_number[alnum(it.number)].append(it.id)
            if it.po:
                for k in number_keys(it.po):
                    self.by_key["PO:" + k].append(it.id)
            self.by_party[it.party].append(it.id)
        for ids in self.by_party.values():
            ids.sort(key=lambda i: (self.items[i].issue, self.items[i].number))

    # ------------------------------------------------------------- queries

    def lookup(self, key: str) -> list[str]:
        return self.by_key.get(key, [])

    def find_number(
        self,
        number: str,
        party: str | None = None,
        side: str | None = None,
        entities: set[str] | None = None,
    ) -> list[str]:
        """Items whose printed number matches (exact alnum first, then noisy keys)."""
        cands = list(self.by_number.get(alnum(number), []))
        if not cands:
            seen: set[str] = set()
            for k in number_keys(number):
                for i in self.by_key.get(k, []):
                    if i not in seen:
                        seen.add(i)
                        cands.append(i)
        return [i for i in cands if self._ok(i, party, side, entities)]

    def _ok(self, iid: str, party: str | None, side: str | None, entities: set[str] | None) -> bool:
        it = self.items[iid]
        return (
            (party is None or it.party == party)
            and (side is None or it.side == side)
            and (entities is None or it.entity in entities)
        )

    def open_items(self, party: str, booking: str, side: str | None = None) -> list[str]:
        return [
            i
            for i in self.by_party.get(party, [])
            if self.open[i] > 0
            and self.items[i].issue <= booking
            and (side is None or self.items[i].side == side)
            and i not in self.cancelled
        ]

    # ------------------------------------------------------------- mutations

    def apply(self, txn: str, iid: str, amount_c: int, exception: str) -> None:
        """Record an allocation. Fully-clearing exceptions close the item."""
        if exception == "RETURNED":
            self.open[iid] += amount_c
            return
        if exception in (
            "SHORT_DISCOUNT",
            "SHORT_BANK_CHARGES",
            "SHORT_WITHHOLDING",
            "FX_DIFFERENCE",
            "OVERPAYMENT",
        ):
            self.open[iid] = 0
        elif exception == "DUPLICATE_PAYMENT":
            pass
        else:
            self.open[iid] = max(0, self.open[iid] - amount_c)
        self.paid[iid].append((txn, amount_c))


# ---------------------------------------------------------------- parties


class Parties:
    """Resolve who paid / who was paid from IBAN, mandate, or (truncated) name in bank text."""

    def __init__(self, customers: pd.DataFrame, suppliers: pd.DataFrame, items: dict[str, Item]):
        self.by_iban: dict[str, set[str]] = defaultdict(set)
        self.by_mandate: dict[str, str] = {}
        self.entity: dict[str, str] = {}
        self.country: dict[str, str] = {}
        self.parent: dict[str, str] = {}
        self.group: dict[str, str] = {}
        self.children: dict[str, set[str]] = defaultdict(set)
        self.group_members: dict[str, set[str]] = defaultdict(set)
        self.names: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        # entity -> first word -> {"FULL KEY|party"}
        self._learned: dict[str, str] = {}
        self._shared: set[str] = set()
        self._words: dict[str, dict[str, set[str]]] | None = None
        self._key_words: dict[str, list[str]] = {}
        for df, kind in ((customers, "C"), (suppliers, "S")):
            for r in df.to_dict("records"):
                pid = r["customer_id"] if kind == "C" else r["supplier_id"]
                self.entity[pid] = r["entity"]
                self.country[pid] = r.get("country", "")
                if r.get("iban"):
                    self.by_iban[alnum(r["iban"])].add(pid)
                if kind == "C":
                    if r.get("sepa_mandate_id"):
                        self.by_mandate[alnum(r["sepa_mandate_id"])] = pid
                    if r.get("parent_customer_id"):
                        self.parent[pid] = r["parent_customer_id"]
                        self.children[r["parent_customer_id"]].add(pid)
                    if r.get("group_id"):
                        self.group[pid] = r["group_id"]
                        self.group_members[r["group_id"]].add(pid)
                    self._add_name(r["entity"], r.get("trade_name", ""), pid)
                self._add_name(r["entity"], r["name"], pid)
        for it in items.values():  # names printed on invoices (pre-rename names)
            self._add_name(it.entity, it.pname, it.party)

    def _add_name(self, entity: str, name: str, pid: str) -> None:
        key = name_key(name)
        if len(key) < 4:
            return
        first = key.split()[0]
        self.names[entity][first].add(f"{key}|{pid}")
        known = self._key_words.setdefault(pid, [])
        for w in key.split():
            if w not in known:
                known.append(w)

    def add_iban(self, iban: str, pid: str) -> None:
        self.by_iban[alnum(iban)].add(pid)

    def learn_iban(self, iban: str, pid: str) -> None:
        """Remember the account of a payer that a reference identified without doubt.

        An account seen paying for two unrelated families is an intermediary (confirming bank,
        factor, agency): it is dropped for good, so it never points to a single customer."""
        iban = alnum(iban)
        if not iban or iban in self._shared:
            return
        if iban in self._learned:
            if pid not in self.family(self._learned[iban]):
                self._shared.add(iban)
                self._learned.pop(iban)
                self.by_iban.pop(iban, None)
            return
        if iban in self.by_iban:  # master data wins
            return
        self._learned[iban] = pid
        self.by_iban[iban].add(pid)

    def _word_index(self) -> dict[str, dict[str, set[str]]]:
        """entity -> word -> {party}: any word of any known name (for out-of-order matches)."""
        if self._words is None:
            self._words = defaultdict(lambda: defaultdict(set))
            self._near: dict[tuple[str, str], set[str]] = defaultdict(set)
            for ent, firsts in self.names.items():
                for cands in firsts.values():
                    for c in cands:
                        key, pid = c.split("|")
                        for w in key.split():
                            if len(w) >= 5:
                                self._words[ent][w].add(pid)
                            if len(w) >= 7:
                                # one edit leaves the first or the last 3 letters untouched
                                self._near[(ent, "^" + w[:3])].add(w)
                                self._near[(ent, "$" + w[-3:])].add(w)
        return self._words

    def fuzzy_names(self, text: str, entities: set[str]) -> list[tuple[str, int]]:
        """Fallback when no name matched as a prefix: every distinctive word of a known name
        appears in the text (any order), or the first word has a single typo."""
        ws = [w for w in words(text) if w not in LEGAL_SUFFIXES and len(w) >= 5]
        if not ws:
            return []
        index = self._word_index()
        found: dict[str, set[str]] = defaultdict(set)
        for ent in sorted(entities):
            table = index.get(ent, {})
            for w in set(ws):
                for pid in table.get(w, ()):
                    found[pid].add(w)
                if len(w) >= 7:  # one substituted / dropped / added letter
                    near = self._near.get((ent, "^" + w[:3]), set()) | self._near.get(
                        (ent, "$" + w[-3:]), set()
                    )
                    for cand in sorted(near):
                        if cand != w and abs(len(cand) - len(w)) <= 1 and _one_edit(cand, w):
                            for pid in table[cand]:
                                found[pid].add(cand)
        best: dict[str, int] = {}
        for pid, hit in found.items():
            key_words = [w for w in self._key_words.get(pid, ()) if len(w) >= 5]
            if not key_words:
                continue
            covered = sum(1 for w in key_words if w in hit)
            # all distinctive words, and at least 2 of them unless the name has a single one
            if covered == len(key_words) and (covered >= 2 or len(key_words[0]) >= 9):
                best[pid] = sum(len(w) for w in hit)
        return sorted(best.items(), key=lambda x: (-x[1], x[0]))

    def family(self, pid: str) -> set[str]:
        """The party plus subsidiaries it may pay for (parent) and group members."""
        fam = {pid} | self.children.get(pid, set())
        g = self.group.get(pid)
        if g:
            fam |= self.group_members[g]
        return fam

    def by_name(self, text: str, entities: set[str], min_len: int = 8) -> list[tuple[str, int]]:
        """[(party, matched_chars)] best first. Handles names truncated in the bank text."""
        ws = [w for w in words(text) if w not in LEGAL_SUFFIXES]  # same normalization as name_key
        joined_from = {}
        best: dict[str, int] = {}
        for i, w in enumerate(ws):
            for ent in sorted(entities):
                cands = self.names.get(ent, {}).get(w)
                if not cands:
                    continue
                if i not in joined_from:
                    joined_from[i] = " ".join(ws[i : i + 8])
                tail = joined_from[i]
                for c in sorted(cands):
                    key, pid = c.split("|")
                    n = _common_prefix(key, tail)
                    # whole name, or a truncation of at least min_len chars that stops in the text
                    truncated_ok = n >= min_len and (
                        n == len(tail) or n >= len(key) - 2 or tail[n : n + 1] == " " or n >= 10
                    )
                    # a complete name beats a truncation of a longer one
                    score = n + (1000 if n == len(key) else 0)
                    if (n == len(key) or truncated_ok) and score > best.get(pid, 0):
                        best[pid] = score
        return sorted(best.items(), key=lambda x: (-x[1], x[0]))


def _one_edit(a: str, b: str) -> bool:
    """True when a and b differ by one substitution, insertion or deletion."""
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1 :]


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


__all__ = ["Book", "Item", "Parties", "fold"]
