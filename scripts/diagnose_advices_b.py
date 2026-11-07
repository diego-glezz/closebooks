"""Lost receipts without any reference: does the paying customer have a remittance advice in the
index within +-20 days? (If so the fix is linking the advice, not guessing invoices.)

    python scripts/diagnose_advices_b.py out/train_2024-07_2024-12 [--data data/challenge]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from closebooks.bankrec.text import name_key, number_keys, text_tokens  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    lost = set(expl.loc[expl["rule"].isin(["unidentified", "category_learned_fallback"]), "bank_txn_id"])
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    g = gold[gold["bank_txn_id"].isin(lost) & gold["item_id"].str.startswith("ARI")]
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str).fillna("").set_index("bank_txn_id")
    inv = pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str).fillna("")
    num = dict(zip(inv["item_id"], inv["document_number"], strict=True))
    party = dict(zip(inv["item_id"], inv["customer_id"], strict=True))
    cust = pd.read_csv(root / "masters" / "customers.csv", dtype=str).fillna("").set_index("customer_id")
    idx = pd.read_csv(root / "train" / "remittance_advices" / "index.csv", dtype=str).fillna("")
    idx["key"] = idx["payer_name"].map(name_key)
    idx["day"] = pd.to_datetime(idx["received_date"])
    by_key = {k: grp["day"].sort_values().tolist() for k, grp in idx.groupby("key")}

    n = with_adv = 0
    for txn, grp in g.groupby("bank_txn_id"):
        b = bank.loc[txn]
        toks = text_tokens(" | ".join(x for x in (b["text"], b["ref1"], b["ref2"], b["remittance_info"]) if x))
        if any(number_keys(num.get(i, "")) & toks for i in grp["item_id"]):
            continue  # some reference cited: not the population we look at
        p = party.get(grp["item_id"].iloc[0])
        if p not in cust.index:
            continue
        n += 1
        names = {name_key(cust.loc[p, "name"]), name_key(cust.loc[p, "trade_name"])} - {""}
        day = pd.Timestamp(b["booking_date"])
        if any(abs((d - day).days) <= 20 for k in names for d in by_key.get(k, [])):
            with_adv += 1
    print(f"lost receipts with NO reference (golden ARI items): {n}")
    print(f"  ... whose customer has an advice within +-20 days: {with_adv} ({with_adv / max(n, 1):.1%})")
    print(f"advices in index: {len(idx)}; distinct payers: {idx['key'].nunique()}")


if __name__ == "__main__":
    main()
