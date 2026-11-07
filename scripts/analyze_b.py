"""Where does role B lose points? Pair precision per rule and misses per segment, from a run's
submission + explain.csv.gz and the TRAIN golden (no engine run needed).

    python scripts/analyze_b.py out/train_2024-07_2024-12 [--data data/challenge]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

B_SEGMENTS = (
    "ar_ref_noise",
    "ar_1toN_text",
    "ar_same_amount_fifo",
    "ar_third_party_parent",
    "ar_third_party_agency",
    "ar_third_party_confirming",
    "ar_new_iban",
    "ar_1toN_credit_note",
    "ar_n_to_m",
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    a = ap.parse_args()
    run = Path(a.run)
    sub = pd.read_csv(run / "submission.csv.gz", dtype=str).fillna("")
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    gold = pd.read_csv(Path(a.data) / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    gold = gold[gold["bank_txn_id"].isin(set(sub["bank_txn_id"]))]
    rule_of = dict(zip(expl["bank_txn_id"], expl["rule"], strict=True))
    seg_of = dict(zip(gold["bank_txn_id"], gold["segment"], strict=True))

    sp = sub[sub["item_id"] != ""]
    gp = gold[gold["item_id"] != ""]
    gset = set(zip(gp["bank_txn_id"], gp["item_id"], strict=True))
    sset = set(zip(sp["bank_txn_id"], sp["item_id"], strict=True))

    # 1) pair precision per rule
    sp = sp.assign(rule=sp["bank_txn_id"].map(rule_of), ok=[(t, i) in gset for t, i in zip(sp["bank_txn_id"], sp["item_id"], strict=True)])
    prec = sp.groupby("rule")["ok"].agg(["size", "sum"]).rename(columns={"size": "pairs", "sum": "tp"})
    prec["precision"] = (prec["tp"] / prec["pairs"]).round(3)
    prec["wrong_pairs"] = prec["pairs"] - prec["tp"]
    print("=== pair precision by rule (worst loss first) ===")
    print(prec.sort_values("wrong_pairs", ascending=False).to_string())

    # 2) golden pairs we miss: segment x what we did instead
    miss = gp[[(t, i) not in sset for t, i in zip(gp["bank_txn_id"], gp["item_id"], strict=True)]].copy()
    miss["our_rule"] = miss["bank_txn_id"].map(rule_of).fillna("?")
    print("\n=== golden pairs missed, by segment (top 15) ===")
    print(miss["segment"].value_counts().head(15).to_string())
    print("\n=== missed pairs: segment x our rule (B segments) ===")
    b = miss[miss["segment"].isin(B_SEGMENTS)]
    print(b.groupby(["segment", "our_rule"]).size().sort_values(ascending=False).head(25).to_string())

    # 3) wrong pairs by segment (we submitted, golden does not have it)
    wrong = sp[~sp["ok"]].copy()
    wrong["segment"] = wrong["bank_txn_id"].map(seg_of).fillna("(not in golden)")
    print("\n=== wrong pairs: segment x rule (top 20) ===")
    print(wrong.groupby(["segment", "rule"]).size().sort_values(ascending=False).head(20).to_string())

    # 4) same-amount FIFO: did we pick an older / newer invoice than the golden?
    print("\n=== receipts left UNIDENTIFIED although the golden has items, by segment ===")
    un = expl[(expl["rule"] == "unidentified")]["bank_txn_id"]
    g_items = set(gp["bank_txn_id"])
    lost = [t for t in un if t in g_items]
    print(pd.Series([seg_of.get(t, "?") for t in lost]).value_counts().head(12).to_string())


if __name__ == "__main__":
    main()
