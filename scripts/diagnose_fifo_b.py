"""Same-amount FIFO errors: when `party_amount` picks a different invoice than the golden, what
distinguishes the golden one (older? newer? partially paid? due date?).

    python scripts/diagnose_fifo_b.py out/train_2024-07_2024-12 [--data data/challenge]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    ap.add_argument("--segment", default="ar_same_amount_fifo")
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    sub = pd.read_csv(run / "submission.csv.gz", dtype=str).fillna("")
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    gold = gold[(gold["segment"] == a.segment) & (gold["item_id"] != "")]
    inv = pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str).set_index("item_id")
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str).set_index("bank_txn_id")
    rule_of = dict(zip(expl["bank_txn_id"], expl["rule"], strict=True))
    sub = sub[(sub["item_id"] != "") & sub["bank_txn_id"].isin(set(gold["bank_txn_id"]))]
    ours = sub.groupby("bank_txn_id")["item_id"].apply(set).to_dict()
    theirs = gold.groupby("bank_txn_id")["item_id"].apply(set).to_dict()
    rows = []
    for t, g in theirs.items():
        o = ours.get(t)
        if o is None:
            rows.append({"txn": t, "case": "no_items", "rule": rule_of.get(t, "?")})
            continue
        if o == g:
            rows.append({"txn": t, "case": "ok", "rule": rule_of.get(t, "?")})
            continue
        if len(o) == 1 and len(g) == 1:
            oi, gi = next(iter(o)), next(iter(g))
            bd = bank.loc[t, "booking_date"]
            od, gd = inv.loc[oi, "issue_date"], inv.loc[gi, "issue_date"]
            case = "golden_older" if gd < od else "golden_newer" if gd > od else "same_issue"
            od2, gd2 = inv.loc[oi, "due_date"], inv.loc[gi, "due_date"]
            rows.append(
                {
                    "txn": t,
                    "case": case,
                    "rule": rule_of.get(t, "?"),
                    "due_cmp": "golden_due_earlier" if gd2 < od2 else "golden_due_later" if gd2 > od2 else "same_due",
                    "same_party": inv.loc[oi, "customer_id"] == inv.loc[gi, "customer_id"],
                    "gap_days_golden": (pd.Timestamp(bd) - pd.Timestamp(gd)).days,
                    "gap_days_ours": (pd.Timestamp(bd) - pd.Timestamp(od)).days,
                }
            )
        else:
            rows.append({"txn": t, "case": f"multi_{len(o)}v{len(g)}", "rule": rule_of.get(t, "?")})
    d = pd.DataFrame(rows)
    print(f"{a.segment}: {len(d)} golden txns")
    print(d["case"].value_counts().to_string())
    wrong = d[d["case"].isin(["golden_older", "golden_newer", "same_issue"])]
    if len(wrong):
        print("\nwrong single picks:", len(wrong))
        print(wrong["due_cmp"].value_counts().to_string())
        print("same party:", wrong["same_party"].mean().round(3))
        print("golden payment gap (days since issue):\n", wrong["gap_days_golden"].astype(float).describe().round(1).to_string())
        print("our pick gap (days since issue):\n", wrong["gap_days_ours"].astype(float).describe().round(1).to_string())
        print("\nrule of wrong picks:\n", wrong["rule"].value_counts().to_string())


if __name__ == "__main__":
    main()
