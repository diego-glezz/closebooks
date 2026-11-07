"""Lost many-invoice payments: are the golden invoices cited in the bank text after all?
Uses the engine's own normalisation (text_tokens / number_keys) and also checks customer POs.

    python scripts/diagnose_refs_b.py out/train_2024-07_2024-12 [--data data/challenge]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from closebooks.bankrec.text import alnum, number_keys, text_tokens  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    ap.add_argument("--examples", type=int, default=6)
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    lost = set(expl.loc[expl["rule"].isin(["unidentified", "category_learned_fallback"]), "bank_txn_id"])
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    g = gold[gold["bank_txn_id"].isin(lost) & (gold["item_id"] != "")]
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str).fillna("")
    bank = bank[bank["bank_txn_id"].isin(set(g["bank_txn_id"]))].set_index("bank_txn_id")
    inv = pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str).fillna("")
    cn = pd.read_csv(root / "train" / "credit_notes.csv.gz", dtype=str).fillna("")
    num = {**dict(zip(inv["item_id"], inv["document_number"], strict=True)), **dict(zip(cn["item_id"], cn["document_number"], strict=True))}
    po = dict(zip(inv["item_id"], inv["customer_po"], strict=True))

    c = Counter()
    per_txn = []
    shown = 0
    for txn, grp in g.groupby("bank_txn_id"):
        b = bank.loc[txn]
        text = " | ".join(x for x in (b["text"], b["ref1"], b["ref2"], b["remittance_info"], b["counterparty_name"]) if x)
        toks = text_tokens(text)
        blob = alnum(text)
        hit_key = hit_po = hit_raw = 0
        for i in grp["item_id"]:
            keys = number_keys(num.get(i, ""))
            hit_key += bool(keys & toks)
            hit_po += bool(po.get(i)) and bool(number_keys(po[i]) & toks)
            # digits of the number appear somewhere, even glued to other text
            d = "".join(ch for ch in num.get(i, "") if ch.isdigit())
            hit_raw += len(d) >= 5 and d[-5:] in blob
        n = len(grp)
        per_txn.append((grp["segment"].iloc[0], n, hit_key / n, hit_po / n, hit_raw / n))
        c["items"] += n
        c["key"] += hit_key
        c["po"] += hit_po
        c["raw5"] += hit_raw
        if shown < a.examples and n >= 4 and hit_key == 0:
            shown += 1
            print(f"--- {txn} {grp['segment'].iloc[0]} n={n} amount={b['amount']}")
            print("   text:", text[:230])
            print("   golden numbers:", [num.get(i, "?") for i in list(grp["item_id"])[:6]])
    d = pd.DataFrame(per_txn, columns=["segment", "n", "key", "po", "raw5"])
    print(f"\nlost receipts: {len(d)}  golden items: {c['items']}")
    print(f"items cited (engine keys): {c['key'] / c['items']:.3f}  by PO: {c['po'] / c['items']:.3f}  "
          f"last-5-digits anywhere: {c['raw5'] / c['items']:.3f}")
    d["any"] = (d["key"] > 0) | (d["po"] > 0)
    print("\nreceipts with at least one golden item cited, by segment:")
    print(d.groupby("segment")["any"].agg(["size", "mean"]).round(3).to_string())
    print("\nreceipts where EVERY golden item is cited (keys or PO):", ((d["key"] + d["po"]) >= 1).mean().round(3))


if __name__ == "__main__":
    main()
