"""Shape of the many-invoice payments we lose: which of the payer's open invoices does the
golden settle? (FIFO prefix? a contiguous block of issue dates? one month? one due month?)

The open list of a payer at time t is rebuilt from the golden itself: invoices issued <= t whose
first golden settlement is on/after t (or never). Approximate but independent of our engine.

    python scripts/diagnose_multi_b.py out/train_2024-07_2024-12 [--data data/challenge]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    ap.add_argument("--min-items", type=int, default=4)
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    lost = set(expl.loc[expl["rule"].isin(["unidentified", "category_learned_fallback"]), "bank_txn_id"])
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str, usecols=["bank_txn_id", "booking_date"])
    bdate = dict(zip(bank["bank_txn_id"], bank["booking_date"], strict=True))
    inv = pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str)[
        ["item_id", "customer_id", "issue_date", "due_date"]
    ]
    gi = gold[gold["item_id"].str.startswith("ARI")].copy()
    gi["date"] = gi["bank_txn_id"].map(bdate)
    first_paid = gi.groupby("item_id")["date"].min().to_dict()
    inv["first_paid"] = inv["item_id"].map(first_paid).fillna("9999")
    by_party = {p: g.sort_values(["issue_date", "item_id"]) for p, g in inv.groupby("customer_id")}
    party_of = dict(zip(inv["item_id"], inv["customer_id"], strict=True))
    issue_of = dict(zip(inv["item_id"], inv["issue_date"], strict=True))
    due_of = dict(zip(inv["item_id"], inv["due_date"], strict=True))

    stats = Counter()
    holes_inside, older_open, spans, n_open = [], [], [], []
    for txn, g in gi[gi["bank_txn_id"].isin(lost)].groupby("bank_txn_id"):
        items = list(g["item_id"])
        if len(items) < a.min_items:
            continue
        parties = {party_of.get(i) for i in items}
        if len(parties) != 1 or None in parties:
            stats["multi_party"] += 1
            continue
        t = bdate[txn]
        pl = by_party[parties.pop()]
        open_now = pl[(pl["issue_date"] <= t) & (pl["first_paid"] >= t)]["item_id"].tolist()
        pos = sorted(open_now.index(i) for i in items if i in open_now)
        if len(pos) != len(items):
            stats["items_not_in_open_list"] += 1
            continue
        stats["analysed"] += 1
        inside = pos[-1] - pos[0] + 1 - len(pos)
        holes_inside.append(inside)
        older_open.append(pos[0])
        n_open.append(len(open_now))
        months = {issue_of[i][:7] for i in items}
        dues = {due_of[i][:7] for i in items}
        stats["contiguous_block"] += inside == 0
        stats["fifo_prefix"] += inside == 0 and pos[0] == 0
        stats["all_open_items"] += len(items) == len(open_now)
        stats["one_issue_month"] += len(months) == 1
        stats["one_due_month"] += len(dues) == 1
        all_of_month = all(
            set(pl[(pl["issue_date"].str[:7] == m) & (pl["issue_date"] <= t) & (pl["first_paid"] >= t)]["item_id"]) <= set(items)
            for m in months
        )
        stats["whole_issue_months"] += all_of_month
        spans.append((pd.Timestamp(max(issue_of[i] for i in items)) - pd.Timestamp(min(issue_of[i] for i in items))).days)
    n = stats["analysed"]
    print(f"lost payments with >= {a.min_items} items: analysed {n}", dict(stats))
    for k in ("fifo_prefix", "contiguous_block", "all_open_items", "one_issue_month", "one_due_month", "whole_issue_months"):
        print(f"  {k:20s} {stats[k] / max(n, 1):.3f}")
    s = pd.Series
    print("\nholes inside the block:\n", s(holes_inside).describe().round(1).to_string())
    print("\nolder open items left before the block:\n", s(older_open).describe().round(1).to_string())
    print("\nopen items of the payer at that time:\n", s(n_open).describe().round(1).to_string())
    print("\nissue-date span of the block (days):\n", s(spans).describe().round(1).to_string())


if __name__ == "__main__":
    main()
