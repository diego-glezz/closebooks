"""Why did _by_reference give up on receipts the golden settles with items?
Needs a run made with trace_ref=1 (scripts/exp_b.py writes trace_ref.csv).

    python scripts/trace_summary_b.py out/exp_b/merged_trace__2024-07-01__2024-12-31
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from closebooks.bankrec.text import number_keys, text_tokens  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--data", default="data/challenge")
    a = ap.parse_args()
    root, run = Path(a.data), Path(a.run)
    why = pd.read_csv(run / "trace_ref.csv", dtype=str)
    why = dict(zip(why["bank_txn_id"], why["reason"], strict=True))
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    lost = set(expl.loc[expl["rule"].isin(["unidentified", "category_learned_fallback"]), "bank_txn_id"])
    gold = pd.read_csv(root / "train" / "golden_allocations.csv.gz", dtype=str).fillna("")
    g = gold[gold["bank_txn_id"].isin(lost) & (gold["item_id"] != "")]
    bank = pd.read_csv(root / "train" / "bank_transactions.csv.gz", dtype=str).fillna("").set_index("bank_txn_id")
    inv = pd.read_csv(root / "train" / "ar_invoices.csv.gz", dtype=str).fillna("")
    cn = pd.read_csv(root / "train" / "credit_notes.csv.gz", dtype=str).fillna("")
    num = {**dict(zip(inv["item_id"], inv["document_number"], strict=True)), **dict(zip(cn["item_id"], cn["document_number"], strict=True))}
    rows = []
    for txn, grp in g.groupby("bank_txn_id"):
        b = bank.loc[txn]
        toks = text_tokens(" | ".join(x for x in (b["text"], b["ref1"], b["ref2"], b["remittance_info"]) if x))
        items = list(grp["item_id"])
        cited = sum(bool(number_keys(num.get(i, "")) & toks) for i in items)
        rows.append(
            {
                "segment": grp["segment"].iloc[0],
                "cites": "all" if cited == len(items) else "some" if cited else "none",
                "reason": why.get(txn, "(not reached)"),
            }
        )
    d = pd.DataFrame(rows)
    print(f"lost receipts with golden items: {len(d)}")
    print("\nreason x how much the text cites:")
    print(pd.crosstab(d["reason"], d["cites"], margins=True).to_string())
    print("\nreceipts citing ALL golden items, reason x segment:")
    allc = d[d["cites"] == "all"]
    print(pd.crosstab(allc["reason"], allc["segment"]).to_string())


if __name__ == "__main__":
    main()
