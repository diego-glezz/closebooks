"""For receipts we leave unidentified but the golden settles with items: is the payer findable
(IBAN / name in the text) and how many items does the payment cover?

    python scripts/diagnose_lost_b.py out/train_2024-07_2024-12 [--data data/challenge]
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

import pandas as pd

SEGS = {"ar_ref_noise", "ar_1toN_text", "ar_1toN_credit_note", "ar_n_to_m"}


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().upper()


def words(s: str) -> list[str]:
    return [w for w in re.split(r"[^A-Z0-9]+", fold(s)) if len(w) >= 4]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    lost_ids = set(
        expl.loc[expl["rule"].isin(["unidentified", "category_learned_fallback"]), "bank_txn_id"]
    )
    g = gold[gold["bank_txn_id"].isin(lost_ids) & (gold["item_id"] != "") & gold["segment"].isin(SEGS)]
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str).fillna("")
    bank = bank[bank["bank_txn_id"].isin(set(g["bank_txn_id"]))].set_index("bank_txn_id")
    inv = pd.concat(
        [
            pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str).rename(
                columns={"customer_id": "party"}
            )[["item_id", "party", "issue_date", "total_amount"]],
            pd.read_csv(root / "train" / "credit_notes.csv.gz", dtype=str).rename(
                columns={"party_id": "party"}
            )[["item_id", "party", "issue_date", "total_amount"]],
        ]
    ).drop_duplicates("item_id").set_index("item_id")
    cust = pd.read_csv(root / "masters" / "customers.csv", dtype=str).fillna("").set_index("customer_id")

    rows = []
    for txn, grp in g.groupby("bank_txn_id"):
        parties = {inv["party"].get(i, "") for i in grp["item_id"]} - {""}
        b = bank.loc[txn]
        blob = fold(" ".join([b["text"], b["counterparty_name"], b["remittance_info"]]))
        text_ibans = set(re.findall(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", blob.replace(" ", "")))
        cp = re.sub(r"\W", "", fold(b["counterparty_account"]))
        iban_hit = name_hit = False
        for p in parties:
            if p not in cust.index:
                continue
            c = cust.loc[p]
            ib = re.sub(r"\W", "", fold(c["iban"]))
            if ib and (ib == cp or any(ib in t or t in ib for t in text_ibans)):
                iban_hit = True
            ws = words(c["name"]) + words(c["trade_name"])
            if ws and any(w in blob for w in ws[:2]):
                name_hit = True
        n = len(grp)
        rows.append(
            {
                "segment": grp["segment"].iloc[0],
                "n_items": "1" if n == 1 else "2-3" if n <= 3 else "4-5" if n <= 5 else "6-10" if n <= 10 else "11+",
                "one_party": len(parties) == 1,
                "iban": iban_hit,
                "name": name_hit,
                "any_id": iban_hit or name_hit,
                "has_counterparty_iban": bool(cp),
            }
        )
    d = pd.DataFrame(rows)
    print(f"lost receipts with golden items (B segments): {len(d)}")
    print("\nby segment:\n", d["segment"].value_counts().to_string())
    print("\nitems per payment:\n", d["n_items"].value_counts().to_string())
    print("\npayer findable? (iban | name | either | counterparty iban printed):")
    print(d[["iban", "name", "any_id", "has_counterparty_iban", "one_party"]].mean().round(3).to_string())
    print("\nfindable by n_items:\n", d.groupby("n_items")["any_id"].agg(["size", "mean"]).round(3).to_string())
    print("\nfindable by segment:\n", d.groupby("segment")["any_id"].agg(["size", "mean"]).round(3).to_string())


if __name__ == "__main__":
    main()
