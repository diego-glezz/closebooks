"""Lost no-reference receipts vs PARSED remittance advices of the window: same amount (+-1 cent)
within +-20 days, and do the advice lines name the golden invoices?

    python scripts/diagnose_advices2_b.py out/train_2024-07_2024-12 --start 2024-07-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from closebooks.bankrec.docs import load_advices  # noqa: E402
from closebooks.bankrec.text import name_key, number_keys, text_tokens  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default="2024-12-31")
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    lost = set(expl.loc[expl["rule"].isin(["unidentified", "category_learned_fallback"]), "bank_txn_id"])
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    g = gold[gold["bank_txn_id"].isin(lost) & gold["item_id"].str.startswith(("ARI", "ARC"))]
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str).fillna("").set_index("bank_txn_id")
    inv = pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str).fillna("")
    cn = pd.read_csv(root / "train" / "credit_notes.csv.gz", dtype=str).fillna("")
    num = {**dict(zip(inv["item_id"], inv["document_number"], strict=True)), **dict(zip(cn["item_id"], cn["document_number"], strict=True))}

    idx = pd.read_csv(root / "train" / "remittance_advices" / "index.csv", dtype=str).fillna("")
    lo = (pd.Timestamp(a.start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    hi = (pd.Timestamp(a.end) + pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    idx = idx[(idx["received_date"] >= lo) & (idx["received_date"] <= hi)]
    advices = load_advices(root / "train" / "remittance_advices", idx.to_dict("records"))
    by_amt: dict[int, list] = defaultdict(list)
    for adv in advices:
        for amt in {adv.paid_total, adv.total_c or 0} - {0}:
            by_amt[abs(amt)].append(adv)
    print(f"advices parsed in window: {len(advices)} (with lines: {sum(bool(x.lines) for x in advices)})")

    n = amt_hit = name_hit = items_hit = 0
    for txn, grp in g.groupby("bank_txn_id"):
        b = bank.loc[txn]
        toks = text_tokens(" | ".join(x for x in (b["text"], b["ref1"], b["ref2"], b["remittance_info"]) if x))
        if any(number_keys(num.get(i, "")) & toks for i in grp["item_id"]):
            continue
        n += 1
        cash = abs(round(float(b["amount"]) * 100))
        day = pd.Timestamp(b["booking_date"])
        cands = [
            x for c in range(cash - 1, cash + 2) for x in by_amt.get(c, [])
            if abs((pd.Timestamp(x.pay_date or x.received) - day).days) <= 20
        ]
        if not cands:
            continue
        amt_hit += 1
        blob = name_key(b["text"] + " " + b["counterparty_name"])
        if any(name_key(x.payer).split()[:1] and name_key(x.payer).split()[0] in blob for x in cands):
            name_hit += 1
        gold_keys = set().union(*(number_keys(num.get(i, "")) for i in grp["item_id"]))
        if any(sum(bool(number_keys(ln.number) & gold_keys) for ln in x.lines) >= max(1, len(grp) // 2) for x in cands):
            items_hit += 1
    print(f"lost receipts with no reference: {n}")
    print(f"  an advice with the same amount within +-20 days: {amt_hit} ({amt_hit / max(n, 1):.1%})")
    print(f"  ... and the payer's first name word in the bank text: {name_hit}")
    print(f"  ... and the advice lists >= half of the golden invoices: {items_hit}")


if __name__ == "__main__":
    main()
