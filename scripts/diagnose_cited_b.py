"""Lost receipts whose text DOES cite some golden invoices: how many are cited vs paid, and where
are the uncited ones (credit notes? close in issue date to the cited ones? among the oldest?).

    python scripts/diagnose_cited_b.py out/train_2024-07_2024-12 [--data data/challenge]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from closebooks.bankrec.text import number_keys, text_tokens  # noqa: E402

SEGS = ("ar_1toN_text", "ar_1toN_credit_note", "ar_ref_noise", "ar_n_to_m", "ar_third_party_parent", "ar_third_party_agency", "ar_dispute_settlement")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    lost = set(expl.loc[expl["rule"].isin(["unidentified", "category_learned_fallback"]), "bank_txn_id"])
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    g = gold[gold["bank_txn_id"].isin(lost) & (gold["item_id"] != "") & gold["segment"].isin(SEGS)]
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str).fillna("").set_index("bank_txn_id")
    inv = pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str).fillna("")
    cn = pd.read_csv(root / "train" / "credit_notes.csv.gz", dtype=str).fillna("")
    meta = pd.concat(
        [
            inv.rename(columns={"customer_id": "party"})[["item_id", "document_number", "party", "issue_date"]],
            cn.rename(columns={"party_id": "party"})[["item_id", "document_number", "party", "issue_date"]],
        ]
    ).drop_duplicates("item_id").set_index("item_id")
    rows = []
    for txn, grp in g.groupby("bank_txn_id"):
        b = bank.loc[txn]
        toks = text_tokens(" | ".join(x for x in (b["text"], b["ref1"], b["ref2"], b["remittance_info"]) if x))
        items = list(grp["item_id"])
        cited = [i for i in items if number_keys(meta["document_number"].get(i, "")) & toks]
        if not cited:
            continue
        rest = [i for i in items if i not in cited]
        parties = {meta["party"].get(i) for i in items}
        cited_parties = {meta["party"].get(i) for i in cited}
        cited_dates = sorted(meta["issue_date"].get(i, "") for i in cited)
        lo, hi = cited_dates[0], cited_dates[-1]
        rest_inside = sum(lo <= meta["issue_date"].get(i, "") <= hi for i in rest)
        rest_cn = sum(i.startswith(("ARC", "APC")) for i in rest)
        rows.append(
            {
                "segment": grp["segment"].iloc[0],
                "n": len(items),
                "cited": len(cited),
                "rest": len(rest),
                "rest_cn": rest_cn,
                "rest_in_cited_date_range": rest_inside,
                "multi_party": len(parties) > 1,
                "cited_other_party_than_rest": bool(rest) and not ({meta["party"].get(i) for i in rest} <= cited_parties),
            }
        )
    d = pd.DataFrame(rows)
    print(f"lost receipts citing >=1 golden item: {len(d)}")
    print(d.groupby("segment")[["n", "cited", "rest", "rest_cn"]].mean().round(2).assign(count=d.groupby("segment").size()).to_string())
    d["rest_bucket"] = pd.cut(d["rest"], [-1, 0, 1, 3, 6, 10_000], labels=["0", "1", "2-3", "4-6", "7+"])
    print("\nuncited items left per receipt:\n", d["rest_bucket"].value_counts().sort_index().to_string())
    print("\nall cited (rest=0) but still lost:", (d["rest"] == 0).sum())
    sub = d[d["rest"] > 0]
    print("of the uncited items: credit notes", round(sub["rest_cn"].sum() / max(1, sub["rest"].sum()), 3),
          "| inside the cited issue-date range", round(sub["rest_in_cited_date_range"].sum() / max(1, sub["rest"].sum()), 3))
    print("multi-party payments:", d["multi_party"].mean().round(3), " uncited belong to another party:", d["cited_other_party_than_rest"].mean().round(3))
    c = Counter(zip(d["segment"], d["rest"] == 0))
    print("\n(segment, all cited) counts:", dict(c))


if __name__ == "__main__":
    main()
