"""Learn categories of no-item bank lines from the TRAIN golden.

A bank line's "shape" = sign | bank_code | bank_subcode | text with digits and punctuation
normalized. For each shape we keep the category distribution of the golden and how often lines
of that shape settle no item at all. The engine uses:
  * strong shapes (mostly no-item, one dominant category) BEFORE matching items
  * any known shape as the fallback category when nothing else matched

Train only on lines booked BEFORE the evaluation window (no leakage).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from closebooks.bankrec.text import fold

_DIG = re.compile(r"\d+")
_PUNCT = re.compile(r"[^A-Z# ]+")
_SPACES = re.compile(r"\s+")


def shape(amount_c: int, bank_code: str, bank_subcode: str, text: str) -> str:
    t = _DIG.sub("#", fold(text))
    t = _SPACES.sub(" ", _PUNCT.sub(" ", t)).strip()
    words = t.split()[:6]
    return f"{'+' if amount_c > 0 else '-'}|{bank_code}|{bank_subcode}|{' '.join(words)}"


def coarse(amount_c: int, bank_code: str, bank_subcode: str) -> str:
    return f"{'+' if amount_c > 0 else '-'}|{bank_code}|{bank_subcode}"


def _fee_table(con, folder: str, before: str, min_n: int = 5) -> dict[str, list[int]]:
    """Shortfalls (cents, account currency) that the golden labels SHORT_BANK_CHARGES, per
    currency: intermediary banks deduct a fixed fee schedule, so the shortfall repeats."""
    rows = con.execute(f"""
        select b.currency, g.amount, b.amount
        from read_csv_auto('{folder}/golden_allocations.csv.gz', all_varchar=true) g
        join read_csv_auto('{folder}/bank_transactions.csv.gz', all_varchar=true) b
          using(bank_txn_id)
        where g.exception = 'SHORT_BANK_CHARGES' and b.booking_date < '{before}'""").fetchall()
    counts: dict[str, Counter] = defaultdict(Counter)
    for ccy, item_amount, bank_amount in rows:
        short = round(float(item_amount) * 100) - abs(round(float(bank_amount) * 100))
        if short > 0:
            counts[ccy][short] += 1
    return {ccy: sorted(v for v, n in c.items() if n >= min_n) for ccy, c in sorted(counts.items())}


def build(root: Path, before: str, out: Path) -> dict:
    import duckdb

    folder = (root / "train").as_posix()
    con = duckdb.connect()
    rows = con.execute(f"""
        with g as (
          select bank_txn_id, max(item_id) as item, max(category) as cat
          from read_csv_auto('{folder}/golden_allocations.csv.gz', all_varchar=true)
          group by 1)
        select b.amount, b.bank_code, coalesce(b.bank_subcode,''), coalesce(b.text,''),
               g.item, g.cat
        from read_csv_auto('{folder}/bank_transactions.csv.gz', all_varchar=true) b
        join g using(bank_txn_id)
        where b.booking_date < '{before}'""").fetchall()
    fine: dict[str, Counter] = defaultdict(Counter)
    rough: dict[str, Counter] = defaultdict(Counter)
    for amount, code, sub, text, item, cat in rows:
        a = round(float(amount) * 100)
        label = cat if not item and cat else "__ITEM__"
        fine[shape(a, code or "", sub, text)][label] += 1
        rough[coarse(a, code or "", sub)][label] += 1

    def summarize(table: dict[str, Counter], min_n: int) -> dict:
        out_t = {}
        for k, c in table.items():
            n = sum(c.values())
            if n < min_n:
                continue
            noitem = n - c.get("__ITEM__", 0)
            cats = Counter({x: v for x, v in c.items() if x != "__ITEM__"})
            if not cats:
                continue
            top, top_n = cats.most_common(1)[0]
            out_t[k] = {
                "n": n,
                "noitem": round(noitem / n, 4),
                "cat": top,
                "purity": round(top_n / noitem, 4),
            }
        return out_t

    model = {
        "before": before,
        "fine": summarize(fine, 5),
        "coarse": summarize(rough, 20),
        "fees": _fee_table(con, folder, before),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(model), encoding="utf-8")
    return model


class CategoryModel:
    def __init__(self, path: Path | None):
        self.fine: dict = {}
        self.coarse: dict = {}
        self.fees: dict[str, list[int]] = {}
        if path and path.exists():
            m = json.loads(path.read_text(encoding="utf-8"))
            self.fine, self.coarse = m["fine"], m["coarse"]
            self.fees = m.get("fees", {})

    def strong(self, r: dict) -> str | None:
        """Category to use BEFORE item matching: the shape almost never settles items."""
        e = self.fine.get(shape(int(r["amount_c"]), r["bank_code"], r["bank_subcode"], r["text"]))
        if e and e["n"] >= 20 and e["noitem"] >= 0.99 and e["purity"] >= 0.97:
            return e["cat"]
        return None

    def fallback(self, r: dict) -> str | None:
        """Most likely category when no item matched."""
        a = int(r["amount_c"])
        e = self.fine.get(shape(a, r["bank_code"], r["bank_subcode"], r["text"]))
        if e and e["purity"] >= 0.6:
            return e["cat"]
        e = self.coarse.get(coarse(a, r["bank_code"], r["bank_subcode"]))
        if e and e["purity"] >= 0.6 and e["noitem"] >= 0.5:
            return e["cat"]
        return None
